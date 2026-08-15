#!/usr/bin/env python3
"""Голосовой сервер Джарвиса: озвучивает тексты штаба нейроголосом (edge-tts).

Слушает только этот мак (127.0.0.1:8756). Страница штаба шлёт POST {text, voice}
и получает mp3. Запускается launchd-задачей com.shtab.voice."""

import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import edge_tts

DEFAULT_VOICE = "ru-RU-DmitryNeural"
ALLOWED_ORIGINS = ("https://vineli46.github.io", "http://localhost", "null")


def synth(text: str, voice: str) -> bytes:
    async def run() -> bytes:
        com = edge_tts.Communicate(text, voice, rate="+8%", pitch="-4Hz")
        buf = bytearray()
        async for chunk in com.stream():
            if chunk["type"] == "audio":
                buf.extend(chunk["data"])
        return bytes(buf)

    return asyncio.run(run())


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _cors(self):
        origin = self.headers.get("Origin", "")
        allow = origin if origin in ALLOWED_ORIGINS or origin.startswith("file://") else ALLOWED_ORIGINS[0]
        self.send_header("Access-Control-Allow-Origin", allow)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Private-Network", "true")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/ping":
            body = b"jarvis-voice"
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            text = (data.get("text") or "").strip()[:1500]
            voice = data.get("voice") or DEFAULT_VOICE
            if not text:
                raise ValueError("empty text")
            audio = synth(text, voice)
            if not audio:
                raise ValueError("empty audio")
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(audio)))
            self.end_headers()
            self.wfile.write(audio)
        except Exception:
            self.send_response(500)
            self._cors()
            self.end_headers()


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", 8756), Handler).serve_forever()
