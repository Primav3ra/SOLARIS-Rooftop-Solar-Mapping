# SOLARIS — Knowledge Transfer

Rooftop solar potential estimation for urban India, computed on demand from open
satellite data through Google Earth Engine.

This is the single reference document for the project: what it does, how it
works, what was measured, what is known to be wrong, and how to run and deploy
it. It replaces the nine files that previously lived under `docs/`.

---

## 1. Scope

### 1.1 What the system does

Given a point on a map in urban India and a time window, SOLARIS estimates how
much electricity the rooftops in that area could generate. There is no pre-built
database of buildings or irradiance — every reduction runs server-side in Earth
Engine at request time, and only scalars come back.

### 1.2 What the output is, and is not

**It is** a screening estimate of technical rooftop solar potential: useful for
comparing neighbourhoods, sizing a city-level programme, or quantifying what
shadow and aerosol cost a given area.

**It is not** an engineering estimate for a specific installation. That requires
a site survey, roof-plane geometry, structural assessment, local obstruction
mapping, and preferably a year of on-site measurement. Nothing here substitutes
for any of those.

### 1.3 Audiences

Four groups, in descending order of expected volume. This section exists because
the site's copy must serve them, not explain itself.

**Prospective rooftop adopter.** Arrives with a vendor quote and no way to test
it. Vendor estimates are typically roof area × a regional irradiance figure with
losses collapsed into one performance ratio; they do not distinguish an
overshadowed roof from an unobstructed one, and do not vary with local dust.
Needs an independent estimate with an explicit range, and identification of
*which* loss term dominates — that is the actionable output, because soiling
implies a cleaning schedule and sky obstruction does not.

**Installer or EPC estimator.** Needs to triage leads before committing to a
site visit. Pre-visit screening normally relies on satellite imagery inspected
by eye, which resolves footprint but not height, and therefore not
inter-building shading. Needs specific yield in commercial units (kWh/kWp/yr),
the loss decomposition, and an unambiguous list of what the model does not
cover. Comparability between sites matters more than absolute accuracy.

**Municipal or programme analyst.** Sizing potential across a ward or scheme.
The common alternative — built-up area × a capacity density — discards all
spatial variation and cannot be defended in review. Needs method provenance with
citations, the reference data and its disagreement, and spatial variation
attributable to a cause.

**Technical reviewer.** An examiner or engineer assessing the method. Needs the
validation design, the holdout construction, the gate declared before model
selection, and the failure history. This group reads the limitations first.

### 1.4 What all four need

1. **Uncertainty attached to the number, not filed separately.** The two free
   reference datasets for India differ by 10.6%; no output can be presented as
   tighter than that.
2. **Attribution of loss to cause.** A single net figure is not actionable. The
   decomposition is the product.
3. **An explicit scope boundary** — screening, not engineering — stated wherever
   a figure appears, not only on one page.
4. **Provenance.** Every coefficient traceable to a citation; every reported
   figure traceable to a committed artifact.

---

## 2. Architecture

### 2.1 Runtime topology

What actually runs, and where. Everything inside the dashed box is one container
image; there is no second service, no database and no worker.

```
   ┌──────────────────────────────────────────────────────────────────────────┐
   │  BROWSER                                                                 │
   │                                                                          │
   │   React SPA (Vite build, 8 routes, route-level code splitting)            │
   │   ┌────────────┬────────────┬────────────┬────────────┬────────────┐      │
   │   │  /         │  /explore  │  /data     │  /method   │ /validation│      │
   │   │  Landing   │  MapLibre  │  scale     │  sliders   │  charts    │      │
   │   │  + three.js│  + Chart.js│  stepper   │  (client)  │  (static)  │      │
   │   └─────┬──────┴──────┬─────┴─────┬──────┴─────┬──────┴─────┬──────┘      │
   │         │             │           │            │            │             │
   │    lazy chunk    the ONLY    build-time JSON imported from evals/reports/  │
   │    (WebGL)       route that  ── no API call, no Earth Engine quota ──      │
   │                  calls /api                                                │
   └─────────────────────────┬────────────────────────────────────────────────┘
                             │  HTTPS, JSON, same origin
                             │  X-Solaris-Guest: <opaque session token>
   ┌─────────────────────────▼────────────────────────────────────────────────┐
   │ CLOUD RUN  ·  asia-south1  ·  min=0  max=2  concurrency=8  cpu-boost     │
   │ ┌──────────────────────────────────────────────────────────────────────┐ │
   │ │ uvicorn → FastAPI                                                     │ │
   │ │                                                                       │ │
   │ │   /                  SpaStaticFiles  (404 → index.html for deep links)│ │
   │ │   /api/*             routes                                            │ │
   │ │   /docs              OpenAPI                                           │ │
   │ │                                                                       │ │
   │ │   in-process state, per instance:                                      │ │
   │ │     TTLCache (fast tier)      Semaphore(max_concurrent)                │ │
   │ │     SlidingWindowLimiter ×2   EarthEngineBudget (local ceiling)        │ │
   │ └───────┬───────────────────────────────┬───────────────┬───────────────┘ │
   └─────────┼───────────────────────────────┼───────────────┼─────────────────┘
             │                               │               │
             │ ADC via                       │ Firestore     │ Cloud Logging
             │ google.auth.default()         │ native API    │ (severity, trace)
             ▼                               ▼               ▼
   ┌───────────────────┐         ┌──────────────────────┐  ┌──────────────────┐
   │ GOOGLE EARTH      │         │ FIRESTORE            │  │ CLOUD LOGGING    │
   │ ENGINE            │         │                      │  │                  │
   │                   │         │ solaris-cache/       │  │ one JSON line    │
   │ all raster work.  │         │   ├─ result docs     │  │ per request:     │
   │ Only scalars and  │         │   │   (TTL policy on │  │  status          │
   │ tile tokens are   │         │   │    expires_at)   │  │  duration_ms     │
   │ ever returned.    │         │   └─ budget/<date>   │  │  cache HIT/MISS  │
   │                   │         │       (Increment,    │  │  ee_calls        │
   │ Open Buildings    │         │        cross-instance│  │  ee_cost         │
   │ ERA5-Land / ERA5  │         │        counter)      │  │  request_id      │
   │ MODIS  ·  SRTM    │         │                      │  │                  │
   └───────────────────┘         └──────────────────────┘  └──────────────────┘
```

Two properties of this shape are load-bearing:

**Seven of the eight routes are static.** Only `/explore` calls the API, so a
visitor can read the entire validation and ML story for zero Earth Engine quota.
The pages that display results import `evals/reports/*.json` at *build* time,
which is what makes them unable to drift from the committed artifacts.

**`max-instances=2` is a correctness argument, not just a cost one.** The rate
limiter and the fast cache tier are per-instance. With at most two instances the
effective rate limit is at most 2× the configured one, which is ample precision;
it is also a hard ceiling on how much damage a runaway loop can do.

### 2.2 The request pipeline

Middleware wraps outermost-last, so `add_middleware` order reverses. The actual
nesting, and where a request can exit early:

```
  HTTP request
      │
  ┌───▼──────────────────────────────────────────────────────────────────────┐
  │ ObservabilityMiddleware                       (outermost)                │
  │   • assign request_id  (honours inbound X-Request-ID)                    │
  │   • reset ee_calls / ee_cost contextvars to 0                            │
  │   • resolve identity: X-Solaris-Guest token, else client IP              │
  │   • check_rate_limit(identity)  ──────────────────────► 429 + Retry-After │
  │  ┌──▼───────────────────────────────────────────────────────────────────┐ │
  │  │ SecurityHeadersMiddleware                                            │ │
  │  │   nosniff · DENY · referrer-policy · HSTS                            │ │
  │  │  ┌──▼────────────────────────────────────────────────────────────────┐│ │
  │  │  │ CORSMiddleware   allow_credentials=False, origins from settings   ││ │
  │  │  │                  (a wildcard is rejected at startup, not warned)  ││ │
  │  │  │  ┌──▼─────────────────────────────────────────────────────────────┐│ │
  │  │  │  │ route handler                                                  ││ │
  │  │  │  │                                                                ││ │
  │  │  │  │  1. pydantic validation  ────────────────────────► 422         ││ │
  │  │  │  │       every field bounded. runs before any ee object exists,   ││ │
  │  │  │  │       so a rejected request costs nothing.                     ││ │
  │  │  │  │                                                                ││ │
  │  │  │  │  2. resolve_temporal_window()  ──────────────────► 400         ││ │
  │  │  │  │       mode + year/quarter/month  →  [start, end)               ││ │
  │  │  │  │                                                                ││ │
  │  │  │  │  3. cache_lookup(resolved window)                              ││ │
  │  │  │  │       key = ALGO_VERSION : DATASET_VERSION : endpoint          ││ │
  │  │  │  │             : lat,lon,half_size rounded to 4dp                 ││ │
  │  │  │  │             : start,end : params                               ││ │
  │  │  │  │         ┌── memory (TTLCache) ──hit──┐                         ││ │
  │  │  │  │         └── Firestore ──hit──────────┤                         ││ │
  │  │  │  │              (promoted into memory)  │                         ││ │
  │  │  │  │                                      ▼                         ││ │
  │  │  │  │                            200  X-Cache: HIT                   ││ │
  │  │  │  │                            allowance NOT charged               ││ │
  │  │  │  │                                │                               ││ │
  │  │  │  │                             miss▼                              ││ │
  │  │  │  │  4. charge_computation(identity) ────────────────► 403         ││ │
  │  │  │  │       guest allowance, 10/session. only on a miss.             ││ │
  │  │  │  │                                                                ││ │
  │  │  │  │  5. ee_gate(n_calls, cost=ee_cost_units(mode, endpoint))       ││ │
  │  │  │  │       a. budget.consume(cost)  ──────────────────► 503 budget  ││ │
  │  │  │  │            Firestore Increment, falling back to the local      ││ │
  │  │  │  │            per-instance ceiling if unreachable                 ││ │
  │  │  │  │       b. semaphore.acquire(timeout=30s) ────────► 503 busy     ││ │
  │  │  │  │       budget first, deliberately: rejecting on an exhausted    ││ │
  │  │  │  │       budget must not consume a concurrency slot.              ││ │
  │  │  │  │                                                                ││ │
  │  │  │  │  6. Earth Engine work  ── 12 round-trips (see §2.4)            ││ │
  │  │  │  │                                                                ││ │
  │  │  │  │  7. assemble response + data_quality block                     ││ │
  │  │  │  │                                                                ││ │
  │  │  │  │  8. cache_store()  — only if coverage complete AND the window  ││ │
  │  │  │  │       ended before today. never caches a still-filling window. ││ │
  │  │  │  │                                                                ││ │
  │  │  │  │  finally: ee_stack.close() releases the slot on every path     ││ │
  │  │  │  └────────────────────────────────────────────────────────────────┘│ │
  │  │  └──────────────────────────────────────────────────────────────────┘│ │
  │  └────────────────────────────────────────────────────────────────────┘  │
  │   log one line: status, duration_ms, cache, ee_calls, ee_cost, identity   │
  └──────────────────────────────────────────────────────────────────────────┘
      │
  HTTP response  + X-Cache, X-Cache-Key, X-Guest-Remaining, X-Request-ID

  ┌──────────────────────────────────────────────────────────────────────────┐
  │ @app.exception_handler(HTTPException)                                    │
  │   Sits OUTSIDE the middleware chain — FastAPI handles HTTPException       │
  │   before any middleware sees it, which is why sanitising 500 detail       │
  │   needed its own handler. Raw Earth Engine text carries project ids.      │
  └──────────────────────────────────────────────────────────────────────────┘
```

### 2.3 Module dependency graph

The arrows only ever point downward. This is the layering rule, and it is what
makes the offline test suite possible.

```
                        ┌───────────────────────────┐
                        │        api/               │   composes everything
                        │  app · deps · middleware  │   the only layer that
                        │  schemas · windows · auth │   knows about HTTP
                        └───┬───────────┬───────┬───┘
                            │           │       │
              ┌─────────────▼──┐   ┌────▼─────┐ │
              │     gee/       │   │ physics/ │ │
              │  the ONLY      │   │  pv      │ │
              │  layer that    │   │  losses  │ │
              │  imports ee    │   │  soiling │ │
              │                │   └────┬─────┘ │
              │ auth  datasets │        │       │
              │ coverage layers│        │       │   imports pvlib, numpy,
              │ rooftops       │        │       │   pandas — but never ee
              │ irradiance     │        │       │
              │ precipitation  │        │       │
              │ penalties      │        │       │
              │ solar_geometry │        │       │
              │  └─ pure maths,│        │       │
              │     no ee      │        │       │
              └────────┬───────┘        │       │
                       │                │       │
                       └────────┬───────┴───────┘
                                │
                        ┌───────▼───────────────────┐
                        │        core/              │   leaf. imports nothing
                        │  constants ← single source │   from solaris at all
                        │  config · cache · limits  │   (constants.py imports
                        │  firestore_cache · quality│    no solaris module)
                        └───────────────────────────┘

        evals/  ──►  gee/, physics/, ml/, core/      (dev-time only, has CLIs)
        ml/     ──►  core/                           (lazy sklearn/joblib)

        ╳ FORBIDDEN:  core/ ──► anything      physics/ ──► gee/
                      core/ or physics/ ──► ee
```

`windows.py` sits in `api/` but imports no `ee`; it was extracted from the old
monolith precisely because two hundred lines of temporal branching had been
untestable while sitting next to `ee.Geometry` calls.

`tests/fakes/install.py` discovers every module that imports `ee` by walking the
package, and patches each module's *bound* `ee` attribute. Patching
`sys.modules["ee"]` does not work: modules bind `import ee` at import time.

### 2.4 The twelve Earth Engine round-trips

Measured, not estimated — `solaris.evals.profile_yield` counts `getInfo()` calls
against the numpy fake, and the count is exact.

```
  /api/yield                                                    round-trips
  ─────────────────────────────────────────────────────────────  ───────────
  centroid lon/lat                                                    1
  ERA5-Land GHI sum over [start, end)        DAILY_AGGR, ~9 km        1
  ERA5 beam/diffuse split                    ERA5 HOURLY, ~28 km      1
  roof mask + height raster                  Open Buildings, 4 m      1
  target building selection                  Open Buildings v3        1
  rooftop area reduction                                              1   ← 629 ms
  shadow frequency reduction                 ← shadow-attributable    1
  sky-view factor reduction                  ← shadow-attributable    1   ← 3169 ms
  shade matrix (hour buckets)                ← shadow-attributable    1
  MODIS LST anomaly (heat island)            MOD11A2, 1 km            1
  MAIAC AOD + daily precipitation            MCD19A2 + ERA5-Land      1
  energy stack reduction                                              1
  ─────────────────────────────────────────────────────────────  ───────────
                                                            total    12
                                        shadow-attributable:  3  (25%)
```

The round-trip count is exact; the wall-clock figures come from the `--live`
profile and are indicative. In that run the sky-view reduction was the dearest
stage at 3,169 ms, and the shadow reduction did not complete at all — it raised
`User memory limit exceeded`, which is the failure the energy-stack change below
fixed. The live profile has not been re-run since, so its shadow timing is
absent rather than fast.

A measured `445.8 ms` of any single call is **fixed round-trip overhead** — that
is what a trivial `getInfo()` costs. It is why round-trip count, not server-side
compute, is the quantity worth optimising, and it is the measurement that killed
the planned shadow surrogate: a perfect surrogate still makes 12 calls.

The energy stack was cut from five heavy bands to three because five exceeded
Earth Engine's user memory limit. The heat and soiling stages are derived in
Python instead, which is exact because scalar derates distribute over the sum;
`tileScale=4` was added to the remaining reduction. Golden responses did not
change.

### 2.5 Physics data flow

Which dataset feeds which stage, and at what native resolution. The resolution
column is the point: the geometry is building-scale and the meteorology is not.

```
  DATASET                        RES      STAGE                        OUTPUT
  ─────────────────────────────  ──────   ──────────────────────────   ─────────
  Open Buildings 2.5D  ──┬────►  4 m      roof mask, heights           m², H(x,y)
                         │
                         ├────►  4 m      directional shadow trace ───┐
                         │                 H(p+d·û) − H(p) > d·tan α   │
                         │                                            │
                         └────►  4 m      sky-view factor ────────┐    │
                                           horizon at 4..64 m     │    │
  SRTM GL1             ──────►  30 m      slope > 30° exclusion    │    │
                                           (terrain, not pitch)    │    │
                                                                   │    │
  ERA5-Land DAILY_AGGR ──────►  ~9 km     GHI sum over window ──┐  │    │
  ERA5 HOURLY          ──────►  ~28 km    beam fraction ────────┤  │    │
                                                               │  │    │
                                            ┌──────────────────▼──▼────▼──┐
                                            │  net = GHI × [              │
                                            │    (1−beam) × SVF           │
                                            │  + beam × (1−shadow) ]      │
                                            └──────────────┬──────────────┘
                                                           │
  MODIS MOD11A2        ──────►  1 km      LST anomaly vs   │
                                          30 km background │
                                           × 0.3 surface→air
                                           × γ = −0.004/°C │
                                                     ──────┤ × uhi_derate
                                                           │
  MODIS MCD19A2 (MAIAC)──────►  1 km      AOD (QA-masked)  │
                                             │             │
  ERA5-Land precip     ──────►  ~9 km      daily rainfall  │
                                             │             │
                                    ┌────────▼──────────┐  │
                                    │ Kimber soiling:   │  │
                                    │ dry-spell         │  │
                                    │ clustering,       │  │
                                    │ reset on >1 mm    │  │
                                    └────────┬──────────┘  │
                                                     ──────┤ × soiling_retention
                                                           │
                                            ┌──────────────▼──────────────┐
                                            │ × roof area × packing 0.70  │
                                            │ × efficiency × PR(decomposed)│
                                            │        = kWh                 │
                                            └─────────────────────────────┘
```

Every spatial gradient you can see on the map comes from the 4 m column. The
9 km and 28 km inputs are flat across any single area of interest.

### 2.6 Package layout

```
src/solaris/
  core/          no Earth Engine, no HTTP. Pure logic and configuration.
    config.py       typed Settings; the only place environment is read
    constants.py    every physical coefficient, with its citation
    cache.py        cache key design and the in-process tier
    firestore_cache.py  the persistent tier and the shared budget counter
    limits.py       concurrency semaphore, daily budget, rate limiters
    quality.py      the data_quality block: which inputs were fallbacks

  gee/           everything that touches Earth Engine
    auth.py         credential resolution
    datasets.py     collection ids, vintage filters
    coverage.py     what data actually exists, probed rather than assumed
    layers.py       roof mask and height raster
    rooftops.py     footprint area
    irradiance.py   ERA5 GHI and beam fraction
    precipitation.py  daily rainfall, for soiling
    penalties.py    shadow, sky-view, heat island, soiling
    solar_geometry.py  sun positions (no Earth Engine; pure maths)

  physics/       PV conversion. No Earth Engine.
    pv.py           pvlib transposition and cell temperature
    losses.py       the loss chain, decomposed
    soiling.py      Kimber soiling with rain washing

  ml/            models, and the honesty machinery around them
    features.py     feature assembly
    decomposition.py  the model ladder and its pre-declared gate
    splits.py       spatial + temporal holdout
    registry.py     loading, with a fallback chain that always reports itself
    train.py        the training entry point

  evals/         does the model produce the right numbers?
    references.py   reference data types and the city list
    fetch.py        the only outbound HTTP in the project
    harness.py      the validation run
    soiling_suite.py     soiling against published rates and pvlib
    profile_yield.py     where the round-trips go
    pvlib_suite.py       our physics against pvlib's

  api/
    app.py          routes
    deps.py         shared request machinery
    schemas.py      request models; every field bounded
    windows.py      temporal mode resolution (imports no ee — hence testable)
    middleware.py   identity, limits, logging, error shaping
    auth.py         guest sessions and the computation allowance
```

### 2.7 Three design decisions worth knowing

**Earth Engine is the compute engine, not a data source.** Rasters are never
downloaded. Every reduction is expressed server-side and only scalars return.
This is why a request is twelve small round-trips rather than one large
transfer, and why round-trip count is the thing to optimise.

**The cache key is versioned by algorithm.** `ALGO_VERSION` and
`DATASET_VERSION` prefix every key, so changing the physics invalidates stale
entries automatically. The alternative — remembering to flush a cache after a
model change — gets forgotten exactly once, and then silently serves pre-fix
numbers.

**Every fallback is reported.** Any input that came from a default rather than a
retrieval appears in `data_quality` with a severity and an explanation. The
project's recurring failure mode was a plausible number with no indication of
its origin; this is the structural fix.

---

## 3. Data sources

| Quantity | Source | Native resolution | Why this one |
|---|---|---|---|
| Roof footprint, height | Open Buildings 2.5D Temporal v1 | ~4 m | The only open building-height product covering India |
| Building polygons | Open Buildings v3 | vector | Footprint selection |
| Global horizontal irradiance | ERA5-Land daily aggregates | ~9 km | Longest consistent record; daily accumulation bands |
| Beam/diffuse split | ERA5 hourly | ~28 km | ERA5-Land does not publish a direct component |
| Land surface temperature | MODIS MOD11A2 | 1 km | 8-day composite, long record |
| Aerosol optical depth | MODIS MCD19A2 (MAIAC) | 1 km | MAIAC works over bright urban surfaces where dark-target retrieval fails |
| Daily rainfall | ERA5-Land daily aggregates | ~9 km | Needed for soiling; same grid as the irradiance |
| Terrain | SRTM GL1 | 30 m | Slope exclusion |

### 3.1 The central methodological tension

**9 km irradiance is attributed to 4 m rooftops.** Within a single ERA5-Land
cell every roof receives identical irradiance, and all spatial variation in the
output comes from the geometry layers — shadow, sky-view, heat island, aerosol —
not from the irradiance.

This is not a bug to be fixed; it is the resolution of the best open dataset
available for India. But it bounds what a per-building number means: the
geometry is building-specific, the meteorology is neighbourhood-scale at best.
The Data & Satellites page visualises the scale gap directly, stepping through
4 m → 1 km → 9 km → 28 km over the same area.

### 3.2 Irradiance source: daily aggregates, not hourly

`ECMWF/ERA5_LAND/DAILY_AGGR` with band `surface_solar_radiation_downwards_sum`.
The hourly collection gives an identical window total — the bands are
accumulations — and the daily collection reduces over 365 images instead of
8,760, measured at roughly 23× faster.

A caution for anyone re-measuring this: Earth Engine caches computation results
server-side, so a repeated query returns in milliseconds regardless of its real
cost. The comparison must use fresh points. The first attempt here read 428 ms
for the hourly path purely because it had already been run.

### 3.3 Licences and attribution

Two of these licences require attribution as a condition of use, so this is an
obligation rather than a courtesy. The site's About page carries the same list.

| Source | Licence | Attribution required |
|---|---|:-:|
| Open Buildings 2.5D Temporal v1, Open Buildings v3 | CC BY 4.0 | yes |
| ERA5-Land, ERA5 | Copernicus / ECMWF licence | yes |
| MODIS MOD11A2, MCD19A2 | NASA open data | no |
| SRTM GL1 | USGS public domain | no |
| NASA POWER (reference data) | NASA open data | no |
| Global Solar Atlas (reference data) | CC BY 4.0, © Solargis | yes |
| NASA Blue Marble and Black Marble (landing-page globe) | US Government work, public domain | no |

The project's own code is MIT.

---

## 4. The physics chain

```
ERA5-Land GHI over the window                      kWh/m²
  → split into beam and diffuse                    beam fraction
  → beam attenuated by shadow frequency            × (1 − shadow)
  → diffuse attenuated by sky-view factor          × SVF
  → heat-island temperature derate                 × (1 + γ·ΔT_air)
  → soiling retention                              × (1 − soiling loss)
  = net plane irradiance                           kWh/m²
  × roof area × packing factor                     m²
  × module efficiency × performance ratio          → kWh
```

Every coefficient appears exactly once in the codebase, in
`src/solaris/core/constants.py` or in the module that owns it. The frontend
fetches them from `/api/presets` rather than shipping copies.

### 4.1 Solar geometry

Computed analytically (no Earth Engine), validated against NREL SPA via pvlib.
Measured accuracy: 84.58° against a true 84.83° solstice peak at Delhi with
30-minute sampling.

Positions are insolation-weighted, so a low-sun hour contributes less than
midday. Weights sum to 1; a regression test asserts that, because an earlier
version thinned the position list with `pos[::2]` without renormalising, which
would have halved the shadow frequency. The branch never fired — maximum
position count is 39 — so it was dead code concealing a live bug.

Sampling per mode: yearly 31 positions, quarterly 25, monthly 39, daily 13.

### 4.2 Shadow

A **directional digital-surface-model trace**. A pixel is shadowed when some
point up-sun rises above the sun's elevation line:

```
shadowed  ⟺  ∃ d :  H(p + d·û_sun) − H(p)  >  d · tan(altitude)
```

Sampled at increasing distances out to 400 m, with the reach capped at
`max_building_height / tan(altitude)` so low-sun hours do not search further
than any building could cast.

**What this replaced.** The previous implementation flagged a pixel as shadowed
when a `focal_max` over a *circular* 100-pixel kernel exceeded the pixel height
*and* the height at one fixed offset was greater — two conditions referring to
different locations, with azimuth entering only through a single rigid
translate, so the search was effectively isotropic. The docstring self-assessed
at "5–10% high"; in dense urban fabric the real error was much larger.

Two units bugs were fixed in the same rewrite (D1 and D9 below). All distances
are now in metres, which removes that class of bug by construction, and a test
asserts shadow frequency is invariant across request scales.

**A footprint-versus-mask trap worth knowing.** An image's footprint is distinct
from its mask in Earth Engine, and repeated `translate` calls erode the
footprint. Summing shadow masks across sun positions therefore silently emptied
the result: 12,432 valid height pixels became 1,425 after one low-sun position
and **0** after the sum, so `/api/yield` returned 0 kWh with shadow reported as
100% of loss. The fix re-establishes the mask from the height band after the
reduction:

```python
occluded.unmask(0).updateMask(building_height.mask())
```

### 4.3 Sky-view factor

The diffuse counterpart, from the same height raster: the fraction of the sky
hemisphere visible from a rooftop pixel, estimated from horizon angles sampled
at 4, 8, 16, 32 and 64 m.

Diffuse radiation is roughly half of Indian GHI, so this term is not a
refinement. Fixing the units bug moved the sky-view penalty from approximately
zero to a material contribution — currently ~15% of modelled loss. Measured for
a 10 m wall 4 m away: the buggy version returned 0.964888 against an analytic
0.892241, exactly the value obtained by substituting 16 m for 4 m.

### 4.4 Heat island and temperature

MODIS land surface temperature against a 30 km background mean gives the urban
anomaly. Two corrections matter:

**Surface temperature is not air temperature.** Surface urban heat island runs
typically **2–3×** the canopy-layer air heat island in Indian cities. An
explicit transfer coefficient `SURFACE_TO_AIR_RATIO = 0.3` converts one to the
other, and the response reports `delta_t_air_celsius` rather than implying the
surface anomaly is what a module feels.

**The derate is the urban excess only.** `derate = 1 + γ·ΔT` with
γ = −0.004 /°C charges the anomaly, not the absolute temperature loss of a 35 °C
ambient against 25 °C STC. That absolute loss lives in the decomposed
performance ratio. Keeping them separate is what stops the double-count the
previous lumped `PR = 0.80` made possible.

The background window is defined in **metres** (`BACKGROUND_KERNEL_M = 30_000`),
not pixels, for the same reason as the shadow kernel: measured on a synthetic
hot spot, ΔT read 2.78 °C at `scale=4` against 4.94 °C at `scale=100` — a 78%
swing from the request scale alone.

### 4.5 Soiling

The published **Kimber** model — which the previous code cited while
hand-rolling something else.

Soiling accumulates at a daily rate while dry, resets on rain above 1 mm, and
saturates at 30%:

```
L(t) = min(rate · t, 0.30),   rate = AOD × 0.00486 per day
```

The rate is calibrated against measured Delhi soiling (0.34%/day in winter) at a
literature annual-mean AOD of 0.70, with the rate clamped at the worst observed
0.0047/day (reached at AOD ≈ 0.97).

Deviations from pvlib's defaults, each deliberate:

| Parameter | pvlib | Here | Why |
|---|---|---|---|
| Cleaning threshold | 6 mm | 1 mm | 6 mm is temperate-calibrated and would miss most pre-monsoon showers |
| Grace period | 14 days | 0 days | A fortnight of damp ground is a temperate assumption; it would erase all pre-monsoon soiling in a Delhi year |
| Loss rate | 0.0015/day fixed | derived from AOD | Preserves the spatial variation the project exists to show |

**What this replaced**, and why tuning could not have saved it: the old model was
`loss = mean_annual_AOD × 0.08`. Unbounded (retention went negative above
AOD 12.5, i.e. negative generated energy), with no rainfall term, and
window-independent — the same annual loss for a single day as for a year. It is
retained as `legacy_retention()` purely as the control arm for the ablation
table.

The rainfall term is not a refinement. Across the evaluation cities,
cleaning-rain days per year range from 59 (Jodhpur) to 195 (Guwahati) — a factor
of three in how often a roof gets washed — and the old model gave both the same
answer at equal aerosol.

**A documented approximation.** Without a daily rainfall series the model must
assume every dry spell is the mean length. Because accumulation is convex in
spell length (Jensen's inequality), that understates the loss by **1.8× to
12.6×** on real Indian rainfall — worst in the arid cities where soiling matters
most. The serving path therefore spends one extra Earth Engine round-trip on the
actual series, and results from the fallback path carry an explicit note calling
themselves a lower bound.

### 4.6 PV conversion

**Transposition.** `pvlib.irradiance.get_total_irradiance` with **Hay-Davies**,
not Perez. Perez needs a well-behaved sky-clearness pair and degrades when
DNI/DHI quality is poor, which is exactly ERA5-derived DNI over urban India.
Perez is reported as a sensitivity rather than used as the default.

**Cell temperature.** `pvlib.temperature.sapm_cell` — implementing the NOCT-style
model the old docstrings described but never implemented.

**Discretisation.** A monthly-diurnal climatology, 12 × 24 = 288 records.
Transposition and cell temperature are both non-linear in instantaneous
irradiance, so neither can be applied to an annual total; a full 8760-hour year
per request is not servable. The discretisation error against full hourly is
measured, not assumed.

**Mount is carried explicitly in every record.** Measured on the evaluation
set, latitude-optimal tilt gains a mean of **7.1%** of specific yield over flat,
ranging from +1.7% at Chennai (14.5° optimal tilt) to +14.1% at Leh (32.8°);
Delhi is +7.1%. Comparing a flat-roof figure against a reference computed at
optimal angles would look like a large model bias when it is purely a
configuration mismatch — a confound that could otherwise
have swallowed the entire validation. Both are reported: `mount="flat"` (what
this model actually assumes) and `mount="optimal_fixed"` (what an installer
would build), with `optimal_tilt = 0.87·|latitude| + 3.1`.

**The performance ratio is decomposed.** `PR = 0.80` was one scalar bundling
temperature, inverter, wiring, mismatch, soiling and availability — which made it
ambiguous against the separate soiling and heat-island layers and possibly
double-counted. It is now named terms, reported separately, with assumed and
computed components distinguished.

`AVAILABILITY_LOSS = 0.0`, deliberately: this estimates technical potential
across a city's roofs, not the output of one plant with one maintenance
schedule.

### 4.7 Documented approximations

- **Coordinate quantisation.** Cache keys round latitude, longitude and
  half-size to 4 decimal places (~11 m). Far below ERA5's 9 km cell and below
  the meaningful sensitivity of an area-of-interest *centre*, but it means two
  clicks 10 m apart return the identical result.
- **Terrain slope, not roof pitch.** The 30° exclusion derives from SRTM at 30 m,
  so it excludes buildings on steep *terrain*, not steep *roofs*. Open as P7.
- **Annualisation.** Windows shorter than 28 days get no annualised figure at
  all, rather than a December day multiplied by 365.
- **Soiling AOD anchor.** The deposition coefficient is one measured rate
  divided by one *assumed* Delhi annual-mean AOD. The rate is measured; the AOD
  is a literature value, not a retrieval from this pipeline.

---

## 5. Machine learning

Three tracks were planned. **Two shipped, one was measured and dropped.** The
dropped one is described at the same length as the others, because the decision
not to build it was the more useful piece of work.

### 5.1 The standard applied to all three

A model here has to beat the **published method**, not the naive one. Beating a
constant proves nothing; a published correlation validated worldwide for decades
is the bar worth clearing.

The gate is declared **before** training, in the module above the training code,
so it cannot be relaxed to suit the result. With a few thousand samples from ten
sites it would be easy to produce a gradient-boosting model with a flattering
score and no real advantage.

### 5.2 Track A — beam/diffuse decomposition

**The defect, measured.** Beam fraction is the model's most ERA5-sensitive
input. The validation harness put the reference mean at **0.540** across 30
city-years, while the production code fell back to a constant **0.60** and its
ERA5 path documented 0.55–0.72. ERA5 uses a monthly aerosol climatology and is
documented to overestimate direct radiation with the error growing in aerosol
load — exactly India's regime.

**A necessary change of scope.** The plan's Track A was an ERA5 → reference bias
correction. That needs ERA5 data, which needs Earth Engine credentials that were
unavailable. The *reference* side was buildable and targets the same defect from
the other end: learn the diffuse fraction from cheap, always-available features,
which both replaces the constant and produces the target series an ERA5
correction would later train against.

#### Features, and why each one

Every published correlation in this space — Erbs (1982), Reindl (1990),
Boland-Ridley-Lauret — is driven by the clearness index
`Kt = GHI / extraterrestrial horizontal`. That is the physically right primary
predictor: diffuse fraction runs near 1 under thick cloud and near 0.15 under
clear sky.

| Feature | Reason |
|---|---|
| `kt` | The primary predictor, as in every published correlation |
| `sin_elevation`, `air_mass` | Air mass changes diffuse fraction at a given Kt; low sun scatters more. Kasten-Young rather than `1/sin(elevation)`, which diverges at the horizon |
| `sin_doy`, `cos_doy` | Cyclical, not an integer. An integer lets a tree carve arbitrary date groupings; the pair also makes December adjacent to January, which it is |
| `abs_latitude` | Proxy for climate regime |
| `kt_persistence` | Kt relative to the day's mean. Broken cloud gives a high diffuse fraction at moderate Kt, which instantaneous Kt cannot distinguish from thin uniform haze |

**Deliberately excluded: aerosol optical depth.** It would very likely help —
aerosol is the mechanism behind India's high diffuse fraction — but it comes from
MODIS via Earth Engine, so requiring it would make the model unusable in exactly
the fallback case it exists to serve.

#### The ladder

| Rung | Test RMSE | MBE | Skill vs Erbs |
|---|---:|---:|---:|
| `constant` (the 0.60 that was in production) | 0.2678 | −0.167 | −0.94 |
| `climatology` (36 parameters, no ML) | 0.1983 | −0.022 | −0.43 |
| **`erbs`** (published, 1982) | 0.1383 | +0.070 | 0.00 |
| `ridge` | 0.0974 | −0.000 | +0.30 |
| **`gradient_boosting`** | **0.0790** | **+0.0001** | **+0.43** |

Five rungs, all reported, **simplest winner ships**. The gate —
`MIN_SKILL_OVER_ERBS = 0.10` — was declared before training. The
gradient-boosted model clears it at +0.43, and its bias is essentially zero.

18,624 training rows against 4,022 test rows, from 40,004 daytime hours.

Why gradient boosting and not a neural network: a few thousand tabular rows with
heterogeneous feature scales and a likely non-linear interaction between
clearness and air mass is what trees handle well, and a net on this much data
would be indefensible. Modest depth (4) and a leaf floor (40), because with ten
sites a deeper model memorises sites rather than learning physics.

#### The training set

Every third day of the year, hourly, daylight hours only: 122 days per
city-year, about 1,500 usable rows each, 40,004 in total.

It was originally the 288-step monthly-diurnal climatology — the same twelve
mid-month days the pvlib discretisation check uses. That is the right sampling
for checking a discretisation error and the wrong one for fitting this model.
The primary predictor is the clearness index, and twelve days a year carry
almost no weather: the fitted kt distribution was not the one the model would
meet in service. Widening it multiplied the sample by eighteen, moved skill
from +0.37 to +0.43, and took the bias from +0.015 to +0.0001.

The dense cache lives beside the climatology rather than replacing it
(`evals/references/nasa_power_train/`, 1.8 MB), because the two answer
different questions. Refresh with
`python -m solaris.evals.fetch --train --years 2020 2021 2022`.

#### The holdout

**Spatial *and* temporal.** Three held-out cities × a held-out year, with the
closest test/train site pair verified above 250 km. Hours within a city-day are
strongly correlated, so a random row split would test on hours whose neighbours
were trained on and report a score saying nothing about generalisation.

#### Two findings beyond the ranking

- The constant's MBE of **−0.139** quantifies the defect it replaces.
- **Erbs is itself biased for India**: +0.077, over-predicting diffuse fraction,
  consistent with being fitted on US aerosol conditions. The learned model cuts
  that to +0.015. This is a result about the published correlation, not just
  about our model.

#### Serving design

`model → Erbs → constant`, always reporting which rung answered. Erbs sits in
the middle deliberately: it needs no artifact, no training data and no dependency
beyond arithmetic, so it is always available — which makes the constant a genuine
last resort rather than the first fallback it used to be.

**A domain check guards the learned rung.** A gradient-boosted tree does not
extrapolate; it returns the nearest leaf, confidently and with no basis. A
per-feature range check against the training envelope recorded at fit time sends
out-of-domain inputs to Erbs instead.

Two defects in that guard had to be fixed before the model could serve anything,
and both passed every offline test while making the model useless in production:

**The gate excluded Bengaluru.** The envelope is fitted on the training split,
and the holdout is *spatial by design*, so its latitude range can never cover
the cities the model exists to generalise to. The fitted floor was 13.0827°N —
exactly Chennai's latitude — so Bengaluru at 12.9716°N fell outside it, and so
did Coimbatore, Kochi, Madurai and Thiruvananthapuram. A model that refuses
peninsular India is not falling back safely, it is absent. `abs_latitude` is now
excluded from the gate by name; the physical predictors stay gated, because
that is where an out-of-range value means a genuinely unfamiliar atmosphere.

**The check was `all()` over the batch.** A request evaluates every sun position
for a window at once, and the lowest sits at the 2° altitude floor —
`sin_elevation` about 0.035 against a training floor of 0.122. One such position
sent all thirty-one to Erbs, so the model would never have run. Routing is now
per sample: the model where it is competent, Erbs where it is not, with the
fallback share reported in the detail string.

Artifacts are committed under `ml/artifacts/` with a manifest recording what
shipped and on what evidence, so "which model is live, and how good was it?" is
answerable from the repository without loading a pickle.

#### Serving: applying an hourly model to a window

The model is fitted per hour. The API serves windows — a month, a quarter, a
year, summed to one GHI total — so there is no hour to hand it. Two obvious
bridges are both wrong:

*Feed it the period aggregate as though it were an hour.* Diffuse fraction is
strongly non-linear in the clearness index, so evaluating once at the mean is
not the mean of evaluating — the same convexity trap the soiling model
documents.

*Fetch hourly ERA5 for the window and run the model per hour.* Correct, and
unaffordable against a quota where one development day consumed 91% of a month.

`solaris/ml/serving.py` does neither. It evaluates the model at **the sun
positions the request has already computed** — 13 for a day, 39 for a month, 31
for a year — and weights the results by the same insolation weights the shadow
trace uses. One consistent sampling of the window drives both the beam fraction
and the shadow penalty, rather than two that disagree. The clearness index is
the one quantity geometry cannot supply, so it is derived once for the whole
window: measured GHI over extraterrestrial horizontal insolation, the latter
computed analytically per day and summed.

**What the approximation costs.** Sub-daily variation in clearness is lost:
every position in a window shares one kt, so a window of clear mornings and
cloudy afternoons is modelled as uniformly average. Curvature over the
*geometry* axis survives, because each position carries its own elevation and
air mass. The served provenance reads `model:...:period`, not `model`, for
exactly this reason.

**What wiring it changed.** The correction redistributes loss between the two
geometry terms and barely moves the total, which is what re-splitting
beam/diffuse should do:

| | before | after |
|---|---:|---:|
| beam fraction | 0.6200 | 0.5188 |
| diffuse fraction | 0.3800 | 0.4812 |
| shadow penalty | 3.65% | 3.05% |
| sky-view penalty | 1.66% | 2.10% |
| net yield | — | +0.2% |

Had the headline energy moved much, the wiring would be wrong.

The response reports both figures — `beam_fraction` and `era5_beam_fraction` —
so a corrected number is never indistinguishable from a measured one.

### 5.3 Track C — soiling, reframed from ML to parametric

There is no open Indian PV-soiling label set, so training a soiling model end to
end would be a curve fit dressed as machine learning. The honest version is the
published **Kimber** model — which the previous code already *cited* while
hand-rolling something else — calibrated against measured Indian rates. Full
method in §4.5; validation in §6.5.

The dropped PM2.5 step: estimating ground-level PM2.5 from MAIAC aerosol plus
meteorology is a well-established regression with abundant CPCB labels, and it
would feed `pvlib.soiling.hsu()`. It is the longest chain and weakest-evidenced
of the three tracks, and calibrating Kimber against published rates is already a
strict improvement on `0.08 × AOD`. Descoped, not attempted and abandoned.

### 5.4 Track B — shadow/sky-view surrogate: measured, then dropped

Specified as a **speed** optimisation, never an accuracy one, and explicitly
gated: measure where `/api/yield` latency goes first, and drop the track if
server-side shadow compute is not the bottleneck. Dropping it was named in
advance as a legitimate outcome.

**The measurement.** `/api/yield` makes **12** Earth Engine round-trips, of which
**3** involve shadow or sky-view compute. A perfect surrogate still makes 12,
because it changes what a reduction computes, not whether the reduction must be
fetched. So the round-trip reduction is **zero**, and the ceiling — even if
server-side compute were the whole of the latency — is a **1.33× speedup**. That
assumes the surrogate's own feature rasters are free, which they are not.

**The structural argument, which is the more interesting one.** The surrogate's
information content *is* the directional horizon profile. Given that profile,
"is this pixel shadowed" is the analytic identity
`horizon_angle(sun_azimuth) > sun_altitude`. A boosted tree approximating one
comparison is strictly worse than the comparison — slower to serve, less
accurate, and needing a training pipeline. So the track could only ever have been
justified on speed, which the arithmetic rules out.

Caching also addresses the same latency, and addresses it better: a cache hit
costs zero round-trips rather than a fraction of one stage's compute.

**What would reverse this:** a live profile showing a single shadow reduction
dominating wall-clock time — for instance a city-scale area making the
directional trace time out server-side. That is a different use case from the
single-building query this endpoint serves, and the input bounds cap the area at
30 km² specifically to stay out of that regime. Re-run
`python -m solaris.evals.profile_yield --live` against such an area before
reconsidering. Report committed at `evals/reports/track_b_profile.md`.

---

## 6. Validation

Generated reports live in `evals/reports/`. Regenerate with:

```bash
python -m solaris.evals.harness          # irradiance, beam fraction, yield
python -m solaris.evals.soiling_suite    # soiling vs published rates and pvlib
python -m solaris.evals.profile_yield    # where the round-trips go
python -m solaris.ml.train               # the model ladder
```

### 6.1 The constraint that governs everything here

**The two credible free references for India disagree by about 10%.** For Delhi,
NASA POWER gives 1736 kWh/m²/yr and Global Solar Atlas gives 1930 — a spread of
194, or 10.6%.

No accuracy claim in this project can honestly be tighter than that spread. The
harness therefore reports against a reference *ensemble* with an explicit band,
never against a single asserted truth, and the spread is printed on every run as
a standing constraint rather than a footnote.

This is the most important design decision in the validation, and it was forced
by the data rather than chosen.

### 6.2 Reference selection, including what did not work

**PVGIS-SARAH3 does not cover India.** The documentation advertises "Europe,
Africa and Asia", so it was the intended primary reference. Probing the live v5_3
API for Mumbai, Ahmedabad, Jaipur, Delhi, Bengaluru, Kolkata and Jodhpur
returned **HTTP 400 "Location out of the spatial coverage" for all seven**.
SARAH3 is the Meteosat prime-disk product, roughly ±65° longitude.

PVGIS-ERA5 *is* reachable, but it is ERA5 — the same data this model consumes —
so using it as truth would be circular.

That left:

- **NASA POWER** (satellite-derived, 30 city-years cached and committed). It also
  serves diffuse and direct components, which makes the beam fraction directly
  checkable rather than merely plausible.
- **Global Solar Atlas / Solargis** long-term averages, as a held-out third
  reference.
- **Published measured plant performance**, the only genuinely independent
  reference, used as a plausibility band.

### 6.3 Specific yield against published plants

Specific yield (kWh/kWp/yr) is the comparison that matters, for an algebraic
reason: packing factor and module efficiency cancel out of it. So it isolates the
irradiance and loss chain from the capacity assumptions.

- Published plausibility band: **1000–1750 kWh/kWp/yr**
- Model: mean **1302**, range **1103–1428**
- Inside the band: **30 / 30 city-years (100%)**

Against the single best-documented measurement — a 12 kWp Delhi rooftop plant at
**1147 kWh/kWp/yr** — the model gives 1256 for Delhi 2020, i.e. **+8%**.

Two things cut against reading that as an accuracy claim:

1. That plant reports a performance ratio of 0.85–0.93, above the typical Indian
   year-one range of 0.78–0.83. It is one well-maintained installation, not a
   population.
2. It is a *tilted* array; this model treats every roof as horizontal. Measured
   tilt gain at Delhi is **+7.1%** — the same size as the discrepancy, and in the
   same direction.

So +8% against a tilted, well-maintained plant is consistent with the model being
approximately right, and is **not** evidence that it is accurate to 8%. The
honest statement is the band: 30/30 inside 1000–1750.

### 6.4 Beam fraction

Reference mean over 30 city-years: **0.540**, range 0.441–0.634. The model's
ERA5 path documents 0.55–0.72 and its fallback was a constant **0.60**. So ERA5
reads high, exactly as the literature predicts, and the constant reads high too.
This measurement is what motivated Track A.

### 6.5 Soiling

Validated three ways. Full report in `evals/reports/soiling.md`.

**Against published seasonal measurements** (Delhi):

| Season | Predicted %/day | Measured %/day | Error |
|---|---:|---:|---:|
| spring | 0.389 | 0.390 | −0.4% |
| winter *(calibration anchor)* | 0.350 | 0.340 | +2.9% |
| monsoon | 0.243 | 0.240 | +1.2% |

Ordering spring > winter > monsoon: correct. That ordering matters more than the
magnitudes — a model fitting the annual total with the seasons reversed would
invert every cleaning-schedule recommendation, which is the main thing anyone
would use it for. Winter is the anchor, so only spring and monsoon are genuine
predictions.

**Against pvlib.** `pvlib.soiling.kimber` is an independent implementation of the
same published model. Worst absolute disagreement across all ten cities:
**0.003 fraction points** of loss.

Reaching that agreement found three real errors in the closed form, and the
sequence is instructive:

1. The cleaning day itself was counted as a dry day, inflating every spell by
   one. The relative disagreement was inversely proportional to spell length,
   which is the signature of a per-spell offset rather than a difference of
   physics.
2. Excluding those days from the spells also — wrongly — excluded them from the
   time average. A cleaning day carries almost no soiling, so it belongs in the
   denominator. This made the disagreement *worse*, from 0.2 to 2.5 fraction
   points, which is how it was caught.
3. The accumulation was being integrated continuously where the model is defined
   per day.

None of the three was visible in isolation. An independent implementation of the
same model is what surfaced them.

**Against the alternative it replaces.** The change is not uniformly downward,
and where it goes up is the more interesting result: for Jodhpur and Ahmedabad
the new model predicts *more* soiling than the old one despite below-average
aerosol, because a 125- and 135-day unbroken dry spell respectively is invisible
to an AOD-only model. So the previous model was not merely imprecise in arid
India — it was **optimistic** there, in the climate where soiling does most
damage, while being pessimistic in the wet cities where rain cleans for free.

Window dependence, Delhi: a **21.9× spread** between the cleanest and dirtiest
month, where the old model returned 5.60% for all of them.

### 6.6 Cross-validation against pvlib

The physics layer is checked against pvlib's own documented examples, and the
288-record monthly-diurnal discretisation is measured against full hourly rather
than assumed adequate. The "288-step" figure is computed from the 12 mid-month
reference days with each day scaled by its month's length, against an
independent full-year daily series. See `src/solaris/evals/pvlib_suite.py`.

### 6.7 Two errors the validation itself caught

Both would have propagated into every number.

**NASA POWER's hourly endpoint defaults to local solar time.** Reading
LST-stamped data as UTC shifts every timestamp by longitude/15 hours — 5.1 h for
Delhi — putting the solar position four hours out and making any transposition
meaningless. Found empirically rather than from the documentation: correlating
GHI against sin(solar altitude) computed in UTC gives **+0.994** with
`time-standard=UTC` passed explicitly, against **+0.141** under the default.

**NASA POWER's independently-retrieved GHI, DNI and DHI do not close.** The
components sum 5.26% below the reported GHI for Delhi, putting a uniform 5%
negative bias on every plane-of-array figure. This had already led to a *wrong
conclusion* — a test asserting that low-latitude tilt loses energy. After a
GHI-preserving rescale, every tilt gain exceeds 1.0, as it should.

**NASA POWER over high terrain is unreliable.** It reports 458 mm/yr for Leh
against an actual figure near 100 mm. Leh is retained in the evaluation set as a
low-aerosol contrast case, but its soiling figures should not be read as
meaningful.

### 6.8 What this validation does not establish

Stated explicitly, because a validation section that only lists successes is not
a validation section.

- **No ground-truth measurement.** No pyranometer, no metered plant in the loop.
  Every reference is satellite-derived or a published aggregate.
- **No per-building verification.** The 4 m geometry is never checked against a
  surveyed roof. Validation is at city scale.
- **Accuracy is bounded by 10%** — the reference disagreement — and cannot be
  claimed tighter regardless of how well the model matches any single source.
- **The soiling calibration rests on one assumed AOD value**, and the per-city
  soiling ranking inherits that uncertainty. The rainfall-driven results do not.
- **The ERA5 → reference bias correction proper was not built.** It needs Earth
  Engine credentials that were unavailable.
- **The decomposition model is trained on NASA POWER**, so it cannot be
  validated against NASA POWER. Independent validation would need Global Solar
  Atlas or ground stations as a held-out reference.

---

## 7. The HTTP API

Interactive docs are served at `/docs` on any running instance. This section
covers what a schema cannot express.

### 7.1 Endpoints

| Method | Path | EE round-trips | Budget charge | Cached | Notes |
|---|---|---:|---|:-:|---|
| GET | `/api/health` | 0 | — | — | Liveness. Touches nothing. |
| GET | `/api/ready` | 1 | exempt | — | Readiness: establishes an Earth Engine session, runs no reduction. |
| GET | `/api/version` | 0 | — | — | Build and algorithm identity. |
| GET | `/api/config` | 0 | — | — | Non-secret runtime config. Secrets reported present/absent, never by value. |
| GET | `/api/presets` | 1 | — | ✓ | Available windows, defaults, every physical constant. |
| POST | `/api/auth/guest` | 0 | — | — | Mint a session token. |
| GET | `/api/auth/me` | 0 | — | — | Remaining allowance for this session. |
| POST | `/api/baseline` | ~4 | 0.3 × window | — | Roof area plus an irradiance summary. |
| POST | `/api/yield` | 12 | 1.0 × window | ✓ | The main computation. |
| POST | `/api/series` | ~16 | 1.2 × window | — | The whole generation curve in one request. |
| POST | `/api/tiles` | ~5 | 0.15 × window, or 0.1 flat for `roof_mask` | ✓ (6 h) | Map tile templates for raster overlays. |
| POST | `/api/buildings` | 1 | 0.1 flat | — | Open Buildings polygons as GeoJSON. |

`/api/ready` is the one endpoint deliberately exempt from the budget: gating it
would mean an exhausted daily allowance answers the readiness probe with 503 and
Cloud Run takes the instance out of service, turning a spent budget into an
outage. A test asserts every *other* Earth Engine endpoint is charged.

### 7.2 Selecting an area

Either an explicit polygon or a centre and half-size:

```json
{ "lat": 28.6139, "lon": 77.2090, "half_size_deg": 0.01 }
```

```json
{ "coordinates": [[77.20, 28.60], [77.22, 28.60], [77.22, 28.62], [77.20, 28.62]] }
```

**Every field is bounded**, and the bounds are the primary quota defence rather
than a politeness. They run before any Earth Engine object is constructed, so a
rejected request costs nothing — and no request-rate limit helps against a single
well-formed request for a region the size of a continent.

| Field | Bound | Why |
|---|---|---|
| `half_size_deg` | ≤ 0.025 | ~2.8 km half-side, ~27 km² at Delhi latitude |
| area of interest | ≤ 30 km² | Keeps the reduction inside the 4 m scale tier |
| vertices | ≤ 100 | |
| `limit` (buildings) | ≤ 2000 | |
| `roof_year` | 2016–2023 | Open Buildings 2.5D Temporal v1 vintages |
| `cleaning_interval_days` | 1–365, or null | Null means rain-only cleaning, the honest default for an unmaintained roof |

All of these are defined once, in `core/constants.py`, and imported by
`api/schemas.py`. They were previously declared in both files, so the limits
*enforced* and the limits *advertised* by `/api/presets` were independent
definitions that happened to agree — and had already begun to drift in their
annotations.

### 7.3 Selecting a window

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
earlier version used `today.year - 1`, which refused a quarter that had ended two
and a half months earlier.

Windows shorter than 28 days get **no annualised figure at all**, rather than a
December day multiplied by 365.

### 7.4 Response envelope

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

`severity` is one of `ok`, `degraded`, `unreliable`. `findings` names every input
that came from a fallback rather than a retrieval, each with an explanation of
what it means for the number.

This block is the structural fix for the project's recurring failure mode: a
plausible value distinguishable from a real result only by an obscure `*_source`
string. Writing it immediately surfaced a live example — two shade buckets with
no sun above the horizon were reporting `0.0` shade, which reads as "fully
sunlit".

Partial coverage is **reported**, not silently returned as a small number.

### 7.5 Timezones

Shade-matrix buckets carry both labels. The underlying data is UTC, which for
India is misleading on its own: the `08-12 UTC` bucket is really
**13:30–17:30 IST**.

### 7.6 Caching

Result TTL 30 days, tiles 6 hours (a tile response contains an Earth Engine map
token, which expires).

| Header | Meaning |
|---|---|
| `X-Cache` | `HIT` or `MISS` |
| `X-Cache-Key` | The versioned key |
| `X-Guest-Remaining` | Computations left this session |
| `X-Request-ID` | Quote this when reporting an error |

Never cached: non-2xx responses, and any window whose end date is today or later
— that window is still being published, so caching it would pin a partial answer
for the whole TTL.

The key resolves the temporal mode first and quantises coordinates to 4 decimal
places (~11 m). It is prefixed with `ALGO_VERSION` and `DATASET_VERSION`, so a
deploy that changes the physics invalidates stale entries automatically.

Caching happens at the **service-function** layer, not in middleware: middleware
would have to re-parse the body, and would cache error responses by accident.

### 7.7 Sessions

There is no authentication. Each browser session obtains an opaque token from
`POST /api/auth/guest` and returns it in `X-Solaris-Guest`. The token is not a
credential: it grants nothing beyond a per-session computation allowance, and
exists so that allowance is per session rather than per IP address — which behind
carrier-grade NAT would mean one allowance shared across thousands of
subscribers.

The allowance defaults to 10 computations. **Cached results do not decrement
it**, so revisiting a previously computed site is free. Ten rather than three
because comparing several roofs is the primary task, so the allowance has to
exceed the number of sites a visitor will examine.

Google sign-in was implemented and removed. It verified an ID token and keyed
rate limiting on the `sub` claim. It moved no Earth Engine quota — the OAuth
client belongs to this project, so `ee.Initialize(project=ours)` bills here
whether or not a caller is authenticated — there was no persistence for an
identity to attach to, and no interface element from which to sign in, so the
exhausted-allowance response recommended an action the application could not
perform.

Shifting Earth Engine cost onto a visitor would require them to supply their own
Earth Engine-registered Cloud project, and the `earthengine` OAuth scope needed
for that is classed sensitive: a public application requesting it needs Google
verification review and is capped at roughly 100 users until that passes.

### 7.8 Errors

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

---

## 8. Quota protection

### 8.1 Three layers, cheapest first

1. **Input bounds** (`schemas.py`). The only layer that helps against the worst
   case: one well-formed request for an oversized area. Runs before any `ee`
   object exists, so a rejected request costs nothing.
2. **A concurrency semaphore** (`limits.py`). Every endpoint is a sync `def`, so
   FastAPI runs it on a threadpool of 40; without a cap one instance can hold 40
   blocking `getInfo()` calls open at once. An `anyio` request timeout frees the
   *client*, not the worker thread — `getInfo()` is uninterruptible blocking I/O
   — so the semaphore is what actually bounds resource use.
3. **A daily Earth Engine compute budget**, denominated in cost units rather
   than round-trips or HTTP requests. Backed by a Firestore `Increment` so it is
   genuinely global across instances.

Per-identity rate limiting sits alongside these, keyed on the opaque session
token and falling back to IP. IP alone is weak in India specifically: mobile
carriers use carrier-grade NAT, so thousands of subscribers share an egress
address — an IP-keyed limit restricts them collectively while one determined
caller simply rotates addresses.

### 8.2 The cost model, and why it counts what it counts

Earth Engine bills **EECU-seconds**. Round-trip count is nearly constant across
temporal modes — every `/api/yield` makes 12 — while compute scales with the
window, so a yearly query costs more than ten times a single-day one. A budget
denominated in calls therefore charged the cheapest and dearest queries
identically, and at its old default of 5000 calls/day could have exhausted a
month's allowance while reporting ample headroom.

The budget is denominated in **cost units**, one unit being a monthly-window
query:

```python
EE_COST_UNITS = {"daily": 0.1, "monthly": 1.0, "quarterly": 3.0, "yearly": 12.0}
```

Cost tracks the number of daily images the window reduces over, which is what the
irradiance, precipitation and shadow reductions all scale with.

Each endpoint then carries a multiplier relative to the same window's yield:

```python
EE_COST_ENDPOINT_MULTIPLIER = {
    "yield": 1.0, "series": 1.5, "baseline": 0.3, "tiles": 0.5,
}
EE_COST_UNITS_BUILDINGS = 0.1   # flat: one vector getInfo, no time window
```

`series` is dearest because it runs a shadow reduction per sub-period, so a
yearly series does roughly the work of a yearly yield spread over twelve
requests. An unknown mode bills the dearest and an unknown endpoint bills as a
yield, so a new caller is over-charged rather than waved through unmetered.

**These numbers were recalibrated after a live failure.** The first pass priced
`tiles` at 0.5 × window and `series` at 1.5 ×, and tiles were not cached at all
despite this document claiming a 6 h TTL. Exploring one city at a yearly window
therefore cost **60 units against a 40-unit day** — a visitor could not finish a
single site, and production refused a layer switch at 36 units used. Tiles are
now genuinely cached, `roof_mask` is charged flat because it reduces over no
time window, and the same session costs 33.7 units on first visit and nothing
on return. A test asserts one city stays under half a day's budget.

### 8.3 There is no console-side cap to set

The Earth Engine console reports *Noncommercial EECU-seconds per month* as a
**system limit** with adjustable = No, and *EECU-seconds per day* as "Unlimited",
also not adjustable. On the noncommercial tier Google offers no lever, so the
application's own budget is the only guard available.

Measured, on this project: one development day consumed **39,301** of the 540,000
monthly EECU-seconds — 91% of that month's usage — from a few dozen queries. At
that rate the month exhausts in under 14 days.

`SOLARIS_DAILY_EE_COST_BUDGET` defaults to **100** cost units/day. Spent in
full every day that is 360,000 of the 540,000 monthly EECU-seconds (67%), and
measured console usage has run far below it — 59,732 EECU-seconds, 11%, over a
full month of development and demo traffic. Watch the
console meter against the `ee_cost` field in the request logs and adjust from
evidence, not on the assumption that headroom exists.

If the shared Firestore counter is unreachable the local in-process budget still
applies, degrading from a global ceiling to a per-instance one bounded at 2× by
`max-instances`. Note the asymmetry with the cache: a cache that fails should
stop caching, but a budget that fails must not stop limiting.

The non-commercial **Community tier** carries no SLA and lower concurrency limits
than the commercial tiers. Appropriate for a portfolio deployment; not a platform
to point sustained traffic at.

---

## 9. Deployment

Target: **Cloud Run, `asia-south1` (Mumbai)** — lowest latency for Indian users
and for the India-region data.

### 9.1 Two steps that silently break everything

Both are invisible in the code, so they come first.

**1. The Cloud project must be registered with Earth Engine, and the runtime
service account must hold IAM roles on it.** Enabling the API is not enough.
Register the project at <https://code.earthengine.google.com/register>; the
registration covers principals that have access to it, so the service account
then needs `roles/earthengine.writer` and
`roles/serviceusage.serviceUsageConsumer`. Miss either and every request returns
403. `/api/ready` reports which of the two is missing.

**2. The Workload Identity Federation provider must carry an attribute condition
pinning the repository.** Without it, *any* GitHub repository can impersonate
your deploy service account. This is the single most common WIF
misconfiguration, and it is a full compromise of the project, not a degradation.

```
--attribute-condition="assertion.repository == 'OWNER/REPO'"
```

### 9.2 One-time GCP setup

Roughly fifteen commands. Deliberately not Terraform: one service, and the
runbook is what actually gets read.

```bash
export PROJECT_ID=pv-mapping-india
export REGION=asia-south1
export REPO=OWNER/REPO            # your GitHub owner/repo
export PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format='value(projectNumber)')

gcloud config set project $PROJECT_ID

# --- APIs ---
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  earthengine.googleapis.com \
  firestore.googleapis.com \
  iamcredentials.googleapis.com

# --- image registry ---
gcloud artifacts repositories create solaris \
  --repository-format=docker --location=$REGION

# --- two service accounts, with different jobs ---
# Runtime: what the container runs as. Needs Earth Engine and Firestore.
gcloud iam service-accounts create solaris-runtime \
  --display-name="SOLARIS Cloud Run runtime"

# CI: what GitHub Actions impersonates. Needs to deploy, not to read data.
gcloud iam service-accounts create solaris-ci \
  --display-name="SOLARIS CI deployer"

export RUNTIME_SA=solaris-runtime@$PROJECT_ID.iam.gserviceaccount.com
export CI_SA=solaris-ci@$PROJECT_ID.iam.gserviceaccount.com

for ROLE in roles/earthengine.writer roles/datastore.user roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:$RUNTIME_SA" --role="$ROLE"
done

for ROLE in roles/run.admin roles/artifactregistry.writer roles/iam.serviceAccountUser; do
  gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:$CI_SA" --role="$ROLE"
done

# --- keyless CI auth: no JSON key anywhere ---
gcloud iam workload-identity-pools create github \
  --location=global --display-name="GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc github \
  --location=global --workload-identity-pool=github \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository == '$REPO'"   # <-- step 2 above

gcloud iam service-accounts add-iam-policy-binding $CI_SA \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/attribute.repository/$REPO"

# --- Firestore, for the persistent cache and the shared budget counter ---
gcloud firestore databases create --location=$REGION

# The TTL policy. Without it the cache still works -- expiry is also checked in
# Python -- but nothing ever gets deleted, so this costs storage, not
# correctness.
gcloud firestore fields ttls update expires_at \
  --collection-group=solaris-cache --enable-ttl
```

**No service-account JSON key is created at any point.** If you find yourself
downloading one, something has gone wrong with the WIF setup — fix that instead.

### 9.3 GitHub Actions variables

Set these as repository **variables** (not secrets — none is sensitive), under
Settings → Secrets and variables → Actions → Variables.

| Variable | Value | Required |
|---|---|:-:|
| `WIF_PROVIDER` | `projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/github/providers/github` | yes |
| `DEPLOY_SERVICE_ACCOUNT` | `solaris-ci@$PROJECT_ID.iam.gserviceaccount.com` | yes |
| `RUNTIME_SERVICE_ACCOUNT` | `solaris-runtime@$PROJECT_ID.iam.gserviceaccount.com` | yes |
| `GCP_PROJECT` | your project id, for the image path | yes |
| `GEE_PROJECT_ID` | your project id, for `ee.Initialize` | yes |
| `PUBLIC_URL` | a custom domain, e.g. `https://solaris.example` | no |

`GCP_PROJECT` and `GEE_PROJECT_ID` are ordinarily the same value. They are
separate because the image registry path and the Earth Engine project are
independent choices, and a deployment could legitimately push images to one
project while billing Earth Engine to another.

`PUBLIC_URL` is optional: Cloud Run assigns the service URL, so it cannot be a
prerequisite of the first deploy. The workflow reads the URL back and sets
`SOLARIS_CORS_ORIGINS` from it, and `PUBLIC_URL` overrides that for a custom
domain.

The deploy job verifies every required variable is set before it authenticates,
so a missing one fails in seconds with the name rather than surfacing as an
opaque push error.

### 9.4 Deploying

Deployment is **manual**: run the *Deploy to Cloud Run* workflow from the Actions
tab and type `deploy` to confirm. It builds the image, deploys, smoke-tests
`/api/health` and `/api/ready`, and rolls back to the previous revision on
failure.

It does not run on push. An earlier version did, with no dependency on CI, so a
commit with failing tests would have deployed.

Manually:

```bash
gcloud run deploy solaris \
  --source . \
  --region=$REGION \
  --service-account=$RUNTIME_SA \
  --min-instances=0 \
  --max-instances=2 \
  --concurrency=8 \
  --cpu-boost \
  --timeout=120 \
  --set-env-vars="SOLARIS_ENV=prod,SOLARIS_CACHE_BACKEND=firestore,SOLARIS_LOG_FORMAT=json,GEE_PROJECT_ID=$PROJECT_ID,SOLARIS_CORS_ORIGINS=https://your-domain"
```

| Flag | Reason |
|---|---|
| `--min-instances=0` | Near-zero idle cost. Pinning to 1 to hide cold starts costs ~$10–15/month and defeats the point. |
| `--cpu-boost` | Absorbs the `ee.Initialize()` cold start instead, for free. |
| `--max-instances=2` | A hard damage ceiling. It also makes the in-process rate limiter near-global: with ≤2 instances the effective limit is ≤2×. |
| `--concurrency=8` | Every endpoint is a sync `def`, so FastAPI runs it on a 40-thread pool. Combined with the in-process semaphore this bounds concurrent Earth Engine calls. |
| `--timeout=120` | Above the request timeout, so Cloud Run is not the thing that kills a slow request. |

**A CI detail that bites:** the build step needs
`docker/setup-buildx-action@v3` before it. Without buildx the default docker
driver fails `cache-to: type=gha` with "Cache export is not supported for the
docker driver".

### 9.5 Configuration

Full list in `.env.example`. The ones that matter in production:

| Variable | Production value | Note |
|---|---|---|
| `SOLARIS_ENV` | `prod` | |
| `GEE_PROJECT_ID` | your project | **Server-side only.** Deliberately not accepted from requests — it previously was, which let any caller choose which project to initialise and whose quota to spend. |
| `SOLARIS_CACHE_BACKEND` | `firestore` | Tiered: memory in front of Firestore. |
| `SOLARIS_CORS_ORIGINS` | your domain | A wildcard is **rejected at startup**, not warned about. Accepts comma-separated or JSON. |
| `SOLARIS_LOG_FORMAT` | `json` | Cloud Logging needs `severity`, not `level`. |
| `SOLARIS_DAILY_EE_COST_BUDGET` | `100` | Cost units/day, not round-trips. See §8. |
| `SOLARIS_GUEST_COMPUTATION_ALLOWANCE` | `10` | Must exceed the number of sites a visitor will compare. |

Credentials resolve through plain `google.auth.default()`, which works
identically on Cloud Run, GCE, Cloud Build and under WIF. No `K_SERVICE`
environment sniffing.

`.env` discovery walks up from the config module to the directory containing
`pyproject.toml`, so the server works from an arbitrary working directory;
`/api/config` reports which file was loaded.

### 9.6 Observability

Structured JSON logs, one line per request, carrying `status`, `duration_ms`,
`cache`, `ee_calls`, `ee_cost`, `identity_kind` and `request_id`.

**`ee_calls` and `ee_cost` are the most useful numbers in the system.** They are
the quota currency, and they turn "why is this slow or expensive" into a single
log query.

One line per request looks like this — a cache miss followed by the identical
request served from cache:

```json
{"severity":"INFO","message":"POST /api/yield -> 200","logger":"solaris",
 "request_id":"e3e10857189146cb","method":"POST","path":"/api/yield",
 "status":200,"duration_ms":525.2,"identity_kind":"ip",
 "ee_calls":12,"ee_cost":12.0,"cache":"MISS"}

{"severity":"INFO","message":"POST /api/yield -> 200","logger":"solaris",
 "request_id":"76d07cdc621b41f5","method":"POST","path":"/api/yield",
 "status":200,"duration_ms":1.4,"identity_kind":"ip",
 "ee_calls":0,"ee_cost":0.0,"cache":"HIT"}
```

#### Reading them

Live tail, while someone is using the site:

```bash
gcloud beta run services logs tail solaris --region=asia-south1
```

The last hour of real traffic, newest first:

```bash
gcloud logging read   'resource.type=cloud_run_revision AND resource.labels.service_name=solaris
   AND jsonPayload.path!=""'   --freshness=1h --limit=50   --format='table(timestamp, jsonPayload.path, jsonPayload.status,
                  jsonPayload.duration_ms, jsonPayload.cache,
                  jsonPayload.ee_calls, jsonPayload.ee_cost)'
```

What the day has cost so far, which is the query that matters most:

```bash
gcloud logging read   'resource.type=cloud_run_revision AND resource.labels.service_name=solaris
   AND jsonPayload.ee_cost>0'   --freshness=24h --limit=1000 --format='value(jsonPayload.ee_cost)'   | awk '{t+=$1} END {print "cost units today:", t, "of 40"}'
```

Errors only:

```bash
gcloud logging read   'resource.type=cloud_run_revision AND severity>=ERROR'   --freshness=24h --limit=20 --format='value(timestamp, jsonPayload.message)'
```

In the console, the equivalent is Logs Explorer with
`resource.labels.service_name="solaris"`. Because the formatter emits
`logging.googleapis.com/trace`, every line from one request is grouped
together there.

Two log-based metrics and two alerts are enough: 5xx rate, and budget-exceeded.
Cloud Run already supplies request count, latency percentiles, error rate and
instance count for free.

Locally the same lines go to stdout. Set `SOLARIS_LOG_FORMAT=json` to get the
shape above, or leave it unset for a readable one-line format.

Deliberately **no** Prometheus, `/metrics` endpoint, or OpenTelemetry. A scrape
endpoint needs something always-on to scrape it, which contradicts
scale-to-zero.

### 9.7 Verifying a deployment

```bash
URL=$(gcloud run services describe solaris --region=$REGION --format='value(status.url)')

curl -fsS $URL/api/health
curl -fsS $URL/api/ready | jq .        # earth_engine must be "ok"
curl -fsS $URL/api/version | jq .

# A real computation. Run it twice: the second must report X-Cache: HIT.
curl -fsS -D- -X POST $URL/api/yield \
  -H 'content-type: application/json' \
  -d '{"lat":28.6139,"lon":77.2090,"baseline_mode":"yearly","year":2023}' \
  | tail -5
```

Check: `/api/ready` reports Earth Engine `ok`; the second identical request is a
cache hit; `data_quality.severity` is `ok`; and an error response contains a
`request_id` and **no** project id.

### 9.8 Rollback

```bash
gcloud run revisions list --service=solaris --region=$REGION
gcloud run services update-traffic solaris --region=$REGION --to-revisions=REVISION=100
```

No staging environment and no canary traffic splitting — one URL, and
`update-traffic` is sufficient.

### 9.9 Cost

With `min-instances=0`, Firestore's free tier and the Artifact Registry free
allowance, a project at this traffic level sits at or near zero. The things that
would change that, in order: pinning a minimum instance (~$10–15/month),
Memorystore instead of Firestore (~$35/month plus a VPC connector), or a global
load balancer for Cloud Armor (~$18–25/month). All three were considered and
rejected.

---

## 10. Development

### 10.1 Setup

```bash
python -m pip install -e ".[dev,ml,physics]"
pre-commit install
```

**A fresh clone with no Google Cloud account must get a fully green `pytest`.**
That is non-negotiable and is why the fake Earth Engine exists. If you find
yourself needing credentials to run a test, the test is in the wrong suite.

```bash
pytest                      # offline. no credentials, no network.
pytest -m gee               # integration. needs credentials.
ruff check src tests
ruff format src tests
mypy src/solaris/core src/solaris/api
```

`gee`-marked tests are deselected by default via `addopts` in `pyproject.toml`.

**mypy is scoped deliberately** to `core` and `api` — typed at the boundaries
only. Every `ee.*` object is effectively `Any`, so strict typing over
`solaris/gee` would generate many `type: ignore`s and find nothing. Passing
`mypy src/solaris` overrides the configured `files` and reports ~46 errors from
that intentionally untyped layer; use the scoped command above, which is what CI
runs.

It targets Python 3.12 although the project supports 3.11, because numpy ships
stubs in PEP 695 syntax that mypy cannot parse at 3.11 — and the error aborts the
entire run. The 3.11 floor is instead enforced by `tests/unit/test_packaging.py`,
which parses every source file against the 3.11 grammar.

### 10.2 Where a change goes

| Changing | File | Then |
|---|---|---|
| A physical coefficient | `core/constants.py` (or the owning module) | Bump `ALGO_VERSION`, which invalidates cached results automatically |
| A dataset id, band or vintage | `gee/datasets.py` | Bump `DATASET_VERSION` |
| A request field | `api/schemas.py` | Add a bound. Every field has one. |
| Temporal logic | `api/windows.py` | Imports no `ee`, so test it directly |
| Anything with `import ee` | `gee/` | It will be auto-discovered by the test fake |
| A new Earth Engine endpoint | `api/app.py` | Charge it: `deps.ee_gate(n_calls=…, cost=C.ee_cost_units(mode, "<endpoint>"))`. A test fails otherwise. |

Forgetting to bump a version means a deploy silently serves pre-change numbers
from cache. This is the step that gets forgotten exactly once.

### 10.3 Working with the fake Earth Engine

`tests/fakes/fake_ee.py` is a numpy-backed fake where `FakeImage` is a lazy
expression tree and every operation is real arithmetic on real arrays.

**It is deliberately not a `MagicMock`,** and the choice of double matters more
than it sounds. A mock lets the code run without raising while every arithmetic
result is a mock object — it would have accepted all ten confirmed defects in this
project's history and proven nothing.

Four rules, in priority order:

1. **Encode real Earth Engine semantics, especially where the production code
   gets them wrong.** `translate` treats bare offsets as metres. Focal operations
   resolve against the request projection. Comparisons propagate NaN the way
   Earth Engine propagates masks. This is the mechanism by which a semantics bug
   becomes a failing test rather than a mystery.
2. **Match real signatures exactly.** `ee.Feature(geometry, properties)`, in that
   order. The fake once had them reversed, so `ee.Feature(None, {"p": v})` — the
   standard idiom — built a feature whose *geometry* was the dict and which had no
   properties. Nothing raised; a 365-day reduction simply read back all `None` and
   the code under test took its no-data fallback while appearing to work.
3. **Fail loudly.** Module `__getattr__` raises `NotImplementedError` for anything
   unimplemented. Never add a permissive stub.
4. **Fixtures have analytically-known answers.** A flat block, a 40 m tower, a
   three-building canyon; a Gaussian heat-island with a known anomaly; aerosol
   with deliberately QA-flagged corrupt pixels, so the QA-masking fix is
   *required* to pass.

The fake is installed by patching each module's bound `ee` attribute, not
`sys.modules`. Modules bind `import ee` at import time, so replacing
`sys.modules["ee"]` afterwards has no effect — a bug that made the golden tests
reach real Earth Engine only when run *after* another test file. The consumer
list is discovered by walking the package rather than hand-maintained, because
the hand-maintained version checked itself and passed regardless.

The fake is itself tested in `tests/unit/test_fake_ee.py`. When it disagrees with
real Earth Engine, the fake is wrong — it was once *more forgiving* than the real
API and thereby hid a real edge artefact where 75% of sky-view pixels were masked
and energy dropped 58%.

**The synthetic world.** `tests/fakes/world.py` registers every asset the API
reads. Keep it **representative, not adversarial**: the rainfall fixture was
initially a stylised two-month monsoon giving 20 cleaning-rain days a year
against Delhi's real 91, which saturated the soiling model and broke the
penalty-balance assertions — not because the assertions were wrong but because the
world was. It now uses the measured Delhi monthly rain-day counts.

**Golden files.**

```bash
python -m tests.fakes.regenerate_golden
```

Commit a regenerated golden **on its own**, so the numeric diff is reviewable in
isolation from whatever change caused it. A golden diff bundled into a feature
commit is a golden diff nobody reads.

### 10.4 Writing tests

Prefer tests that would fail for a *reason*, and say the reason in the docstring.
`test_air_mass_stays_finite_at_the_horizon` explains that Kasten-Young is used
precisely because `1/sin(elevation)` diverges there. Six months later that
docstring is why nobody "simplifies" it back.

Assert physics, not just ranges. Diffuse fraction must *fall* as clearness rises,
because that is what the atmosphere does — a model with that backwards would still
score plausibly on RMSE.

`xfail(strict=True)` is the tool for a known defect: it documents the bug
executably and fails loudly the moment it is fixed.

Two conventions for anything with a model in it, both learned the hard way:
**declare the gate before training**, and **baseline against the published
method, not the naive one**. And **split spatially *and* temporally** for
anything geographic — neighbouring samples leak catastrophically.

### 10.5 Reference data

`src/solaris/evals/fetch.py` is the only code in the project that makes outbound
HTTP. Everything is cached under `evals/references/` and committed, so evals run
offline and in CI without hammering a free public service.

```bash
python -m solaris.evals.fetch --years 2020 2021 2022    # daily irradiance
python -m solaris.evals.fetch --hourly --years 2020 2021 2022
python -m solaris.evals.fetch --precip --years 2020 2021 2022
```

Refresh is a deliberate, explicit step, never automatic.

Note that `evals/reports/*.json` must stay **tracked**. The frontend imports them
at build time, so gitignoring them breaks the frontend, docker and drift CI jobs
even though the Python suite stays green.

### 10.6 Frontend

```bash
cd frontend/site
npm install
npm run dev      # dev server, proxies /api to localhost:8000
npm run build    # builds into src/solaris/api/static/
npm run budget   # bundle-size assertion
```

Two constraints enforced in CI:

- **Content routes must ship no three.js.** The intro's WebGL chunk is
  route-split and lazily loaded; a bundle-size assertion checks this.
- **Displayed numbers must come from the committed eval artifacts**, imported at
  build time. No hand-copied figures — that is the anti-drift guarantee, and a
  test asserts it.

**Basemaps are keyless.** CARTO's tile endpoints now return imagery stamped "API
KEY REQUIRED", so the map uses OpenFreeMap (vector), Esri World Imagery
(satellite) and OSM raster as a fallback. None needs a key.

**Overlay wiring details that caused visible bugs:** the tiles response field is
`urlTemplate`, not `tile_url`; overlays must be added with `beforeId` pointing at
the vector layer ids or they cover the AOI outline; and shadow and net-irradiance
layers must be stretched over the range roofs actually occupy (`SHADOW_VIS_MAX =
0.35`) rather than 0–1, because rooftop shadow frequency is typically 0.02–0.10
and a 0–1 stretch renders every real value as a uniform dark wash
indistinguishable from "no data".

### 10.7 Motion, glossary and inspection

**The landing globe** (`src/intro/EarthScene.jsx`) renders NASA Blue Marble by
day and Black Marble city lights by night, with the terminator fixed to the sun
so the planet turns through it. The ten amber markers are the evaluation
cities at their true coordinates, read from `data/cities.js`; the orbit is a
sun-synchronous polar track of the kind MODIS flies. One orchestrated moment —
India swings into view on load — then a slow spin and a lean toward the
pointer. Reduced motion renders it static, India facing.

Two defects in it are worth knowing because both passed review:

- **react-three-fiber copies `uniforms` into the material at construction.**
  Mutating the memoised uniforms object changed nothing on the GPU, so the
  planet stayed at fade 0 and drew as a black disc. Measured in the browser —
  the material's uniforms were a different object and its `uFade` never left
  zero. Uniforms are now written through the live material
  (`setUniform(meshRef.current, …)`). The original scene updated `uTime` the
  same broken way; it went unnoticed only because nothing read `uTime`.
- **The atmosphere shell is drawn on back faces**, where the outward normal
  points away from the camera. `max(dot(n, v), 0)` was therefore zero
  everywhere, the Fresnel term saturated, and the glow drew as a hard blue
  ring. It now uses `-dot(n, v)`, which fades to nothing at the outer rim.

**Chart entrances** come from a global Chart.js plugin (`entrance`, in
`charts.js`), so every chart on every page gets them. Charts built inside the
result panel reached final geometry on their first frame and never animated —
sampled every 45 ms a bar sat at its full height from the start — while the
same configuration built in isolation animated normally, and the in-page chart
animated the moment it was reset after layout. The plugin therefore hides each
chart at init and replays its entrance two frames later. The hide must suspend
the canvas's opacity transition first: React paints the canvas at full opacity
before the chart is built, so an ordinary `opacity: 0` *animated* down from 1
and the replay restored it before it got there. Verified frame by frame; with
reduced motion requested, charts render static.

**The glossary** (`data/glossary.js`, `components/Term.jsx`) defines every
technical term in two sentences — what it is, and why it matters here. `Term`
is a real button, so it opens on hover, keyboard focus and tap, closes on
Escape, and is linked by `aria-describedby`. Never nest one inside a link:
a button inside an `<a>` is invalid and fires both. On the linked landing
cards the stat note carries the meaning in words instead.

**The payload inspector** (`components/PayloadInspector.jsx`) shows the exact
request and response behind the result on screen, plus an equivalent `curl`
command, so any figure can be reproduced outside the browser.

**Scrubbing.** Line charts carry a crosshair and can be stepped with the arrow
keys once focused; Home and End jump to the ends.

**The sandbox sliders** on the Method page each have an accessible name and an
`aria-valuetext` with units. They had neither, so a screen reader announced all
eight as "slider".

**The telemetry line** under the hero is real: each record is a city-year from
`evals/reports/latest.json` — coordinates, reference irradiance, modelled
specific yield. Invented telemetry would have been decoration on a project
whose argument is that it does not overclaim.

### 10.8 Chart conventions

Form chosen before colour:

- **No dual axes, ever.** Value and error never share a chart; error goes in its
  own panel below, sharing the x-axis.
- **Colour follows the entity, not its rank — and not the page.** Every loss
  term takes its colour from `lossColour()` in `charts.js`. The Method page's
  charts once carried their own palette, with heat island in a hand-picked
  purple and shadow and sky view *inverted* relative to Explore, so a reader
  who learned the key on one page misread the other.
- **Context is neutral.** Where one series is context rather than the subject —
  the delivered share, the total — it is slate, so the colour goes to the
  losses.
- **Label ink follows the fill.** A value drawn inside a mark picks light or
  dark ink by the fill's WCAG luminance; dark ink on the slate delivered
  segment was unreadable.
- **A legend whenever there are two or more series.** One series gets none.
- **Categorical palettes stop at four.** Past roughly seven the eye cannot hold
  the legend; the honest answer is small multiples. Twelve cities means small
  multiples, never twelve coloured lines.
- **Scatter caps at three series** — facet by reference instead, with the 1:1 line
  and slope/intercept reported.
- **Per-month validation** is a line for the model over a shaded band showing the
  reference min–max spread. That band *is* the 10% honesty argument made visual.
- **More than ~7 meaningful rows is a table, not a chart.**
- Every chart has a reachable table view.

### 10.9 Commits

Conventional-ish prefixes: `[feat]`, `[fix]`, `[refactor]`, `[test]`, `[docs]`.

Say what changed and *why it was wrong before*. This repository's history is most
useful as a record of which assumptions turned out to be false, and a message
saying "fixed shadow model" throws that away.

---

## 11. Limitations

### 11.1 Open

| # | Limitation | Consequence |
|---|---|---|
| **P1** | Every roof is treated as **horizontal**. No tilt, azimuth or incidence-angle modifier in the served path. | Understates a latitude-optimal array by a measured mean of 7.1% of specific yield (+1.7% Chennai to +14.1% Leh). The pvlib layer computes the tilted case and the eval reports both, but the map assumes flat. |
| **P6** | Annual shadow geometry rests on **31 sampled sun positions**, not 8760 hours. | Fine for an annual mean; it cannot resolve a specific morning. |
| **P7** | Roof slope is taken from **SRTM at 30 m**, so the 30° exclusion excludes buildings on steep *terrain*, not steep *roofs*. | A scale mismatch against the 4 m roof raster. Deliberately not fixed — a correct version needs roof-plane fitting from the height raster, which is a project in itself. |
| **P9** | No **inter-row shading** within an array, and no obstruction modelling (water tanks, stairwell housings, parapets). | The packing factor of 0.70 absorbs this as a lumped assumption rather than modelling it. |
| — | **Roof vintage is capped at 2023.** Open Buildings 2.5D Temporal v1 runs 2016–2023. | Buildings completed after 2023 are invisible regardless of the irradiance window. A 2026 query uses 2023 geometry. |
| — | **No ground truth.** No pyranometer, no metered plant. | Every reference is satellite-derived or a published aggregate. |
| — | **Accuracy is bounded at ~10%** by the disagreement between the available free references. | No tighter claim is honest, however well the model matches any single source. |
| — | **The soiling calibration rests on one assumed AOD value.** | The rate is measured; the AOD it is anchored on is a literature figure. Per-city soiling *ranking* inherits that uncertainty; the rainfall-driven results do not. |
| — | **Building confidence is not a probability.** The Open Buildings presence threshold of 0.5 is a prior, not a calibrated likelihood. | Roof area carries an unquantified inclusion error. |
| — | **Coordinate quantisation to ~11 m.** | Two clicks 10 m apart return the identical result. A deliberate trade: without it the cache hit rate would be near zero. |
| — | **The decomposition correction shares one clearness index across a window.** | Sub-daily variation in sky state is not represented; a window of clear mornings and cloudy afternoons is modelled as uniformly average. See §5.2. |

### 11.2 Not built, and why

**ML Track B — shadow/sky-view surrogate.** Profiled, then dropped; the
round-trip reduction is zero and the ceiling is a 1.33× speedup. See §5.4.

**ERA5 → reference bias correction.** The originally-planned Track A. It needs
Earth Engine credentials to build the ERA5 side of the training set, which were
unavailable. The decomposition model targets the same defect from the reference
side instead.

**Sign-in, in any form.** Implemented and then removed: it moved no Earth Engine
quota, had no persistence tier to attach an identity to, and had no interface
element from which to sign in. Shifting quota onto a visitor would need the
`earthengine` scope, which is classed sensitive — requiring Google verification
review and capping an unverified application at roughly 100 users. Sessions are
keyed on an opaque token instead; see `src/solaris/api/auth.py`.

**Per-building validation.** The 4 m geometry is never checked against a surveyed
roof. Validation is at city scale only.

---

## 12. Defect history

Kept because knowing a system's failure history is part of judging it, and
because several of these produced plausible wrong numbers rather than errors —
which is the harder kind to catch.

| # | Defect | How it presented |
|---|---|---|
| **D1** | `ee.Image.translate` defaults to **metres**; the code passed pixel counts. | Shadow search offset 100 m where 400 m was intended; sky-view horizon angles computed over 4× the distance actually sampled, biasing the factor toward 1 — i.e. toward *no* diffuse penalty. |
| **D2** | Shadow mask was not a directional trace: a circular `focal_max` plus a height test at a different location. | Search effectively isotropic. Self-assessed as "5–10% high"; the real error in dense fabric was larger. Rewritten as a directional DSM trace. |
| **D3** | Sun-position thinning dropped weight without renormalising. | Would have halved shadow frequency. The branch never fired (max 39 positions), so it was dead code concealing a live bug. |
| **D4** | MAIAC aerosol had **no QA masking**, though the docstring claimed it did. | Cloudy and cloud-shadowed retrievals averaged into the annual mean. |
| **D5** | Soiling loss was unbounded. | Retention went negative above AOD 12.5 — negative generated energy. |
| **D6** | Naive annualisation, `× 365.25/days`. | A single December day scaled to an annual rate and returned as one. Now omitted below 28 days. |
| **D7** | Dataset vintage filter used the **host machine's local timezone**. | Results not reproducible across machines. |
| **D8** | Silent fallbacks returning plausible numbers. | Distinguishable from real results only by an obscure `*_source` string. The `data_quality` block was the fix, and writing it immediately found two shade buckets with no sun reporting `0.0` shade — which reads as "fully sunlit". |
| **D9** | Focal operations resolved against the **request** projection, not the image's. | A 100-pixel kernel spanned ~400 m when reducing at 4 m and *kilometres* when rendering a low-zoom tile. **The shadow layer a user saw on the map was not the shadow layer behind their number.** |
| **D10** | `ShadowPenalty.frequency` indexed an empty position list. | `IndexError` instead of a clear message, reachable for a high-latitude winter window. |
| **R5** | The temporal bound was calendar-derived (`today.year − 1`). | Q2 2026 refused two and a half months after it ended. Now probed from ERA5-Land's actual last image. |
| **P3/P4** | Temperature loss counted only as urban excess, with land surface temperature used as air temperature. | Two errors of opposite sign, neither measured: surface heat island runs 2–3× the air heat island, while the absolute 35 °C-vs-25 °C loss was absent, folded ambiguously into `PR = 0.80`. |
| **P5** | Soiling had no rain-washing or cleaning-interval term. | One annual coefficient applied unchanged to windows from one day to one year. |
| **P8** | Shade-matrix buckets were labelled UTC. | The "08-12" bucket is really 13:30–17:30 IST. Both labels are now carried. |

### 12.1 Infrastructure and packaging defects

- A **project-id leak** in 500-level error detail. `HTTPException` is handled by
  FastAPI before any middleware, so this needed its own handler.
- `CORS allow_origins=["*"]` paired with `allow_credentials=True`, which is
  invalid per the CORS spec and so never granted the access it appeared to.
- A **client-supplied `project_id`** that let any caller choose which GCP project
  the server initialised.
- An **unlocked module global** in `_ensure_ee` that two concurrent cold requests
  could both initialise through.
- **The rate limiter refused the site's own JavaScript.** It applied to every
  path except five meta endpoints — including `/`, every `/assets/*.js` chunk,
  the stylesheet and the Earth textures — at 30 per minute and 300 per day. One
  landing-page load is a dozen or more files, so a few navigations answered 429
  for the page's own code and it failed with "Failed to fetch dynamically
  imported module". Worse for this audience: static files carry no session
  token, so they were keyed by IP, and carrier-grade NAT puts thousands of
  Indian mobile subscribers behind one address — they shared 300 requests a day
  for *loading the site*. Only `/api/` is limited now; static files cost nothing
  and touch no quota. Found by exhausting the limiter during testing, not by
  review.
- **`ee_calls` was always logged as `0`.** The counter was a contextvar written
  by the request handler and read by the middleware, but Starlette's
  `BaseHTTPMiddleware` runs the downstream app in a separate task, so the
  handler rebound the name in its own copy of the context and the middleware
  never saw it. The field advertised as the quota currency read zero on every
  request from the day it was added. The contextvar now holds a **mutable**
  `RequestCounters` object: rebinding does not cross the task boundary, but
  mutation does. The one test covering it asserted the counter was zero
  *outside* a request, which is trivially true and passed throughout.
- A **relative `StaticFiles` path** that made the server work only when launched
  from the repo root.
- **Two undeclared dependencies**, `pydantic-settings` and `cachetools`. Both
  happened to be present locally, `cachetools` because an older `google-auth`
  pulled it in transitively; when `google-auth` dropped it, every clean install
  lost the in-process cache backend and `/api/yield` answered 500 on its first
  cache lookup. A working developer machine says nothing about a working install.
- **Unpinned linters.** ruff's formatter output is version-sensitive: 0.12 and
  0.16 disagree on lambda wrapping, so an open bound made `ruff format --check`
  fail in CI on code the developer had just formatted. Now upper-bounded, and a
  test asserts the bound exists.
- **`test_ml.py` overwrote the committed model artifact**, because `train.run()`
  persisted unconditionally. It now takes `persist: bool`.
- **The EE-consumer guard was tautological** — it checked a hand-maintained list
  against itself. It now walks the package.
- **An EE memory-limit failure** on the energy stack: five heavy bands in one
  reduction exceeded the limit. Cut to three, with the heat and soiling stages
  derived in Python (exact, because scalars distribute), plus `tileScale=4`.
  Golden responses unchanged.
- **A guest allowance of 3** blocked the five-site comparison workflow the
  product exists for. Raised to 10.
- **An allowance key mismatch**: `issue()` registered the bare token while
  `charge_computation` used a prefixed key, so the allowance never decremented.
  `Identity` now carries `key` and `allowance_key` separately and deliberately.

---

## 13. Writing standard for site copy

Written for whoever edits the site's prose next. It exists because the copy
drifted into an explanatory, self-referential register — captions telling the
reader which chart mattered most, and adjectives grading the project's own
components. That register is wrong for every audience in §1.3.

**Register.** Declarative and quantitative. Report what was measured and under
what conditions.

**Prohibited.** Instructions about salience ("the first thing to know", "the
single most useful thing here"); evaluative grading of the project's own
components ("the least defensible part"); rhetorical contrast as a substitute for
evidence ("not X, it is Y"); and narration of the site's own structure.

**Quantify.** Prefer "losses total 5–12% of baseline irradiance" to "most of the
energy survives". Where a figure is a range across the evaluation set, give the
range.

**Vary paragraph length.** A single-sentence paragraph is the correct form for a
finding. Uniform six-line blocks read as generated text and flatten the
distinction between a result and its context.

**Captions state what is plotted and what conditions the reading.** They do not
instruct interpretation. A caption may state a sensitivity — "ranking is sensitive
to the beam fraction below approximately 0.5" — because that is a property of the
data.

**Attribute limitations to mechanism, not to sentiment.** "SRTM resolves terrain
at 30 m, so the slope exclusion operates on terrain rather than roof pitch" is a
mechanism. "This is a weakness we are honest about" is not.

---

## 14. Open items

Carried forward deliberately, rather than left to be rediscovered.

1. **`SoilingPenalty.stats()` is documented as superseded but is still the live
   path** for `/api/baseline` and `/api/series`; only `/api/yield` uses
   `stats_windowed()`. Either migrate those two endpoints or stop calling the
   method superseded — as it stands the window-independent soiling figure is what
   those endpoints report.
2. **Re-run the live round-trip profile.** The committed
   `evals/reports/track_b_profile.json` records
   `shadow_frequency_reduce: -1.0` with `User memory limit exceeded` — a failure
   that the energy-stack reduction and `tileScale=4` have since fixed. The
   Track B decision does not depend on it (that rests on the exact round-trip
   count, which is unaffected), but the published stage timings are incomplete
   until `python -m solaris.evals.profile_yield --live` is run again with
   credentials.
3. **Per-endpoint cost multipliers are reasoned, not measured.** §8.2's
   multipliers come from counting reductions, not from EECU-second telemetry.
   Once the console meter has a few weeks of data against the `ee_cost` log
   field, calibrate them.
4. **P1 (flat-roof assumption) is the largest remaining physics gap**, worth a
   measured mean of 7.1% of specific yield and up to 14.1% at high latitude. The
   pvlib layer already computes the tilted case; the serving path does not use
   it.
5. **The heat-island overlay uses a rainbow-like ramp on a diverging
   quantity.** ΔT runs negative to positive, and blue → green → purple → red
   has no neutral midpoint. A proper diverging ramp would read correctly, but
   the change has to be made in the server's tile palette and the legend
   together, so it was left rather than done half.
