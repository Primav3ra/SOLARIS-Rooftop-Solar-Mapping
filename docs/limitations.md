# Limitations

Written for: anyone deciding what this system's output can and cannot be used
for.

This page exists because a project that only documents its strengths is harder
to trust, not easier. Everything below is either resolved-and-recorded or open-
and-unfixed, and the open ones are the honest reason not to treat an output as
an engineering estimate.

## The one that matters most

**9 km irradiance is attributed to 4 m rooftops.** ERA5-Land's native cell is
about 9 km, so within a single cell every roof receives identical irradiance.
All spatial variation in the output comes from the geometry layers — shadow,
sky-view, heat island, aerosol — not from the irradiance.

This is not a bug to be fixed; it is the resolution of the best open dataset
available for India. But it bounds what a per-building number means: the
geometry is building-specific, the meteorology is neighbourhood-scale at best.

## Open limitations

| # | Limitation | Consequence |
|---|---|---|
| **P1** | Every roof is treated as **horizontal**. No tilt, azimuth or incidence-angle modifier in the served path. | Understates a well-installed array by 8–12% at north-Indian latitudes. The pvlib layer can compute tilted output and the eval reports both, but the map assumes flat. |
| **P6** | Annual shadow geometry rests on **31 sampled sun positions**, not 8760 hours. | Fine for an annual mean; it cannot resolve a specific morning. |
| **P7** | Roof slope is taken from **SRTM at 30 m**, so the 30° exclusion excludes buildings on steep *terrain*, not steep *roofs*. | A scale mismatch against the 4 m roof raster. Deliberately not fixed — a correct version needs roof-plane fitting from the height raster, which is a project in itself. |
| **P9** | No **inter-row shading** within an array, and no obstruction modelling (water tanks, stairwell housings, parapets). | The packing factor of 0.70 absorbs this as a lumped assumption rather than modelling it. |
| — | **Roof vintage is capped at 2023.** Open Buildings 2.5D Temporal v1 runs 2016–2023. | Buildings completed after 2023 are invisible regardless of the irradiance window selected. A 2026 query uses 2023 geometry. |
| — | **No ground truth.** No pyranometer, no metered plant. | Every reference is satellite-derived or a published aggregate. See `docs/validation.md`. |
| — | **Accuracy is bounded at ~10%** by the disagreement between the available free references. | No tighter claim is honest, however well the model matches any single source. |
| — | **The soiling calibration rests on one assumed AOD value.** | The rate is measured; the AOD it is anchored on is a literature figure, not a retrieval from this pipeline. Per-city soiling *ranking* inherits that uncertainty; the rainfall-driven results do not. |
| — | **Building confidence is not a probability.** The Open Buildings presence threshold of 0.5 is a prior, not a calibrated likelihood. | Roof area carries an unquantified inclusion error. |
| — | **Coordinate quantisation to ~11 m.** Cache keys round to 4 decimal places. | Two clicks 10 m apart return the identical result. A deliberate trade: without it the cache hit rate would be near zero. |

## Not built, and why

**ML Track B — shadow/sky-view surrogate.** Profiled, then dropped. `/api/yield`
makes 12 Earth Engine round-trips, of which 3 involve shadow compute; a perfect
surrogate still makes 12, so the round-trip reduction is zero and the ceiling is
a 1.33× speedup. The full measurement and the condition that would reverse the
decision are in `evals/reports/track_b_profile.md`.

**ERA5 → reference bias correction.** The originally-planned Track A. It needs
Earth Engine credentials to build the ERA5 side of the training set, which were
unavailable. The decomposition model targets the same defect — ERA5's
overestimate of the direct component — from the reference side instead.

**Sign-in, in any form.** Google sign-in was implemented and then removed: it
moved no Earth Engine quota, had no persistence tier to attach an identity to,
and had no interface element from which to sign in. Shifting quota onto a
visitor would need the `earthengine` scope, which is classed sensitive —
requiring Google verification review and capping an unverified application at
roughly 100 users. Sessions are keyed on an opaque token instead; see
`src/solaris/api/auth.py`.

**Per-building validation.** The 4 m geometry is never checked against a
surveyed roof. Validation is at city scale only.

## Resolved, and recorded

Kept here because knowing a system's failure history is part of judging it, and
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
| **R5** | The temporal bound was calendar-derived (`today.year − 1`). | Q2 2026 was refused two and a half months after it ended. Now probed from ERA5-Land's actual last image. |
| **P3/P4** | Temperature loss counted only as urban excess, with land surface temperature used as air temperature. | Two errors of opposite sign, neither measured: surface heat island runs 2–3× the air heat island, while the absolute 35 °C-vs-25 °C loss was absent, folded ambiguously into `PR = 0.80`. The headline number could look reasonable with both components wrong. |
| **P5** | Soiling had no rain-washing or cleaning-interval term. | One annual coefficient applied unchanged to windows from one day to one year. |
| **P8** | Shade-matrix buckets were labelled UTC. | The "08-12" bucket is really 13:30–17:30 IST. Both labels are now carried. |

Also fixed, in the infrastructure: a project-id leak in 500-level error detail
(`HTTPException` is handled by FastAPI before any middleware, so this needed its
own handler); `CORS allow_origins=["*"]` paired with `allow_credentials=True`,
which is invalid per the CORS spec and so never granted the access it appeared
to; a client-supplied `project_id` that let any caller choose which GCP project
the server initialised; an unlocked module global in `_ensure_ee` that two
concurrent cold requests could both initialise through; and a relative
`StaticFiles` path that made the server work only when launched from the repo
root.

## Errors found in the reference data itself

Both would have propagated silently into every number.

**NASA POWER's hourly endpoint defaults to local solar time**, not UTC. Reading
it as UTC shifts every timestamp by longitude/15 hours — 5.1 h for Delhi.
Correlation of GHI against sin(solar altitude) in UTC: **+0.994** with
`time-standard=UTC` passed explicitly, **+0.141** under the default.

**NASA POWER's GHI, DNI and DHI do not close**, summing 5.26% below the reported
GHI for Delhi. This put a uniform 5% negative bias on every plane-of-array
figure and had already produced a *wrong conclusion* — a test asserting that
low-latitude tilt loses energy.

**NASA POWER over high terrain is unreliable.** It reports 458 mm/yr for Leh
against an actual figure near 100 mm. Leh is retained in the evaluation set as a
low-aerosol contrast case, but its soiling figures should not be read as
meaningful.

## What this output is, and is not

**It is** a screening estimate of technical rooftop solar potential — useful for
comparing neighbourhoods, sizing a city-level programme, or seeing how much
shadow and aerosol cost a given area.

**It is not** an engineering estimate for a specific installation. For that you
need a site survey, roof-plane geometry, structural assessment, local
obstruction mapping, and preferably a year of on-site measurement. Nothing here
substitutes for any of those.
