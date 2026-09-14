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
pip install -e ".[dev,physics]"

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
pytest                      # 405 offline tests; no credentials, no network
pytest -m gee               # 23 live Earth Engine tests; requires auth
ruff check src tests        # lint
ruff format src tests       # format
python -m solaris.evals.harness   # validation report against references
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
- **Soiling still carries most of the loss** — ~63% of modelled loss on a
  representative case, against ~27% shadow and ~10% sky-view. Better than the
  ~88% before the geometry fixes, but the figure still leans on one
  uncalibrated linear coefficient (`mean_AOD × 0.08`) with no rain-washing or
  cleaning-interval term.
- **The urban heat-island layer is weakly constrained.** It uses land *surface*
  temperature as a proxy for *air* temperature via an explicit but uncertain
  0.3 transfer coefficient, and charges only the urban excess — the absolute
  loss against 25 °C STC sits inside the lumped `PERFORMANCE_RATIO`.
- **Sub-year windows are annualised naively** (`× 365.25/days`), with no
  seasonal correction. Not yet fixed.
- **References disagree by ~10%**: NASA POWER gives Delhi 1736 kWh/m²/yr
  (2020–22 mean) against Global Solar Atlas's ~1930. No accuracy claim can be
  tighter than that spread.

## Validation

`python -m solaris.evals.harness` compares the model against independent
references and writes `evals/reports/latest.md`.

Current result — **30/30 city-years fall inside the published plausibility band**
of 1000–1750 kWh/kWp/yr, mean 1302 (range 1103–1428):

| Site | Zone | Specific yield (kWh/kWp/yr) |
|---|---|---:|
| Jodhpur | arid, dusty | 1420 |
| Ahmedabad | semi-arid | 1399 |
| Bengaluru | plateau | 1358 |
| Delhi | composite, high aerosol | 1241 |
| Kolkata | humid subtropical | 1152 |
| Guwahati | high cloud, north-east | 1115 |

The ordering is physically right (arid outyields cloudy), and Delhi's 1241 sits
**8% above** a measured 12 kWp Delhi rooftop at 1147 kWh/kWp/yr — reasonable
given this model applies no tilt gain (+8–12% in north India) and that plant ran
at an unusually high PR of 85–93%.

Specific yield is the chosen metric because `packing_factor` and
`panel_efficiency` cancel out of it, so it tests the irradiance and loss chain
independently of the least defensible constants in the model.

### pvlib reference engine

A second engine runs the same sites through [pvlib](https://pvlib-python.readthedocs.io)
for proper plane-of-array transposition (Hay-Davies) and a computed SAPM cell
temperature, replacing the lumped `0.18 × 0.80 × 0.70` scalar. It resolves the
double-count between `performance_ratio` and the heat-island derate by naming
every loss and computing the ones that are computable — the assumed terms
mirror NREL's PVWatts v5 list, so they're traceable.

| Mount | Mean specific yield | Transposition gain | Mean PR | Mean cell T |
|---|---:|---:|---:|---:|
| `flat` (horizontal roof) | 1252 | 1.000 | 0.752 | 44.8 °C |
| `optimal_fixed` (latitude tilt) | 1340 | 1.075 | 0.749 | 46.0 °C |

**Tilting to the latitude optimum is worth +7.1% annually.** Optimal tilt at
Delhi comes out 28°, matching what PVGIS reports for `optimalangles=1`. Cell
temperatures run 44–52 °C across the Indian sites (Leh 20 °C), giving computed
temperature losses of 7.5–11% — previously buried inside the lumped PR *and*
double-charged against the UHI layer.

`mount` is carried explicitly in every record because the published 1400–1700
band assumes tilted arrays: comparing a horizontal-roof result against it would
read as model bias when much of the gap is a configuration mismatch.

Two things the engine measures rather than assumes:

- **Component closure.** NASA POWER retrieves GHI, DNI and DHI independently, so
  `DNI·cos(z) + DHI` came out **5.26% below GHI** for Delhi. Since pvlib builds
  POA from the components, that put a 5% negative bias on every POA figure — and
  it would have read as model error. The components are now scaled to close
  against GHI, and the invariant that catches it is that POA at zero tilt must
  equal GHI exactly (asserted per site).
- **Climatology discretisation.** The chain runs on a 288-step monthly-diurnal
  climatology rather than 8760 hourly steps. Checked against the independent
  full-year daily series: **mean absolute error 2.6%, worst 8.0%** at the
  monsoon-variable sites.

**One finding worth flagging:** the reference beam fraction averages **0.540**
across 30 city-years (range 0.441–0.634), while the model falls back to 0.60 and
its ERA5 path documents 0.55–0.72. ERA5 uses a monthly aerosol climatology and
is documented to overestimate direct radiation with the error growing in aerosol
load — which is exactly India's regime. That is a quantified motivation for a
bias-correction model.

## License

[MIT](LICENSE) for the code. The Earth Engine datasets remain under their own terms —
Open Buildings CC BY 4.0, ERA5 Copernicus licence, MODIS NASA open data, SRTM public
domain. See [LICENSE](LICENSE) for the full table.
