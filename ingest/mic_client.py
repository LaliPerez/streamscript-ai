"""Reference ingest client: streams mic (or a WAV file) into a room as PCM16/16kHz.

Usage:
    python mic_client.py --room <room_id> [--server ws://localhost:8000] [--file talk.wav]

Without --file it captures the default system microphone. --file is handy to
simulate several concurrent rooms in Phase 2 testing without needing several
physical microphones.
"""
from __future__ import annotations

import argparse
import asyncio
import wave

import sounddevice as sd
import websockets

SAMPLE_RATE = 16000
CHUNK_MS = 100
CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_MS // 1000


async def stream_mic(ws) -> None:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes] = asyncio.Queue()

    def callback(indata, frames, time_info, status):
        loop.call_soon_threadsafe(queue.put_nowait, bytes(indata))

    with sd.RawInputStream(
        samplerate=SAMPLE_RATE, blocksize=CHUNK_SAMPLES, channels=1, dtype="int16", callback=callback
    ):
        print("Grabando del micrófono. Ctrl+C para detener.")
        while True:
            chunk = await queue.get()
            await ws.send(chunk)


async def stream_file(ws, path: str) -> None:
    with wave.open(path, "rb") as wf:
        if wf.getframerate() != SAMPLE_RATE or wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            raise SystemExit(
                f"{path} debe ser WAV mono 16-bit a {SAMPLE_RATE}Hz "
                f"(encontrado: {wf.getframerate()}Hz, {wf.getnchannels()}ch, {wf.getsampwidth()*8}bit)"
            )
        print(f"Streameando {path}…")
        while True:
            data = wf.readframes(CHUNK_SAMPLES)
            if not data:
                break
            await ws.send(data)
            await asyncio.sleep(CHUNK_MS / 1000)


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--room", required=True)
    parser.add_argument("--server", default="ws://localhost:8000")
    parser.add_argument("--file", default=None, help="WAV mono 16-bit 16kHz en vez del micrófono")
    args = parser.parse_args()

    url = f"{args.server}/ws/ingest/{args.room}"
    async with websockets.connect(url, max_size=None) as ws:
        if args.file:
            await stream_file(ws, args.file)
        else:
            await stream_mic(ws)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
