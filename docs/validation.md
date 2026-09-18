# Validation

Written for: an examiner asking "are these numbers right, and how would you
know?"

Generated reports live in `evals/reports/`. This page explains what they mean
and what they cannot establish. Regenerate with:

```bash
python -m solaris.evals.harness          # irradiance, beam fraction, yield
python -m solaris.evals.soiling_suite    # soiling vs published rates and pvlib
python -m solaris.evals.profile_yield    # where the round-trips go
python -m solaris.ml.train               # the model ladder
```

## The constraint that governs everything here

**The two credible free references for India disagree by about 10%.** For Delhi,
NASA POWER gives 1736 kWh/m²/yr and Global Solar Atlas gives 1930 — a spread of
194, or 10.6%.

No accuracy claim in this project can honestly be tighter than that spread. So
the harness reports against a reference *ensemble* with an explicit band, never
against a single asserted truth, and the spread is printed on every run as a
standing constraint rather than a footnote.

This is the most important design decision in the validation, and it was forced
by the data rather than chosen.

## Reference selection, including what did not work

**PVGIS-SARAH3 does not cover India.** The documentation advertises "Europe,
Africa and Asia", so it was the intended primary reference. Probing the live
v5_3 API for Mumbai, Ahmedabad, Jaipur, Delhi, Bengaluru, Kolkata and Jodhpur
returned **HTTP 400 "Location out of the spatial coverage" for all seven**.
SARAH3 is the Meteosat prime-disk product, roughly ±65° longitude.

PVGIS-ERA5 *is* reachable, but it is ERA5 — the same data this model consumes —
so using it as truth would be circular.

That left:

- **NASA POWER** (satellite-derived, 30 city-years cached and committed). It
  also serves diffuse and direct components, which makes the beam fraction —
  the model's most ERA5-sensitive input — directly checkable rather than merely
  plausible.
- **Global Solar Atlas / Solargis** long-term averages, as a held-out third
  reference.
- **Published measured plant performance**, the only genuinely independent
  reference, used as a plausibility band.

## Specific yield against published plants

Specific yield (kWh/kWp/yr) is the comparison that matters, for an algebraic
reason: packing factor and module efficiency cancel out of it. So it isolates
the irradiance and loss chain from the capacity assumptions, which are the least
defensible part of the model.

- Published plausibility band: **1000–1750 kWh/kWp/yr**
- Model: mean **1302**, range **1103–1428**
- Inside the band: **30 / 30 city-years (100%)**

Against the single best-documented measurement — a 12 kWp Delhi rooftop plant at
**1147 kWh/kWp/yr** — the model gives 1256 for Delhi 2020, i.e. **+8%**.

Two things to note about that comparison, both of which cut against reading it
as an accuracy claim:

1. That plant reports a performance ratio of 0.85–0.93, above the typical
   Indian year-one range of 0.78–0.83. It is one well-maintained installation,
   not a population.
2. It is a *tilted* array; this model treats every roof as horizontal. Latitude
   tilt is worth 8–12% at Delhi, which is the same size as the discrepancy and
   in the same direction.

So +8% against a tilted, well-maintained plant is consistent with the model
being approximately right, and is **not** evidence that it is accurate to 8%.
The honest statement is the band: 30/30 inside 1000–1750.

## Beam fraction

The most ERA5-sensitive input. ERA5 uses a monthly aerosol climatology and is
documented to overestimate direct radiation while underestimating diffuse, with
the error growing with aerosol load — precisely India's regime.

Measured: the reference mean over 30 city-years is **0.540**, range 0.441–0.634.
The model's ERA5 path documents 0.55–0.72 and its fallback was a constant
**0.60**. So ERA5 reads high, exactly as the literature predicts, and the
constant reads high too.

This measurement is what motivated the decomposition model below.

## The decomposition model

A learned beam/diffuse split, replacing that 0.60 constant.

| Rung | Test RMSE | MBE | Skill vs Erbs |
|---|---:|---:|---:|
| `constant` (the 0.60 in production) | 0.2540 | −0.139 | −0.78 |
| `climatology` (36 parameters, no ML) | 0.1958 | +0.015 | −0.37 |
| **`erbs`** (published, 1982) | 0.1426 | +0.077 | 0.00 |
| `ridge` | 0.1097 | +0.014 | +0.23 |
| **`gradient_boosting`** | **0.0893** | +0.015 | **+0.37** |

**The baseline is Erbs, not the constant.** Beating a fixed number proves
nothing; Erbs is a published correlation validated worldwide for four decades,
and a learned model that cannot beat it is not worth shipping or maintaining.

**The 0.10 skill gate was declared in the module before training**, so it could
not be relaxed to suit the result. The gradient-boosted model clears it at
+0.37.

**The holdout is spatial *and* temporal**: three held-out cities × a held-out
year, with the closest test/train site pair verified above 250 km. Hours within
a city-day are strongly correlated, so a random row split would test on hours
whose neighbours were trained on and report a score that says nothing about
generalisation.

Two findings beyond the ranking:

- The constant's MBE of **−0.139** quantifies the defect it replaces.
- **Erbs is itself biased for India**, +0.077 — it over-predicts diffuse
  fraction, consistent with being fitted on US aerosol conditions. The learned
  model cuts that to +0.015.

Serving falls through `model → Erbs → constant`, always reporting which rung
answered. Out-of-domain inputs fall back to Erbs, because a boosted tree does
not extrapolate — it returns the nearest leaf, confidently and baselessly.
Verified: latitude 75°N falls back correctly.

## Soiling

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

**Against pvlib.** `pvlib.soiling.kimber` is an independent implementation of
the same published model. Worst absolute disagreement across all ten cities:
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
3. The accumulation was being integrated continuously where the model is
   defined per day.

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

## ML Track B: measured, then dropped

The shadow/sky-view surrogate was specified as a *speed* optimisation and
explicitly gated on profiling `/api/yield` first, with dropping it named in
advance as a legitimate outcome.

Measured: `/api/yield` makes **12** Earth Engine round-trips, of which **3**
involve shadow or sky-view compute. A perfect surrogate still makes 12, because
it changes what a reduction computes, not whether the reduction must be
fetched — so the round-trip reduction is **zero** and the ceiling, even if
server-side compute were the whole of the latency, is a **1.33× speedup**. That
assumes the surrogate's own feature rasters are free, which they are not.

There is also a structural argument. The surrogate's information content is the
directional horizon profile, and given that profile "is this pixel shadowed" is
the analytic identity `horizon_angle(sun_azimuth) > sun_altitude`. A boosted
tree approximating one comparison is strictly worse than the comparison. So the
track could only ever be justified on speed, which the arithmetic rules out.

Dropped, with the report committed at `evals/reports/track_b_profile.md` and the
condition that would reverse the decision stated in it.

## Cross-validation against pvlib

The physics layer is checked against pvlib's own documented examples, and the
288-record monthly-diurnal discretisation is measured against full hourly rather
than assumed adequate. See `evals/reports/` and `src/solaris/evals/pvlib_suite.py`.

## Two errors the validation itself caught

Worth recording, because both would have propagated into every number.

**NASA POWER's hourly endpoint defaults to local solar time.** Reading
LST-stamped data as UTC shifts every timestamp by longitude/15 hours — 5.1 h for
Delhi — putting the solar position four hours out and making any transposition
meaningless. Found empirically rather than from the documentation: correlating
GHI against sin(solar altitude) computed in UTC gives **+0.994** with
`time-standard=UTC` passed explicitly, against **+0.141** under the default.

**NASA POWER's independently-retrieved GHI, DNI and DHI do not close.** The
components sum 5.26% below the reported GHI for Delhi, which put a uniform 5%
negative bias on every plane-of-array figure. This had already led to a *wrong
conclusion* — a test asserting that low-latitude tilt loses energy. After a
GHI-preserving rescale, every tilt gain exceeds 1.0, as it should.

## What this validation does not establish

Stated explicitly, because a validation section that only lists successes is not
a validation section.

- **No ground-truth measurement.** There is no pyranometer or metered plant in
  the loop. Every reference is satellite-derived or published aggregate.
- **No per-building verification.** The 4 m geometry is never checked against a
  surveyed roof. The validation is at city scale.
- **Accuracy is bounded by 10%** — the reference disagreement — and cannot be
  claimed tighter regardless of how well the model matches any single source.
- **The soiling calibration rests on one assumed AOD value**, and the per-city
  soiling ranking inherits that uncertainty. The rainfall-driven results do not.
- **The ERA5→reference bias correction proper was not built.** It needs Earth
  Engine credentials that were unavailable; the reference side (the
  decomposition model) targets the same defect from the other end.
- **The decomposition model is trained on NASA POWER**, so it cannot be
  validated against NASA POWER. Independent validation would need Global Solar
  Atlas or ground stations as a held-out reference.
