import json

import pytest

from app.ws_hub import WsHub


class FakeWebSocket:
    """Duck-types the one method ws_hub.py actually calls."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.received: list[str] = []

    async def send_text(self, payload: str) -> None:
        if self.fail:
            raise RuntimeError("connection closed")
        self.received.append(payload)


@pytest.fixture
def hub():
    return WsHub()


@pytest.mark.asyncio
async def test_broadcast_reaches_all_subscribers_of_the_room(hub):
    a, b = FakeWebSocket(), FakeWebSocket()
    await hub.subscribe("room1", a)
    await hub.subscribe("room1", b)

    await hub.broadcast("room1", {"type": "caption", "source_text": "hi"})

    assert json.loads(a.received[0]) == {"type": "caption", "source_text": "hi"}
    assert json.loads(b.received[0]) == {"type": "caption", "source_text": "hi"}


@pytest.mark.asyncio
async def test_broadcast_does_not_leak_across_rooms(hub):
    a = FakeWebSocket()
    await hub.subscribe("room1", a)

    await hub.broadcast("room2", {"type": "caption", "source_text": "other room"})

    assert a.received == []


@pytest.mark.asyncio
async def test_unsubscribe_stops_future_broadcasts(hub):
    a = FakeWebSocket()
    await hub.subscribe("room1", a)
    await hub.unsubscribe("room1", a)

    await hub.broadcast("room1", {"type": "caption", "source_text": "hi"})

    assert a.received == []


@pytest.mark.asyncio
async def test_a_dead_socket_is_pruned_and_does_not_break_other_subscribers(hub):
    dead = FakeWebSocket(fail=True)
    alive = FakeWebSocket()
    await hub.subscribe("room1", dead)
    await hub.subscribe("room1", alive)

    await hub.broadcast("room1", {"type": "caption", "source_text": "hi"})
    assert alive.received  # the failure on `dead` must not stop delivery to `alive`

    # second broadcast should not even try the pruned socket
    await hub.broadcast("room1", {"type": "caption", "source_text": "again"})
    assert len(alive.received) == 2
