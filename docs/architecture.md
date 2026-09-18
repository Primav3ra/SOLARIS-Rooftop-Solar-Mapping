# Architecture

Written for: a developer picking this repository up for the first time.

## What the system does

Given a point on a map in urban India and a time window, SOLARIS estimates how
much electricity the rooftops in that area could generate. It does so entirely
from open satellite data, computed on demand — there is no pre-built database of
buildings or irradiance.

## The shape of it

```
                       browser
                          │
              ┌───────────┴───────────┐
              │  React site (Vite)    │   static routes carry no API calls
              │  /  /explore  /data   │   and cost zero Earth Engine quota
              │  /method /validation  │
              └───────────┬───────────┘
                          │  JSON over HTTPS
              ┌───────────┴───────────┐
              │   FastAPI (Cloud Run) │
              │                       │
              │  middleware:          │
              │   identity → limits   │
              │   → logging           │
              │                       │
              │  cache lookup ────────┼──► memory ──► Firestore
              │      │ miss           │
              │  guest allowance      │
              │      │                │
              │  Earth Engine gate    │   bounded concurrency,
              │      │                │   daily call budget
              └──────┼────────────────┘
                     │  getInfo() × 12
              ┌──────┴────────────────┐
              │  Google Earth Engine  │   all heavy raster work runs here
              └───────────────────────┘
```

Nothing is stored between requests except cache entries. There is no database,
no queue, and no background worker — once caching is in place nothing is
long-running enough to need one.

## Package layout

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

### The layering rule

`core` and `physics` import no `ee`. `gee` does. `api` composes them. This is
not decoration: it is what makes most of the project testable without
credentials. `windows.py` in particular was extracted from the old monolith
precisely because two hundred lines of temporal branching had been untestable
while it sat next to `ee.Geometry` calls.

## Request flow for `/api/yield`

1. **Validate.** Pydantic bounds every field, including an area-of-interest
   ceiling. This runs before any `ee` object exists, so an oversized request
   costs nothing. It is the primary quota defence — no request-rate limit helps
   against one well-formed request for a region the size of a continent.
2. **Resolve the window.** `yearly/2023` and the equivalent explicit date range
   become the same concrete `[start, end)` pair.
3. **Cache lookup.** Keyed on the *resolved* window, so those two requests
   collapse to one entry. Coordinates are quantised to 4 decimal places
   (~11 m); without that the hit rate would be near zero, because areas of
   interest come from raw map-click floats.
4. **Charge the session allowance** — only now, on a miss. A cached answer is
   free to serve, so charging for it would penalise exactly the requests that
   cost nothing.
5. **Take an Earth Engine slot.** Bounded concurrency and a daily call budget.
6. **Compute.** Roof mask, sun positions, shadow trace, sky-view factor, heat
   island, soiling, and the PV chain. Twelve round-trips.
7. **Assemble**, including a `data_quality` block naming every input that came
   from a fallback.
8. **Store**, but only if the window was fully covered — caching a partial
   answer would pin it for the whole TTL.

## Three design decisions worth knowing

**Earth Engine is the compute engine, not a data source.** Rasters are never
downloaded. Every reduction is expressed server-side and only scalars come back.
This is why a request is twelve small round-trips rather than one large
transfer, and why the number of round-trips is the thing to optimise.

**The cache key is versioned by algorithm.** `ALGO_VERSION` and
`DATASET_VERSION` prefix every key, so changing the physics invalidates stale
entries automatically. The alternative — remembering to flush a cache after a
model change — is the kind of step that gets forgotten exactly once and then
silently serves pre-fix numbers.

**Every fallback is reported.** Any input that came from a default rather than a
retrieval appears in `data_quality` with a severity and an explanation. The
project's recurring failure mode was a plausible number with no indication of
where it came from; this is the structural fix.

## Testing architecture

The offline suite runs with no Google Cloud account and no network. That is
possible because of `tests/fakes/fake_ee.py`: a numpy-backed fake Earth Engine
where `FakeImage` is a lazy expression tree and every operation is real
arithmetic on real arrays.

It is deliberately not a `MagicMock`. A mock would let the code run without
raising while every result was a mock object — it would have accepted all ten of
the defects this project found and proven nothing. The fake instead encodes real
Earth Engine semantics, *including the places the production code originally got
them wrong*, which is how those defects became failing tests.

The fake is installed by patching each module's bound `ee` attribute, not
`sys.modules`. Modules bind `import ee` at import time, so replacing
`sys.modules["ee"]` afterwards has no effect — a bug that made the golden tests
reach real Earth Engine only when run *after* another test file. The consumer
list is discovered by walking the package rather than hand-maintained, because
the hand-maintained version checked itself and passed regardless.

See `docs/contributing.md` for how to run it.
