"""
Grahan (eclipse) API — drop-in FastAPI router using Swiss Ephemeris.

Add to your app:
    from .eclipses import router as eclipses_router
    app.include_router(eclipses_router)

GET /v1/eclipses?lat=38.678&lon=-121.176&tz=America/Los_Angeles[&from=2026-11-06&count=6&ayanamsa=lahiri&visible_only=false]

For each eclipse (solar and lunar, merged by date):
  - global type and greatest-eclipse time
  - local visibility for the given place, contact times (sparsha / madhya / moksha), and coverage
  - Sutak window (12 h before a solar, 9 h before a lunar sparsha), only when visible locally and not penumbral-only
  - sidereal rashi and nakshatra of the eclipse (Moon position at maximum)

Relies on swe.set_ephe_path(...) having been called already (as in main.py).
Swiss Ephemeris keeps global state (sidereal mode); if your app serialises calls with a lock, wrap `eclipses()` the same way.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import swisseph as swe
from fastapi import APIRouter, HTTPException, Query

router = APIRouter()

RASHI = ["Mesha", "Vrishabha", "Mithuna", "Karka", "Simha", "Kanya", "Tula", "Vrishchika", "Dhanu", "Makara", "Kumbha", "Meena"]
NAK = ["Ashwini", "Bharani", "Krittika", "Rohini", "Mrigashira", "Ardra", "Punarvasu", "Pushya", "Ashlesha", "Magha",
       "Purva Phalguni", "Uttara Phalguni", "Hasta", "Chitra", "Swati", "Vishakha", "Anuradha", "Jyeshtha", "Mula",
       "Purva Ashadha", "Uttara Ashadha", "Shravana", "Dhanishta", "Shatabhisha", "Purva Bhadrapada", "Uttara Bhadrapada", "Revati"]
AYAN = {"lahiri": swe.SIDM_LAHIRI, "raman": swe.SIDM_RAMAN, "kp": swe.SIDM_KRISHNAMURTI, "yukteshwar": swe.SIDM_YUKTESHWAR,
        "true_chitra": swe.SIDM_TRUE_CITRA, "true_pushya": swe.SIDM_TRUE_PUSHYA, "fagan_bradley": swe.SIDM_FAGAN_BRADLEY}
EPH = swe.FLG_SWIEPH
SUTAK_H = {"solar": 12, "lunar": 9}


def _utc(jd: float | None) -> datetime | None:
    if not jd or jd <= 0:
        return None
    y, m, d, h = swe.revjul(jd)
    return (datetime(y, m, d, tzinfo=timezone.utc) + timedelta(hours=h)).replace(microsecond=0)


def _t(jd: float | None, z: ZoneInfo) -> dict | None:
    u = _utc(jd)
    if not u:
        return None
    loc = u.astimezone(z)
    return {"utc": u.isoformat().replace("+00:00", "Z"), "local": loc.strftime("%a %d %b %Y, %I:%M %p").replace(" 0", " "), "jd": round(jd, 6)}


def _where(jd: float) -> dict:
    moon = swe.calc_ut(jd, swe.MOON, EPH | swe.FLG_SIDEREAL)[0][0] % 360
    return {"rashi": RASHI[int(moon // 30)], "nakshatra": NAK[int(moon // (360 / 27))], "moon_sidereal_lon": round(moon, 4)}


def _solar(tstart: float, geo: tuple, z: ZoneInfo) -> tuple[float, dict]:
    res, g = swe.sol_eclipse_when_glob(tstart, EPH, 0)
    kind = ("total" if res & swe.ECL_TOTAL else "annular" if res & swe.ECL_ANNULAR
            else "hybrid" if res & swe.ECL_ANNULAR_TOTAL else "partial")
    out = {"kind": "solar", "type": kind, "greatest": _t(g[0], z), **_where(g[0]), "local": {"visible": False}}
    lr, lt, attr = swe.sol_eclipse_when_loc(g[0] - 1, geo, EPH)
    if lr > 0 and abs(lt[0] - g[0]) < 0.5:          # same eclipse, seen from here
        ltype = "total" if lr & swe.ECL_TOTAL else "annular" if lr & swe.ECL_ANNULAR else "partial"
        begin, end = lt[1], lt[4]
        out["local"] = {
            "visible": True, "type_here": ltype,
            "sparsha_begin": _t(begin, z), "madhya_max": _t(lt[0], z), "moksha_end": _t(end, z),
            "totality_begin": _t(lt[2], z) if lt[2] else None, "totality_end": _t(lt[3], z) if lt[3] else None,
            "magnitude": round(attr[0], 3), "sun_covered_pct": round(min(1.0, attr[2]) * 100, 1),
            "sutak_begin": _t(begin - SUTAK_H["solar"] / 24, z), "sutak_end": _t(end, z),
        }
    return g[0], out


def _lunar(tstart: float, geo: tuple, z: ZoneInfo) -> tuple[float, dict]:
    res, g = swe.lun_eclipse_when(tstart, EPH, 0)
    # g: 0 max, 2 partial begin, 3 partial end, 4 total begin, 5 total end, 6 penumbral begin, 7 penumbral end
    kind = "total" if res & swe.ECL_TOTAL else "partial" if res & swe.ECL_PARTIAL else "penumbral"
    out = {"kind": "lunar", "type": kind, "greatest": _t(g[0], z), **_where(g[0]),
           "global": {"penumbral_begin": _t(g[6], z), "partial_begin": _t(g[2], z) if g[2] else None,
                      "total_begin": _t(g[4], z) if g[4] else None, "total_end": _t(g[5], z) if g[5] else None,
                      "partial_end": _t(g[3], z) if g[3] else None, "penumbral_end": _t(g[7], z)},
           "local": {"visible": False}}
    lr, lt, attr = swe.lun_eclipse_when_loc(g[0] - 1, geo, EPH)
    if lr > 0 and abs(lt[0] - g[0]) < 0.5:
        # Sparsha/moksha = umbral contacts (partial phase); penumbral-only eclipses are traditionally not observed
        begin = lt[2] if lt[2] else lt[6]
        end = lt[3] if lt[3] else lt[7]
        observed = kind != "penumbral"
        out["local"] = {
            "visible": True, "moonrise": _t(lt[8], z) if lt[8] else None, "moonset": _t(lt[9], z) if lt[9] else None,
            "sparsha_begin": _t(begin, z), "madhya_max": _t(lt[0], z), "moksha_end": _t(end, z),
            "umbral_magnitude": round(attr[0], 3), "penumbral_magnitude": round(attr[1], 3),
            "traditionally_observed": observed,
            "sutak_begin": _t(begin - SUTAK_H["lunar"] / 24, z) if observed else None,
            "sutak_end": _t(end, z) if observed else None,
            "note": None if observed else "Penumbral only — the Moon dims slightly; no Sutak is observed traditionally.",
        }
    return g[0], out


def _list_eclipses(lat: float, lon: float, tz: str, from_: str | None, count: int, ayanamsa: str, visible_only: bool) -> dict:
    try:
        z = ZoneInfo(tz)
    except Exception:
        raise HTTPException(400, f"Unknown time zone '{tz}'")
    if ayanamsa not in AYAN:
        raise HTTPException(400, f"ayanamsa must be one of {', '.join(AYAN)}")
    swe.set_sid_mode(AYAN[ayanamsa], 0, 0)
    start = datetime.fromisoformat(from_).replace(tzinfo=timezone.utc) if from_ else datetime.now(timezone.utc)
    jd0 = swe.julday(start.year, start.month, start.day, start.hour + start.minute / 60)
    geo = (lon, lat, 0)
    found, ts, tl = [], jd0, jd0
    for _ in range(80):                                   # safety bound
        if len(found) >= count:
            break
        js, s = _solar(ts, geo, z)
        jl, l = _lunar(tl, geo, z)
        if js <= jl:
            ev, ts = s, js + 20
        else:
            ev, tl = l, jl + 20
        if not visible_only or ev["local"]["visible"]:
            found.append(ev)
    return {"place": {"lat": lat, "lon": lon, "tz": tz}, "ayanamsa": ayanamsa,
            "sutak_rule": "Solar: 12 h before first contact; lunar: 9 h before umbral contact; only when the eclipse is visible at the place.",
            "eclipses": found}


@router.get("/v1/eclipses")
def eclipses(lat: float = Query(..., ge=-90, le=90), lon: float = Query(..., ge=-180, le=180), tz: str = "UTC",
             from_: str | None = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
             count: int = Query(6, ge=1, le=20), ayanamsa: str = "lahiri", visible_only: bool = False):
    # Same lock as the other Swiss Ephemeris routes: sidereal mode is process-wide.
    from .main import _LOCK
    with _LOCK:
        return _list_eclipses(lat, lon, tz, from_, count, ayanamsa, visible_only)
