"""Chat proxy. ANTHROPIC_API_KEY stays on the server. Prompts and replies are not logged."""
from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request

from fastapi import APIRouter, HTTPException, Request
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
    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=raw,
        headers={"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90, context=_ssl_context()) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise HTTPException(502, "The Anthropic API key was rejected")
        raise HTTPException(502, "The AI provider rejected the request")
    except Exception:
        raise HTTPException(502, "The AI provider is unreachable")
    content = data.get("content") or []
    text = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    tool_uses = [
        {"id": b.get("id"), "name": b.get("name"), "input": b.get("input") or {}}
        for b in content if isinstance(b, dict) and b.get("type") == "tool_use"
    ]
    return {"text": text, "stop_reason": data.get("stop_reason"), "content": content, "tool_uses": tool_uses}
