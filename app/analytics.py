"""
Anonymous product analytics for Jyotish AI — SQLite, no third parties.

The app sends a random install id (uid) + session id (sid) and a batch of events.
Never stored: birth date/time/place, names, exact coordinates, chat text.

  POST /v1/events        body (JSON, sent as text/plain so sendBeacon works cross-origin):
       {"uid": "...", "sid": "...", "v": "build", "platform": "web|pwa|ios|android", "lang": "en-US",
        "tz": "America/Los_Angeles", "events": [{"name": "tab_view", "ts": "ISO", "props": {...}}]}
  GET  /admin/stats?days=30   header x-admin-key: ADMIN_KEY
  GET  /admin                 dashboard page (asks for the admin key)
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse

from .envfile import load_local_env

load_local_env()
DB_PATH = os.environ.get("ANALYTICS_DB", os.path.join(os.path.dirname(__file__), "..", "data", "analytics.db"))
ADMIN_KEY = os.environ.get("ADMIN_KEY", "")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
UID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
router = APIRouter()
_local = threading.local()
_rate: dict[str, list[float]] = {}

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users (
  uid TEXT PRIMARY KEY, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
  platform TEXT, app_version TEXT, lang TEXT, tz TEXT, city TEXT, country TEXT,
  has_kundli INTEGER DEFAULT 0, events INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, uid TEXT NOT NULL, sid TEXT, ts TEXT NOT NULL, received TEXT NOT NULL,
  name TEXT NOT NULL, props TEXT, platform TEXT, app_version TEXT);
CREATE INDEX IF NOT EXISTS ev_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ev_name_ts ON events(name, ts);
CREATE INDEX IF NOT EXISTS ev_uid_ts ON events(uid, ts);
"""

def db() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA busy_timeout=10000")
        c.executescript(SCHEMA)
        _local.conn = c
    return c

def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

def _clean_ts(s: str | None) -> str:
    now = datetime.now(timezone.utc)
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)
        if t > now + timedelta(minutes=10) or t < now - timedelta(days=30):
            t = now                       # clamp bad device clocks / very old queues
    except Exception:
        t = now
    return t.replace(microsecond=0).isoformat().replace("+00:00", "Z")

def _limited(uid: str, n: int) -> bool:
    """≤ 600 events per uid per 10 minutes (per worker)."""
    now = time.time(); w = [t for t in _rate.get(uid, []) if now - t < 600]
    if len(w) + n > 600:
        _rate[uid] = w; return True
    _rate[uid] = w + [now] * n
    return False

# ------------------------------------------------------------------ ingest
@router.post("/v1/events")
async def ingest(request: Request):
    raw = await request.body()
    if len(raw) > 200_000:
        raise HTTPException(413, "batch too large")
    try:
        body = json.loads(raw or b"{}")
    except Exception:
        raise HTTPException(400, "body must be JSON")
    uid = str(body.get("uid", ""))
    if not UID_RE.match(uid):
        raise HTTPException(400, "bad uid")
    events = body.get("events") or []
    if not isinstance(events, list) or len(events) > 100:
        raise HTTPException(400, "events must be a list of ≤ 100")
    if _limited(uid, len(events)):
        raise HTTPException(429, "slow down")
    sid = str(body.get("sid", ""))[:64]
    plat = str(body.get("platform", ""))[:16]; ver = str(body.get("v", ""))[:32]
    lang = str(body.get("lang", ""))[:16]; tz = str(body.get("tz", ""))[:48]
    now = _now(); rows = []; city = country = None; kundli = 0
    for e in events:
        if not isinstance(e, dict): continue
        name = str(e.get("name", ""))
        if not NAME_RE.match(name): continue
        props = e.get("props") if isinstance(e.get("props"), dict) else {}
        pj = json.dumps(props, separators=(",", ":"))[:2000]
        if name in ("city_set", "app_open") and props.get("city"):
            city = str(props.get("city", ""))[:80] or None; country = str(props.get("country", ""))[:40] or None
        if name == "kundli_generated": kundli = 1
        rows.append((uid, sid, _clean_ts(e.get("ts")), now, name, pj, plat, ver))
    c = db()
    c.execute("BEGIN")
    try:
        c.executemany("INSERT INTO events(uid,sid,ts,received,name,props,platform,app_version) VALUES (?,?,?,?,?,?,?,?)", rows)
        c.execute("""INSERT INTO users(uid,first_seen,last_seen,platform,app_version,lang,tz,city,country,has_kundli,events)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(uid) DO UPDATE SET last_seen=excluded.last_seen, platform=excluded.platform,
                       app_version=excluded.app_version, lang=excluded.lang, tz=excluded.tz,
                       city=COALESCE(excluded.city, users.city), country=COALESCE(excluded.country, users.country),
                       has_kundli=MAX(users.has_kundli, excluded.has_kundli), events=users.events+excluded.events""",
                  (uid, now, now, plat, ver, lang, tz, city, country, kundli, len(rows)))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK"); raise
    return {"ok": True, "stored": len(rows)}

# ------------------------------------------------------------------ stats
def _admin(key: str | None):
    if not ADMIN_KEY:
        raise HTTPException(503, "ADMIN_KEY not configured on the server")
    if not key:
        raise HTTPException(401, "Enter the admin key")
    if key != ADMIN_KEY:
        raise HTTPException(401, "That key was not accepted. Enter it again.")

def q(sql: str, *a):
    return [dict(r) for r in db().execute(sql, a).fetchall()]

@router.get("/admin/stats")
def stats(days: int = 30, x_admin_key: str | None = Header(None)):
    _admin(x_admin_key)
    days = max(1, min(365, days))
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    ago = lambda n: (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()
    active = lambda n: q("SELECT COUNT(DISTINCT uid) n FROM events WHERE ts >= ?", ago(n))[0]["n"]
    out = {
        "generated": _now(), "days": days,
        "totals": {
            "users": q("SELECT COUNT(*) n FROM users")[0]["n"],
            "with_kundli": q("SELECT COUNT(*) n FROM users WHERE has_kundli=1")[0]["n"],
            "events": q("SELECT COUNT(*) n FROM events")[0]["n"],
            "dau": active(1), "wau": active(7), "mau": active(30),
        },
        "daily": q("""SELECT substr(ts,1,10) day, COUNT(DISTINCT uid) users, COUNT(DISTINCT sid) sessions, COUNT(*) events
                      FROM events WHERE ts >= ? GROUP BY day ORDER BY day""", since),
        "new_users": q("SELECT substr(first_seen,1,10) day, COUNT(*) n FROM users WHERE first_seen >= ? GROUP BY day ORDER BY day", since),
        "top_events": q("SELECT name, COUNT(*) n, COUNT(DISTINCT uid) users FROM events WHERE ts >= ? GROUP BY name ORDER BY n DESC LIMIT 30", since),
        "top_tabs": q("""SELECT json_extract(props,'$.tab') tab, COUNT(*) n, COUNT(DISTINCT uid) users FROM events
                         WHERE name='tab_view' AND ts >= ? GROUP BY tab ORDER BY n DESC""", since),
        "cities": q("SELECT COALESCE(city,'(unknown)') city, COUNT(*) users FROM users GROUP BY city ORDER BY users DESC LIMIT 25"),
        "platforms": q("SELECT COALESCE(NULLIF(platform,''),'?') platform, COUNT(*) users FROM users GROUP BY platform ORDER BY users DESC"),
        "ai": {
            "by_chat": q("""SELECT json_extract(props,'$.chat') chat, COUNT(*) questions, COUNT(DISTINCT uid) users,
                              ROUND(AVG(json_extract(props,'$.ms'))) avg_ms, SUM(json_extract(props,'$.ok')=0) failed
                            FROM events WHERE name='ai_question' AND ts >= ? GROUP BY chat""", since),
            "tools": q("""SELECT j.value tool, COUNT(*) n FROM events e, json_each(json_extract(e.props,'$.tools')) j
                          WHERE e.name='ai_question' AND e.ts >= ? GROUP BY tool ORDER BY n DESC""", since),
        },
        "muhurtham": q("""SELECT json_extract(props,'$.event') event, COUNT(*) n FROM events
                          WHERE name='muhurtham_search' AND ts >= ? GROUP BY event ORDER BY n DESC""", since),
        "engine": q("""SELECT json_extract(props,'$.engine') engine, COUNT(*) n FROM events
                       WHERE name='kundli_generated' AND ts >= ? GROUP BY engine""", since),
        "retention": q("""WITH c AS (SELECT uid, first_seen, substr(first_seen,1,10) d0 FROM users WHERE first_seen <= ?)
            SELECT COUNT(*) cohort,
              ROUND(100.0*SUM(EXISTS(SELECT 1 FROM events e WHERE e.uid=c.uid AND e.ts >= datetime(c.d0,'+1 day') AND e.ts < datetime(c.d0,'+2 day')))/MAX(COUNT(*),1),1) d1_pct,
              ROUND(100.0*SUM(EXISTS(SELECT 1 FROM events e WHERE e.uid=c.uid AND e.ts >= datetime(c.d0,'+7 day') AND e.ts < datetime(c.d0,'+14 day')))/MAX(COUNT(*),1),1) d7_pct
            FROM c WHERE first_seen >= ?""", ago(7), ago(days + 7)),
        "recent_users": q("""SELECT substr(uid,1,8) uid, first_seen, last_seen, platform, city, has_kundli, events
                             FROM users ORDER BY last_seen DESC LIMIT 25"""),
    }
    return out

@router.get("/admin/user/{uid_prefix}")
def user_timeline(uid_prefix: str, x_admin_key: str | None = Header(None)):
    """One user's recent event trail (by the 8-char prefix shown on the dashboard)."""
    _admin(x_admin_key)
    if not re.match(r"^[A-Za-z0-9-]{4,64}$", uid_prefix): raise HTTPException(400, "bad id")
    u = q("SELECT * FROM users WHERE uid LIKE ? LIMIT 1", uid_prefix + "%")
    if not u: raise HTTPException(404, "not found")
    return {"user": u[0], "events": q("SELECT ts, name, props FROM events WHERE uid=? ORDER BY ts DESC LIMIT 200", u[0]["uid"])}

def _dashboard():
    with open(os.path.join(os.path.dirname(__file__), "admin.html"), encoding="utf-8") as f:
        html = f.read()
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})

@router.get("/admin", response_class=HTMLResponse)
@router.get("/admin/", response_class=HTMLResponse, include_in_schema=False)
def dashboard():
    return _dashboard()
