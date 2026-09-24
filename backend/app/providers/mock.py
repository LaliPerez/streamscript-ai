"""Fake provider that turns incoming audio volume into fake captions.

Lets the rest of the stack (rooms, worker, ws_hub, frontend, export) be built
and demoed end-to-end before a real Gemini API key is available.
"""
from __future__ import annotations

import array
import asyncio
import math
import time
import uuid
from collections.abc import AsyncIterator

from app.providers.base import TranscriptEvent, TranscriptionProvider

_SAMPLE_LINES = [
    ("Welcome everyone to this talk about distributed systems.",
     "Bienvenidos a esta charla sobre sistemas distribuidos."),
    ("Today we are going to deploy this service with Kubernetes.",
     "Hoy vamos a desplegar este servicio con Kubernetes."),
    ("Let's take a look at how hydration works in this framework.",
     "Veamos cómo funciona la hidratación en este framework."),
    ("Any questions so far before we move to the next section?",
     "¿Alguna pregunta antes de pasar a la siguiente sección?"),
]

_SILENCE_RMS_THRESHOLD = 300
_MIN_SPEECH_MS_BEFORE_EMIT = 1500


def _rms(pcm16_bytes: bytes) -> float:
    # Not audioop.rms: that module was removed in Python 3.13 (PEP 594).
    samples = array.array("h")
    samples.frombytes(pcm16_bytes[: len(pcm16_bytes) - len(pcm16_bytes) % 2])
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


class MockProvider(TranscriptionProvider):
    def __init__(self, source_lang: str = "en", target_langs: list[str] | None = None) -> None:
        self.source_lang = source_lang
        self.target_langs = target_langs or ["es"]
        self._queue: asyncio.Queue[TranscriptEvent | None] = asyncio.Queue()
        self._speech_ms = 0
        self._line_idx = 0
        self._closed = False

    async def connect(self) -> None:
        return None

    async def send_audio_chunk(self, pcm16_bytes: bytes) -> None:
        if not pcm16_bytes:
            return
        rms = _rms(pcm16_bytes)
        chunk_ms = int(len(pcm16_bytes) / 2 / 16000 * 1000)
        if rms < _SILENCE_RMS_THRESHOLD:
            return
        self._speech_ms += chunk_ms
        if self._speech_ms >= _MIN_SPEECH_MS_BEFORE_EMIT:
            self._speech_ms = 0
            await self._emit_next_line()

    async def _emit_next_line(self) -> None:
        source, es = _SAMPLE_LINES[self._line_idx % len(_SAMPLE_LINES)]
        self._line_idx += 1
        now_ms = int(time.time() * 1000)
        translations = {lang: es if lang == "es" else source for lang in self.target_langs}
        event = TranscriptEvent(
            utterance_id=str(uuid.uuid4()),
            source_text=source,
            source_lang=self.source_lang,
            translations=translations,
            is_final=True,
            start_ms=now_ms - _MIN_SPEECH_MS_BEFORE_EMIT,
            end_ms=now_ms,
        )
        await self._queue.put(event)

    async def receive_events(self) -> AsyncIterator[TranscriptEvent]:
        while True:
            event = await self._queue.get()
            if event is None:
                return
            yield event

    async def close(self) -> None:
        self._closed = True
        await self._queue.put(None)
