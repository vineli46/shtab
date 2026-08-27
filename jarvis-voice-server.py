#!/usr/bin/env python3
"""Сервер Джарвиса на маке: нейроголос (edge-tts) + умный мозг (GigaChat Max).

Слушает только этот мак (127.0.0.1:8756). Запускается launchd-задачей com.shtab.voice.
  POST /        {text, voice}        -> mp3 (озвучка)
  GET  /ping                         -> jarvis-voice (жив ли сервер)
  GET  /ai/ping                      -> {"key": true/false} (введён ли ключ Сбера)
  POST /setkey  {key}                -> сохранить ключ GigaChat в ~/штаб/.gigachat-key
  POST /ai      {system, question}   -> {"answer": "..."} от GigaChat
"""

import asyncio
import glob
import json
import os
import queue
import shutil
import ssl
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import edge_tts

BASE = os.path.expanduser("~/штаб")
KEY_FILE = os.path.join(BASE, ".gigachat-key")
CA_FILE = os.path.join(BASE, ".russian_ca.pem")
DEFAULT_VOICE = "ru-RU-DmitryNeural"
ALLOWED_ORIGINS = ("https://vineli46.github.io", "http://localhost", "null")
GIGA_MODELS = ["GigaChat-2-Max", "GigaChat-Max", "GigaChat-2-Pro", "GigaChat-Pro", "GigaChat-2", "GigaChat"]

_token = {"value": None, "until": 0}
FABLE_LOCK = threading.Lock()


def find_claude():
    path = shutil.which("claude")
    if path:
        return path
    cands = sorted(glob.glob(os.path.expanduser(
        "~/.vscode/extensions/anthropic.claude-code-*/resources/native-binary/claude")))
    return cands[-1] if cands else None


FABLE_PROMPT = ("Голосовая реплика Винели со страницы «Штаб». Ответь по-русски, кратко — "
                "до четырёх предложений, для озвучки вслух: без списков, без markdown, "
                "без код-блоков. Свежие цифры бизнеса лежат в файле stats.json, общие "
                "задачи — в tasks.json (текущая папка). Если это поручение — выполни его "
                "сразу сама и отчитайся одним абзацем. Реплика: ")


class FableWarm:
    """Постоянно тёплый headless-Claude: без холодного старта на каждый вопрос."""

    def __init__(self):
        self.proc = None
        self.q = queue.Queue()
        self.lock = threading.Lock()

    def _reader(self, proc, out_q):
        for line in proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("type") == "result":
                out_q.put((ev.get("result") or "").strip())

    def ensure(self):
        if self.proc and self.proc.poll() is None:
            return
        claude = find_claude()
        if not claude:
            raise RuntimeError("claude not found")
        self.q = queue.Queue()
        self.proc = subprocess.Popen(
            [claude, "-p", "--input-format", "stream-json", "--output-format",
             "stream-json", "--verbose", "--permission-mode", "acceptEdits",
             "--model", "haiku", "--strict-mcp-config"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, cwd=BASE, text=True, bufsize=1)
        threading.Thread(target=self._reader, args=(self.proc, self.q), daemon=True).start()

    def ask(self, text, timeout=110):
        with self.lock:
            self.ensure()
            msg = json.dumps({"type": "user", "message": {
                "role": "user", "content": [{"type": "text", "text": text}]}})
            try:
                self.proc.stdin.write(msg + "\n")
                self.proc.stdin.flush()
            except Exception:
                self.proc = None
                self.ensure()
                self.proc.stdin.write(msg + "\n")
                self.proc.stdin.flush()
            try:
                return self.q.get(timeout=timeout)
            except queue.Empty:
                try:
                    self.proc.kill()
                except Exception:
                    pass
                self.proc = None
                raise RuntimeError("timeout")


FABLE_WARM = FableWarm()


def fable_ask(question):
    # «подумай / как следует / сложный / важный» — полная модель со всеми инструментами
    deep = any(w in question.lower() for w in ("подумай", "как следует", "сложн", "важн"))
    prompt = FABLE_PROMPT + question
    if not deep:
        try:
            answer = FABLE_WARM.ask(prompt)
            if answer:
                return answer[-2500:]
        except Exception:
            pass
    claude = find_claude()
    if not claude:
        raise RuntimeError("claude not found")
    with FABLE_LOCK:
        r = subprocess.run(
            [claude, "-p", prompt, "--permission-mode", "acceptEdits", "--continue"],
            cwd=BASE, capture_output=True, text=True, timeout=140)
        answer = (r.stdout or "").strip()
        if r.returncode != 0 or not answer:
            r = subprocess.run(
                [claude, "-p", prompt, "--permission-mode", "acceptEdits"],
                cwd=BASE, capture_output=True, text=True, timeout=140)
            answer = (r.stdout or "").strip()
    return answer[-2500:]


def ssl_ctx():
    try:
        return ssl.create_default_context(cafile=CA_FILE)
    except Exception:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx


def giga_key():
    try:
        return open(KEY_FILE, encoding="utf-8").read().strip()
    except Exception:
        return None


def giga_token():
    if _token["value"] and time.time() < _token["until"] - 60:
        return _token["value"]
    key = giga_key()
    if not key:
        raise RuntimeError("no key")
    req = urllib.request.Request(
        "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
        data=urllib.parse.urlencode({"scope": "GIGACHAT_API_PERS"}).encode(),
        headers={
            "Authorization": "Basic " + key,
            "RqUID": str(uuid.uuid4()),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30, context=ssl_ctx()) as r:
        data = json.loads(r.read().decode())
    _token["value"] = data["access_token"]
    _token["until"] = int(data.get("expires_at", 0)) / 1000 or (time.time() + 1700)
    return _token["value"]


def giga_ask(system, question):
    token = giga_token()
    last_err = None
    for model in GIGA_MODELS:
        body = json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": question},
            ],
            "max_tokens": 220,
            "temperature": 0.5,
        }).encode()
        req = urllib.request.Request(
            "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            data=body,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=60, context=ssl_ctx()) as r:
                data = json.loads(r.read().decode())
            answer = data["choices"][0]["message"]["content"].strip()
            if answer:
                return answer
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code in (404, 422, 400):
                continue
            raise
    raise last_err or RuntimeError("no answer")


def synth(text, voice):
    async def run():
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

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        elif self.path == "/ai/ping":
            self._json(200, {"key": bool(giga_key())})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._json(400, {"error": "bad json"})
            return

        if self.path == "/setkey":
            key = (data.get("key") or "").strip()
            if len(key) < 20:
                self._json(400, {"error": "short key"})
                return
            with open(KEY_FILE, "w", encoding="utf-8") as f:
                f.write(key)
            os.chmod(KEY_FILE, 0o600)
            _token["value"] = None
            try:
                giga_token()
            except Exception:
                os.remove(KEY_FILE)
                self._json(401, {"error": "key rejected"})
                return
            self._json(200, {"ok": True})
            return

        if self.path == "/ai":
            if not giga_key():
                self._json(503, {"error": "no key"})
                return
            try:
                answer = giga_ask(data.get("system") or "", (data.get("question") or "")[:4000])
                self._json(200, {"answer": answer})
            except Exception as e:
                self._json(502, {"error": str(e)[:200]})
            return

        if self.path == "/fable":
            question = (data.get("question") or "").strip()[:4000]
            if not question:
                self._json(400, {"error": "empty"})
                return
            try:
                answer = fable_ask(question)
                if not answer:
                    raise RuntimeError("empty answer")
                self._json(200, {"answer": answer})
            except Exception as e:
                self._json(502, {"error": str(e)[:200]})
            return

        # озвучка (корневой POST)
        try:
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
    threading.Thread(target=lambda: FABLE_WARM.ensure(), daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", 8756), Handler).serve_forever()
