"""Per-room pub/sub fan-out to audience/overlay/dashboard WebSocket clients."""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict

from fastapi import WebSocket


class WsHub:
    def __init__(self) -> None:
        self._subscribers: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def subscribe(self, room_id: str, ws: WebSocket) -> None:
        async with self._lock:
            self._subscribers[room_id].add(ws)

    async def unsubscribe(self, room_id: str, ws: WebSocket) -> None:
        async with self._lock:
            self._subscribers[room_id].discard(ws)

    async def broadcast(self, room_id: str, message: dict) -> None:
        payload = json.dumps(message, ensure_ascii=False)
        dead: list[WebSocket] = []
        for ws in list(self._subscribers.get(room_id, ())):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._subscribers[room_id].discard(ws)


hub = WsHub()
