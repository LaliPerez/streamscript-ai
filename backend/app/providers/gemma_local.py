"""100% local TranscriptionProvider: faster-whisper for ASR + a local Gemma
model served by Ollama for translation. No cloud calls, no API key -- for
conferences that can't or won't send audio to a third party.

Same two-stage shape as GeminiLiveProvider (ASR, then a separate translate
call), and for the same underlying reason: Gemma is a text model family, it
doesn't process audio at all, so *some* dedicated ASR engine is required
regardless of provider. faster-whisper (CTranslate2-based, runs fine on
CPU) fills that role here.

Default translate model is gemma3:1b, not the more specialized
TranslateGemma, purely because of the disk budget available on the machine
this was developed on -- swap via GEMMA_TRANSLATE_MODEL once you have more
room (translategemma:4b is a ~7.8GB pull, see ollama.com/library/translategemma).

Whisper has no built-in notion of "sentence boundaries" the way the Gemini
Live ASR stream does -- there's no equivalent of interim_input_transcription
to diff against. Instead this buffers audio and uses the same RMS-based
silence heuristic as MockProvider to decide when a speaker has paused long
enough to call an utterance "done" and run it through Whisper.
"""
from __future__ import annotations

import array
import asyncio
import json
import logging
import math
import os
import time
import uuid
from collections.abc import AsyncIterator

import httpx

from app.providers.base import TranscriptEvent, TranscriptionProvider

logger = logging.getLogger(__name__)

_OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://ollama:11434")
_TRANSLATE_MODEL = os.getenv("GEMMA_TRANSLATE_MODEL", "gemma3:1b")
_WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL_SIZE", "base")

_SAMPLE_RATE = 16000
_SILENCE_RMS_THRESHOLD = 300
_MIN_SPEECH_MS = 1200  # don't bother transcribing a fragment shorter than this
_SILENCE_GAP_MS = 700  # this much quiet after speech closes out the utterance
_MAX_BUFFER_MS = 12000  # flush anyway if someone talks for this long without a pause

# Loaded once, shared by every room's GemmaLocalProvider instance -- model
# weights are ~150MB (the "base" size) and there's no reason to duplicate
# that per room. Guarded by a lock since first use triggers a blocking load.
_whisper_model = None
_whisper_lock = asyncio.Lock()
# Separate lock serializing actual transcribe() calls across rooms: unclear
# whether CTranslate2 (faster-whisper's backend) guarantees thread-safety
# for concurrent inference on one shared model instance, and a hackathon
# fallback path silently corrupting transcripts under multi-room load is a
# worse trade than serializing ASR (translation still runs in parallel).
_transcribe_lock = asyncio.Lock()


def _rms(pcm16_bytes: bytes) -> float:
    samples = array.array("h")
    samples.frombytes(pcm16_bytes[: len(pcm16_bytes) - len(pcm16_bytes) % 2])
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


async def _get_whisper_model():
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    async with _whisper_lock:
        if _whisper_model is None:
            from faster_whisper import WhisperModel

            logger.info("loading faster-whisper model %r (first use, downloads on cold cache)", _WHISPER_MODEL_SIZE)
            _whisper_model = await asyncio.to_thread(
                WhisperModel, _WHISPER_MODEL_SIZE, device="cpu", compute_type="int8"
            )
        return _whisper_model


def _translate_prompt(text: str, source_lang: str, target_langs: list[str], glossary_block: str) -> str:
    keys = ", ".join(f'"{lang}"' for lang in target_langs)
    prompt = (
        f"Translate this sentence from a tech conference talk, spoken in '{source_lang}', "
        f"into each of these languages: {keys}. Respond with ONLY a JSON object mapping each "
        "language code to its translation -- no prose, no markdown fences, no explanation."
    )
    if glossary_block:
        prompt += (
            "\n\nReference glossary -- keep these terms/spellings as-is, do not "
            f"translate proper nouns:\n{glossary_block}"
        )
    prompt += f"\n\nSentence: {text}"
    return prompt


class GemmaLocalProvider(TranscriptionProvider):
    def __init__(self, source_lang: str, target_langs: list[str], glossary_block: str = "") -> None:
        self.source_lang = source_lang
        self.target_langs = target_langs
        self.glossary_block = glossary_block
        self._buffer = bytearray()
        self._speech_ms = 0
        self._silence_ms = 0
        self._events: asyncio.Queue[TranscriptEvent | None] = asyncio.Queue()
        self._closed = False
        self._pending_tasks: set[asyncio.Task] = set()
        self._http = httpx.AsyncClient(timeout=60.0)

    async def connect(self) -> None:
        await _get_whisper_model()  # pay the cold-load cost at connect time, not on first audio

    async def send_audio_chunk(self, pcm16_bytes: bytes) -> None:
        if not pcm16_bytes or self._closed:
            return
        chunk_ms = int(len(pcm16_bytes) / 2 / _SAMPLE_RATE * 1000)
        loud = _rms(pcm16_bytes) >= _SILENCE_RMS_THRESHOLD

        if loud:
            self._buffer.extend(pcm16_bytes)
            self._speech_ms += chunk_ms
            self._silence_ms = 0
        elif self._buffer:
            self._silence_ms += chunk_ms

        should_flush = self._buffer and self._speech_ms >= _MIN_SPEECH_MS and (
            self._silence_ms >= _SILENCE_GAP_MS or self._speech_ms >= _MAX_BUFFER_MS
        )
        if should_flush:
            await self._flush()

    async def _flush(self) -> None:
        pcm_bytes = bytes(self._buffer)
        self._buffer = bytearray()
        self._speech_ms = 0
        self._silence_ms = 0
        task = asyncio.create_task(self._transcribe_and_translate(pcm_bytes))
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def _transcribe_and_translate(self, pcm_bytes: bytes) -> None:
        started = time.monotonic()
        try:
            source_text = await self._transcribe(pcm_bytes)
            if not source_text:
                return
            translations, ok = await self._translate(source_text)
        except Exception:
            logger.exception("gemma_local: utterance processing failed")
            return
        processing_ms = (time.monotonic() - started) * 1000
        now_ms = int(time.time() * 1000)
        await self._events.put(
            TranscriptEvent(
                utterance_id=str(uuid.uuid4()),
                source_text=source_text,
                source_lang=self.source_lang,
                translations=translations,
                is_final=True,
                start_ms=now_ms,
                end_ms=now_ms,
                processing_ms=processing_ms,
                degraded=not ok,
            )
        )

    async def _transcribe(self, pcm_bytes: bytes) -> str:
        import numpy as np

        audio = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        model = await _get_whisper_model()

        def _run():
            segments, _info = model.transcribe(audio, language=self.source_lang, beam_size=1)
            return " ".join(seg.text.strip() for seg in segments).strip()

        async with _transcribe_lock:
            return await asyncio.to_thread(_run)

    async def _translate(self, text: str) -> tuple[dict[str, str], bool]:
        prompt = _translate_prompt(text, self.source_lang, self.target_langs, self.glossary_block)
        for attempt in range(3):
            try:
                resp = await self._http.post(
                    f"{_OLLAMA_HOST}/api/generate",
                    json={"model": _TRANSLATE_MODEL, "prompt": prompt, "stream": False, "format": "json"},
                )
                resp.raise_for_status()
                raw = resp.json().get("response", "")
                return json.loads(raw), True
            except (json.JSONDecodeError, TypeError, KeyError):
                logger.warning("gemma_local: translation response not valid JSON, falling back: %r", text[:200])
                break
            except Exception:
                if attempt == 2:
                    logger.exception("gemma_local: translation call failed after retries, falling back")
                    break
                await asyncio.sleep(0.5 * (attempt + 1))
        return {lang: text for lang in self.target_langs}, False

    async def receive_events(self) -> AsyncIterator[TranscriptEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    async def close(self) -> None:
        self._closed = True
        if self._buffer and self._speech_ms >= _MIN_SPEECH_MS:
            await self._flush()
        for task in list(self._pending_tasks):
            with_timeout = asyncio.wait_for(task, timeout=10)
            try:
                await with_timeout
            except Exception:
                pass
        await self._http.aclose()
        await self._events.put(None)
