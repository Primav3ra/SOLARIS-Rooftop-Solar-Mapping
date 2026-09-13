<h1 align="center">SOLARIS</h1>
<p align="center">
  Rooftop solar PV yield mapping for urban India.<br>
  Built on Google Earth Engine, FastAPI, and MapLibre GL.
</p>
<p align="center">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/api-FastAPI-009688?logo=fastapi&logoColor=white">
  <img alt="MapLibre GL" src="https://img.shields.io/badge/map-MapLibre%20GL-295DAA?logo=maplibre&logoColor=white">
  <img alt="Google Earth Engine" src="https://img.shields.io/badge/geo-Earth%20Engine-34A853?logo=googleearth&logoColor=white">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-green">
  <img alt="resolution" src="https://img.shields.io/badge/roof%20resolution-4m-brightgreen">
</p>

---

<img width="1917" height="928" alt="SOLARIS dashboard" src="https://github.com/user-attachments/assets/7af6d917-0f41-4aed-8d67-d28a4ff892be" />

Geospatial pipeline and web app that estimates **rooftop solar PV yield** for urban
planning from **Google Earth Engine** datasets, served through a **FastAPI** backend and an
interactive **MapLibre GL** dashboard.

## What it does

- Builds a rooftop candidate mask from Open Buildings 2.5D (4 m) with terrain-slope exclusion
- Computes a global horizontal irradiance baseline from ERA5-Land for a user-selected window
- Applies four physics-inspired penalty layers — beam shadowing, diffuse sky-view
  obstruction, urban-heat-island temperature derate, and aerosol-driven soiling
- Returns per-building yield with a stage-by-stage breakdown of every penalty

## Architecture

```
Google Earth Engine
  ├─ Open Buildings 2.5D Temporal (4 m)  → rooftop mask + heights
  ├─ Open Buildings v3 Polygons (vector) → building footprints
  ├─ ERA5-Land Hourly (~9 km)            → GHI baseline
  ├─ ERA5 Hourly (~28 km)                → direct/diffuse split
  ├─ MODIS MOD11A2 (1 km)                → daytime LST for UHI
  ├─ MODIS MCD19A2 (1 km)                → MAIAC AOD for soiling
  └─ SRTM (30 m)                         → terrain slope exclusion

src/solaris/
  core/constants.py   every physical and dataset constant, with provenance
  gee/                Earth Engine accessors and the physics layers
  api/
    app.py            request handlers
    windows.py        temporal window resolution (no Earth Engine dependency)
    schemas.py        request models and their bounds
    deps.py           Earth Engine session, AOI, rooftop layers
    static/           the dashboard
tests/
  unit/               offline; no credentials, no network
  integration/        marked `gee`; requires Earth Engine auth
  fakes/              numpy-backed fake Earth Engine + synthetic fixtures
```

The net-energy formula, with each factor's provenance, is:

```
E = GHI_period
    × roof_area
    × [ diffuse_fraction × SVF  +  beam_fraction × (1 − shadow_frequency) ]
    × uhi_derate
    × soiling_retention
    × panel_efficiency × performance_ratio × packing_factor
```

## Setup

**Prerequisites**

- Python 3.11+
- A Google Earth Engine account
- A Google Cloud project with the Earth Engine API enabled **and registered**
  (noncommercial or commercial)

```bash
python -m venv .venv

# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# Linux / macOS / WSL:
source .venv/bin/activate

# Install the package (editable) plus dev tooling
pip install -e ".[dev]"

earthengine authenticate

cp .env.example .env     # then set GEE_PROJECT_ID
```

Configuration is read from the environment; see [.env.example](.env.example) for every
variable. `GEE_PROJECT_ID` is the only required one.

## Run locally

```bash
solaris-api
# or, equivalently:
uvicorn solaris.api.app:app --reload --port 8000
```

Open `http://localhost:8000`. The server no longer depends on the working directory —
static assets resolve relative to the package.

## Build the intro bundle (optional)

```bash
cd frontend/intro && npm ci && npm run build
```

The dashboard degrades gracefully to a CSS-only header if the bundle is absent.

## API

| Endpoint | Method | Description |
|---|---|---|
| `/api/health` | GET | Liveness check |
| `/api/presets` | GET | Supported modes and year bounds |
| `/api/baseline` | POST | AOI rooftop area + ERA5 irradiance summary |
| `/api/yield` | POST | Per-building PV yield with penalty breakdowns |
| `/api/series` | POST | Generation curve over the selected window |
| `/api/tiles` | POST | XYZ tile templates for six raster overlays |
| `/api/buildings` | POST | Open Buildings footprints as GeoJSON |

Interactive OpenAPI docs are at `/docs`.

### Temporal modes

Set via `baseline_mode`:

| Mode | Required fields | Window |
|---|---|---|
| `yearly` | `year` | Full calendar year |
| `quarterly` | `year`, `quarter` (1–4) | Three calendar months |
| `monthly` | `year`, `month` (1–12) | One calendar month |
| `daily` | `start_date`, `end_date_exclusive` | Exactly one calendar day |

All windows are UTC, to line up with ERA5.

### Request limits

AOIs are capped at **30 km²** (`half_size_deg` ≤ 0.025, ≤ 100 polygon vertices). The Earth
Engine project is server configuration and is **not** accepted from the request body.

## Using the dashboard

1. Click a building on the map to define an AOI.
2. Choose a temporal range.
3. **Compute** returns the yield plus the full penalty cascade and a generation curve.

## Development

```bash
pytest                      # 317 offline tests; no credentials, no network
pytest -m gee               # 23 live Earth Engine tests; requires auth
ruff check src tests        # lint
ruff format src tests       # format
```

**A fresh clone with no Google Cloud account must get a green `pytest`.** The
offline suite achieves that with a numpy-backed fake Earth Engine
([tests/fakes/fake_ee.py](tests/fakes/fake_ee.py)) in which every operation is
real arithmetic on real arrays, so a wrong formula produces a wrong number. A
synthetic "world" ([tests/fakes/world.py](tests/fakes/world.py)) registers every
dataset the API reads, letting all five endpoints run end to end offline and
giving a deterministic golden file to regress against.

Some tests are marked `xfail(strict=True)`. Those are **executable
specifications of known defects**: they assert the physically correct answer,
which the current code does not yet produce. Because the marker is strict,
fixing a defect turns the test from xfail into a failure until the marker is
removed — so the defect list cannot drift out of date in either direction.

The live suite includes **semantics probes** that assert what the Earth Engine
API actually does, for the behaviours the fake reproduces. If a probe fails, the
fake is wrong and every offline test built on it is suspect.

## Known limitations

Stated plainly, because they bound how the numbers should be read:

- **Roofs are treated as horizontal.** GHI is used directly as plane-of-array irradiance —
  no tilt transposition, albedo, or incidence-angle modifier.
- **PV conversion is one lumped scalar** (`0.18 × 0.80 × 0.70`). No temperature-dependent
  efficiency; the UHI layer charges only the *urban excess* temperature, not the absolute
  loss against 25 °C STC.
- **A 9 km irradiance cell is attributed to 4 m rooftops.** This scale gap is the project's
  central methodological tension.
- **Soiling has no rain-washing or cleaning-interval term**, though Indian soiling is
  strongly monsoon-modulated.
- **Terrain slope comes from a 30 m DEM**, so it excludes buildings on steep *terrain*, not
  buildings with steep *roofs*.
- **Recency is bounded** by ERA5-Land's publication lag and by Open Buildings 2.5D vintages
  (2016–2023), so rooftop geometry is 2023 at newest.
- **The shadow and sky-view geometry is wrong by a factor of the pixel size.**
  `ee.Image.translate` defaults to metres and both models pass pixel counts.
  Measured effect: a 10 m wall 4 m away yields a sky-view factor of 0.9649
  against an analytic 0.8922 — exactly the value obtained by substituting 16 m
  for 4 m. Fix pending; see `pytest -q tests/unit/test_penalties.py -rx`.
- **Soiling dominates the result.** On a representative case it accounts for
  ~88% of all modelled loss, against ~8% for shadowing and ~4% for sky-view
  obstruction. So the headline figure rests largely on one uncalibrated linear
  coefficient (`mean_AOD x 0.08`) rather than on the geometric modelling.
- Outputs have **not yet been validated** against independent references. Note
  also that the two obvious references disagree: NASA POWER gives Delhi
  1753 kWh/m²/yr for 2020 while Global Solar Atlas gives ~1930 — about 10% apart.
  No accuracy claim can be tighter than that spread. Treat absolute figures as
  indicative.

## License

[MIT](LICENSE) for the code. The Earth Engine datasets remain under their own terms —
Open Buildings CC BY 4.0, ERA5 Copernicus licence, MODIS NASA open data, SRTM public
domain. See [LICENSE](LICENSE) for the full table.
