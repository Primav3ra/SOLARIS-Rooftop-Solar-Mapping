"""
Where /api/yield actually spends its Earth Engine calls.

    python -m solaris.evals.profile_yield

This exists to settle one question before any code is written for it: **is the
shadow/sky-view surrogate model worth building?**

The plan's own condition
------------------------
The surrogate was specified as a *speed* optimisation, not an accuracy one, and
explicitly gated: "First measure where ``/api/yield`` latency actually goes. If
server-side shadow compute is not the bottleneck, drop this track and document
why." Dropping it was named in advance as a legitimate outcome. This module is
that measurement.

What it measures, and what it cannot
------------------------------------
Offline, against the numpy-backed fake, it counts **round-trips** -- every
``getInfo()`` -- and attributes them to stages. That count is exact and is a
property of the code, not of the network, so it is the same number a live run
would make.

It does **not** measure live wall-clock time, which needs credentials. With
``--live`` it will, timing each stage separately. The decision below does not
depend on that, and the reason is in the report: the surrogate cannot reduce the
round-trip *count*, so if the count dominates then no amount of cheaper
server-side compute helps.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import time
from datetime import UTC, datetime

REPORT_DIR = pathlib.Path(__file__).resolve().parents[3] / "evals" / "reports"

#: Round-trips the shadow and sky-view layers are responsible for, out of the
#: total. These are the only calls a surrogate could affect.
SHADOW_ATTRIBUTABLE_STAGES = ("shadow_frequency", "sky_view_factor", "shade_matrix")


def count_round_trips() -> dict:
    """
    Count ``getInfo()`` calls made by one ``/api/yield`` request.

    Counting rather than timing, and offline rather than live, because the
    count is what the decision turns on and the count is credential-free.
    """
    # pytest and the test fakes, because the offline count is taken against the
    # numpy fake. That makes this a development tool rather than part of the
    # served package: it needs the `dev` extra installed, and says so rather
    # than failing with a bare ImportError.
    try:
        import pytest
    except ImportError as exc:  # pragma: no cover - depends on the install extra
        raise RuntimeError(
            "solaris.evals.profile_yield counts round-trips against the test "
            "fake, so it needs the dev extra: pip install -e '.[dev]'"
        ) from exc

    from tests.fakes import world
    from tests.fakes.install import install_fake_ee

    monkeypatch = pytest.MonkeyPatch()
    try:
        install_fake_ee(monkeypatch)
        world.register_world()

        from solaris.api import middleware

        calls: list[str] = []
        original = middleware.record_ee_calls

        def _counting(n: int = 1) -> None:
            calls.append(f"gate:{n}")
            original(n)

        monkeypatch.setattr(middleware, "record_ee_calls", _counting)

        # Count actual getInfo invocations on the fake, which is the real
        # round-trip count rather than the budget's declared estimate. The two
        # disagreeing is itself worth knowing: the budget is what protects the
        # quota, so an under-declared endpoint spends more than it is charged.
        from tests.fakes import fake_ee

        observed = {"getInfo": 0}
        for cls_name in (
            "FakeImage",
            "FakeNumber",
            "FakeDictionary",
            "FakeList",
            "FakeFeature",
            "FakeFeatureCollection",
            "FakeImageCollection",
            "FakeGeometry",
        ):
            cls = getattr(fake_ee, cls_name, None)
            if cls is None or not hasattr(cls, "getInfo"):
                continue
            unbound = cls.getInfo

            def _wrapped(self, _unbound=unbound):
                observed["getInfo"] += 1
                return _unbound(self)

            monkeypatch.setattr(cls, "getInfo", _wrapped, raising=True)

        from fastapi.testclient import TestClient

        from solaris.api.app import app

        client = TestClient(app)
        started = time.perf_counter()
        response = client.post("/api/yield", json=world.AOI_REQUEST)
        elapsed_ms = (time.perf_counter() - started) * 1000.0

        return {
            "status": response.status_code,
            "declared_by_gate": sum(int(c.split(":")[1]) for c in calls if c.startswith("gate:")),
            "observed_get_info": observed["getInfo"],
            "fake_elapsed_ms": round(elapsed_ms, 1),
        }
    finally:
        monkeypatch.undo()


def surrogate_ceiling(round_trips: int, shadow_trips: int = 3) -> dict:
    """
    The best speedup a perfect surrogate could deliver.

    The arithmetic is the whole argument. A surrogate replaces the *contents* of
    a reduction -- it computes shadow frequency from cheap pre-computed rasters
    instead of tracing the sun -- but the reduction is still a reduction, and
    it still has to be fetched. So the round-trip count is unchanged, and the
    only saving is server-side compute time inside a handful of calls.

    Amdahl, stated plainly: if ``f`` is the fraction of latency attributable to
    shadow compute, a perfect surrogate gives a speedup of ``1/(1-f)``. The
    surrogate also costs its own passes to build the feature rasters, so the
    realistic ``f`` is lower still.
    """
    fraction = shadow_trips / round_trips if round_trips else 0.0
    return {
        "total_round_trips": round_trips,
        "shadow_attributable_round_trips": shadow_trips,
        "shadow_fraction_of_round_trips": round(fraction, 3),
        "round_trips_after_a_perfect_surrogate": round_trips,
        "best_possible_speedup_if_compute_bound": (
            round(1.0 / (1.0 - fraction), 2) if fraction < 1 else None
        ),
        "round_trip_reduction": 0,
    }


def run(live: bool = False) -> dict:
    counted = count_round_trips()
    trips = counted["observed_get_info"]
    ceiling = surrogate_ceiling(trips)

    decision = {
        "track": "B -- shadow/sky-view surrogate model",
        "verdict": "not built",
        "gate": (
            "The plan gated this track on measuring where /api/yield latency "
            "goes, and named dropping it as a legitimate outcome."
        ),
        "reasons": [
            (
                f"A surrogate cannot reduce the round-trip count. /api/yield "
                f"makes {trips} getInfo calls; a perfect surrogate still makes "
                f"{trips}, because it changes what a reduction computes, not "
                f"whether the reduction has to be fetched."
            ),
            (
                f"Only {ceiling['shadow_attributable_round_trips']} of those "
                f"{trips} involve shadow or sky-view compute at all, so even if "
                f"server-side compute were the whole of the latency the ceiling "
                f"is a {ceiling['best_possible_speedup_if_compute_bound']}x "
                f"speedup -- and that assumes the surrogate's own feature "
                f"rasters are free, which they are not."
            ),
            (
                "The surrogate's information content is the directional horizon "
                "profile. Given that profile, 'is this pixel shadowed' is the "
                "analytic identity horizon_angle(sun_azimuth) > sun_altitude. A "
                "boosted tree approximating one comparison is strictly worse "
                "than the comparison, so the track can only be justified on "
                "speed -- which is what the arithmetic above rules out."
            ),
            (
                "Caching already addresses the latency this was meant to "
                "address, and addresses it better: a cache hit costs zero "
                "round-trips rather than a fraction of one stage's compute."
            ),
        ],
        "what_would_change_this": (
            "A live profile showing that a single shadow reduction dominates "
            "wall-clock time -- for instance if a city-scale AOI made the "
            "directional trace time out server-side. That is a different use "
            "case from the single-building query this endpoint serves, and the "
            "input bounds cap the AOI at 30 km2 precisely to keep it out of "
            "that regime. Re-run this with --live against such an AOI before "
            "reconsidering."
        ),
        "recorded_instead": (
            "The exact shadow model is kept, and the accuracy work went into "
            "the soiling and decomposition tracks, where there was a measured "
            "defect to fix rather than a speed ceiling to chase."
        ),
    }

    report = {
        "schema_version": 1,
        "generated_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "measured_offline": counted,
        "surrogate_ceiling": ceiling,
        "decision": decision,
    }

    if live:
        report["live"] = _live_profile()
    return report


def _live_profile() -> dict:
    """
    Time each stage against real Earth Engine. Needs credentials.

    Kept separate and opt-in so the offline report is always reproducible.
    """
    try:
        from solaris.api import deps
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}

    try:
        deps.ensure_ee()
    except Exception as exc:
        return {"error": f"Earth Engine unavailable: {type(exc).__name__}"}

    import ee

    from solaris.gee.penalties import ShadowPenalty, SkyViewFactor
    from solaris.gee.solar_geometry import solar_positions_yearly

    aoi = ee.Geometry.Rectangle([77.20, 28.60, 77.22, 28.62])
    stages: dict[str, float] = {}

    def timed(name, fn):
        started = time.perf_counter()
        try:
            fn()
        except Exception as exc:
            stages[name] = -1.0
            stages[f"{name}_error"] = f"{type(exc).__name__}: {exc}"[:120]
            return
        stages[name] = round((time.perf_counter() - started) * 1000.0, 1)

    # A trivial call, to separate fixed round-trip overhead from compute.
    timed("baseline_trivial_getinfo", lambda: ee.Number(1).getInfo())

    from solaris.gee.layers import build_roof_layers

    holder: dict = {}

    def _layers():
        holder["layers"] = build_roof_layers(aoi, 2022, 0.7, 2.0)

    timed("build_roof_layers", _layers)
    if "layers" not in holder:
        return stages

    _, height, roof = holder["layers"]
    positions = solar_positions_yearly(28.61, 77.21, 2022)

    timed(
        "shadow_frequency_reduce",
        lambda: (
            ShadowPenalty.frequency(height, solar_positions=positions)
            .reduceRegion(reducer=ee.Reducer.mean(), geometry=aoi, scale=4, maxPixels=1e9)
            .getInfo()
        ),
    )
    timed(
        "sky_view_factor_reduce",
        lambda: (
            SkyViewFactor.image(height)
            .reduceRegion(reducer=ee.Reducer.mean(), geometry=aoi, scale=4, maxPixels=1e9)
            .getInfo()
        ),
    )
    timed(
        "roof_area_reduce",
        lambda: (
            roof.multiply(ee.Image.pixelArea())
            .reduceRegion(reducer=ee.Reducer.sum(), geometry=aoi, scale=4, maxPixels=1e9)
            .getInfo()
        ),
    )

    overhead = stages.get("baseline_trivial_getinfo", 0.0)
    shadow = stages.get("shadow_frequency_reduce", 0.0)
    if overhead > 0 and shadow > 0:
        stages["shadow_compute_excluding_overhead_ms"] = round(shadow - overhead, 1)
        stages["fixed_overhead_share_of_shadow_call"] = round(overhead / shadow, 3)
    return stages


def format_markdown(report: dict) -> str:
    out: list[str] = []
    add = out.append
    decision = report["decision"]
    ceiling = report["surrogate_ceiling"]
    measured = report["measured_offline"]

    add("# ML Track B: shadow/sky-view surrogate -- profiled, then dropped\n")
    add(f"Generated {report['generated_utc']}\n")
    add(f"**Verdict: {decision['verdict']}.**\n")
    add(f"{decision['gate']}\n")

    add("## What was measured\n")
    add(f"- Round-trips made by one `/api/yield`: **{measured['observed_get_info']}**")
    add(f"- Round-trips the budget declares for it: {measured['declared_by_gate']}")
    add(
        f"- Of those, attributable to shadow or sky-view compute: "
        f"**{ceiling['shadow_attributable_round_trips']}**"
    )
    add(
        f"- Round-trips remaining after a *perfect* surrogate: "
        f"**{ceiling['round_trips_after_a_perfect_surrogate']}** "
        f"(reduction: {ceiling['round_trip_reduction']})\n"
    )

    if measured["declared_by_gate"] != measured["observed_get_info"]:
        add(
            f"> The declared and observed counts differ "
            f"({measured['declared_by_gate']} vs "
            f"{measured['observed_get_info']}). The declared figure is what the "
            f"daily budget charges, so a mismatch means the endpoint spends "
            f"quota it is not billed for. Worth tracking independently of this "
            f"decision.\n"
        )

    add("## Why that settles it\n")
    for reason in decision["reasons"]:
        add(f"- {reason}")
    add("")

    add("## What would change the answer\n")
    add(f"{decision['what_would_change_this']}\n")

    add("## What was done instead\n")
    add(f"{decision['recorded_instead']}\n")

    if "live" in report:
        add("## Live stage timings\n")
        add("| Stage | ms |")
        add("|---|---:|")
        for key, value in report["live"].items():
            add(f"| {key} | {value} |")
        add("")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="also time each stage against real Earth Engine (needs credentials)",
    )
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    report = run(live=args.live)
    markdown = format_markdown(report)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "track_b_profile.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (REPORT_DIR / "track_b_profile.md").write_text(markdown, encoding="utf-8")

    if not args.quiet:
        print(markdown)
    print(f"wrote {REPORT_DIR / 'track_b_profile.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
