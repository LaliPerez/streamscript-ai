"""Gemini-backed provider: streaming ASR via the Live API + per-sentence text translation.

Validated against the real API (Sept 2026) with a live API key. Two things
learned during that spike that shaped this design:

1. Every currently-available *conversational* Live model (gemini-3.8-live,
   gemini-live-2.5-flash-preview, the native-audio family) rejects
   `response_modalities=["TEXT"]` outright -- either at connect time or as
   soon as audio starts flowing. Asking a Live chat model to just hand back
   JSON text, as an earlier version of this file did, does not work anymore.
2. `gemini-3.5-transcribe-live` (a model dedicated to streaming ASR) *does*
   accept the connection and streams real, accurate transcriptions -- but
   only transcription. It has no notion of "translate this" and doesn't
   chat. Its messages carry `server_content.interim_input_transcription.text`,
   which is *cumulative* for the whole session (each message repeats
   everything transcribed so far, not just the new words).

So the pipeline here is two stages: gemini-3.5-transcribe-live for
low-latency streaming ASR, sentence-boundary detection on the cumulative
transcript (diff against what we've already emitted, cut at the last
sentence-ending punctuation), and a small separate `generate_content` call
per finished sentence to translate it (with the glossary as context).

Audio-only Live sessions are capped at ~15 minutes server-side, so this
provider transparently reconnects (`_MAX_SESSION_SECONDS`) using a fresh
session. Google's session-resumption tokens are a possible future upgrade
to avoid even that reconnect gap.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator

from google import genai
from google.genai import types

from app.providers.base import TranscriptEvent, TranscriptionProvider

logger = logging.getLogger(__name__)

# Verified available for this API key via GET /v1beta/models (bidiGenerateContent).
_ASR_MODEL = os.getenv("GEMINI_LIVE_MODEL", "gemini-3.5-transcribe-live")
# gemini-3.8-flash is Google's current recommendation, but was hitting
# persistent 503 "high demand" errors as of this writing; gemini-3.5-flash
# responded reliably in testing. Bump via env if 3.8 capacity improves.
_TRANSLATE_MODEL = os.getenv("GEMINI_TRANSLATE_MODEL", "gemini-3.5-flash")
_MAX_SESSION_SECONDS = 14 * 60  # reconnect before hitting the ~15min server cap
_SENTENCE_END_CHARS = ".!?¿¡"  # includes Spanish inverted marks as no-ops (not sentence-enders)


def _translate_prompt(text: str, source_lang: str, target_langs: list[str], glossary_block: str) -> str:
    keys = ", ".join(f'"{lang}"' for lang in target_langs)
    prompt = (
        f"Translate this sentence from a tech conference talk, spoken in "
        f"'{source_lang}', into each of these languages: {keys}. "
        "Respond with ONLY a JSON object mapping each language code to its "
        "translation -- no prose, no markdown fences, no explanation."
    )
    if glossary_block:
        prompt += (
            "\n\nReference glossary -- keep these terms/spellings as-is, do not "
            f"translate proper nouns:\n{glossary_block}"
        )
    prompt += f"\n\nSentence: {text}"
    return prompt


class GeminiLiveProvider(TranscriptionProvider):
    def __init__(self, source_lang: str, target_langs: list[str], glossary_block: str = "") -> None:
        self.source_lang = source_lang
        self.target_langs = target_langs
        self.glossary_block = glossary_block
        self._client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        self._session = None
        self._session_cm = None
        self._session_started_at = 0.0
        self._closed = False
        self._emitted_upto = 0  # char offset already finalized out of the cumulative ASR transcript
        self._events: asyncio.Queue[TranscriptEvent | None] = asyncio.Queue()
        self._pump_task: asyncio.Task | None = None
        self._translate_tasks: set[asyncio.Task] = set()

    async def connect(self) -> None:
        await self._open_session()
        self._pump_task = asyncio.create_task(self._run())

    async def _open_session(self) -> None:
        config = types.LiveConnectConfig(
            response_modalities=["TEXT"],
            input_audio_transcription=types.AudioTranscriptionConfig(),
        )
        self._session_cm = self._client.aio.live.connect(model=_ASR_MODEL, config=config)
        self._session = await self._session_cm.__aenter__()
        self._session_started_at = time.monotonic()
        self._emitted_upto = 0

    async def send_audio_chunk(self, pcm16_bytes: bytes) -> None:
        if self._closed or self._session is None:
            return
        if time.monotonic() - self._session_started_at > _MAX_SESSION_SECONDS:
            await self._rotate_session()
        await self._session.send_realtime_input(
            audio=types.Blob(data=pcm16_bytes, mime_type="audio/pcm;rate=16000")
        )

    async def _rotate_session(self) -> None:
        old_cm = self._session_cm
        await self._open_session()
        if old_cm is not None:
            try:
                await old_cm.__aexit__(None, None, None)
            except Exception:  # pragma: no cover - best-effort cleanup
                logger.exception("error closing previous Gemini Live session")

    async def _run(self) -> None:
        try:
            while not self._closed and self._session is not None:
                async for message in self._session.receive():
                    self._handle_message(message)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Gemini Live receive loop crashed")
        finally:
            await self._events.put(None)

    def _handle_message(self, message) -> None:
        server_content = getattr(message, "server_content", None)
        transcription = getattr(server_content, "interim_input_transcription", None)
        full_text = getattr(transcription, "text", None)
        if not full_text:
            return
        new_part = full_text[self._emitted_upto :]
        cut = max((new_part.rfind(ch) for ch in _SENTENCE_END_CHARS), default=-1)
        if cut == -1:
            return
        finalized = full_text[self._emitted_upto : self._emitted_upto + cut + 1].strip()
        self._emitted_upto += cut + 1
        if finalized:
            task = asyncio.create_task(self._translate_and_emit(finalized))
            self._translate_tasks.add(task)
            task.add_done_callback(self._translate_tasks.discard)

    async def _translate_and_emit(self, source_text: str) -> None:
        translate_started = time.monotonic()
        translations, ok = await self._translate(source_text)
        processing_ms = (time.monotonic() - translate_started) * 1000
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

    async def _translate(self, text: str) -> tuple[dict[str, str], bool]:
        prompt = _translate_prompt(text, self.source_lang, self.target_langs, self.glossary_block)
        for attempt in range(3):
            try:
                response = await self._client.aio.models.generate_content(model=_TRANSLATE_MODEL, contents=prompt)
                return json.loads(response.text), True
            except (json.JSONDecodeError, TypeError, AttributeError):
                logger.warning("translation response not valid JSON, falling back to source text: %r", text[:200])
                break
            except Exception:
                if attempt == 2:
                    logger.exception("translation call failed after retries, falling back to source text")
                    break
                await asyncio.sleep(0.5 * (attempt + 1))  # brief backoff for transient 503s
        return {lang: text for lang in self.target_langs}, False

    async def receive_events(self) -> AsyncIterator[TranscriptEvent]:
        while True:
            event = await self._events.get()
            if event is None:
                return
            yield event

    async def close(self) -> None:
        self._closed = True
        if self._pump_task is not None:
            self._pump_task.cancel()
            # Must finish before __aexit__: the session's receive() generator
            # can't be closed while _run() is still concurrently iterating it
            # (raises "anext(): asynchronous generator is already running").
            try:
                await self._pump_task
            except asyncio.CancelledError:
                pass
        for task in list(self._translate_tasks):
            task.cancel()
        if self._session_cm is not None:
            try:
                await self._session_cm.__aexit__(None, None, None)
            except RuntimeError:
                # Observed against the real API: __aexit__ can still race the
                # cancelled receive() generator's own unwind ("anext(): ...
                # already running"). Harmless -- the underlying connection is
                # gone either way -- so this is swallowed instead of logged.
                pass
            except Exception:  # pragma: no cover - best-effort cleanup
                logger.exception("error closing Gemini Live session")
        await self._events.put(None)
