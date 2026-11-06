"""
Jyotish Ephemeris API — Swiss Ephemeris (native) behind FastAPI.

Endpoints (all angles in degrees, all instants in UTC ISO-8601):
  GET  /health
  GET  /v1/ayanamsas
  GET  /v1/chart      ?datetime=1990-01-15T01:00:00Z&lat=17.385&lon=78.4867[&ayanamsa=lahiri&node=mean&houses=W]
  GET  /v1/panchang   ?date=2026-11-06&lat=..&lon=..&tz=Asia/Kolkata[&ayanamsa=lahiri&sunrise=standard]
  GET  /v1/transits   ?datetime=...[&ayanamsa=lahiri&node=mean]           (geocentric, no location needed)
  POST /v1/positions  {"datetimes": [...], "bodies": ["Sun","Moon"], "ayanamsa": "lahiri"}   (batch, <= 2000)
"""
from __future__ import annotations

import functools
import os
import threading
from datetime import date as Date, datetime, timedelta, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

import swisseph as swe
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

EPHE_PATH = os.environ.get("EPHE_PATH", os.path.join(os.path.dirname(__file__), "..", "ephe"))
swe.set_ephe_path(EPHE_PATH)
ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "*").split(",")]

app = FastAPI(title="Jyotish Ephemeris API", version="1.1.0")
from .analytics import router as analytics_router  # noqa: E402
app.include_router(analytics_router)
API_KEY = os.environ.get("API_KEY", "")   # optional: require header x-api-key


@app.middleware("http")
async def guard_and_cache(request, call_next):
    if API_KEY and request.url.path.startswith("/v1/") and request.url.path != "/v1/events" and request.headers.get("x-api-key") != API_KEY:
        from fastapi.responses import JSONResponse
        return JSONResponse({"detail": "missing or bad x-api-key"}, status_code=401)
    resp = await call_next(request)
    if request.method == "GET" and resp.status_code == 200:
        path = request.url.path
        if path == "/v1/chart" or (path == "/v1/transits" and "datetime" in request.query_params):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"   # a past instant never changes
        elif path == "/v1/panchang":
            resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["GET", "POST"], allow_headers=["*"], max_age=86400)

# ---------------------------------------------------------------- constants
AYANAMSA = {
    "lahiri": swe.SIDM_LAHIRI, "raman": swe.SIDM_RAMAN, "kp": swe.SIDM_KRISHNAMURTI,
    "yukteshwar": swe.SIDM_YUKTESHWAR, "true_chitra": swe.SIDM_TRUE_CITRA,
    "true_pushya": swe.SIDM_TRUE_PUSHYA, "fagan_bradley": swe.SIDM_FAGAN_BRADLEY,
}
BODIES = {"Sun": swe.SUN, "Moon": swe.MOON, "Mars": swe.MARS, "Mercury": swe.MERCURY,
          "Jupiter": swe.JUPITER, "Venus": swe.VENUS, "Saturn": swe.SATURN}
RASHI = ["Mesha", "Vrishabha", "Mithuna", "Karka", "Simha", "Kanya", "Tula", "Vrishchika", "Dhanu", "Makara", "Kumbha", "Meena"]
NAK = ["Ashwini", "Bharani", "Krittika", "Rohini", "Mrigashira", "Ardra", "Punarvasu", "Pushya", "Ashlesha", "Magha",
       "Purva Phalguni", "Uttara Phalguni", "Hasta", "Chitra", "Swati", "Vishakha", "Anuradha", "Jyeshtha", "Mula",
       "Purva Ashadha", "Uttara Ashadha", "Shravana", "Dhanishta", "Shatabhisha", "Purva Bhadrapada", "Uttara Bhadrapada", "Revati"]
TITHI = ["Pratipada", "Dwitiya", "Tritiya", "Chaturthi", "Panchami", "Shashthi", "Saptami", "Ashtami", "Navami",
         "Dashami", "Ekadashi", "Dwadashi", "Trayodashi", "Chaturdashi"]
YOGA = ["Vishkambha", "Priti", "Ayushman", "Saubhagya", "Shobhana", "Atiganda", "Sukarma", "Dhriti", "Shula", "Ganda",
        "Vriddhi", "Dhruva", "Vyaghata", "Harshana", "Vajra", "Siddhi", "Vyatipata", "Variyana", "Parigha", "Shiva",
        "Siddha", "Sadhya", "Shubha", "Shukla", "Brahma", "Indra", "Vaidhriti"]
KARANA_MOV = ["Bava", "Balava", "Kaulava", "Taitila", "Gara", "Vanija", "Vishti"]
NAK_SPAN = 360 / 27
FLAGS = swe.FLG_SWIEPH | swe.FLG_SPEED | swe.FLG_SIDEREAL
_LOCK = threading.Lock()   # Swiss Ephemeris keeps global state (sidereal mode) — serialise calls

def locked(fn):
    @functools.wraps(fn)
    def wrap(*a, **k):
        with _LOCK:
            return fn(*a, **k)
    return wrap

# ---------------------------------------------------------------- helpers
def parse_dt(s: str) -> datetime:
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(400, f"Bad datetime '{s}'. Use ISO-8601, e.g. 1990-01-15T01:00:00Z")
    if d.tzinfo is None:
        raise HTTPException(400, "datetime must include a UTC offset or Z")
    return d.astimezone(timezone.utc)

def jd_of(d: datetime) -> float:
    return swe.julday(d.year, d.month, d.day, d.hour + d.minute / 60 + (d.second + d.microsecond / 1e6) / 3600)

def dt_of(jd: float) -> str:
    y, m, d, h = swe.revjul(jd)
    base = datetime(y, m, d, tzinfo=timezone.utc) + timedelta(hours=h)
    return base.replace(microsecond=0).isoformat().replace("+00:00", "Z")

def set_ayanamsa(name: str) -> None:
    if name not in AYANAMSA:
        raise HTTPException(400, f"Unknown ayanamsa '{name}'. Options: {', '.join(AYANAMSA)}")
    swe.set_sid_mode(AYANAMSA[name], 0, 0)

def describe(lon: float) -> dict:
    nk = int(lon // NAK_SPAN)
    return {"sign": int(lon // 30), "sign_name": RASHI[int(lon // 30)], "deg_in_sign": round(lon % 30, 6),
            "nakshatra": nk, "nakshatra_name": NAK[nk], "pada": int((lon % NAK_SPAN) // (NAK_SPAN / 4)) + 1}

def body_pos(jd: float, body: int) -> tuple[float, float, float]:
    xx, _ = swe.calc_ut(jd, body, FLAGS)
    return xx[0] % 360, xx[1], xx[3]          # longitude, latitude, speed (deg/day)

def sun_moon(jd: float) -> tuple[float, float]:
    return body_pos(jd, swe.SUN)[0], body_pos(jd, swe.MOON)[0]

# ---------------------------------------------------------------- endpoints
@app.get("/health")
def health():
    return {"ok": True, "swisseph": swe.version, "ephe_path": os.path.abspath(EPHE_PATH),
            "ephe_files": sorted(f for f in os.listdir(EPHE_PATH) if f.endswith(".se1")) if os.path.isdir(EPHE_PATH) else []}

@app.get("/v1/ayanamsas")
def ayanamsas():
    return {"options": list(AYANAMSA), "default": "lahiri"}

def _planets(jd: float, node: str) -> dict:
    out = {}
    for name, b in BODIES.items():
        lon, lat, spd = body_pos(jd, b)
        out[name] = {"lon": round(lon, 6), "lat": round(lat, 6), "speed": round(spd, 6), "retro": spd < 0, **describe(lon)}
    nlon, _, nspd = body_pos(jd, swe.TRUE_NODE if node == "true" else swe.MEAN_NODE)
    out["Rahu"] = {"lon": round(nlon, 6), "lat": 0.0, "speed": round(nspd, 6), "retro": True, **describe(nlon)}
    k = (nlon + 180) % 360
    out["Ketu"] = {"lon": round(k, 6), "lat": 0.0, "speed": round(nspd, 6), "retro": True, **describe(k)}
    return out

@app.get("/v1/chart")
@locked
def chart(datetime_: str = Query(..., alias="datetime"), lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180),
          ayanamsa: str = "lahiri", node: str = Query("mean", pattern="^(mean|true)$"), houses: str = Query("W", min_length=1, max_length=1)):
    set_ayanamsa(ayanamsa)
    d = parse_dt(datetime_); jd = jd_of(d)
    cusps, ascmc = swe.houses_ex(jd, lat, lon, houses.encode(), swe.FLG_SIDEREAL)
    asc = ascmc[0] % 360
    planets = _planets(jd, node)
    for p in planets.values():
        p["house"] = ((p["sign"] - int(asc // 30)) % 12) + 1      # whole-sign house from Lagna
    return {"datetime": dt_of(jd), "jd_ut": jd, "ayanamsa": ayanamsa, "ayanamsa_value": round(swe.get_ayanamsa_ut(jd), 6),
            "lagna": {"lon": round(asc, 6), **describe(asc)}, "mc": round(ascmc[1] % 360, 6),
            "house_system": houses, "cusps": [round(c % 360, 6) for c in cusps], "planets": planets}

@app.get("/v1/transits")
@locked
def transits(datetime_: str = Query(None, alias="datetime"), ayanamsa: str = "lahiri", node: str = Query("mean", pattern="^(mean|true)$")):
    set_ayanamsa(ayanamsa)
    d = parse_dt(datetime_) if datetime_ else datetime.now(timezone.utc)
    jd = jd_of(d)
    return {"datetime": dt_of(jd), "ayanamsa": ayanamsa, "planets": _planets(jd, node)}

class PositionsReq(BaseModel):
    datetimes: list[str] = Field(..., max_length=2000)
    bodies: list[str] = ["Sun", "Moon"]
    ayanamsa: str = "lahiri"

@app.post("/v1/positions")
@locked
def positions(req: PositionsReq):
    set_ayanamsa(req.ayanamsa)
    for b in req.bodies:
        if b not in BODIES and b not in ("Rahu",):
            raise HTTPException(400, f"Unknown body {b}")
    rows = []
    for s in req.datetimes:
        jd = jd_of(parse_dt(s))
        rows.append({b: round(body_pos(jd, BODIES.get(b, swe.MEAN_NODE))[0], 6) for b in req.bodies})
    return {"ayanamsa": req.ayanamsa, "rows": rows}

# ---------------------------------------------------------------- panchang
def _elements(jd: float) -> dict:
    s, m = sun_moon(jd)
    diff = (m - s) % 360
    return {"tithi": int(diff // 12), "nakshatra": int(m // NAK_SPAN), "yoga": int(((s + m) % 360) // NAK_SPAN), "karana": int(diff // 6)}

def _next_change(key: str, jd0: float, direction: int = 1) -> float:
    v0 = _elements(jd0)[key]
    a = jd0
    for _ in range(100):                          # step 1 hour
        b = a + direction / 24
        if _elements(b)[key] != v0:
            break
        a = b
    lo, hi = a, b
    while abs(hi - lo) > 1 / 86400:               # 1-second precision
        mid = (lo + hi) / 2
        if _elements(mid)[key] == v0: lo = mid
        else: hi = mid
    return hi

def _name(key: str, idx: int) -> str:
    if key == "tithi":
        paksha = "Shukla" if idx < 15 else "Krishna"
        return f"{paksha} {'Purnima' if idx == 14 else 'Amavasya' if idx == 29 else TITHI[idx % 15]}"
    if key == "nakshatra": return NAK[idx]
    if key == "yoga": return YOGA[idx]
    if idx == 0: return "Kimstughna"
    if idx >= 57: return ["Shakuni", "Chatushpada", "Naga"][idx - 57]
    return KARANA_MOV[(idx - 1) % 7]

def _rise_set(jd_start: float, lat: float, lon: float, body: int, rsmi: int) -> float | None:
    res, tret = swe.rise_trans(jd_start, body, rsmi, (lon, lat, 0), 1013.25, 15, swe.FLG_SWIEPH)
    return tret[0] if res == 0 else None

@lru_cache(maxsize=4096)
def _panchang(day: str, lat: float, lon: float, tz: str, ayanamsa: str, mode: str) -> dict:
    set_ayanamsa(ayanamsa)
    zone = ZoneInfo(tz)
    y, mo, d = map(int, day.split("-"))
    midnight = datetime(y, mo, d, tzinfo=zone).astimezone(timezone.utc)
    jd0 = jd_of(midnight)
    flag = swe.BIT_HINDU_RISING if mode == "hindu" else 0
    rise = _rise_set(jd0, lat, lon, swe.SUN, swe.CALC_RISE | flag)
    sset = _rise_set(jd0, lat, lon, swe.SUN, swe.CALC_SET | flag)
    nrise = _rise_set(jd0 + 1, lat, lon, swe.SUN, swe.CALC_RISE | flag)
    mrise = _rise_set(jd0, lat, lon, swe.MOON, swe.CALC_RISE)
    mset = _rise_set(jd0, lat, lon, swe.MOON, swe.CALC_SET)
    if rise is None or nrise is None:
        raise HTTPException(422, "No sunrise at this latitude/date (polar day/night)")
    el = {}
    for key in ("tithi", "nakshatra", "yoga", "karana"):
        items, t = [], rise + 1 / 86400
        for _ in range(4):
            idx = _elements(t)[key]
            start = _next_change(key, t, -1)
            end = _next_change(key, t, 1)
            items.append({"index": idx, "name": _name(key, idx), "start": dt_of(start), "end": dt_of(end)})
            if end > nrise: break
            t = end + 1 / 86400
        el[key] = items
    s, m = sun_moon(rise)
    return {"date": day, "tz": tz, "ayanamsa": ayanamsa, "sunrise_mode": mode,
            "sunrise": dt_of(rise), "sunset": dt_of(sset) if sset else None, "next_sunrise": dt_of(nrise),
            "moonrise": dt_of(mrise) if mrise else None, "moonset": dt_of(mset) if mset else None,
            "weekday": (datetime(y, mo, d).weekday() + 1) % 7,       # 0 = Sunday
            "sun_at_sunrise": {"lon": round(s, 6), **describe(s)}, "moon_at_sunrise": {"lon": round(m, 6), **describe(m)},
            "elements": el}

@app.get("/v1/panchang")
@locked
def panchang(date: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$"), lat: float = Query(..., ge=-66, le=66), lon: float = Query(..., ge=-180, le=180),
             tz: str = "Asia/Kolkata", ayanamsa: str = "lahiri", sunrise: str = Query("standard", pattern="^(standard|hindu)$")):
    try: ZoneInfo(tz)
    except Exception: raise HTTPException(400, f"Unknown time zone '{tz}'")
    Date.fromisoformat(date)
    return _panchang(date, round(lat, 4), round(lon, 4), tz, ayanamsa, sunrise)
