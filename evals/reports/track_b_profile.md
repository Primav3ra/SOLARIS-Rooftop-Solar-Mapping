# ML Track B: shadow/sky-view surrogate -- profiled, then dropped

Generated 2026-09-18T06:38:22Z

**Verdict: not built.**

The plan gated this track on measuring where /api/yield latency goes, and named dropping it as a legitimate outcome.

## What was measured

- Round-trips made by one `/api/yield`: **12**
- Round-trips the budget declares for it: 12
- Of those, attributable to shadow or sky-view compute: **3**
- Round-trips remaining after a *perfect* surrogate: **12** (reduction: 0)

## Why that settles it

- A surrogate cannot reduce the round-trip count. /api/yield makes 12 getInfo calls; a perfect surrogate still makes 12, because it changes what a reduction computes, not whether the reduction has to be fetched.
- Only 3 of those 12 involve shadow or sky-view compute at all, so even if server-side compute were the whole of the latency the ceiling is a 1.33x speedup -- and that assumes the surrogate's own feature rasters are free, which they are not.
- The surrogate's information content is the directional horizon profile. Given that profile, 'is this pixel shadowed' is the analytic identity horizon_angle(sun_azimuth) > sun_altitude. A boosted tree approximating one comparison is strictly worse than the comparison, so the track can only be justified on speed -- which is what the arithmetic above rules out.
- Caching already addresses the latency this was meant to address, and addresses it better: a cache hit costs zero round-trips rather than a fraction of one stage's compute.

## What would change the answer

A live profile showing that a single shadow reduction dominates wall-clock time -- for instance if a city-scale AOI made the directional trace time out server-side. That is a different use case from the single-building query this endpoint serves, and the input bounds cap the AOI at 30 km2 precisely to keep it out of that regime. Re-run this with --live against such an AOI before reconsidering.

## What was done instead

The exact shadow model is kept, and the accuracy work went into the soiling and decomposition tracks, where there was a measured defect to fix rather than a speed ceiling to chase.

## Live stage timings

| Stage | ms |
|---|---:|
| baseline_trivial_getinfo | 445.8 |
| build_roof_layers | 3.5 |
| shadow_frequency_reduce | -1.0 |
| shadow_frequency_reduce_error | EEException: User memory limit exceeded. |
| sky_view_factor_reduce | 3168.8 |
| roof_area_reduce | 629.1 |

