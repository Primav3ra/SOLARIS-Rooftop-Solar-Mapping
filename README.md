<h1 align="center">SOLARIS</h1>
<p align="center">
   https://solaris-6z7g4xx6iq-el.a.run.app
  Rooftop solar potential for urban India, computed on demand from open satellite data.<br>
  Google Earth Engine · FastAPI · pvlib · React + MapLibre GL
</p>
<p align="center">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/api-FastAPI-009688?logo=fastapi&logoColor=white">
  <img alt="MapLibre GL" src="https://img.shields.io/badge/map-MapLibre%20GL-295DAA?logo=maplibre&logoColor=white">
  <img alt="Earth Engine" src="https://img.shields.io/badge/geo-Earth%20Engine-34A853?logo=googleearth&logoColor=white">
  <img alt="tests" src="https://img.shields.io/badge/tests-660%20passing-brightgreen">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-green">
</p>

---

Given a point in urban India and a time window, SOLARIS estimates how much electricity the
rooftops there could generate. It models the things that make a real roof underperform a
brochure: the building next door, the sky the roof cannot see, the heat of the city, and
the dust that settles between monsoons.

Nothing is precomputed. Every query runs against Google Earth Engine on demand.

## Contents

- [What it does](#what-it-does)
- [Quick start](#quick-start)
- [How it works](#how-it-works)
- [Validation](#validation)
- [Machine learning](#machine-learning)
- [Testing](#testing)
- [Deployment](#deployment)
- [Limitations](#limitations)
- [Documentation](#documentation)

## What it does

- Builds a rooftop candidate mask from Open Buildings 2.5D at ~4 m, with heights.
- Sums ERA5-Land global horizontal irradiance over any window the data supports.
- Applies four physics layers: a **directional shadow trace**, a **sky-view factor** for
  diffuse obstruction, an **urban heat island** temperature derate, and **rain-aware
  soiling** from the published Kimber model.
- Converts to energy through pvlib, with the performance ratio decomposed into named terms
  rather than one lumped scalar.
- Reports, on every response, which inputs came from a fallback rather than a measurement.

That last point is the design principle rather than a feature. The recurring failure mode
this project has been unpicking is a plausible number with no indication of where it came
from.

## Quick start

### Run it locally

```bash
# 1. Python
python -m pip install -e ".[dev,ml,physics]"

# 2. Earth Engine credentials (needed only for live queries)
earthengine authenticate

# 3. Build the frontend. It is generated, not committed -- Vite writes straight
#    into the package where FastAPI serves it from.
npm --prefix frontend/site install
npm --prefix frontend/site run build

# 4. Serve
solaris-api          # or: uvicorn solaris.api.app:app --reload
```

Then open <http://127.0.0.1:8000>.

Every page except **Explore** is fully static and works without credentials — including
the validation and model results, which are read from committed artifacts. Only the map
needs Earth Engine.

### Frontend development

```bash
npm --prefix frontend/site run dev     # port 5173, proxies /api to localhost:8000
```

### Run the tests

```bash
pytest               # offline. no credentials, no network, no npm install.
```

A fresh clone with no Google Cloud account gets a fully green run. That is deliberate —
see [Testing](#testing).

## How it works

```
                       browser
                          │
              ┌───────────┴───────────┐
              │  React site (Vite)    │  static routes cost zero
              │  8 routes, split      │  Earth Engine quota
              └───────────┬───────────┘
                          │ JSON
              ┌───────────┴───────────┐
              │   FastAPI             │
              │  identity → limits    │
              │  → cache → EE gate    │
              └───────────┬───────────┘
                          │ getInfo() × 12
              ┌───────────┴───────────┐
              │  Google Earth Engine  │  all raster work runs here
              └───────────────────────┘
```

The physics chain, in order:

```
ERA5-Land GHI                                     kWh/m²
  → split into beam and diffuse
  → beam × (1 − shadow frequency)
  → diffuse × sky-view factor
  → × (1 + γ·ΔT_air)                              heat island
  → × (1 − soiling loss)                          dust, net of rain
  × roof area × packing factor × efficiency × PR  → kWh
```

**Earth Engine is the compute engine, not a data source.** No raster is ever downloaded;
every reduction is expressed server-side and only scalars come back. That is why one query
is a dozen small round-trips, and why the round-trip count is the thing worth optimising —
a fact that later killed a planned surrogate model.

Data sources, resolutions and licences: [docs/methodology.md](docs/methodology.md), or the
**Data** page of the running site.

### The central approximation

ERA5-Land irradiance is ~9 km while roof geometry is ~4 m. Within one ERA5 cell every roof
receives identical irradiance, so **all spatial variation in the output comes from the
geometry layers**. This is not a bug to fix — it is the resolution of the best open dataset
for India — but it bounds what a per-building figure means. The site visualises the scale
gap directly rather than describing it.

## Validation

Full report: [docs/validation.md](docs/validation.md) and `evals/reports/`.

### The constraint that governs everything

**The two credible free references for India disagree by 10.6%.** For Delhi, NASA POWER
gives 1736 kWh/m²/yr and Global Solar Atlas gives 1930.

No accuracy claim here can honestly be tighter than that spread. The harness therefore
reports against a reference *ensemble* with an explicit band, never a single asserted
truth, and prints the spread on every run.

This was forced by the data, not chosen: **PVGIS-SARAH3 does not cover India.** Its docs
advertise Asia and it was the intended primary reference; probing the live API for seven
Indian cities returned a spatial-coverage error for all seven.

### Specific yield against measured plants

Specific yield is the comparison that matters, for an algebraic reason: packing factor and
module efficiency cancel out of it, so it isolates the irradiance and loss chain from the
capacity assumptions.

| | |
|---|---|
| Published plausibility band | 1000–1750 kWh/kWp/yr |
| Model | mean **1302**, range 1103–1428 |
| Inside the band | **30 / 30** city-years |

Against the best-documented measurement — a 12 kWp Delhi rooftop at 1147 kWh/kWp/yr — the
model gives 1256, i.e. **+8%**. Two things cut against reading that as an accuracy claim:
that plant reports a performance ratio of 0.85–0.93 against a typical Indian year-one
0.78–0.83, and it is a *tilted* array while this model treats every roof as horizontal.
Tilt is worth 7.1% at Delhi — the same size as the discrepancy, in the same direction.

### Two errors the validation itself caught

Both would have propagated into every number.

**NASA POWER's hourly endpoint defaults to local solar time**, not UTC. Reading it as UTC
shifts every timestamp by longitude/15 hours — 5.1 h at Delhi — putting the solar position
four hours out and making any transposition meaningless. Found empirically rather than from
the documentation: correlating GHI against sin(solar altitude) in UTC gives **+0.994** with
the flag passed explicitly against **+0.141** under the default.

**Its GHI, DNI and DHI do not close**, summing 5.26% below the reported GHI for Delhi. That
put a uniform 5% negative bias on every plane-of-array figure, and had already produced a
*wrong conclusion* — a test asserting that low-latitude tilt loses energy.

## Machine learning

Three tracks planned; **two shipped and one was measured and dropped**. Details in
[docs/ml.md](docs/ml.md).

Two rules applied to all three:

- A model must beat the **published method**, not the naive one. Beating a constant proves
  nothing.
- The gate is declared **before training**, in the module above the training code, so it
  cannot be relaxed to suit the result.

### Track A — beam/diffuse decomposition

The beam fraction is the model's most ERA5-sensitive input. The harness measured the
reference mean at **0.540** across 30 city-years while production fell back to a constant
**0.60**.

| Rung | Test RMSE | MBE | Skill vs Erbs |
|---|---:|---:|---:|
| `constant` (what production used) | 0.2540 | −0.139 | −0.78 |
| `climatology` (36 params, no ML) | 0.1958 | +0.015 | −0.37 |
| **`erbs`** (published, 1982) | 0.1426 | +0.077 | 0.00 |
| `ridge` | 0.1097 | +0.014 | +0.23 |
| **`gradient_boosting`** | **0.0893** | +0.015 | **+0.37** |

The holdout is spatial **and** temporal — three held-out cities × a held-out year, closest
test/train pair verified above 250 km. Hours within a city-day are strongly correlated, so
a random row split would report a fantasy score.

Two findings beyond the ranking: the constant's bias of **−0.139** quantifies the defect it
replaces, and **Erbs is itself biased for India** at +0.077, consistent with being fitted
on US aerosol conditions.

Serving falls through `model → Erbs → constant`, always reporting which rung answered.
Out-of-domain inputs fall back to Erbs, because a boosted tree does not extrapolate — it
returns the nearest leaf, confidently and baselessly.

### Track C — soiling

Reframed from ML to **parametric, deliberately**: there is no open Indian PV-soiling label
set, so training end to end would be a curve fit dressed as machine learning. Instead, the
published Kimber model calibrated against measured Indian rates.

- Seasonal rates match published Delhi measurements within **3%**, with the correct
  ordering (spring worst, monsoon best).
- Agreement with `pvlib.soiling.kimber` to **0.003 fraction points** across ten cities.
  Reaching it found three real errors in the closed form.
- A **21.9× spread** across Delhi's months, where the old model returned one figure for all
  of them.
- The change is **not uniformly downward**: for Jodhpur and Ahmedabad the new model predicts
  *more* soiling despite lower aerosol, because 125- and 135-day unbroken dry spells are
  invisible to an AOD-only model. The old model was optimistic in arid India — the climate
  where soiling does most damage.

### Track B — measured, then dropped

A shadow surrogate, specified as a *speed* optimisation and gated on profiling
`/api/yield` first.

Measured: **12** round-trips per query, **3** involving shadow compute. A perfect surrogate
still makes 12, because it changes what a reduction computes and not whether it must be
fetched — so the round-trip reduction is **zero** and the ceiling is a **1.33× speedup**.

There is also a structural argument: the surrogate's information content is the directional
horizon profile, and given that profile "is this pixel shadowed" is the analytic identity
`horizon_angle(sun_azimuth) > sun_altitude`. A boosted tree approximating one comparison is
strictly worse than the comparison.

Dropped, with the measurement and the condition that would reverse it committed at
`evals/reports/track_b_profile.md`.

## Testing

**660 tests, offline, no credentials, no network.** That is possible because of
`tests/fakes/fake_ee.py`: a numpy-backed fake Earth Engine where `FakeImage` is a lazy
expression tree and every operation is real arithmetic on real arrays.

It is deliberately **not** a `MagicMock`. A mock would let the code run without raising
while every result was a mock object — it would have accepted all ten of the defects this
project found and proven nothing. The fake instead encodes real Earth Engine semantics,
*including the places the production code originally got them wrong*, which is how those
defects became failing tests.

Two bugs the test infrastructure itself had, both worth knowing:

- `sys.modules["ee"]` patching is ineffective, because modules bind `import ee` at import
  time. The golden tests silently reached real Earth Engine, but only when run *after*
  another test file.
- The "is every consumer patched?" guard checked a hand-maintained list against itself, so
  it passed regardless. Adding one new module produced eleven failures with a live
  authentication error. The list is now discovered by walking the package.

See [docs/contributing.md](docs/contributing.md).

## Deployment

Cloud Run in `asia-south1`, keyless via Workload Identity Federation. No service-account
JSON key anywhere. Full runbook: [docs/deployment.md](docs/deployment.md).

Two setup steps are invisible in the code and break everything silently:

1. **The runtime service account must itself be registered as an Earth Engine user.**
   Enabling the API is not enough; miss this and every request 403s.
2. **The WIF provider must pin `assertion.repository`.** Without it, any GitHub repository
   can impersonate your deploy identity.

### Quota protection, three layers

Cheapest first, because they defend against different things.

1. **Input bounds.** The only layer that helps against the worst case — one well-formed
   request for an oversized area. Runs before any `ee` object exists, so a rejection costs
   nothing.
2. **A concurrency semaphore.** Every endpoint is a sync `def`, so FastAPI runs it on a
   40-thread pool. A request timeout frees the *client*, not the worker thread, because
   `getInfo()` is uninterruptible blocking I/O.
3. **A daily Earth Engine call budget**, denominated in round-trips rather than HTTP
   requests, backed by a Firestore `Increment` so it is global across instances.

### Caching

Two tiers: in-process, in front of Firestore with a native TTL policy. Firestore over Redis
deliberately — Memorystore needs a VPC connector and bills ~$35/month always-on, which
contradicts scale-to-zero, and Firestore's free tier also supplies the transactional
counter the budget needs.

The key design matters more than the backend. It resolves the temporal mode first, so
`{yearly, 2023}` and the equivalent date range collapse to one entry; quantises coordinates
to 4 decimal places (~11 m), without which the hit rate would be near zero because areas
come from raw map-click floats; and is prefixed with `ALGO_VERSION`, so changing the physics
invalidates stale entries automatically instead of needing a manual flush.

### Sessions

Guest access only; there is no sign-in. Each browser session gets an opaque token and an
allowance of 10 computations, and **cached results do not count against it**, so
revisiting an already-computed site is free.

Google sign-in was built and then removed. It verified an ID token and keyed rate limiting
on the `sub` claim, which is a better key than IP — carrier-grade NAT puts thousands of
Indian subscribers behind one address. But it moved no Earth Engine quota (the OAuth client
is this project's, so calls bill here regardless), there was no persistence for an identity
to attach to, and no interface from which to sign in. Reinstating it would be justified
alongside a persistence tier. See `src/solaris/api/auth.py`.

### Deliberately not done

| Cut | Why |
|---|---|
| Redis / Memorystore | ~$35/mo always-on plus a VPC connector; contradicts scale-to-zero |
| Cloud Armor | Needs a global load balancer, ~$18–25/mo, to replace ~10 lines |
| Prometheus / OpenTelemetry | Cloud Run supplies the same signals free; a scrape endpoint needs always-on infrastructure |
| Terraform | One service, ~15 `gcloud` commands. The runbook is what gets read |
| Staging environment | One URL; `update-traffic` rollback suffices |
| Celery / a database | Nothing is long-running once caching lands |

## Limitations

Stated at length in [docs/limitations.md](docs/limitations.md), and on the site's own
Limitations page — a clear limitations page is a credibility asset, not a weakness.

The ones that most affect a number:

- **Every roof is treated as horizontal.** Understates a well-installed array by 7–12% at
  north-Indian latitudes.
- **Roof vintage is capped at 2023**, so a 2026 query uses 2023 geometry.
- **Roof slope comes from 30 m terrain**, so the 30° exclusion excludes buildings on steep
  *terrain*, not steep *roofs*.
- **No ground truth.** No pyranometer and no metered plant are in the loop.
- **The soiling calibration rests on one assumed aerosol value.**

Ten confirmed defects were found and fixed along the way, including a units bug that made
the shadow and sky-view geometry wrong by a factor of four, and focal operations that
resolved against the request projection — so **the shadow layer a user saw on the map was
not the shadow layer behind their number**. All are listed with their symptoms.

**What this output is:** a screening estimate of technical rooftop potential, useful for
comparing neighbourhoods or sizing a city-level programme.

**What it is not:** an engineering estimate for a specific installation. That needs a site
survey, roof-plane geometry, structural assessment and preferably a year of on-site
measurement.

## Documentation

| Document | Contents |
|---|---|
| [architecture.md](docs/architecture.md) | Package layout, request flow, the layering rule |
| [methodology.md](docs/methodology.md) | Every coefficient, its value and its citation |
| [validation.md](docs/validation.md) | Results, the reference-spread caveat, what it does not establish |
| [ml.md](docs/ml.md) | All three tracks, including the one that was dropped |
| [api-reference.md](docs/api-reference.md) | Endpoints, bounds, errors, caching, auth |
| [deployment.md](docs/deployment.md) | The Cloud Run runbook |
| [limitations.md](docs/limitations.md) | Open and resolved, with symptoms |
| [contributing.md](docs/contributing.md) | Working with the fake Earth Engine |

Interactive API docs are at `/docs` on any running instance.

## License

MIT. Dataset licences and attribution requirements are listed in
[docs/methodology.md](docs/methodology.md) and on the site's About page — CC BY 4.0 and the
Copernicus licence both require attribution as a condition of use.
