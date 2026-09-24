"""Formats a room's transcript log as SRT, VTT or plain text."""
from __future__ import annotations

from app.providers.base import TranscriptEvent


def _ms_to_srt_timestamp(ms: int) -> str:
    hours, rem = divmod(max(ms, 0), 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    seconds, millis = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _ms_to_vtt_timestamp(ms: int) -> str:
    return _ms_to_srt_timestamp(ms).replace(",", ".")


def _line_for(event: TranscriptEvent, lang: str | None) -> str:
    if lang is None or lang == event.source_lang:
        return event.source_text
    return event.translations.get(lang, event.source_text)


def to_srt(entries: list[TranscriptEvent], lang: str | None = None) -> str:
    blocks = []
    for i, event in enumerate(entries, start=1):
        blocks.append(
            f"{i}\n"
            f"{_ms_to_srt_timestamp(event.start_ms)} --> {_ms_to_srt_timestamp(event.end_ms)}\n"
            f"{_line_for(event, lang)}\n"
        )
    return "\n".join(blocks)


def to_vtt(entries: list[TranscriptEvent], lang: str | None = None) -> str:
    blocks = ["WEBVTT\n"]
    for event in entries:
        blocks.append(
            f"{_ms_to_vtt_timestamp(event.start_ms)} --> {_ms_to_vtt_timestamp(event.end_ms)}\n"
            f"{_line_for(event, lang)}\n"
        )
    return "\n".join(blocks)


def to_txt(entries: list[TranscriptEvent], lang: str | None = None) -> str:
    return "\n".join(_line_for(event, lang) for event in entries)


FORMATTERS = {"srt": to_srt, "vtt": to_vtt, "txt": to_txt}
