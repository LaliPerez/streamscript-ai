"""Provider-agnostic contract for speech transcription + translation backends.

Any engine (Gemini Live today, a local Gemma model tomorrow) implements this
interface so the rest of the system (rooms, worker, ws_hub, export) never has
to know which one is behind a room.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field


@dataclass
class TranscriptEvent:
    utterance_id: str
    source_text: str
    source_lang: str
    translations: dict[str, str]
    is_final: bool
    start_ms: int
    end_ms: int
    created_at: float = field(default_factory=time.time)
    # How long the provider's own processing (e.g. a translation call) took
    # to produce this event, if it tracks that. None for providers that don't
    # have a measurable processing step (e.g. MockProvider).
    processing_ms: float | None = None


class TranscriptionProvider(ABC):
    """One instance per active room/session."""

    @abstractmethod
    async def connect(self) -> None:
        """Open the underlying streaming session."""

    @abstractmethod
    async def send_audio_chunk(self, pcm16_bytes: bytes) -> None:
        """Push a chunk of 16-bit PCM mono audio (16kHz) into the session."""

    @abstractmethod
    def receive_events(self) -> AsyncIterator[TranscriptEvent]:
        """Yield transcript/translation events as they become available."""

    @abstractmethod
    async def close(self) -> None:
        """Tear down the session and release resources."""
