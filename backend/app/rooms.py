"""In-memory registry of active rooms.

One process, one asyncio task per active room (see worker.py) is the
hackathon-scale answer to "run several sessions at once". The production
scale-out story (Redis-backed registry + horizontal workers + pub/sub) is
documented in the README rather than built here, since it isn't needed to
prove concurrency for a demo with a handful of simultaneous rooms.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

from app.providers.base import TranscriptionProvider
from app.transcript_store import TranscriptStore


@dataclass
class Room:
    id: str
    title: str
    source_lang: str
    target_langs: list[str]
    glossary_path: str | None = None
    status: str = "starting"  # starting | active | error | closed
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None  # epoch seconds when the provider went active; origin for SRT/VTT timing
    last_activity: float = field(default_factory=time.time)
    last_error: str | None = None
    error_count: int = 0
    provider: TranscriptionProvider | None = field(default=None, repr=False)
    store: TranscriptStore = field(init=False, repr=False)
    audio_queue: asyncio.Queue[bytes | None] = field(init=False, repr=False)
    worker_task: asyncio.Task | None = field(default=None, repr=False)
    recent_latencies_ms: deque[float] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.store = TranscriptStore(self.id)
        self.audio_queue = asyncio.Queue(maxsize=200)
        self.recent_latencies_ms = deque(maxlen=20)

    def to_public_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "source_lang": self.source_lang,
            "target_langs": self.target_langs,
            "status": self.status,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "idle_s": round(time.time() - self.last_activity, 1) if self.status == "active" else None,
            "avg_latency_ms": (
                round(sum(self.recent_latencies_ms) / len(self.recent_latencies_ms))
                if self.recent_latencies_ms
                else None
            ),
            "utterance_count": len(self.store.entries),
            "error_count": self.error_count,
            "last_error": self.last_error,
        }


class RoomManager:
    def __init__(self) -> None:
        self._rooms: dict[str, Room] = {}

    def create(self, title: str, source_lang: str, target_langs: list[str], glossary_path: str | None) -> Room:
        room_id = uuid.uuid4().hex[:8]
        room = Room(
            id=room_id,
            title=title,
            source_lang=source_lang,
            target_langs=target_langs,
            glossary_path=glossary_path,
        )
        self._rooms[room_id] = room
        return room

    def get(self, room_id: str) -> Room | None:
        return self._rooms.get(room_id)

    def list(self) -> list[Room]:
        return list(self._rooms.values())

    async def close(self, room_id: str) -> Room | None:
        room = self._rooms.get(room_id)
        if room is None:
            return None
        room.status = "closed"
        await room.audio_queue.put(None)
        if room.worker_task is not None:
            room.worker_task.cancel()
        if room.provider is not None:
            await room.provider.close()
        return room


manager = RoomManager()
