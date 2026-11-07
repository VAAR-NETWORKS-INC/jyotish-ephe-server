# Jyotish Ephemeris API

# Local Development
cd /Users/vadaliramesh/Projects/jyotish/jyotish-ephe-server
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
EPHE_PATH=./ephe uvicorn app.main:app --reload --port 8000



High-precision Vedic astronomy service for the Jyotish AI app: **native Swiss Ephemeris** (pyswisseph 2.10, official `.se1` files, 1800–2400 AD) behind a small FastAPI app, served over HTTPS by Caddy. Built to run on a Hetzner box with Docker.

This repository is astronomy only: charts, transits, panchang, eclipse lists, and batch positions. The AI proxy, anonymous analytics, admin dashboard, and any future accounts live in `jyotish-api`, which stays private. That split is what lets this service be published on its own.

What it gives you over the app's built-in formulas:

| | Built-in JS engine | This server |
|---|---|---|
| Planet / Moon accuracy | ~1′ (arc-minute) | ~0.001″ (JPL-grade) |
| Ayanamsas | Lahiri (approx.) | Lahiri, Raman, KP, Yukteshwar, True Chitra, True Pushya, Fagan-Bradley |
| Rahu | mean only | mean or true |
| Sunrise | simple formula (±1 min) | refraction-corrected, standard or Hindu (disc centre) |
| Panchang end times | 30 s steps | 1 s precision, with start times and moonrise/moonset |

## Deploy on the existing Hetzner box

uAlbums already binds ports 80 and 443 with `ualbums-caddy`. This service joins the `ualbums_net` Docker network and does not start its own web server. From the server, with the uAlbums stack already running:

```bash
cd /path/to/ualbums-server-infra
# DNS A record: ephe.vadali.com -> this server, before the first request.
./scripts/deploy-hetzner.sh ephe
curl https://ephe.vadali.com/health
```

The script clones [jyotish-ephe-server](https://github.com/VAAR-NETWORKS-INC/jyotish-ephe-server) next to `ualbums-server-infra`, builds `jyotish-ephe`, and reloads Caddy. AI and the admin dashboard are a separate service (`jyotish-api`); they are not on this host.

A dedicated machine that is not already serving 80/443 can still use `docker compose -f docker-compose.standalone.yml up -d --build` with `DOMAIN` set in `.env`.

**Optional, recommended: Cloudflare in front.** Proxy the DNS record (orange cloud) and set SSL to *Full (strict)*. Chart responses carry `Cache-Control: immutable`, so Cloudflare's edge cache absorbs repeat requests for free and adds DDoS protection and rate limiting.

**Sizing:** a CX22 (2 vCPU, 4 GB) handles roughly 100+ chart requests/s; the app only calls the server when a chart is generated or a day is opened.

## Endpoints

All angles are sidereal degrees; all instants are UTC ISO-8601. If `API_KEY` is set, send it as the `x-api-key` header.

### `GET /v1/chart`
Birth chart: planets (with speed, retrograde flag, sign, nakshatra, pada, whole-sign house), Lagna, MC and house cusps.

```
/v1/chart?datetime=1947-08-14T18:30:00Z&lat=28.6139&lon=77.209&ayanamsa=lahiri&node=mean&houses=W
```
- `ayanamsa`: `lahiri` (default) · `raman` · `kp` · `yukteshwar` · `true_chitra` · `true_pushya` · `fagan_bradley`
- `node`: `mean` (default) or `true`
- `houses`: Swiss Ephemeris house-system letter for `cusps` (`W` whole sign, `P` Placidus, `E` equal…). The `house` field on each planet is always whole-sign from the Lagna.

### `GET /v1/panchang`
One local day for a place: sunrise, sunset, next sunrise, moonrise and moonset, plus every tithi, nakshatra, yoga and karana that touches the Vedic day (sunrise to next sunrise), each with start and end times.

```
/v1/panchang?date=2026-11-08&lat=38.678&lon=-121.176&tz=America/Los_Angeles&sunrise=standard
```
- `sunrise`: `standard` (upper limb with refraction — what Drik Panchang and almanacs print) or `hindu` (disc centre, no refraction)

### `GET /v1/transits?datetime=…`
Current or any-moment planetary positions (no location needed). Omit `datetime` for "now".

### `POST /v1/positions`
Batch longitudes for scanning (festival / muhurtham search), up to 2,000 instants per call.
```json
{ "datetimes": ["2026-11-08T12:00:00Z", "2026-11-08T13:00:00Z"], "bodies": ["Sun", "Moon"], "ayanamsa": "lahiri" }
```

### `GET /health`, `GET /v1/ayanamsas`

## Using it from the app

```js
const EPHE = 'https://ephe.yourdomain.com';

async function remoteChart(dateUTC, lat, lon, ayanamsa = 'lahiri') {
  const url = `${EPHE}/v1/chart?datetime=${dateUTC.toISOString()}&lat=${lat}&lon=${lon}&ayanamsa=${ayanamsa}`;
  const r = await fetch(url);
  if (!r.ok) throw new Error('ephemeris ' + r.status);
  return r.json();   // { lagna: {lon, sign, ...}, planets: { Sun: {lon, speed, retro, sign, nakshatra, pada, house}, ... } }
}
```
Recommended pattern: use the server when online, and fall back to the built-in JS engine offline. Every downstream feature (D9/D10, Shadbala, Ashtakavarga, dasha, matching…) only needs the longitudes, so it can take either source.

The claude.ai prototype can't call external servers from its sandbox; this is for the production web and mobile app.

## Tests

```bash
pip install -r requirements.txt httpx
python tests/smoke_test.py
```
Checks India's 1947 chart (Lagna Vrishabha 7°44′, Moon in Pushya pada 1, Lahiri 23°07.5′), a Hyderabad panchang, KP and true node, batch positions, and error handling.

## Licensing — read before going public

Swiss Ephemeris (and pyswisseph) is dual-licensed by Astrodienst: **AGPL-3.0** or a paid **Swiss Ephemeris Professional License**. Running it as a public network service under AGPL means you must offer this server's source code to its users. This repo is the whole of that service: charts, panchang, transits, positions, and eclipses. The web app, the AI proxy, and analytics talk to it over HTTP and are separate programs. If you'd rather not publish this repo, buy the professional license from astro.com. I'm not a lawyer, so confirm before launch.

The `.se1` data files come from the official repository (github.com/aloistr/swisseph) under the same terms.
