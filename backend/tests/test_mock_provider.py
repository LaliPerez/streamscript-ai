import asyncio
import struct

import pytest

from app.providers.mock import MockProvider

SAMPLE_RATE = 16000


def loud_chunk(ms: int) -> bytes:
    samples = int(SAMPLE_RATE * ms / 1000)
    # a simple loud square-ish wave, well above the silence RMS threshold
    return struct.pack(f"<{samples}h", *([10000, -10000] * (samples // 2) + [0] * (samples % 2)))


def silent_chunk(ms: int) -> bytes:
    samples = int(SAMPLE_RATE * ms / 1000)
    return struct.pack(f"<{samples}h", *([0] * samples))


@pytest.mark.asyncio
async def test_silence_never_emits_a_caption():
    provider = MockProvider(target_langs=["es"])
    await provider.connect()
    for _ in range(5):
        await provider.send_audio_chunk(silent_chunk(200))
    await provider.close()

    events = [e async for e in provider.receive_events()]
    assert events == []


@pytest.mark.asyncio
async def test_enough_loud_audio_emits_one_event_with_translations():
    provider = MockProvider(source_lang="en", target_langs=["es"])
    await provider.connect()

    events: list = []

    async def collect():
        async for event in provider.receive_events():
            events.append(event)

    consumer = asyncio.create_task(collect())
    for _ in range(8):  # 8 * 200ms > the 1500ms threshold
        await provider.send_audio_chunk(loud_chunk(200))
    await provider.close()
    await consumer

    assert len(events) == 1
    event = events[0]
    assert event.source_lang == "en"
    assert "es" in event.translations
    assert event.translations["es"]  # non-empty
    assert event.is_final is True


@pytest.mark.asyncio
async def test_empty_chunk_is_a_no_op():
    provider = MockProvider()
    await provider.connect()
    await provider.send_audio_chunk(b"")  # must not raise
    await provider.close()
    events = [e async for e in provider.receive_events()]
    assert events == []
