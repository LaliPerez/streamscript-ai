"""Tests the buffering/flush control flow in GemmaLocalProvider without
touching the real faster-whisper model or a real Ollama server -- both
_transcribe and _translate are monkeypatched, so this only exercises the
code actually written here (silence-gap segmentation, event shaping),
not Whisper's or Gemma's own correctness.
"""
from __future__ import annotations

import struct

import pytest

from app.providers.gemma_local import (
    _MAX_BUFFER_MS,
    _MIN_SPEECH_MS,
    _SILENCE_GAP_MS,
    GemmaLocalProvider,
)

SAMPLE_RATE = 16000


def loud_chunk(ms: int) -> bytes:
    samples = int(SAMPLE_RATE * ms / 1000)
    values = [10000, -10000] * (samples // 2) + [0] * (samples % 2)
    return struct.pack(f"<{samples}h", *values)


def silent_chunk(ms: int) -> bytes:
    samples = int(SAMPLE_RATE * ms / 1000)
    return struct.pack(f"<{samples}h", *([0] * samples))


@pytest.fixture
def provider(monkeypatch):
    p = GemmaLocalProvider(source_lang="en", target_langs=["es"])

    async def fake_transcribe(pcm_bytes: bytes) -> str:
        return "Hello world."

    async def fake_translate(text: str):
        return {"es": "Hola mundo."}, True

    monkeypatch.setattr(p, "_transcribe", fake_transcribe)
    monkeypatch.setattr(p, "_translate", fake_translate)
    return p


async def _collect_one(provider, timeout=2.0):
    import asyncio

    gen = provider.receive_events()
    return await asyncio.wait_for(gen.__anext__(), timeout=timeout)


@pytest.mark.asyncio
async def test_short_burst_of_speech_does_not_flush_before_min_speech(provider):
    # well under _MIN_SPEECH_MS, and no silence gap yet either
    await provider.send_audio_chunk(loud_chunk(200))
    assert provider._events.empty()


@pytest.mark.asyncio
async def test_speech_then_silence_gap_flushes_an_event(provider):
    speech_ms = 0
    while speech_ms < _MIN_SPEECH_MS:
        await provider.send_audio_chunk(loud_chunk(200))
        speech_ms += 200
    silence_ms = 0
    while silence_ms < _SILENCE_GAP_MS:
        await provider.send_audio_chunk(silent_chunk(200))
        silence_ms += 200

    # _collect_one awaits the queue for real (asyncio.Queue.get() properly
    # suspends), which is what actually gives the flush's background task
    # a chance to run -- a "while qsize() == 0: send more audio" polling
    # loop here would spin forever, since send_audio_chunk on an
    # already-empty buffer never awaits anything and so never yields.
    event = await _collect_one(provider)
    assert event.source_text == "Hello world."
    assert event.translations == {"es": "Hola mundo."}
    assert event.degraded is False


@pytest.mark.asyncio
async def test_silence_before_min_speech_never_flushes(provider):
    await provider.send_audio_chunk(loud_chunk(200))  # well under _MIN_SPEECH_MS
    for _ in range(10):
        await provider.send_audio_chunk(silent_chunk(200))
    # no matter how long the silence, a fragment under _MIN_SPEECH_MS never
    # counts as a real utterance -- it just sits buffered, unflushed
    assert provider._events.empty()
    assert len(provider._buffer) > 0


@pytest.mark.asyncio
async def test_continuous_speech_past_max_buffer_flushes_without_silence(provider):
    ms = 0
    while ms < _MAX_BUFFER_MS:
        await provider.send_audio_chunk(loud_chunk(200))
        ms += 200
        if provider._events.qsize():
            break
    event = await _collect_one(provider)
    assert event.source_text == "Hello world."


@pytest.mark.asyncio
async def test_close_flushes_a_pending_utterance(provider):
    speech_ms = 0
    while speech_ms < _MIN_SPEECH_MS:
        await provider.send_audio_chunk(loud_chunk(200))
        speech_ms += 200
    # closed mid-utterance, before any silence gap was ever observed
    await provider.close()

    events = [e async for e in provider.receive_events()]
    assert len(events) == 1
    assert events[0].source_text == "Hello world."


@pytest.mark.asyncio
async def test_translation_failure_marks_event_degraded():
    p = GemmaLocalProvider(source_lang="en", target_langs=["es"])

    async def fake_transcribe(pcm_bytes: bytes) -> str:
        return "Hello world."

    async def fake_translate(text: str):
        return {"es": text}, False  # simulates the real _translate's failure fallback

    p._transcribe = fake_transcribe
    p._translate = fake_translate

    speech_ms = 0
    while speech_ms < _MIN_SPEECH_MS:
        await p.send_audio_chunk(loud_chunk(200))
        speech_ms += 200
    await p.close()

    event = await _collect_one(p)
    assert event.degraded is True
    assert event.translations == {"es": "Hello world."}
