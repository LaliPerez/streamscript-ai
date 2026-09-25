"""ADMIN_TOKEN gates room creation/closing; everything else stays public."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.rooms import manager


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
    manager._rooms.clear()


def test_no_token_configured_means_no_auth_required(client, monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    res = client.post("/api/rooms", json={"title": "X", "source_lang": "en", "target_langs": ["es"]})
    assert res.status_code == 200


def test_create_room_without_header_is_401_when_token_configured(client, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "s3cr3t")
    res = client.post("/api/rooms", json={"title": "X", "source_lang": "en", "target_langs": ["es"]})
    assert res.status_code == 401


def test_create_room_with_wrong_header_is_401(client, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "s3cr3t")
    res = client.post(
        "/api/rooms",
        json={"title": "X", "source_lang": "en", "target_langs": ["es"]},
        headers={"X-Admin-Token": "wrong"},
    )
    assert res.status_code == 401


def test_create_room_with_correct_header_succeeds(client, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "s3cr3t")
    res = client.post(
        "/api/rooms",
        json={"title": "X", "source_lang": "en", "target_langs": ["es"]},
        headers={"X-Admin-Token": "s3cr3t"},
    )
    assert res.status_code == 200


def test_close_room_also_requires_the_token(client, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "s3cr3t")
    room = manager.create("X", "en", ["es"], None)  # bypass auth to set up the precondition

    assert client.delete(f"/api/rooms/{room.id}").status_code == 401
    assert client.delete(f"/api/rooms/{room.id}", headers={"X-Admin-Token": "wrong"}).status_code == 401

    res = client.delete(f"/api/rooms/{room.id}", headers={"X-Admin-Token": "s3cr3t"})
    assert res.status_code == 200
    assert res.json()["status"] == "closed"


def test_viewing_endpoints_stay_public_even_with_a_token_configured(client, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "s3cr3t")
    room = manager.create("X", "en", ["es"], None)  # bypass auth to set up the precondition

    assert client.get("/api/rooms").status_code == 200
    assert client.get(f"/api/rooms/{room.id}").status_code == 200
    assert client.get(f"/api/rooms/{room.id}/export", params={"format": "txt"}).status_code == 200
    assert client.get(f"/api/rooms/{room.id}/qr.png").status_code == 200
    with client.websocket_connect(f"/ws/subtitles/{room.id}") as ws:
        assert ws.receive_json()["type"] == "status"
