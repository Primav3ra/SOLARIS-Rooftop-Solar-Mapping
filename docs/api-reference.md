# API reference

Written for: someone integrating with the HTTP API, or debugging a response.

Interactive docs are served at `/docs` on any running instance. This page covers
the parts a schema cannot express.

## Endpoints

| Method | Path | Earth Engine calls | Cached | Notes |
|---|---|---:|:-:|---|
| GET | `/api/health` | 0 | — | Liveness. Touches nothing. |
| GET | `/api/ready` | 1 | — | Readiness: establishes an Earth Engine session. Used by the post-deploy smoke test. |
| GET | `/api/version` | 0 | — | Build and algorithm identity. |
| GET | `/api/config` | 0 | — | Non-secret runtime configuration. Secrets reported present/absent, never by value. |
| GET | `/api/presets` | 1 | ✓ | Available windows, defaults, and every physical constant. |
| POST | `/api/auth/guest` | 0 | — | Mint a session token. |
| GET | `/api/auth/me` | 0 | — | Remaining allowance for this session. |
| POST | `/api/baseline` | ~6 | — | Roof area plus an irradiance summary. |
| POST | `/api/yield` | 12 | ✓ | The main computation. |
| POST | `/api/series` | ~8 | — | The whole generation curve in one request. |
| POST | `/api/tiles` | ~4 | ✓ (6 h) | Map tile templates for raster overlays. |
| POST | `/api/buildings` | ~2 | — | Open Buildings polygons as GeoJSON. |

`/api/presets` fetches its constants from the server so the frontend never ships
its own copies. An earlier version duplicated the grid emission factor and the
PV constants in two JavaScript files with a "keep in sync" comment and no
enforcement.

## Selecting an area

Either an explicit polygon or a centre and half-size:

```json
{ "lat": 28.6139, "lon": 77.2090, "half_size_deg": 0.01 }
```

```json
{ "coordinates": [[77.20, 28.60], [77.22, 28.60], [77.22, 28.62], [77.20, 28.62]] }
```

**Every field is bounded**, and the bounds are the primary quota defence rather
than a politeness. They run before any Earth Engine object is constructed, so a
rejected request costs nothing — and no request-rate limit helps against a
single well-formed request for a region the size of a continent.

| Field | Bound | Why |
|---|---|---|
| `half_size_deg` | ≤ 0.025 | ~2.8 km half-side, ~30 km² at Delhi latitude |
| area of interest | ≤ 30 km² | Keeps the reduction inside the 4 m scale tier |
| vertices | ≤ 100 | |
| `limit` (buildings) | ≤ 2000 | |
| `roof_year` | 2016–2023 | Open Buildings 2.5D Temporal v1 vintages |
| `cleaning_interval_days` | 1–365, or null | Null means rain-only cleaning, the honest default for an unmaintained roof |

## Selecting a window

Four modes. All resolve to a concrete `[start_date, end_date_exclusive)` pair
before anything else happens, which is also what makes the cache work.

```json
{ "baseline_mode": "yearly",    "year": 2023 }
{ "baseline_mode": "quarterly", "year": 2026, "quarter": 2 }
{ "baseline_mode": "monthly",   "year": 2024, "month": 7 }
{ "baseline_mode": "daily",     "start_date": "2024-03-15" }
```

**The upper bound is data-derived, not calendar-derived.** It comes from probing
ERA5-Land's actual last published image, with a 92-day conservative margin. An
earlier version used `today.year - 1`, which refused a quarter that had ended
two and a half months earlier.

Windows shorter than 28 days get **no annualised figure at all**, rather than a
December day multiplied by 365.

## Response envelope

Every computation carries a `data_quality` block:

```json
{
  "data_quality": {
    "severity": "ok",
    "fully_computed": true,
    "findings": [],
    "coverage": { "status": "complete", "days_with_data": 365, "days_requested": 365 }
  }
}
```

`severity` is one of `ok`, `degraded`, `unreliable`. `findings` names every
input that came from a fallback rather than a retrieval, each with an
explanation of what it means for the number.

This block is the structural fix for the project's recurring failure mode: a
plausible value distinguishable from a real result only by an obscure `*_source`
string. Writing it immediately surfaced a live example — two shade buckets with
no sun above the horizon were reporting `0.0` shade, which reads as "fully
sunlit".

Partial coverage is **reported**, not silently returned as a small number. A
window past the reanalysis publication lag comes back visibly incomplete.

## Timezones

Shade-matrix buckets carry both labels. The underlying data is UTC, which for
India is misleading on its own: the `08-12 UTC` bucket is really
**13:30–17:30 IST**.

## Caching

Result TTL 30 days, tiles 6 hours (a tile response contains an Earth Engine map
token, which expires).

Response headers:

| Header | Meaning |
|---|---|
| `X-Cache` | `HIT` or `MISS` |
| `X-Cache-Key` | The versioned key |
| `X-Guest-Remaining` | Computations left this session |
| `X-Request-ID` | Quote this when reporting an error |

Never cached: non-2xx responses, and any window whose end date is today or
later — that window is still being published, so caching it would pin a partial
answer for the whole TTL.

The key resolves the temporal mode first and quantises coordinates to 4 decimal
places (~11 m). Without the quantisation the hit rate would be approximately
zero, because areas of interest arrive as raw map-click floats. It is prefixed
with `ALGO_VERSION` and `DATASET_VERSION`, so a deploy that changes the physics
invalidates stale entries automatically.

## Sessions

There is no authentication. Each browser session obtains an opaque token from
`POST /api/auth/guest` and returns it in `X-Solaris-Guest`. The token is not a
credential: it grants nothing beyond a per-session computation allowance, and
exists so that allowance is per session rather than per IP address — which
behind carrier-grade NAT would mean one allowance shared across thousands of
subscribers.

The allowance defaults to 10 computations. **Cached results do not decrement
it**, so revisiting a previously computed site is free.

Google sign-in was implemented and removed. It verified an ID token and keyed
rate limiting on the `sub` claim. It moved no Earth Engine quota — the OAuth
client belongs to this project, so `ee.Initialize(project=ours)` bills here
whether or not a caller is authenticated — there was no persistence for an
identity to attach to, and no interface element from which to sign in, so the
exhausted-allowance response recommended an action the application could not
perform. The reasoning is recorded in `src/solaris/api/auth.py`.

Shifting Earth Engine cost onto a visitor would require them to supply their own
Earth Engine-registered Cloud project, and the `earthengine` OAuth scope needed
for that is classed sensitive: a public application requesting it needs Google
verification review and is capped at roughly 100 users until that passes.

## Errors

One shape for every failure:

```json
{
  "error": { "code": "rate_limited", "message": "..." },
  "request_id": "a1b2c3d4e5f6"
}
```

| Status | Code | Meaning |
|---|---|---|
| 400 | `bad_request` | Unresolvable window or malformed area |
| 403 | `allowance_exhausted` | Session allowance used up; carries `used` and `allowance` |
| 422 | — | Pydantic validation; names the offending field |
| 429 | `rate_limited` | Per-identity limit; carries `Retry-After` |
| 500 | `internal_error` | Detail is logged, never returned |
| 503 | `budget_exhausted` | Daily Earth Engine budget gone; carries `Retry-After` |
| 503 | `busy` | No Earth Engine slot available |

**500-level detail is sanitised.** Raw Earth Engine exception text carries
project ids and asset paths; the deployed instance previously answered a bad
request with `"Caller does not have required permission to use project
pv-mapping-india"`. Note that `HTTPException` is handled by FastAPI *before* any
middleware, so this needed an explicit exception handler rather than middleware
alone. 4xx validation messages are kept, since they tell the caller what to fix.
