"""Per-room worker: audio queue -> provider -> transcript store + ws_hub broadcast.

Started as one asyncio task per room, so N rooms run concurrently in a single
process (see rooms.py for why that's enough for a hackathon-scale demo).
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

from app.glossary import load_glossary_block
from app.providers.base import TranscriptionProvider
from app.rooms import Room
from app.ws_hub import hub

logger = logging.getLogger(__name__)


def _estimate_display_ms(text: str) -> int:
    """Reading-time estimate for captions with no real end timestamp."""
    return max(1200, len(text) * 60)


def _build_provider(room: Room) -> TranscriptionProvider:
    glossary_block = load_glossary_block(room.glossary_path)
    if os.getenv("GEMINI_API_KEY"):
        from app.providers.gemini_live import GeminiLiveProvider

        return GeminiLiveProvider(room.source_lang, room.target_langs, glossary_block)
    from app.providers.mock import MockProvider

    logger.warning("GEMINI_API_KEY not set, room %s running on MockProvider", room.id)
    return MockProvider(room.source_lang, room.target_langs)


async def run_room(room: Room) -> None:
    provider = _build_provider(room)
    room.provider = provider
    try:
        await provider.connect()
        room.status = "active"
        room.started_at = time.time()
        await hub.broadcast(room.id, {"type": "status", "status": "active"})
    except Exception as exc:  # noqa: BLE001
        logger.exception("failed to start provider for room %s", room.id)
        room.status = "error"
        room.last_error = str(exc)
        room.error_count += 1
        await hub.broadcast(room.id, {"type": "status", "status": "error", "detail": str(exc)})
        return

    async def pump_audio() -> None:
        while True:
            chunk = await room.audio_queue.get()
            if chunk is None:
                return
            try:
                await provider.send_audio_chunk(chunk)
            except Exception as exc:  # noqa: BLE001
                logger.exception("error sending audio for room %s", room.id)
                room.error_count += 1
                room.last_error = str(exc)

    async def pump_events() -> None:
        origin_ms = int(room.started_at * 1000)
        async for event in provider.receive_events():
            # Providers stamp events with absolute epoch ms; rebase to
            # room-relative ms so exported SRT/VTT timing starts at 00:00:00.
            event.start_ms = max(event.start_ms - origin_ms, 0)
            event.end_ms = max(event.end_ms - origin_ms, event.start_ms)
            if event.end_ms == event.start_ms:
                # Providers may not give us real utterance boundaries (e.g. a
                # text-only Gemini response has no timing info); without this
                # an SRT/VTT cue would have zero duration and never display.
                event.end_ms += _estimate_display_ms(event.source_text)
            room.store.append(event)
            room.last_activity = event.created_at
            if event.processing_ms is not None:
                room.recent_latencies_ms.append(event.processing_ms)
            await hub.broadcast(
                room.id,
                {
                    "type": "caption",
                    "utterance_id": event.utterance_id,
                    "source_text": event.source_text,
                    "source_lang": event.source_lang,
                    "translations": event.translations,
                    "is_final": event.is_final,
                    "start_ms": event.start_ms,
                    "end_ms": event.end_ms,
                },
            )

    try:
        await asyncio.gather(pump_audio(), pump_events())
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # noqa: BLE001
        logger.exception("room %s worker crashed", room.id)
        room.status = "error"
        room.last_error = str(exc)
        room.error_count += 1
        await hub.broadcast(room.id, {"type": "status", "status": "error"})
    finally:
        await provider.close()
