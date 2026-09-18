# Methodology

Written for: a reviewer who wants to check the physics, and a reader deciding
how much to trust a number this system produces.

Every coefficient below appears exactly once in the codebase, in
`src/solaris/core/constants.py` or in the module that owns it. The frontend
fetches them from `/api/presets` rather than shipping copies.

## The chain

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

## Data sources

| Quantity | Source | Native resolution | Why this one |
|---|---|---|---|
| Roof footprint, height | Open Buildings 2.5D Temporal v1 | ~4 m | The only open building-height product covering India |
| Building polygons | Open Buildings v3 | vector | Footprint selection |
| Global horizontal irradiance | ERA5-Land hourly | ~9 km | Longest consistent record with hourly resolution |
| Beam/diffuse split | ERA5 hourly | ~28 km | ERA5-Land does not publish a direct component |
| Land surface temperature | MODIS MOD11A2 | 1 km | 8-day composite, long record |
| Aerosol optical depth | MODIS MCD19A2 (MAIAC) | 1 km | MAIAC works over bright urban surfaces where dark-target retrieval fails |
| Daily rainfall | ERA5-Land daily aggregates | ~9 km | Needed for soiling; same grid as the irradiance |
| Terrain | SRTM GL1 | 30 m | Slope exclusion |

**The central methodological tension** is in that table: 9 km irradiance is
attributed to 4 m rooftops. Within a single 9 km ERA5 cell every roof receives
the same irradiance, and all spatial variation in the output comes from the
geometry layers. This is stated plainly rather than buried, and the Data &
Satellites page in the web interface visualises the scale gap directly.

## Solar geometry

Sun positions are computed analytically (no Earth Engine), validated against
NREL SPA via pvlib. Measured accuracy: 84.58° against a true 84.83° solstice
peak at Delhi with 30-minute sampling.

Positions are insolation-weighted, so a low-sun hour contributes less than
midday. Weights sum to 1; a regression test asserts that, because an earlier
version thinned the position list with `pos[::2]` without renormalising, which
would have halved the shadow frequency. (The branch never fired — maximum
position count is 39 — so it was dead code concealing a live bug.)

Sampling per mode: yearly 31 positions, quarterly 25, monthly 39, daily 13.

## Shadow

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
*and* the height at one fixed offset was greater. Two conditions referring to
different locations, with azimuth entering only through a single rigid
translate — so the search was effectively isotropic. The docstring self-assessed
at "5–10% high"; in dense urban fabric the real error was much larger.

Two units bugs were fixed in the same rewrite:

- `ee.Image.translate` defaults to **metres**, and the old code passed pixel
  counts. The shadow search was offset 100 m where 400 m was intended.
- Focal kernels in pixel units resolve against the **request** projection, not
  the image's. A 100-pixel kernel spanned ~400 m when reducing at 4 m and
  *kilometres* when rendering a low-zoom tile. The shadow layer a user saw on
  the map was therefore not the shadow layer behind their number.

All distances are now in metres, which removes that class of bug by
construction. A test asserts shadow frequency is invariant across request
scales.

## Sky-view factor

The diffuse counterpart, from the same height raster: the fraction of the sky
hemisphere visible from a rooftop pixel, estimated from horizon angles sampled
at 4, 8, 16, 32 and 64 m.

Diffuse radiation is roughly half of Indian GHI, so this term is not a
refinement. Fixing the units bug moved the sky-view penalty from approximately
zero to a material contribution — currently ~15% of modelled loss.

## Heat island and temperature

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

## Soiling

The published **Kimber** model — which the previous code cited while
hand-rolling something else.

Soiling accumulates at a daily rate while dry, resets on rain above 1 mm, and
saturates at 30%:

```
L(t) = min(rate · t, 0.30),   rate = AOD × 0.00486 per day
```

The rate is calibrated against measured Delhi soiling (0.34%/day in winter) at a
literature annual-mean AOD of 0.70. Seasonal predictions then match published
measurement within 3%, with the correct ordering: spring 0.389%/day worst
(dust-storm season), winter 0.350%, monsoon 0.243% (atmospheric washout lowers
the loading itself).

Deviations from pvlib's defaults, each deliberate:

| Parameter | pvlib | Here | Why |
|---|---|---|---|
| Cleaning threshold | 6 mm | 1 mm | 6 mm is temperate-calibrated and would miss most pre-monsoon showers |
| Grace period | 14 days | 0 days | A fortnight of damp ground is a temperate assumption; it would erase all pre-monsoon soiling in a Delhi year |
| Loss rate | 0.0015/day fixed | derived from AOD | Preserves the spatial variation the project exists to show |

**Validated against pvlib to within 0.003 fraction points** across all ten
cities. Reaching that agreement found three real errors in the closed form; see
`docs/validation.md`.

**What this replaced**, and why tuning could not have saved it: the old model
was `loss = mean_annual_AOD × 0.08`. Unbounded (retention went negative above
AOD 12.5, i.e. negative generated energy), with no rainfall term, and
window-independent — the same annual loss for a single day as for a year.

The rainfall term is not a refinement. Across the evaluation cities,
cleaning-rain days per year range from 59 (Jodhpur) to 195 (Guwahati), a factor
of three in how often a roof gets washed, and the old model gave both the same
answer at equal aerosol.

**A documented approximation.** Without a daily rainfall series the model must
assume every dry spell is the mean length. Because accumulation is convex in
spell length, that understates the loss by **1.8× to 12.6×** on real Indian
rainfall — worst in the arid cities where soiling matters most. The serving path
therefore spends one extra Earth Engine round-trip on the actual series, and
results from the fallback path carry an explicit note calling themselves a lower
bound.

## PV conversion

**Transposition.** `pvlib.irradiance.get_total_irradiance` with **Hay-Davies**,
not Perez. Perez needs a well-behaved sky-clearness pair and degrades when
DNI/DHI quality is poor, which is exactly ERA5-derived DNI over urban India.
Perez is reported as a sensitivity rather than used as the default.

**Cell temperature.** `pvlib.temperature.sapm_cell` — implementing the
NOCT-style model the old docstrings described but never implemented.

**Discretisation.** A monthly-diurnal climatology, 12 × 24 = 288 records.
Transposition and cell temperature are both non-linear in instantaneous
irradiance, so neither can be applied to an annual total; a full 8760-hour year
per request is not servable. The discretisation error against full hourly is
measured, not assumed.

**Mount is carried explicitly in every record.** Flat versus latitude-optimal
tilt is worth 8–12% annually in north India. Comparing a flat-roof figure
against a reference computed at optimal angles would look like a large model
bias when it is purely a configuration mismatch — a confound that could
otherwise have swallowed the entire validation. Both are reported:
`mount="flat"` (what this model actually assumes) and `mount="optimal_fixed"`
(what an installer would build), with
`optimal_tilt = 0.87·|latitude| + 3.1`.

**The performance ratio is decomposed.** `PR = 0.80` was one scalar bundling
temperature, inverter, wiring, mismatch, soiling and availability — which made
it ambiguous against the separate soiling and heat-island layers and possibly
double-counted. It is now named terms, reported separately, with assumed and
computed components distinguished.

`AVAILABILITY_LOSS = 0.0`, deliberately: this estimates technical potential
across a city's roofs, not the output of one plant with one maintenance
schedule.

## Documented approximations

Each is a real simplification, stated here rather than discovered by a reader.

- **Coordinate quantisation.** Cache keys round latitude, longitude and
  half-size to 4 decimal places (~11 m). Far below ERA5's 9 km cell and below
  the meaningful sensitivity of an area-of-interest *centre*, but it means two
  clicks 10 m apart return the identical result.
- **Terrain slope, not roof pitch.** The 30° exclusion derives from SRTM at
  30 m, so it excludes buildings on steep *terrain*, not steep *roofs*. Tracked
  as limitation P7 and not fixed — see `docs/limitations.md`.
- **Annualisation.** Windows shorter than 28 days get no annualised figure at
  all, rather than a December day multiplied by 365.
- **Soiling AOD anchor.** The deposition coefficient is one measured rate
  divided by one *assumed* Delhi annual-mean AOD. The rate is measured; the AOD
  is a literature value, not a retrieval from this pipeline.
