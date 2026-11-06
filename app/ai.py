"""Chat proxy. ANTHROPIC_API_KEY stays on the server. Prompts and replies are not logged."""
from __future__ import annotations

import json
import os
import ssl
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
        return os.environ.get("ANTHROPIC_MODEL_QUICK", "claude-haiku-4-5")
    return os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")


class Msg(BaseModel):
    role: str
    content: str | list


class ToolDef(BaseModel):
    name: str
    description: str = ""
    input_schema: dict = Field(default_factory=dict)


class AIReq(BaseModel):
    tier: str = "default"
    messages: list[Msg]
    tools: list[ToolDef] = []
    stream: bool = False


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


@router.post("/v1/ai")
def ai_complete(body: AIReq, request: Request):
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
        "max_tokens": 1200 if body.tier == "quick" else 2200,
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
            _sse(raw, key),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )
    data = _complete(raw, key)
    return _reply(data)


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


def _sse(raw: bytes, key: str):
    # A first byte goes out before the model starts, so a proxy does not
    # treat the long reading as a dead connection and drop it.
    yield _sse_line({"type": "ping"})
    payload = json.loads(raw)
    payload["stream"] = True
    body = json.dumps(payload).encode()
    fold = _Fold()
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
                    if piece:
                        yield _sse_line({"type": "delta", "text": piece})
    except urllib.error.HTTPError as e:
        yield _sse_line({"type": "error", "detail": _provider_error(e)})
        return
    except Exception:
        yield _sse_line({"type": "error", "detail": "The AI provider is unreachable"})
        return
    yield _sse_line({"type": "done", **fold.finish()})
