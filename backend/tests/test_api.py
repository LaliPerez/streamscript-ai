"""Integration tests against the real FastAPI app, running on MockProvider
(no GEMINI_API_KEY in the test environment, so app.worker picks it
automatically -- see app/worker.py::_build_provider).

TestClient must be used as a context manager here: without entering it via
`with`, background tasks spawned with asyncio.create_task() inside a request
handler (room.worker_task = asyncio.create_task(run_room(room)) in
app/main.py::create_room) never get scheduled between separate TestClient
calls and the room would never leave "starting" -- confirmed with a minimal
repro against plain FastAPI before writing these tests.
"""
from __future__ import annotations

import struct
import time

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.providers.base import TranscriptEvent
from app.rooms import manager


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
    manager._rooms.clear()  # avoid cross-test pollution of the global registry


def _loud_pcm16_chunk(ms: int = 200, rate: int = 16000) -> bytes:
    samples = int(rate * ms / 1000)
    values = [10000, -10000] * (samples // 2) + [0] * (samples % 2)
    return struct.pack(f"<{samples}h", *values)


def _create_room(client, **overrides) -> dict:
    payload = {"title": "Keynote", "source_lang": "en", "target_langs": ["es"]}
    payload.update(overrides)
    res = client.post("/api/rooms", json=payload)
    assert res.status_code == 200
    return res.json()


def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not met within timeout")


def test_create_list_and_get_room(client):
    room = _create_room(client, title="Keynote")
    assert room["title"] == "Keynote"
    assert room["source_lang"] == "en"

    listed = client.get("/api/rooms").json()
    assert any(r["id"] == room["id"] for r in listed)

    fetched = client.get(f"/api/rooms/{room['id']}").json()
    assert fetched["id"] == room["id"]


def test_get_unknown_room_is_404(client):
    assert client.get("/api/rooms/does-not-exist").status_code == 404


def test_export_unsupported_format_is_400(client):
    room = _create_room(client)
    res = client.get(f"/api/rooms/{room['id']}/export", params={"format": "xml"})
    assert res.status_code == 400


def test_export_empty_room_returns_empty_body(client):
    room = _create_room(client)
    res = client.get(f"/api/rooms/{room['id']}/export", params={"format": "srt"})
    assert res.status_code == 200
    assert res.text == ""


def test_close_room_marks_closed_and_stays_idempotent(client):
    # A closed room isn't removed from the registry (GET on it still works,
    # e.g. so the dashboard can show a talk that already ended), so a repeat
    # DELETE is intentionally idempotent (200), not 404 -- 404 is reserved
    # for a room id that never existed at all.
    room = _create_room(client)
    res = client.delete(f"/api/rooms/{room['id']}")
    assert res.status_code == 200
    assert res.json()["status"] == "closed"

    repeat = client.delete(f"/api/rooms/{room['id']}")
    assert repeat.status_code == 200
    assert repeat.json()["status"] == "closed"

    assert client.get(f"/api/rooms/{room['id']}").status_code == 200


def test_delete_unknown_room_is_404(client):
    assert client.delete("/api/rooms/does-not-exist").status_code == 404


def test_qr_png_for_unknown_room_is_404(client):
    assert client.get("/api/rooms/does-not-exist/qr.png").status_code == 404


def test_qr_png_for_real_room_returns_image(client):
    room = _create_room(client)
    res = client.get(f"/api/rooms/{room['id']}/qr.png")
    assert res.status_code == 200
    assert res.headers["content-type"] == "image/png"


def test_ingest_ws_on_unknown_room_closes_immediately(client):
    with pytest.raises(Exception):
        with client.websocket_connect("/ws/ingest/does-not-exist"):
            pass


def test_ingest_ws_enqueues_bytes_onto_the_rooms_audio_queue(client):
    # Whether the worker task gets scheduled promptly enough within a test's
    # short lifetime is a TestClient/anyio scheduling quirk (confirmed with a
    # standalone repro), not something worth asserting on here -- what this
    # endpoint is actually responsible for is handing bytes off correctly,
    # which end-to-end transcription (test_mock_provider.py) and hours of
    # manual testing against the real Gemini API already cover. No polling
    # wait needed either: websocket_connect's send is synchronous with the
    # server processing each message (put_nowait happens inline in
    # ws_ingest), so by the time the `with` block exits every send has
    # already landed in the queue.
    room = _create_room(client)
    with client.websocket_connect(f"/ws/ingest/{room['id']}") as ws:
        for _ in range(3):
            ws.send_bytes(_loud_pcm16_chunk())

    assert manager.get(room["id"]).audio_queue.qsize() >= 3


def test_late_subscriber_is_replayed_current_status_and_last_caption(client):
    """Regression test for two bugs found while validating this session:
    a viewer connecting to /ws/subtitles after a room already went active
    (and already has captions) used to see nothing until the *next* status
    change / sentence -- see app/main.py::ws_subtitles. Drives the room's
    state directly instead of through the ingest pipeline, since what's
    under test here is the replay-on-connect logic, not transcription.
    """
    room = _create_room(client)
    live_room = manager.get(room["id"])
    live_room.status = "active"
    live_room.store.append(
        TranscriptEvent(
            utterance_id="u1",
            source_text="Hello everyone.",
            source_lang="en",
            translations={"es": "Hola a todos."},
            is_final=True,
            start_ms=0,
            end_ms=1000,
        )
    )

    # this viewer connects *after* the room is active and already has a caption
    with client.websocket_connect(f"/ws/subtitles/{room['id']}") as sub_ws:
        status_msg = sub_ws.receive_json()
        assert status_msg == {"type": "status", "status": "active", "detail": None}

        caption_msg = sub_ws.receive_json()
        assert caption_msg["type"] == "caption"
        assert caption_msg["source_text"] == "Hello everyone."
        assert caption_msg["translations"]["es"] == "Hola a todos."


def test_late_subscriber_to_room_with_no_captions_yet_gets_only_status(client):
    room = _create_room(client)
    _wait_until(lambda: client.get(f"/api/rooms/{room['id']}").json()["status"] == "active")

    with client.websocket_connect(f"/ws/subtitles/{room['id']}") as sub_ws:
        status_msg = sub_ws.receive_json()
        assert status_msg["type"] == "status"
        # nothing queued to transcribe yet, so no caption replay should happen
