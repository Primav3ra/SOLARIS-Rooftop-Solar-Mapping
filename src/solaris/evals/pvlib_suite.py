"""
The pvlib engine evaluated against published plant performance.

Why this suite exists separately from the legacy one
---------------------------------------------------
The published figures this project validates against (1400-1700 kWh/kWp/yr for
a typical Indian rooftop) come from **tilted** arrays. The legacy chain treats
every roof as horizontal. Comparing the two directly reads as a large model
bias when much of the gap is purely a mount-configuration mismatch -- so this
suite runs both ``flat`` and ``optimal_fixed`` and reports them side by side.
Every record carries ``mount`` explicitly for that reason.

What it establishes
-------------------
1. **Transposition gain.** The tilt gain was previously implicitly 1.0, because
   GHI was used directly as plane-of-array irradiance. Measuring it quantifies
   how much that assumption cost.
2. **A computed temperature loss** replacing the temperature component
   previously buried inside the lumped ``performance_ratio = 0.80`` -- and
   double-counted against the urban heat-island derate.
3. **Climatology discretisation error**, by comparing the 288-step
   monthly-diurnal annual GHI against the independent full-year daily total.
   Measured rather than assumed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from solaris.evals.references import (
    CITIES,
    SPECIFIC_YIELD_BAND,
    City,
    load_cached,
    load_cached_hourly,
)
from solaris.physics import pv

#: Soiling retention for the eval run. AOD 0.70 at the production coefficient,
#: matching the value the reference-engine suite uses so the two are comparable.
NOMINAL_SOILING_LOSS = 0.056

#: Geometric losses. Stand-ins for the Earth Engine layers, held identical
#: across mounts so the flat-vs-tilted comparison isolates the tilt.
NOMINAL_SHADING_BEAM_LOSS = 0.06
NOMINAL_SKY_VIEW_DIFFUSE_LOSS = 0.04


@dataclass
class PvlibCheck:
    """One city-mount evaluation through the pvlib chain."""

    city: str
    zone: str
    year: int
    mount: str
    tilt_deg: float
    ghi_kwh_m2_yr: float
    poa_kwh_m2_yr: float
    transposition_gain: float
    mean_cell_temperature_c: float
    mean_daytime_air_temp_c: float
    temperature_loss: float
    performance_ratio: float
    specific_yield_kwh_per_kwp_yr: float
    in_published_band: bool


def evaluate(city: City, year: int, mount: pv.MountType) -> PvlibCheck | None:
    """Run the pvlib chain for one city and mount configuration."""
    hourly = load_cached_hourly(city.key, year)
    if hourly is None or hourly.n_steps == 0:
        return None

    columns = hourly.as_columns()
    times = hourly.datetime_index()
    config = pv.ArrayConfig(mount=mount)

    result = pv.annual_specific_yield(
        latitude=city.lat,
        longitude=city.lon,
        ghi_per_step_w_m2=columns["ghi"],
        times=times,
        air_temperature_c=columns["temp_air"],
        wind_speed_m_s=columns["wind"],
        config=config,
        dni_per_step=columns["dni"],
        dhi_per_step=columns["dhi"],
        soiling_loss=NOMINAL_SOILING_LOSS,
        shading_beam_loss=NOMINAL_SHADING_BEAM_LOSS,
        sky_view_diffuse_loss=NOMINAL_SKY_VIEW_DIFFUSE_LOSS,
    )

    low, high = SPECIFIC_YIELD_BAND
    yield_value = result.specific_yield_kwh_per_kwp_yr

    return PvlibCheck(
        city=city.name,
        zone=city.zone,
        year=year,
        mount=mount,
        tilt_deg=round(result.poa.tilt_deg, 1),
        ghi_kwh_m2_yr=round(hourly.annual_ghi_kwh_m2(), 1),
        poa_kwh_m2_yr=round(result.poa.poa_global_kwh_m2, 1),
        transposition_gain=round(result.poa.transposition_gain, 4),
        mean_cell_temperature_c=round(result.mean_cell_temperature_c, 2),
        mean_daytime_air_temp_c=round(hourly.mean_daytime_air_temp_c(), 2),
        temperature_loss=round(result.breakdown.temperature or 0.0, 4),
        performance_ratio=round(result.breakdown.performance_ratio, 4),
        specific_yield_kwh_per_kwp_yr=round(yield_value, 1),
        in_published_band=low <= yield_value <= high,
    )


def climatology_discretisation_error(year: int) -> list[dict]:
    """
    How well the 288-step climatology reproduces a full year.

    The 12 mid-month days are compared against the independent full-year daily
    series. This is the check that keeps the "288 steps is enough" claim from
    being an assumption.
    """
    rows = []
    for city in CITIES:
        hourly = load_cached_hourly(city.key, year)
        daily = load_cached(city.key, year)
        if hourly is None or daily is None:
            continue
        climatology = hourly.annual_ghi_kwh_m2()
        full_year = daily.annual_ghi_kwh_m2()
        if not full_year:
            continue
        rows.append(
            {
                "city": city.name,
                "climatology_288_step_kwh_m2": round(climatology, 1),
                "full_year_daily_kwh_m2": round(full_year, 1),
                "error_pct": round((climatology - full_year) / full_year * 100.0, 2),
            }
        )
    return rows


def run(year: int = 2022) -> dict:
    """Run the pvlib suite for both mount configurations."""
    try:
        pv._require_pvlib()
    except pv.PvlibUnavailableError as exc:
        return {"skipped": str(exc)}

    checks: list[PvlibCheck] = []
    for mount in ("flat", "optimal_fixed"):
        for city in CITIES:
            check = evaluate(city, year, mount)  # type: ignore[arg-type]
            if check is not None:
                checks.append(check)

    if not checks:
        return {"skipped": "no cached hourly reference data"}

    by_mount: dict[str, list[PvlibCheck]] = {}
    for check in checks:
        by_mount.setdefault(check.mount, []).append(check)

    summary = {}
    for mount, rows in by_mount.items():
        values = [r.specific_yield_kwh_per_kwp_yr for r in rows]
        gains = [r.transposition_gain for r in rows]
        summary[mount] = {
            "n": len(rows),
            "mean_specific_yield": round(sum(values) / len(values), 1),
            "min_specific_yield": round(min(values), 1),
            "max_specific_yield": round(max(values), 1),
            "mean_transposition_gain": round(sum(gains) / len(gains), 4),
            "n_in_band": sum(1 for r in rows if r.in_published_band),
            "mean_performance_ratio": round(sum(r.performance_ratio for r in rows) / len(rows), 4),
            "mean_cell_temperature_c": round(
                sum(r.mean_cell_temperature_c for r in rows) / len(rows), 2
            ),
        }

    flat = summary.get("flat", {})
    tilted = summary.get("optimal_fixed", {})
    tilt_benefit = None
    if flat and tilted:
        tilt_benefit = round(
            (tilted["mean_specific_yield"] / flat["mean_specific_yield"] - 1.0) * 100.0,
            2,
        )

    return {
        "year": year,
        "engine": "pvlib",
        "transposition_model": "haydavies",
        "band": list(SPECIFIC_YIELD_BAND),
        "by_mount": summary,
        "tilt_benefit_pct": tilt_benefit,
        "records": [asdict(c) for c in checks],
        "climatology_discretisation": climatology_discretisation_error(year),
        "nominal_losses": {
            "soiling": NOMINAL_SOILING_LOSS,
            "shading_beam": NOMINAL_SHADING_BEAM_LOSS,
            "sky_view_diffuse": NOMINAL_SKY_VIEW_DIFFUSE_LOSS,
            "note": (
                "Geometric losses are stand-ins for the Earth Engine layers and "
                "are held identical across mounts, so the flat-vs-tilted "
                "comparison isolates the effect of tilt alone."
            ),
        },
    }


def format_markdown(suite: dict) -> str:
    """Render the pvlib suite as markdown."""
    if "skipped" in suite:
        return f"\n## pvlib engine\n\nSkipped: {suite['skipped']}\n"

    out: list[str] = []
    add = out.append
    add("\n## pvlib engine: transposition and computed cell temperature\n")
    add(
        "The published band assumes **tilted** arrays, so flat and "
        "latitude-optimal mounts are reported separately. Comparing a "
        "horizontal-roof result against a tilted reference would read as model "
        "bias when much of the gap is a configuration mismatch.\n"
    )

    add(
        "| Mount | n | Mean specific yield | Range | Transposition gain | Mean PR | Mean cell T | In band |"
    )
    add("|---|---:|---:|---:|---:|---:|---:|---:|")
    for mount, s in suite["by_mount"].items():
        add(
            f"| `{mount}` | {s['n']} | **{s['mean_specific_yield']:.0f}** "
            f"| {s['min_specific_yield']:.0f}-{s['max_specific_yield']:.0f} "
            f"| {s['mean_transposition_gain']:.3f} "
            f"| {s['mean_performance_ratio']:.3f} "
            f"| {s['mean_cell_temperature_c']:.1f} C "
            f"| {s['n_in_band']}/{s['n']} |"
        )

    if suite.get("tilt_benefit_pct") is not None:
        add(
            f"\nTilting to the latitude optimum is worth "
            f"**{suite['tilt_benefit_pct']:+.1f}%** annually. Treating every roof "
            "as horizontal, as the legacy chain does, forgoes exactly that."
        )

    add("\n### Per site, latitude-optimal mount\n")
    add("| City | Zone | Tilt | GHI | POA | Gain | Cell T | Temp loss | PR | Specific yield |")
    add("|---|---|---:|---:|---:|---:|---:|---:|---:|---:|")
    for row in suite["records"]:
        if row["mount"] != "optimal_fixed":
            continue
        add(
            f"| {row['city']} | {row['zone']} | {row['tilt_deg']:.0f} deg "
            f"| {row['ghi_kwh_m2_yr']:.0f} | {row['poa_kwh_m2_yr']:.0f} "
            f"| {row['transposition_gain']:.3f} "
            f"| {row['mean_cell_temperature_c']:.1f} C "
            f"| {row['temperature_loss'] * 100:.1f}% "
            f"| {row['performance_ratio']:.3f} "
            f"| {row['specific_yield_kwh_per_kwp_yr']:.0f} |"
        )

    add("\n### Climatology discretisation error\n")
    add(
        "The physics chain runs on a 288-step monthly-diurnal climatology "
        "rather than 8760 hourly steps. This compares its implied annual GHI "
        "against the independent full-year daily series, so the "
        '"288 steps is enough" claim is measured rather than assumed.\n'
    )
    add("| City | 288-step | Full year | Error |")
    add("|---|---:|---:|---:|")
    errors = []
    for row in suite["climatology_discretisation"]:
        errors.append(abs(row["error_pct"]))
        add(
            f"| {row['city']} | {row['climatology_288_step_kwh_m2']:.0f} "
            f"| {row['full_year_daily_kwh_m2']:.0f} | {row['error_pct']:+.1f}% |"
        )
    if errors:
        add(
            f"\nMean absolute error **{sum(errors) / len(errors):.1f}%**, worst "
            f"**{max(errors):.1f}%**. The outliers are the monsoon-variable "
            "sites, where a single mid-month day represents its month least well."
        )
    return "\n".join(out) + "\n"
