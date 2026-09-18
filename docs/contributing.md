# Contributing

Written for: a developer making their first change to this repository.

## Setup

```bash
python -m pip install -e ".[dev,ml,physics]"
pre-commit install
```

**A fresh clone with no Google Cloud account must get a fully green `pytest`.**
That is non-negotiable and is why the fake Earth Engine exists. If you find
yourself needing credentials to run a test, the test is in the wrong suite.

```bash
pytest                      # offline. no credentials, no network.
pytest -m gee               # integration. needs credentials.
ruff check src tests
ruff format src tests
```

Current state: **648 tests passing**, `gee`-marked tests deselected by default
via `addopts` in `pyproject.toml`.

## Where a change goes

| Changing | File | Then |
|---|---|---|
| A physical coefficient | `core/constants.py` (or the owning module) | Bump `ALGO_VERSION`, which invalidates cached results automatically |
| A dataset id, band or vintage | `gee/datasets.py` | Bump `DATASET_VERSION` |
| A request field | `api/schemas.py` | Add a bound. Every field has one. |
| Temporal logic | `api/windows.py` | Imports no `ee`, so test it directly |
| Anything with `import ee` | `gee/` | It will be auto-discovered by the test fake |

Forgetting to bump a version means a deploy silently serves pre-change numbers
from cache. This is the step that gets forgotten exactly once.

## Working with the fake Earth Engine

`tests/fakes/fake_ee.py` is a numpy-backed fake where `FakeImage` is a lazy
expression tree and every operation is real arithmetic on real arrays.

**It is deliberately not a `MagicMock`,** and the choice of double matters more
than it sounds. A mock lets the code run without raising while every arithmetic
result is a mock object — it would have accepted all ten confirmed defects in
this project's history and proven nothing.

Four rules, in priority order:

1. **Encode real Earth Engine semantics, especially where the production code
   gets them wrong.** `translate` treats bare offsets as metres. Focal
   operations resolve against the request projection. Comparisons propagate NaN
   the way Earth Engine propagates masks. This is the mechanism by which a
   semantics bug becomes a failing test rather than a mystery.
2. **Match real signatures exactly.** `ee.Feature(geometry, properties)`, in
   that order. The fake once had them reversed, so
   `ee.Feature(None, {"p": v})` — the standard idiom — built a feature whose
   *geometry* was the dict and which had no properties. Nothing raised; a
   365-day reduction simply read back all `None` and the code under test took
   its no-data fallback while appearing to work.
3. **Fail loudly.** Module `__getattr__` raises `NotImplementedError` for
   anything unimplemented. Never add a permissive stub.
4. **Fixtures have analytically-known answers.** A flat block, a 40 m tower, a
   three-building canyon; a Gaussian heat-island with a known anomaly; aerosol
   with deliberately QA-flagged corrupt pixels, so the QA-masking fix is
   *required* to pass.

The fake is itself tested, in `tests/unit/test_fake_ee.py`. When it disagrees
with real Earth Engine, the fake is wrong — it was once *more forgiving* than
the real API and thereby hid a real edge artefact where 75% of sky-view pixels
were masked and energy dropped 58%.

### The synthetic world

`tests/fakes/world.py` registers every asset the API reads. Keep it
**representative, not adversarial**: the rainfall fixture was initially a
stylised two-month monsoon giving 20 cleaning-rain days a year against Delhi's
real 91, which saturated the soiling model and broke the penalty-balance
assertions — not because the assertions were wrong but because the world was. It
now uses the measured Delhi monthly rain-day counts.

### Golden files

```bash
python -m tests.fakes.regenerate_golden
```

**Commit a regenerated golden on its own**, so the numeric diff is reviewable in
isolation from whatever change caused it. A golden diff bundled into a feature
commit is a golden diff nobody reads.

## Writing tests

Prefer tests that would fail for a *reason*, and say the reason in the
docstring. `test_air_mass_stays_finite_at_the_horizon` explains that
Kasten-Young is used precisely because `1/sin(elevation)` diverges there. Six
months later that docstring is why nobody "simplifies" it back.

Assert physics, not just ranges. Diffuse fraction must *fall* as clearness rises,
because that is what the atmosphere does — a model with that backwards would
still score plausibly on RMSE.

`xfail(strict=True)` is the tool for a known defect: it documents the bug
executably and fails loudly the moment it is fixed, so a fix cannot land without
the test being updated.

## Validation and ML conventions

Two rules, both learned the hard way.

**Declare the gate before training.** `MIN_SKILL_OVER_ERBS = 0.10` sits in the
module, above the training code, so it cannot be relaxed to suit the result.

**Baseline against the published method, not the naive one.** Beating a constant
proves nothing. The decomposition model's baseline is Erbs (1982), and the
soiling model is cross-checked against an independent implementation of Kimber.
Doing that found three real errors that agreeing-with-ourselves would not have.

**Split spatially *and* temporally** for anything geographic. Neighbouring
samples leak catastrophically.

## Reference data

`src/solaris/evals/fetch.py` is the only code in the project that makes outbound
HTTP. Everything is cached under `evals/references/` and committed, so evals run
offline and in CI without hammering a free public service.

```bash
python -m solaris.evals.fetch --years 2020 2021 2022    # daily irradiance
python -m solaris.evals.fetch --hourly --years 2020 2021 2022
python -m solaris.evals.fetch --precip --years 2020 2021 2022
```

Refresh is a deliberate, explicit step, never automatic.

## Frontend

```bash
cd frontend/site
npm install
npm run dev      # dev server, proxies /api to localhost:8000
npm run build    # builds into src/solaris/api/static/
```

Two constraints enforced in CI:

- **Content routes must ship no three.js.** The intro's WebGL chunk is
  route-split and lazily loaded; a bundle-size assertion checks this.
- **Displayed numbers must come from the committed eval artifacts**, imported at
  build time. No hand-copied figures — that is the anti-drift guarantee, and it
  is asserted by a test.

## Commits

Conventional-ish prefixes: `[feat]`, `[fix]`, `[refactor]`, `[test]`, `[docs]`.

Say what changed and *why it was wrong before*. This repository's history is
most useful as a record of which assumptions turned out to be false, and a
message saying "fixed shadow model" throws that away.
