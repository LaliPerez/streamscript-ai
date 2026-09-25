import time

import pytest

from app.providers.base import TranscriptEvent
from app.rooms import RoomManager


@pytest.fixture
def manager():
    return RoomManager()


def test_create_assigns_id_and_defaults(manager):
    room = manager.create("Keynote", "en", ["es"], None)
    assert room.id
    assert room.title == "Keynote"
    assert room.status == "starting"
    assert room.error_count == 0
    assert manager.get(room.id) is room


def test_list_returns_all_created_rooms(manager):
    a = manager.create("A", "en", ["es"], None)
    b = manager.create("B", "en", ["es"], None)
    assert {r.id for r in manager.list()} == {a.id, b.id}


def test_get_unknown_room_returns_none(manager):
    assert manager.get("does-not-exist") is None


@pytest.mark.asyncio
async def test_close_marks_closed_and_wakes_audio_queue(manager):
    room = manager.create("Keynote", "en", ["es"], None)
    closed = await manager.close(room.id)
    assert closed is room
    assert room.status == "closed"
    # the sentinel that tells worker.pump_audio to stop
    assert await room.audio_queue.get() is None


@pytest.mark.asyncio
async def test_close_unknown_room_returns_none(manager):
    assert await manager.close("nope") is None


def test_to_public_dict_idle_s_only_reported_when_active(manager):
    room = manager.create("Keynote", "en", ["es"], None)
    assert room.to_public_dict()["idle_s"] is None  # still "starting"
    room.status = "active"
    room.last_activity = time.time() - 5
    idle_s = room.to_public_dict()["idle_s"]
    assert idle_s is not None and idle_s >= 5


def test_to_public_dict_avg_latency_is_mean_of_recent_samples(manager):
    room = manager.create("Keynote", "en", ["es"], None)
    assert room.to_public_dict()["avg_latency_ms"] is None
    room.recent_latencies_ms.extend([1000, 2000, 3000])
    assert room.to_public_dict()["avg_latency_ms"] == 2000


def test_to_public_dict_utterance_count_reflects_store(manager):
    room = manager.create("Keynote", "en", ["es"], None)
    assert room.to_public_dict()["utterance_count"] == 0
    room.store.append(
        TranscriptEvent(
            utterance_id="u1",
            source_text="Hi",
            source_lang="en",
            translations={"es": "Hola"},
            is_final=True,
            start_ms=0,
            end_ms=100,
        )
    )
    assert room.to_public_dict()["utterance_count"] == 1
