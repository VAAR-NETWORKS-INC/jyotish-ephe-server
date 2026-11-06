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

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

router = APIRouter()
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_hits: dict[str, list[float]] = {}
_settings_lock = threading.Lock()
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,79}$")
PRESETS = {
    "anthropic": ["claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1"],
    "openai": ["gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.4-mini"],
}


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _anthropic_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


def _settings_path() -> str:
    analytics = os.environ.get("ANALYTICS_DB", os.path.join(os.path.dirname(__file__), "..", "data", "analytics.db"))
    return os.path.join(os.path.dirname(os.path.abspath(analytics)), "llm.json")


def _default_slot(tier: str) -> dict:
    if tier == "quick":
        model = os.environ.get("ANTHROPIC_MODEL_QUICK", "claude-haiku-5-5")
    else:
        model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
    if not _MODEL_RE.fullmatch(model):
        model = "claude-haiku-5-5" if tier == "quick" else "claude-sonnet-5-5"
    return {"provider": "anthropic", "model": model}


def _load_settings() -> dict:
    try:
        with open(_settings_path(), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _slot(tier: str, saved: dict | None = None) -> dict:
    raw = (saved if saved is not None else _load_settings()).get("basic" if tier == "quick" else "advanced")
    base = _default_slot(tier)
    if not isinstance(raw, dict):
        return base
    provider = raw.get("provider") if raw.get("provider") in ("anthropic", "openai") else base["provider"]
    model = str(raw.get("model") or "").strip()
    if not _MODEL_RE.fullmatch(model):
        model = base["model"]
    return {"provider": provider, "model": model}


def _openai_key(saved: dict | None = None) -> str:
    file_key = str((saved if saved is not None else _load_settings()).get("openai_key") or "").strip()
    return file_key or os.environ.get("OPENAI_API_KEY", "").strip()


def _provider_key(provider: str, saved: dict | None = None) -> str:
    if provider == "openai":
        return _openai_key(saved)
    return _anthropic_key()


def _key_hint(key: str) -> str:
    if len(key) < 8:
        return ""
    return "…" + key[-4:]


def _llm_view() -> dict:
    saved = _load_settings()
    file_key = str(saved.get("openai_key") or "").strip()
    okey = file_key or os.environ.get("OPENAI_API_KEY", "").strip()
    return {
        "basic": _slot("quick", saved),
        "advanced": _slot("default", saved),
        "openai_key_set": bool(okey),
        "openai_key_saved": bool(file_key),
        "openai_key_hint": _key_hint(okey),
        "anthropic_key_set": bool(_anthropic_key()),
        "presets": PRESETS,
    }


class LLMSave(BaseModel):
    basic_provider: str
    basic_model: str
    advanced_provider: str
    advanced_model: str
    openai_key: str | None = None
    clear_openai_key: bool = False


def _clean_slot(provider: str, model: str, label: str) -> dict:
    if provider not in ("anthropic", "openai"):
        raise HTTPException(400, label + " provider must be Anthropic or OpenAI")
    model = model.strip()
    if not _MODEL_RE.fullmatch(model):
        raise HTTPException(400, label + " model id can use letters, numbers, and . _ : -")
    return {"provider": provider, "model": model}


def _save_llm(body: LLMSave) -> dict:
    basic = _clean_slot(body.basic_provider, body.basic_model, "Basic")
    advanced = _clean_slot(body.advanced_provider, body.advanced_model, "Advanced")
    incoming = (body.openai_key or "").strip()
    if incoming and (len(incoming) > 300 or any(c.isspace() for c in incoming)):
        raise HTTPException(400, "That OpenAI key does not look valid")
    with _settings_lock:
        saved = _load_settings()
        out = {"basic": basic, "advanced": advanced}
        if body.clear_openai_key:
            pass
        elif incoming:
            out["openai_key"] = incoming
        elif str(saved.get("openai_key") or "").strip():
            out["openai_key"] = str(saved["openai_key"]).strip()
        path = _settings_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(out, f)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    return _llm_view()


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
    saved = _load_settings()
    used = {_slot("quick", saved)["provider"], _slot("default", saved)["provider"]}
    return {"ok": any(_provider_key(p, saved) for p in used)}


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
    if body.tier not in ("quick", "default"):
        raise HTTPException(400, "tier must be quick or default")
    saved = _load_settings()
    slot = _slot(body.tier, saved)
    key = _provider_key(slot["provider"], saved)
    if not key:
        if slot["provider"] == "openai":
            raise HTTPException(503, "Add an OpenAI API key on the admin page")
        raise HTTPException(503, "ANTHROPIC_API_KEY is not configured on the server")
    _limit(request.client.host if request.client else "?")
    if not body.messages or len(body.messages) > 40:
        raise HTTPException(400, "conversation length is not allowed")
    for m in body.messages:
        if m.role not in ("user", "assistant"):
            raise HTTPException(400, "bad message role")
    payload = _openai_payload(slot["model"], body) if slot["provider"] == "openai" else _anthropic_payload(slot["model"], body)
    raw = json.dumps(payload).encode()
    if len(raw) > 150_000:
        raise HTTPException(413, "request too large")
    if body.stream:
        gen = _sse_openai(payload, key, cache_k) if slot["provider"] == "openai" else _sse(raw, key, cache_k)
        return StreamingResponse(gen, media_type="text/event-stream", headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})
    reply = _complete_openai(payload, key) if slot["provider"] == "openai" else _reply(_complete(raw, key))
    if cache_k and reply["text"] and not reply["tool_uses"]:
        _cache_put(cache_k, reply["text"])
    return reply


def _anthropic_payload(model: str, body: AIReq) -> dict:
    payload: dict = {
        "model": model,
        "max_tokens": 4000 if body.tier == "quick" else 8000,
        "messages": [m.model_dump() for m in body.messages],
    }
    if body.tools:
        payload["tools"] = [
            {"name": t.name[:64], "description": t.description[:4000], "input_schema": t.input_schema}
            for t in body.tools[:12]
        ]
    return payload


def _block_text(block) -> str:
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        if block.get("type") == "text":
            return block.get("text") or ""
        if isinstance(block.get("text"), str):
            return block["text"]
    return ""


def _openai_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        blocks = content if isinstance(content, list) else []
        if role == "assistant":
            text = "\n".join(piece for b in blocks if (piece := _block_text(b)))
            calls = []
            for b in blocks:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                calls.append({
                    "id": b.get("id") or "call",
                    "type": "function",
                    "function": {"name": b.get("name") or "", "arguments": json.dumps(b.get("input") or {})},
                })
            msg: dict = {"role": "assistant", "content": text}
            if calls:
                msg["tool_calls"] = calls
            out.append(msg)
            continue
        results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
        if results and len(results) == len([b for b in blocks if isinstance(b, dict)]):
            for b in results:
                piece = b.get("content")
                out.append({
                    "role": "tool",
                    "tool_call_id": b.get("tool_use_id") or "",
                    "content": piece if isinstance(piece, str) else json.dumps(piece),
                })
            continue
        text = "\n".join(piece for b in blocks if (piece := _block_text(b)))
        out.append({"role": "user", "content": text})
    return out


def _openai_payload(model: str, body: AIReq) -> dict:
    # Reasoning tokens count against this limit, so leave room for the answer.
    payload: dict = {
        "model": model,
        "max_completion_tokens": 8000 if body.tier == "quick" else 16000,
        "messages": _openai_messages([m.model_dump() for m in body.messages]),
    }
    if body.tools:
        payload["tools"] = [
            {
                "type": "function",
                "function": {
                    "name": t.name[:64],
                    "description": t.description[:4000],
                    "parameters": t.input_schema or {"type": "object", "properties": {}},
                },
            }
            for t in body.tools[:12]
        ]
    return payload


def _tool_uses_from_calls(calls: list[dict]) -> tuple[list[dict], list[dict]]:
    content = []
    tools = []
    for call in calls:
        fn = call.get("function") or {}
        raw = fn.get("arguments") or "{}"
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except json.JSONDecodeError:
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        item = {"type": "tool_use", "id": call.get("id"), "name": fn.get("name"), "input": parsed}
        content.append(item)
        tools.append({"id": item["id"], "name": item["name"], "input": parsed})
    return content, tools


def _openai_reply(data: dict) -> dict:
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    text = msg.get("content") or ""
    if isinstance(text, list):
        text = "".join(_block_text(p) for p in text)
    elif not isinstance(text, str):
        text = ""
    content = [{"type": "text", "text": text}] if text else []
    calls, tools = _tool_uses_from_calls(msg.get("tool_calls") or [])
    reason = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}.get(choice.get("finish_reason"), choice.get("finish_reason"))
    return {"text": text, "stop_reason": reason, "content": content + calls, "tool_uses": tools}


class _OpenFold:
    def __init__(self) -> None:
        self.text = ""
        self.calls: dict[int, dict] = {}
        self.finish: str | None = None

    def feed(self, data: dict) -> str:
        choice = (data.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        piece = delta.get("content") or ""
        if isinstance(piece, list):
            piece = "".join(_block_text(p) for p in piece)
        elif not isinstance(piece, str):
            piece = ""
        self.text += piece
        for tc in delta.get("tool_calls") or []:
            idx = int(tc.get("index") or 0)
            cur = self.calls.setdefault(idx, {"id": "", "name": "", "arguments": ""})
            if tc.get("id"):
                cur["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                cur["name"] += fn["name"]
            if fn.get("arguments"):
                cur["arguments"] += fn["arguments"]
        if choice.get("finish_reason"):
            self.finish = choice["finish_reason"]
        return piece

    def finish_payload(self) -> dict:
        calls = []
        for idx in sorted(self.calls):
            cur = self.calls[idx]
            calls.append({"id": cur["id"], "function": {"name": cur["name"], "arguments": cur["arguments"] or "{}"}})
        content, tools = _tool_uses_from_calls(calls)
        if self.text:
            content = [{"type": "text", "text": self.text}] + content
        reason = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}.get(self.finish, self.finish)
        return {"text": self.text, "stop_reason": reason, "content": content, "tool_uses": tools}


def _request(raw: bytes, key: str) -> urllib.request.Request:
    return urllib.request.Request(
        ANTHROPIC_URL,
        data=raw,
        headers={"content-type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"},
        method="POST",
    )


def _provider_error(e: urllib.error.HTTPError, who: str = "Anthropic") -> str:
    if e.code in (401, 403):
        return f"The {who} API key was rejected"
    detail = ""
    try:
        body = json.loads(e.read().decode("utf-8", "replace"))
        err = body.get("error") if isinstance(body, dict) else None
        msg = err.get("message") if isinstance(err, dict) else None
        if isinstance(msg, str):
            detail = " ".join(msg.split())[:180]
    except Exception:
        detail = ""
    if detail and "sk-" not in detail.lower():
        return detail
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


def _openai_request(payload: dict, key: str) -> urllib.request.Request:
    body = dict(payload)
    body["stream"] = True
    return urllib.request.Request(
        OPENAI_URL,
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "authorization": "Bearer " + key},
        method="POST",
    )


def _complete_openai(payload: dict, key: str) -> dict:
    req = urllib.request.Request(
        OPENAI_URL,
        data=json.dumps(payload).encode(),
        headers={"content-type": "application/json", "authorization": "Bearer " + key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90, context=_ssl_context()) as resp:
            return _openai_reply(json.loads(resp.read().decode()))
    except urllib.error.HTTPError as e:
        raise HTTPException(502, _provider_error(e, "OpenAI"))
    except Exception:
        raise HTTPException(502, "The AI provider is unreachable")


def _sse_openai(payload: dict, key: str, cache_k: str | None = None):
    yield _sse_line({"type": "ping"})
    fold = _OpenFold()
    last = time.monotonic()
    try:
        with urllib.request.urlopen(_openai_request(payload, key), timeout=90, context=_ssl_context()) as resp:
            for raw_line in resp:
                line = raw_line.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    parsed = json.loads(data)
                except json.JSONDecodeError:
                    continue
                piece = fold.feed(parsed)
                now = time.monotonic()
                if piece:
                    yield _sse_line({"type": "delta", "text": piece})
                    last = now
                elif now - last >= 5:
                    yield _sse_line({"type": "ping"})
                    last = now
    except urllib.error.HTTPError as e:
        yield _sse_line({"type": "error", "detail": _provider_error(e, "OpenAI")})
        return
    except Exception:
        yield _sse_line({"type": "error", "detail": "The AI provider is unreachable"})
        return
    done = fold.finish_payload()
    if cache_k and done["text"] and not done["tool_uses"]:
        _cache_put(cache_k, done["text"])
    yield _sse_line({"type": "done", **done})


def _admin(key: str | None) -> None:
    from .analytics import _admin as check
    check(key)


@router.get("/admin/llm")
def admin_llm(x_admin_key: str | None = Header(None)):
    _admin(x_admin_key)
    return _llm_view()


@router.post("/admin/llm")
def admin_llm_save(body: LLMSave, x_admin_key: str | None = Header(None)):
    _admin(x_admin_key)
    return _save_llm(body)
