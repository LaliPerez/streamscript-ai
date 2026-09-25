from __future__ import annotations

import asyncio
import io
import logging
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.export import FORMATTERS
from app.rooms import manager
from app.worker import caption_message, run_room
from app.ws_hub import hub

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="StreamScript AI")

# Open by default ("*") so any conference's own site can call the REST API
# (POST/GET /api/rooms/...) from their own domain without forking the code.
# WebSocket connections aren't gated by CORS in the first place -- browsers
# don't enforce the same-origin policy on ws:// handshakes -- so this only
# affects the fetch()/XHR-based endpoints. Lock down to specific origins in
# production via CORS_ALLOWED_ORIGINS (comma-separated). No cookies/session
# auth are used here, so allowing all origins doesn't expose any credentialed
# state -- allow_credentials stays False, which is also what makes "*" valid.
_cors_origins = os.getenv("CORS_ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def require_admin(x_admin_token: str | None = Header(default=None)) -> None:
    """Gate room creation/closing behind a shared secret.

    Read fresh on every call (not cached at import time) so tests can set/
    unset ADMIN_TOKEN per-case and so an ops team can rotate it with a
    plain process restart. Unset/empty ADMIN_TOKEN disables the check
    entirely -- convenient for local dev, but see the README: leaving it
    unset on a publicly reachable deployment (now that CORS is wide open
    by default) means anyone who finds the URL can create rooms and burn
    through the Gemini quota on that API key.

    Viewing endpoints (GET, export, the /ws/subtitles feed) stay open on
    purpose -- the audience scanning a QR code shouldn't need a token.
    """
    expected = os.getenv("ADMIN_TOKEN")
    if not expected:
        return
    if not x_admin_token or not secrets.compare_digest(x_admin_token, expected):
        raise HTTPException(401, "missing or invalid X-Admin-Token header")


class CreateRoomRequest(BaseModel):
    title: str
    source_lang: str = "en"
    target_langs: list[str] = ["es"]
    glossary_path: str | None = None


@app.post("/api/rooms", dependencies=[Depends(require_admin)])
async def create_room(req: CreateRoomRequest):
    room = manager.create(req.title, req.source_lang, req.target_langs, req.glossary_path)
    room.worker_task = asyncio.create_task(run_room(room))
    return room.to_public_dict()


@app.get("/api/rooms")
async def list_rooms():
    return [room.to_public_dict() for room in manager.list()]


@app.get("/api/rooms/{room_id}")
async def get_room(room_id: str):
    room = manager.get(room_id)
    if room is None:
        raise HTTPException(404, "room not found")
    return room.to_public_dict()


@app.delete("/api/rooms/{room_id}", dependencies=[Depends(require_admin)])
async def close_room(room_id: str):
    room = await manager.close(room_id)
    if room is None:
        raise HTTPException(404, "room not found")
    return room.to_public_dict()


@app.get("/api/rooms/{room_id}/export")
async def export_room(room_id: str, format: str = "srt", lang: str | None = None):
    room = manager.get(room_id)
    if room is None:
        raise HTTPException(404, "room not found")
    formatter = FORMATTERS.get(format)
    if formatter is None:
        raise HTTPException(400, f"unsupported format '{format}', use one of {list(FORMATTERS)}")
    content = formatter(room.store.all(), lang)
    media_types = {"srt": "text/plain", "vtt": "text/vtt", "txt": "text/plain"}
    filename = f"{room.title or room.id}.{format}".replace(" ", "_")
    return PlainTextResponse(
        content,
        media_type=media_types[format],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/rooms/{room_id}/qr.png")
async def room_qr(room_id: str, request: Request):
    import qrcode

    room = manager.get(room_id)
    if room is None:
        raise HTTPException(404, "room not found")
    url = str(request.base_url).rstrip("/") + f"/watch.html?room={room_id}"
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")


@app.websocket("/ws/ingest/{room_id}")
async def ws_ingest(websocket: WebSocket, room_id: str):
    room = manager.get(room_id)
    if room is None:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    try:
        while True:
            chunk = await websocket.receive_bytes()
            try:
                room.audio_queue.put_nowait(chunk)
            except asyncio.QueueFull:
                pass  # drop chunk rather than block the ingest client
    except WebSocketDisconnect:
        pass


@app.websocket("/ws/subtitles/{room_id}")
async def ws_subtitles(websocket: WebSocket, room_id: str):
    room = manager.get(room_id)
    if room is None:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    await hub.subscribe(room_id, websocket)
    # Status broadcasts only fire on change, so a viewer connecting after the
    # room already went active (the common case) would otherwise never learn
    # the current status and stay stuck on "conectando..." forever.
    await websocket.send_json({"type": "status", "status": room.status, "detail": room.last_error})
    # Same problem for captions: without this, a viewer joining a talk
    # already in progress stares at "esperando subtitulos" until the next
    # sentence finishes, even though the room has been live for minutes.
    if room.store.entries:
        await websocket.send_json(caption_message(room.store.entries[-1]))
    try:
        while True:
            await websocket.receive_text()  # keepalive/pings from client; no client->server data expected
    except WebSocketDisconnect:
        pass
    finally:
        await hub.unsubscribe(room_id, websocket)


_FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"
app.mount("/", StaticFiles(directory=_FRONTEND_DIR, html=True), name="frontend")
