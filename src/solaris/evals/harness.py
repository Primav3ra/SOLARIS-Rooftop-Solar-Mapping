"""
Validation harness: does the model produce numbers the literature supports?

Run offline (uses the committed reference cache)::

    python -m solaris.evals.harness

What this checks, and what it deliberately does not
---------------------------------------------------
The headline metric is **specific yield** in kWh/kWp/yr, because that is the
quantity published plant studies actually report, so it is the only place this
model can be compared against a genuinely independent measurement.

A useful algebraic property makes it the right choice. Writing out the chain::

    kWp     = area * packing * efficiency * 1 kW/m2
    E       = GHI * area * retention * derate * efficiency * PR * packing
    E / kWp = GHI * retention * derate * PR

The packing factor and module efficiency **cancel**. So specific yield tests the
irradiance and loss chain on its own, independently of the capacity assumptions
-- which matters, because ``packing_factor`` is the least defensible constant in
the model and would otherwise contaminate the comparison.

Two engines:

``reference``
    Drives the loss chain with NASA POWER's measured GHI and beam fraction.
    Runs offline, and isolates the loss model from the irradiance source.
``era5``
    Uses the project's own ERA5-Land baseline. Requires Earth Engine
    credentials, and additionally tests the irradiance source. This is where
    ERA5's documented positive bias over aerosol-heavy India should show up.

The spread rule
---------------
Every irradiance comparison reports each reference separately **and** the
inter-reference spread. For Delhi the two credible free references differ by
~10%, so a tighter accuracy claim than that would be dishonest. See
``references.py`` for the full argument.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from solaris.core import constants as C
from solaris.evals import metrics, pvlib_suite
from solaris.evals.references import (
    CITIES,
    PERFORMANCE_RATIO_BAND,
    PUBLISHED_YIELDS,
    SPECIFIC_YIELD_BAND,
    City,
    load_cached,
)

REPORT_DIR = pathlib.Path(__file__).resolve().parents[3] / "evals" / "reports"

DEFAULT_YEARS = (2020, 2021, 2022)

#: Representative loss factors for the reference-engine run.
#:
#: These stand in for the geometric layers, which need Earth Engine. Chosen at
#: the low end of what the model produces on a mixed urban block so the check
#: is not flattered: a denser AOI would lose more, a sparse one less.
#: The Earth Engine engine replaces them with computed values.
NOMINAL_SHADOW_FRACTION = 0.06
NOMINAL_SKY_VIEW_FACTOR = 0.96
NOMINAL_SOILING_RETENTION = 0.944  # AOD 0.70 at the production coefficient
NOMINAL_UHI_DERATE = 0.997  # 2.5 C surface anomaly x 0.3 air ratio x -0.004


@dataclass
class YieldCheck:
    """One city-year specific-yield evaluation."""

    city: str
    zone: str
    year: int
    reference_ghi_kwh_m2: float
    reference_beam_fraction: float
    net_retention: float
    combined_derate: float
    performance_ratio: float
    specific_yield_kwh_per_kwp_yr: float
    in_published_band: bool
    distance_from_band: float


def net_retention(beam_fraction: float, shadow_fraction: float, svf: float) -> float:
    """
    Geometric retention: ``diffuse * SVF + beam * (1 - shadow)``.

    Shadows attenuate only the direct beam; sky-view obstruction attenuates only
    the diffuse. This is the decomposition the penalty stack implements.
    """
    return (1.0 - beam_fraction) * svf + beam_fraction * (1.0 - shadow_fraction)


def specific_yield(
    ghi_kwh_m2: float,
    retention: float,
    derate: float,
    performance_ratio: float = C.PERFORMANCE_RATIO,
) -> float:
    """
    Annual AC energy per installed kWp.

    Packing factor and module efficiency cancel out of this ratio, so it
    isolates the irradiance and loss chain. See the module docstring.
    """
    return ghi_kwh_m2 * retention * derate * performance_ratio


def evaluate_city_year(city: City, year: int) -> YieldCheck | None:
    """Evaluate one city-year against the reference cache."""
    series = load_cached(city.key, year)
    if series is None:
        return None
    ghi = series.annual_ghi_kwh_m2()
    beam = series.beam_fraction()
    if not ghi or beam is None:
        return None

    retention = net_retention(beam, NOMINAL_SHADOW_FRACTION, NOMINAL_SKY_VIEW_FACTOR)
    derate = NOMINAL_UHI_DERATE * NOMINAL_SOILING_RETENTION
    sy = specific_yield(ghi, retention, derate)

    low, high = SPECIFIC_YIELD_BAND
    inside = low <= sy <= high
    distance = 0.0 if inside else (low - sy if sy < low else sy - high)

    return YieldCheck(
        city=city.name,
        zone=city.zone,
        year=year,
        reference_ghi_kwh_m2=round(ghi, 1),
        reference_beam_fraction=round(beam, 4),
        net_retention=round(retention, 4),
        combined_derate=round(derate, 4),
        performance_ratio=C.PERFORMANCE_RATIO,
        specific_yield_kwh_per_kwp_yr=round(sy, 1),
        in_published_band=inside,
        distance_from_band=round(distance, 1),
    )


def irradiance_spread() -> list[dict]:
    """
    Where two independent references disagree, and by how much.

    This is the honesty constraint made numeric: it bounds how tight any
    accuracy claim downstream can legitimately be.
    """
    rows = []
    for city in CITIES:
        if city.gsa_ghi_kwh_m2_yr is None:
            continue
        power_values = [
            s.annual_ghi_kwh_m2()
            for year in DEFAULT_YEARS
            if (s := load_cached(city.key, year)) is not None
        ]
        if not power_values:
            continue
        power_mean = sum(power_values) / len(power_values)
        gsa = city.gsa_ghi_kwh_m2_yr
        rows.append(
            {
                "city": city.name,
                "nasa_power_kwh_m2_yr": round(power_mean, 1),
                "global_solar_atlas_kwh_m2_yr": gsa,
                "spread_kwh_m2_yr": round(abs(gsa - power_mean), 1),
                "spread_pct_of_mean": round(
                    abs(gsa - power_mean) / ((gsa + power_mean) / 2) * 100.0, 1
                ),
            }
        )
    return rows


def beam_fraction_summary() -> dict:
    """
    Reference beam fraction, against what the project's ERA5 path assumes.

    ERA5 uses a monthly aerosol climatology and is documented to overestimate
    direct while underestimating diffuse, with the error growing in aerosol
    load. India is exactly that regime, so this is the single most
    ERA5-sensitive input in the model.
    """
    values = []
    for city in CITIES:
        for year in DEFAULT_YEARS:
            series = load_cached(city.key, year)
            if series is None:
                continue
            beam = series.beam_fraction()
            if beam is not None:
                values.append(beam)
    if not values:
        return {}
    return {
        "n_city_years": len(values),
        "reference_beam_fraction_mean": round(sum(values) / len(values), 4),
        "reference_beam_fraction_min": round(min(values), 4),
        "reference_beam_fraction_max": round(max(values), 4),
        "era5_fallback_used_by_model": 0.60,
        "note": (
            "The model falls back to 0.60 when ERA5 sampling fails, and its "
            "docstring claims an annual 0.55-0.72 range. Compare against the "
            "reference mean above."
        ),
    }


def run(years: tuple[int, ...] = DEFAULT_YEARS) -> dict:
    """Run every check and assemble the report payload."""
    checks = [
        check
        for city in CITIES
        for year in years
        if (check := evaluate_city_year(city, year)) is not None
    ]
    values = [c.specific_yield_kwh_per_kwp_yr for c in checks]
    inside = [c for c in checks if c.in_published_band]

    return {
        "schema_version": 1,
        "generated_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "engine": "reference",
        "algo_version": C.ALGO_VERSION,
        "dataset_version": C.DATASET_VERSION,
        "years": list(years),
        "suites": {
            "specific_yield_vs_published": {
                "n": len(checks),
                "band": list(SPECIFIC_YIELD_BAND),
                "n_in_band": len(inside),
                "frac_in_band": round(len(inside) / len(checks), 4) if checks else 0.0,
                "summary": metrics.describe(values),
                "by_city_year": [asdict(c) for c in checks],
            },
            "irradiance_reference_spread": {
                "note": (
                    "No accuracy claim can be tighter than this spread. "
                    "Reported per city rather than aggregated."
                ),
                "rows": irradiance_spread(),
            },
            "beam_fraction": beam_fraction_summary(),
            "pvlib_engine": pvlib_suite.run(max(years)),
        },
        "published_references": [asdict(p) for p in PUBLISHED_YIELDS],
        "performance_ratio_band": list(PERFORMANCE_RATIO_BAND),
    }


def format_markdown(report: dict) -> str:
    """Human-readable report."""
    out: list[str] = []
    suite = report["suites"]["specific_yield_vs_published"]
    add = out.append

    add("# SOLARIS validation report\n")
    add(f"Generated {report['generated_utc']}  ")
    add(f"engine `{report['engine']}`, algo v{report['algo_version']}, ")
    add(f"datasets v{report['dataset_version']}\n")

    add("## Specific yield against published plant performance\n")
    add(
        "Specific yield is the comparison that matters: it is what plant studies "
        "report, and the packing factor and module efficiency cancel out of it, "
        "so it isolates the irradiance and loss chain from the capacity "
        "assumptions.\n"
    )
    band = suite["band"]
    s = suite["summary"]
    add(f"- Published plausibility band: **{band[0]:.0f}-{band[1]:.0f} kWh/kWp/yr**")
    add(f"- Model: mean **{s['mean']:.0f}**, range {s['min']:.0f}-{s['max']:.0f}")
    add(
        f"- Inside the band: **{suite['n_in_band']}/{suite['n']}** city-years "
        f"({suite['frac_in_band'] * 100:.0f}%)\n"
    )

    add("| City | Zone | Year | Ref GHI | Beam | Retention | Derate | Specific yield | In band |")
    add("|---|---|---|---:|---:|---:|---:|---:|:-:|")
    for row in suite["by_city_year"]:
        add(
            f"| {row['city']} | {row['zone']} | {row['year']} "
            f"| {row['reference_ghi_kwh_m2']:.0f} "
            f"| {row['reference_beam_fraction']:.3f} "
            f"| {row['net_retention']:.3f} "
            f"| {row['combined_derate']:.3f} "
            f"| {row['specific_yield_kwh_per_kwp_yr']:.0f} "
            f"| {'yes' if row['in_published_band'] else 'NO'} |"
        )

    add("\n## Reference disagreement\n")
    add(
        "The two credible free references for India do not agree. This bounds "
        "how tight any accuracy claim can honestly be.\n"
    )
    add("| City | NASA POWER | Global Solar Atlas | Spread | Spread % |")
    add("|---|---:|---:|---:|---:|")
    for row in report["suites"]["irradiance_reference_spread"]["rows"]:
        add(
            f"| {row['city']} | {row['nasa_power_kwh_m2_yr']:.0f} "
            f"| {row['global_solar_atlas_kwh_m2_yr']:.0f} "
            f"| {row['spread_kwh_m2_yr']:.0f} | {row['spread_pct_of_mean']:.1f}% |"
        )

    beam = report["suites"]["beam_fraction"]
    if beam:
        add("\n## Beam fraction\n")
        add(
            "The most ERA5-sensitive input in the model. ERA5 uses a monthly "
            "aerosol climatology and is documented to overestimate direct while "
            "underestimating diffuse, with the error growing in aerosol load.\n"
        )
        add(
            f"- Reference mean over {beam['n_city_years']} city-years: "
            f"**{beam['reference_beam_fraction_mean']:.3f}** "
            f"(range {beam['reference_beam_fraction_min']:.3f}-"
            f"{beam['reference_beam_fraction_max']:.3f})"
        )
        add(f"- Model fallback when ERA5 sampling fails: {beam['era5_fallback_used_by_model']:.2f}")

    add("\n## Published references used\n")
    for pub in report["published_references"]:
        lo, hi = pub["specific_yield_kwh_per_kwp_yr"]
        rng = f"{lo:.0f}" if lo == hi else f"{lo:.0f}-{hi:.0f}"
        add(f"- **{pub['label']}**: {rng} kWh/kWp/yr — {pub['source']}")
        if pub["caveat"]:
            add(f"  - {pub['caveat']}")

    add("\n## What this does not establish\n")
    add(
        "- Roofs are treated as horizontal, so no tilt gain (+8-12% annually in "
        "north India) is included. A tilted comparison would sit higher.\n"
        "- The geometric loss factors in the `reference` engine are nominal "
        "stand-ins; only the `era5` engine computes them per site.\n"
        "- Reference GHI is satellite-derived, not ground-measured. NASA POWER "
        "is only semi-independent of ERA5 (shared MERRA-2 meteorology lineage, "
        "distinct radiation retrieval)."
    )
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the SOLARIS validation harness.")
    parser.add_argument("--years", type=int, nargs="+", default=list(DEFAULT_YEARS))
    parser.add_argument("--out", type=pathlib.Path, default=None, help="report directory")
    parser.add_argument(
        "--quiet", action="store_true", help="write files without printing the report"
    )
    args = parser.parse_args(argv)

    report = run(tuple(args.years))
    suite = report["suites"]["specific_yield_vs_published"]
    if suite["n"] == 0:
        print(
            "No reference data cached. Run:\n"
            "  python -m solaris.evals.fetch --years 2020 2021 2022",
            file=sys.stderr,
        )
        return 1

    markdown = format_markdown(report)
    out_dir = args.out or REPORT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "latest.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out_dir / "latest.md").write_text(markdown, encoding="utf-8")

    if not args.quiet:
        print(markdown)
    print(f"wrote {out_dir / 'latest.json'} and {out_dir / 'latest.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
