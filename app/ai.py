"""Chat proxy. ANTHROPIC_API_KEY stays on the server. Prompts and replies are not logged."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import ssl
import threading
import time
import urllib.error
import urllib.request

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

router = APIRouter()
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_hits: dict[str, list[float]] = {}


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


def _model(tier: str) -> str:
    if tier == "quick":
        return os.environ.get("ANTHROPIC_MODEL_QUICK", "claude-haiku-5-5")
    return os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")


class Msg(BaseModel):
    role: str
    content: str | list


class ToolDef(BaseModel):
    name: str
    description: str = ""
    input_schema: dict = Field(default_factory=dict)


class CacheId(BaseModel):
    birth: str
    lat: float
    lon: float
    tz: str
    lang: str = "en"
    ayanamsa: str = "lahiri"


class AIReq(BaseModel):
    tier: str = "default"
    messages: list[Msg]
    tools: list[ToolDef] = []
    stream: bool = False
    cache_id: CacheId | None = None
    cache_key: str | None = None


# Readings that include "current" dasha and transits. Same birth is reused until this age.
CACHE_TTL = 30 * 86400
CACHE_VERSION = "2"
_cache_init = threading.Lock()


class _Fold:
    """Turn Anthropic SSE events into text deltas and a final message."""

    def __init__(self) -> None:
        self.blocks: list[dict] = []
        self.text = ""
        self.stop: str | None = None

    def feed(self, event: str, data: dict) -> str:
        if event == "content_block_start":
            block = data.get("content_block") or {}
            kind = block.get("type")
            if kind == "text":
                self.blocks.append({"type": "text", "text": block.get("text") or ""})
            elif kind == "tool_use":
                self.blocks.append({"type": "tool_use", "id": block.get("id"), "name": block.get("name"), "input": "", "_raw": ""})
            elif kind in ("thinking", "redacted_thinking"):
                self.blocks.append(dict(block))
            return ""
        if event == "content_block_delta":
            delta = data.get("delta") or {}
            if not self.blocks:
                return ""
            cur = self.blocks[-1]
            if delta.get("type") == "text_delta" and cur.get("type") == "text":
                piece = delta.get("text") or ""
                cur["text"] += piece
                self.text += piece
                return piece
            if delta.get("type") == "thinking_delta" and cur.get("type") == "thinking":
                cur["thinking"] = (cur.get("thinking") or "") + (delta.get("thinking") or "")
            elif delta.get("type") == "signature_delta" and cur.get("type") == "thinking":
                cur["signature"] = (cur.get("signature") or "") + (delta.get("signature") or "")
            if delta.get("type") == "input_json_delta" and cur.get("type") == "tool_use":
                cur["_raw"] += delta.get("partial_json") or ""
            return ""
        if event == "content_block_stop" and self.blocks and self.blocks[-1].get("type") == "tool_use":
            cur = self.blocks[-1]
            raw = cur.pop("_raw", "") or "{}"
            try:
                cur["input"] = json.loads(raw)
            except json.JSONDecodeError:
                cur["input"] = {}
            return ""
        if event == "message_delta":
            self.stop = (data.get("delta") or {}).get("stop_reason") or self.stop
        return ""

    def finish(self) -> dict:
        content = []
        tools = []
        for b in self.blocks:
            if b.get("type") == "text":
                content.append({"type": "text", "text": b.get("text") or ""})
            elif b.get("type") == "tool_use":
                item = {"type": "tool_use", "id": b.get("id"), "name": b.get("name"), "input": b.get("input") or {}}
                content.append(item)
                tools.append({"id": item["id"], "name": item["name"], "input": item["input"]})
            elif b.get("type") in ("thinking", "redacted_thinking"):
                content.append({k: v for k, v in b.items() if not str(k).startswith("_")})
        return {"text": self.text, "stop_reason": self.stop, "content": content, "tool_uses": tools}


def _limit(ip: str) -> None:
    now = time.time()
    arr = [t for t in _hits.get(ip, []) if now - t < 600]
    if len(arr) >= 60:
        raise HTTPException(429, "Too many AI requests. Try again in a few minutes.")
    arr.append(now)
    _hits[ip] = arr


@router.get("/v1/ai/status")
def ai_status():
    return {"ok": bool(_key())}


def _cache_path() -> str:
    analytics = os.environ.get("ANALYTICS_DB", os.path.join(os.path.dirname(__file__), "..", "data", "analytics.db"))
    return os.environ.get("AI_CACHE_DB", os.path.join(os.path.dirname(os.path.abspath(analytics)), "ai_cache.db"))


def _cache_db() -> sqlite3.Connection:
    path = _cache_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    c = sqlite3.connect(path, timeout=10, isolation_level=None)
    c.execute("PRAGMA busy_timeout=10000")
    with _cache_init:
        c.execute("CREATE TABLE IF NOT EXISTS readings (k TEXT PRIMARY KEY, body TEXT NOT NULL, created INTEGER NOT NULL)")
    return c


def _cache_key(cid: CacheId | None) -> str | None:
    if cid is None:
        return None
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2})(?::\d{2})?", cid.birth.strip())
    tz = cid.tz.strip()
    lang = cid.lang.strip().lower()
    ayan = cid.ayanamsa.strip().lower()
    if not match:
        return None
    birth = f"{match.group(1)} {match.group(2)}"
    if not (-90 <= cid.lat <= 90 and -180 <= cid.lon <= 180):
        return None
    if not tz or len(tz) > 64 or lang not in ("en", "te") or not ayan or len(ayan) > 32:
        return None
    raw = "|".join([CACHE_VERSION, birth, f"{cid.lat:.4f}", f"{cid.lon:.4f}", tz, lang, ayan])
    return hashlib.sha256(raw.encode()).hexdigest()


def _cache_get(k: str) -> str | None:
    try:
        c = _cache_db()
        row = c.execute("SELECT body, created FROM readings WHERE k = ?", (k,)).fetchone()
        c.close()
    except sqlite3.Error:
        return None
    if not row or time.time() - row[1] > CACHE_TTL:
        return None
    return row[0] or None


def _cache_put(k: str, text: str) -> None:
    if not k or not text or len(text) > 200_000:
        return
    now = int(time.time())
    try:
        c = _cache_db()
        # First reading wins until it expires. A later request cannot replace it.
        c.execute(
            "INSERT INTO readings (k, body, created) VALUES (?,?,?) "
            "ON CONFLICT(k) DO UPDATE SET body = excluded.body, created = excluded.created "
            "WHERE readings.created < ?",
            (k, text, now, now - CACHE_TTL),
        )
        c.close()
    except sqlite3.Error:
        return


def _cached_sse(text: str):
    yield _sse_line({"type": "ping"})
    yield _sse_line({"type": "delta", "text": text})
    yield _sse_line({"type": "done", "text": text, "stop_reason": "end_turn", "content": [{"type": "text", "text": text}], "tool_uses": [], "cached": True})


@router.post("/v1/ai")
def ai_complete(body: AIReq, request: Request):
    cache_k = body.cache_key if body.cache_key and re.fullmatch(r"[0-9a-f]{64}", body.cache_key) else _cache_key(body.cache_id)
    if cache_k:
        hit = _cache_get(cache_k)
        if hit:
            if body.stream:
                return StreamingResponse(_cached_sse(hit), media_type="text/event-stream", headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})
            return {"text": hit, "stop_reason": "end_turn", "content": [{"type": "text", "text": hit}], "tool_uses": [], "cached": True}
    key = _key()
    if not key:
        raise HTTPException(503, "ANTHROPIC_API_KEY is not configured on the server")
    _limit(request.client.host if request.client else "?")
    if body.tier not in ("quick", "default"):
        raise HTTPException(400, "tier must be quick or default")
    if not body.messages or len(body.messages) > 40:
        raise HTTPException(400, "conversation length is not allowed")
    for m in body.messages:
        if m.role not in ("user", "assistant"):
            raise HTTPException(400, "bad message role")
    payload: dict = {
        "model": _model(body.tier),
        "max_tokens": 4000 if body.tier == "quick" else 8000,
        "messages": [m.model_dump() for m in body.messages],
    }
    if body.tools:
        payload["tools"] = [
            {"name": t.name[:64], "description": t.description[:4000], "input_schema": t.input_schema}
            for t in body.tools[:12]
        ]
    raw = json.dumps(payload).encode()
    if len(raw) > 150_000:
        raise HTTPException(413, "request too large")
    if body.stream:
        return StreamingResponse(
            _sse(raw, key, cache_k),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )
    data = _complete(raw, key)
    reply = _reply(data)
    if cache_k and reply["text"] and not reply["tool_uses"]:
        _cache_put(cache_k, reply["text"])
    return reply


def _request(raw: bytes, key: str) -> urllib.request.Request:
    return urllib.request.Request(
        ANTHROPIC_URL,
        data=raw,
        headers={"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"},
        method="POST",
    )


def _provider_error(e: urllib.error.HTTPError) -> str:
    if e.code in (401, 403):
        return "The Anthropic API key was rejected"
    return "The AI provider rejected the request"


def _complete(raw: bytes, key: str) -> dict:
    try:
        with urllib.request.urlopen(_request(raw, key), timeout=90, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise HTTPException(502, _provider_error(e))
    except Exception:
        raise HTTPException(502, "The AI provider is unreachable")


def _reply(data: dict) -> dict:
    content = data.get("content") or []
    text = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    tool_uses = [
        {"id": b.get("id"), "name": b.get("name"), "input": b.get("input") or {}}
        for b in content if isinstance(b, dict) and b.get("type") == "tool_use"
    ]
    return {"text": text, "stop_reason": data.get("stop_reason"), "content": content, "tool_uses": tool_uses}


def _sse_line(obj: dict) -> bytes:
    return ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()


def _sse(raw: bytes, key: str, cache_k: str | None = None):
    # A first byte goes out before the model starts, so a proxy does not
    # treat the long reading as a dead connection and drop it.
    yield _sse_line({"type": "ping"})
    payload = json.loads(raw)
    payload["stream"] = True
    body = json.dumps(payload).encode()
    fold = _Fold()
    # Thinking produces no visible text. Forward a ping so a proxy does not
    # close the connection while the model is still working.
    last = time.monotonic()
    try:
        with urllib.request.urlopen(_request(body, key), timeout=90, context=_ssl_context()) as resp:
            event = ""
            for raw_line in resp:
                line = raw_line.decode("utf-8", "replace").strip()
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    try:
                        data = json.loads(line[5:].strip())
                    except json.JSONDecodeError:
                        continue
                    piece = fold.feed(event, data)
                    now = time.monotonic()
                    if piece:
                        yield _sse_line({"type": "delta", "text": piece})
                        last = now
                    elif event == "ping" or now - last >= 5:
                        yield _sse_line({"type": "ping"})
                        last = now
    except urllib.error.HTTPError as e:
        yield _sse_line({"type": "error", "detail": _provider_error(e)})
        return
    except Exception:
        yield _sse_line({"type": "error", "detail": "The AI provider is unreachable"})
        return
    done = fold.finish()
    if cache_k and done["text"] and not done["tool_uses"]:
        _cache_put(cache_k, done["text"])
    yield _sse_line({"type": "done", **done})
