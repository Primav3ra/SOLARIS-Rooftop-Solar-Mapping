# Machine learning

Written for: a reviewer assessing whether the ML in this project earns its
place.

Three tracks were planned. **Two shipped, one was measured and dropped.** The
dropped one is described at the same length as the others, because the decision
not to build it was the more useful piece of work.

## The standard applied to all three

A model here has to beat the **published method**, not the naive one. Beating a
constant proves nothing; a published correlation validated worldwide for decades
is the bar worth clearing.

And the gate is declared **before** training, in the module above the training
code, so it cannot be relaxed to suit the result. With a few thousand samples
from ten sites it would be easy to produce a gradient-boosting model with a
flattering score and no real advantage — committing to the threshold up front is
what makes the comparison mean anything.

## Track A — beam/diffuse decomposition

**The defect, measured.** The model's beam fraction is its most ERA5-sensitive
input. The validation harness put the reference mean at **0.540** across 30
city-years, while the production code fell back to a constant **0.60** and its
ERA5 path documented 0.55–0.72. ERA5 uses a monthly aerosol climatology and is
documented to overestimate direct radiation with the error growing in aerosol
load — which is exactly India's regime.

**A necessary change of scope.** The plan's Track A was an ERA5 → reference bias
correction. That needs ERA5 data, which needs Earth Engine credentials that were
unavailable. The *reference* side was buildable, and it targets the same defect
from the other end: learn the diffuse fraction from cheap, always-available
features, which both replaces the constant and produces the target series an
ERA5 correction would later train against.

### Features, and why each one

Every published correlation in this space — Erbs (1982), Reindl (1990),
Boland-Ridley-Lauret — is driven by the **clearness index**
`Kt = GHI / extraterrestrial horizontal`. That is the physically right primary
predictor: it measures how much the atmosphere removed, and diffuse fraction
runs near 1 under thick cloud and near 0.15 under clear sky.

| Feature | Reason |
|---|---|
| `kt` | The primary predictor, as in every published correlation |
| `sin_elevation`, `air_mass` | Air mass changes diffuse fraction at a given Kt; low sun scatters more. Kasten-Young rather than `1/sin(elevation)`, which diverges at the horizon |
| `sin_doy`, `cos_doy` | Cyclical, not an integer. An integer lets a tree carve arbitrary date groupings; the pair also makes December adjacent to January, which it is |
| `abs_latitude` | Proxy for climate regime |
| `kt_persistence` | Kt relative to the day's mean. Broken cloud gives a high diffuse fraction at moderate Kt, which instantaneous Kt cannot distinguish from thin uniform haze |

**Deliberately excluded: aerosol optical depth.** It would very likely help —
aerosol is the mechanism behind India's high diffuse fraction — but it comes
from MODIS via Earth Engine, so requiring it would make the model unusable in
exactly the fallback case it exists to serve.

### The ladder

| Rung | Test RMSE | MBE | Skill vs Erbs |
|---|---:|---:|---:|
| `constant` (the 0.60 in production) | 0.2540 | −0.139 | −0.78 |
| `climatology` (36 parameters, no ML) | 0.1958 | +0.015 | −0.37 |
| **`erbs`** (published, 1982) | 0.1426 | +0.077 | 0.00 |
| `ridge` | 0.1097 | +0.014 | +0.23 |
| **`gradient_boosting`** | **0.0893** | +0.015 | **+0.37** |

Five rungs, all reported, **simplest winner ships**. The gate —
`MIN_SKILL_OVER_ERBS = 0.10` — was declared before training. The
gradient-boosted model clears it at +0.37.

Why gradient boosting and not a neural network: a few thousand tabular rows with
heterogeneous feature scales and a likely non-linear interaction between
clearness and air mass is what trees handle well, and a net on this much data
would be indefensible. Modest depth (4) and a leaf floor (40) because with ten
sites a deeper model memorises sites rather than learning physics.

### The holdout

**Spatial *and* temporal.** Three held-out cities × a held-out year, with the
closest test/train site pair verified above 250 km. Hours within a city-day are
strongly correlated, so a random row split would test on hours whose neighbours
were trained on and report a score saying nothing about generalisation.

### Two findings beyond the ranking

- The constant's MBE of **−0.139** quantifies the defect it replaces.
- **Erbs is itself biased for India**: +0.077, over-predicting diffuse fraction,
  consistent with being fitted on US aerosol conditions. The learned model cuts
  that to +0.015. This is a result about the published correlation, not just
  about our model.

### Serving

`model → Erbs → constant`, always reporting which rung answered.

Erbs sits in the middle deliberately: it needs no artifact, no training data and
no dependency beyond arithmetic, so it is always available — which makes the
constant a genuine last resort rather than the first fallback it used to be.

**A domain check guards the learned rung.** A gradient-boosted tree does not
extrapolate; it returns the nearest leaf, confidently and with no basis. A
per-feature range check against the training envelope recorded at fit time sends
out-of-domain inputs to Erbs instead. Verified: latitude 75°N falls back
correctly.

Artifacts are committed with a manifest recording what shipped and on what
evidence, so "which model is live, and how good was it?" is answerable from the
repository without loading a pickle.

## Track C — soiling

**Reframed from ML to parametric, deliberately.** There is no open Indian
PV-soiling label set, so training a soiling model end to end would be a curve fit
dressed as machine learning. The honest version is the published **Kimber**
model — which the previous code already *cited* while hand-rolling something
else — calibrated against measured Indian rates.

Calling this a parametric physics model rather than ML is the point. Full detail
in `docs/methodology.md`; validation in `docs/validation.md`.

Headline results:

- Seasonal rates match published Delhi measurements within **3%**, with the
  correct ordering (spring worst, monsoon best).
- Agreement with `pvlib.soiling.kimber` to **0.003 fraction points** across ten
  cities. Reaching it found three real errors in the closed form.
- A **21.9× spread** across Delhi's months, where the old model returned one
  figure for all of them.
- The change is **not uniformly downward**: for Jodhpur and Ahmedabad the new
  model predicts *more* soiling despite lower aerosol, because 125- and 135-day
  unbroken dry spells are invisible to an AOD-only model. The old model was
  optimistic in arid India, the climate where soiling does most damage.

The dropped PM2.5 step: estimating ground-level PM2.5 from MAIAC aerosol plus
meteorology is a well-established regression with abundant CPCB labels, and it
would feed `pvlib.soiling.hsu()`. It is the longest chain and weakest-evidenced
of the three tracks, and calibrating Kimber against published rates is already a
strict improvement on `0.08 × AOD`. Descoped, not attempted and abandoned.

## Track B — shadow/sky-view surrogate: measured, then dropped

Specified as a **speed** optimisation, never an accuracy one, and explicitly
gated: measure where `/api/yield` latency goes first, and drop the track if
server-side shadow compute is not the bottleneck. Dropping it was named in
advance as a legitimate outcome.

**The measurement.** `/api/yield` makes **12** Earth Engine round-trips, of
which **3** involve shadow or sky-view compute. A perfect surrogate still makes
12, because it changes what a reduction computes, not whether the reduction must
be fetched. So:

- round-trip reduction: **zero**
- ceiling, even if server-side compute were the whole latency: **1.33×**
- and that assumes the surrogate's own feature rasters are free, which they are
  not.

**The structural argument, which is the more interesting one.** The surrogate's
information content *is* the directional horizon profile. Given that profile,
"is this pixel shadowed" is the analytic identity
`horizon_angle(sun_azimuth) > sun_altitude`. A boosted tree approximating one
comparison is strictly worse than the comparison — slower to serve, less
accurate, and needing a training pipeline. So the track could only ever have
been justified on speed, which the arithmetic rules out.

Caching also addresses the same latency, and addresses it better: a cache hit
costs zero round-trips rather than a fraction of one stage's compute.

**What would reverse this:** a live profile showing a single shadow reduction
dominating wall-clock time — for instance a city-scale area of interest making
the directional trace time out server-side. That is a different use case from
the single-building query this endpoint serves, and the input bounds cap the area
at 30 km² specifically to stay out of that regime. Re-run
`python -m solaris.evals.profile_yield --live` against such an area before
reconsidering.

Report committed at `evals/reports/track_b_profile.md`.

## Reproducing

```bash
python -m solaris.evals.fetch --hourly --years 2020 2021 2022
python -m solaris.ml.train
python -m solaris.evals.soiling_suite
python -m solaris.evals.profile_yield
```

Reports land in `ml/reports/` and `evals/reports/`. The web interface imports
them at build time, so the ML and Validation pages cannot drift from the
committed results.
