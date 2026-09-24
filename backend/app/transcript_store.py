"""Append-only transcript log per room: in-memory list + JSONL persistence."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from app.providers.base import TranscriptEvent

_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "transcripts"


class TranscriptStore:
    def __init__(self, room_id: str) -> None:
        self.room_id = room_id
        self.entries: list[TranscriptEvent] = []
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        self._path = _DATA_DIR / f"{room_id}.jsonl"

    def append(self, event: TranscriptEvent) -> None:
        self.entries.append(event)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(event), ensure_ascii=False) + "\n")

    def all(self) -> list[TranscriptEvent]:
        return list(self.entries)
