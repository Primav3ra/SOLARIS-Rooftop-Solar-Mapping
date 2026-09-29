<h1 align="center">SOLARIS</h1>
<p align="center">
   https://solaris-6z7g4xx6iq-el.a.run.app<br>
   Rooftop solar potential for urban India, computed on demand from open satellite data.<br>
   Google Earth Engine · FastAPI · pvlib · React + MapLibre GL
</p>
<p align="center">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/api-FastAPI-009688?logo=fastapi&logoColor=white">
  <img alt="MapLibre GL" src="https://img.shields.io/badge/map-MapLibre%20GL-295DAA?logo=maplibre&logoColor=white">
  <img alt="Earth Engine" src="https://img.shields.io/badge/geo-Earth%20Engine-34A853?logo=googleearth&logoColor=white">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-green">
</p>

---

Given a point in urban India and a time window, SOLARIS estimates how much electricity the
rooftops there could generate. It models the things that make a real roof underperform a
brochure: the building next door, the sky the roof cannot see, the heat of the city, and
the dust that settles between monsoons.

Nothing is precomputed. Every query runs against Google Earth Engine on demand.

## What it does

- Builds a rooftop candidate mask from Open Buildings 2.5D at ~4 m, with heights.
- Sums ERA5-Land global horizontal irradiance over any window the data supports.
- Applies four physics layers: a **directional shadow trace**, a **sky-view factor** for
  diffuse obstruction, an **urban heat island** temperature derate, and **rain-aware
  soiling** from the published Kimber model.
- Converts to energy through pvlib, with the performance ratio decomposed into named terms
  rather than one lumped scalar.
- Reports, on every response, which inputs came from a fallback rather than a measurement.

## Quick start

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

```bash
pytest                                  # offline. no credentials, no network, no npm install.
npm --prefix frontend/site run dev      # port 5173, proxies /api to localhost:8000
```

A fresh clone with no Google Cloud account gets a fully green test run. That is
deliberate, and it is why the numpy-backed fake Earth Engine exists.

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
is a dozen small round-trips, and why round-trip count is the thing worth optimising.

### The central approximation

ERA5-Land irradiance is ~9 km while roof geometry is ~4 m. Within one ERA5 cell every roof
receives identical irradiance, so **all spatial variation in the output comes from the
geometry layers**. This is not a bug to fix — it is the resolution of the best open dataset
for India — but it bounds what a per-building figure means. The site visualises the scale
gap directly rather than describing it.

### The constraint on every accuracy claim

**The two credible free references for India disagree by 10.6%.** For Delhi, NASA POWER
gives 1736 kWh/m²/yr and Global Solar Atlas gives 1930. No claim here can honestly be
tighter than that spread, so the validation harness reports against a reference *ensemble*
with an explicit band rather than a single asserted truth.

Measured specific yield sits inside the published plausibility band of
1000–1750 kWh/kWp/yr for **30 of 30** city-years, at a mean of 1302.

## Scope

**What this output is:** a screening estimate of technical rooftop potential, useful for
comparing neighbourhoods or sizing a city-level programme.

**What it is not:** an engineering estimate for a specific installation. That needs a site
survey, roof-plane geometry, structural assessment and preferably a year of on-site
measurement.

The limitations that most affect a number — the flat-roof assumption, the 2023 roof
vintage cap, terrain-derived slope, and the absence of any ground truth — are listed with
their consequences in the documentation below and on the site's Limitations page.

## Deployment

Cloud Run in `asia-south1`, keyless via Workload Identity Federation. No service-account
JSON key anywhere. Deployment is manual: run the *Deploy to Cloud Run* workflow and type
`deploy` to confirm.

Two setup steps are invisible in the code and break everything silently:

1. **The Cloud project must be registered with Earth Engine, and the runtime service
   account must hold IAM roles on it.** Enabling the API is not enough; miss either and
   every request 403s. `/api/ready` reports which.
2. **The WIF provider must pin `assertion.repository`.** Without it, any GitHub repository
   can impersonate your deploy identity.

Full runbook, including the one-time `gcloud` setup and the required Actions variables:
[KNOWLEDGE_TRANSFER.md](KNOWLEDGE_TRANSFER.md), section 9.

## Documentation

Everything — architecture, the physics chain with every coefficient and citation, the
validation results and what they do not establish, the ML tracks including the one that
was measured and dropped, the API reference, the deployment runbook, the limitations and
the defect history — is in a single document:

**[KNOWLEDGE_TRANSFER.md](KNOWLEDGE_TRANSFER.md)**

Interactive API docs are at `/docs` on any running instance.

## License

MIT. Dataset licences and attribution requirements are listed in
[KNOWLEDGE_TRANSFER.md](KNOWLEDGE_TRANSFER.md), section 3, and on the site's About page —
CC BY 4.0 and the Copernicus licence both require attribution as a condition of use.
