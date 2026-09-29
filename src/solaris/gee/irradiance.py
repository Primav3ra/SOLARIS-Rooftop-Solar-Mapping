"""
ERA5 irradiance -- the GHI baseline and the beam/diffuse split.

GHI comes from ERA5-Land daily aggregates (ECMWF/ERA5_LAND/DAILY_AGGR, band
surface_solar_radiation_downwards_sum, ~9 km). It's accumulated J/m^2, so divide by 3.6e6
for kWh/m^2. Daily rather than hourly because the band is an accumulation -- the period
total is identical either way -- and a full-year sum costs 1.1 s instead of 25.7 s.

The beam fraction (how much of GHI is direct) needs the direct-radiation band, which
ERA5-Land doesn't carry -- so that part comes from plain ERA5 hourly (~28 km) as
sum(direct)/sum(GHI) over the period. Why we bother: shadows only knock out the direct
beam, diffuse still reaches a shadowed roof from the open sky. Over urban India it lands
around 0.55-0.72 a year -- higher in the dry season, lower under monsoon cloud.
"""

from __future__ import annotations

from typing import Any

import ee

from solaris.core import constants as _C

#: ERA5-Land **daily** aggregates, not hourly.
#:
#: The band is an accumulation, so summing 365 daily images gives exactly the
#: same period total as summing 8760 hourly ones -- verified to five
#: significant figures at several points, not assumed. What differs is the
#: cost: a cold full-year sum measured **25.7 s hourly against 1.1 s daily**,
#: a 23x speedup, and the hourly path was the single largest component of a
#: yearly query. Before this, yearly mode could not finish inside the
#: configured request timeout at all.
#:
#: A caution for anyone re-measuring: Earth Engine caches computation results
#: server-side, so a repeated query returns in milliseconds regardless of its
#: real cost. The figures above are from fresh points chosen to defeat that;
#: the first comparison attempted here read 428 ms for the hourly path purely
#: because it had already been run.
ERA5_COLLECTION = "ECMWF/ERA5_LAND/DAILY_AGGR"
ERA5_BAND = "surface_solar_radiation_downwards_sum"  # J/m^2 accumulated per day

ERA5_SCALE_M = _C.ERA5_SCALE_M  # 0.1 deg at equator (~9 km native)
_J_TO_KWH = 3_600_000.0

# ERA5 HOURLY (not ERA5-Land) -- used for beam/diffuse split only
_ERA5_HOURLY_COLLECTION = "ECMWF/ERA5/HOURLY"
_ERA5_HOURLY_GHI_BAND = "surface_solar_radiation_downwards"  # J/m^2 accumulated
_ERA5_HOURLY_DIRECT_BAND = "total_sky_direct_solar_radiation_at_surface"  # J/m^2 accumulated
_ERA5_HOURLY_SCALE_M = _C.ERA5_HOURLY_SCALE_M  # 0.25 deg (~28 km native)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _mean_over_aoi(image: ee.Image, band: str, aoi: ee.Geometry, scale: float) -> dict[str, Any]:
    """
    Robust mean over aoi: reduceRegion -> bestEffort -> centroid sample.
    Never clip the image before calling -- clipping a small AOI on a coarse
    image removes pixel centres and causes reduceRegion to return null.
    """

    def _reduce(best_effort: bool):
        return image.reduceRegion(
            reducer=ee.Reducer.mean(),
            geometry=aoi,
            scale=scale,
            maxPixels=1e9,
            tileScale=2,
            bestEffort=best_effort,
        ).getInfo()

    for be in (False, True):
        raw = _reduce(be)
        val = raw.get(band) if raw else None
        if val is not None:
            return {
                "value": float(val),
                "source": "reduceRegion_bestEffort" if be else "reduceRegion",
                "raw": raw,
            }

    centroid = aoi.centroid(1)
    fc = image.sample(region=centroid, scale=scale, numPixels=1, geometries=False)
    s = fc.first().getInfo() if fc.size().getInfo() > 0 else None
    if s and "properties" in s and band in s["properties"]:
        return {"value": float(s["properties"][band]), "source": "centroid_sample", "raw": s}

    return {"value": 0.0, "source": "fallback_zero", "raw": None}


def _era5_total(start_date: str, end_date_exclusive: str) -> ee.Image:
    return (
        ee.ImageCollection(ERA5_COLLECTION)
        .filterDate(start_date, end_date_exclusive)
        .select(ERA5_BAND)
        .sum()
        .divide(_J_TO_KWH)
        .rename("total_GHI_kWh_m2")
    )


# ---------------------------------------------------------------------------
# Public: yearly baseline
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Public: arbitrary date-range baseline
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Public: point sampling for /api/yield (centroid, period)
# ---------------------------------------------------------------------------


def sample_era5_period_ghi_kwh_m2_at_point(
    point: ee.Geometry,
    start_date: str,
    end_date_exclusive: str,
    scale_m: float = ERA5_SCALE_M,
) -> dict[str, Any]:
    """Period-integrated GHI (kWh/m^2) at a point (AOI centroid). Used by /api/yield."""
    img = _era5_total(start_date, end_date_exclusive)
    fc = img.sample(region=point, scale=scale_m, numPixels=1, geometries=False)
    if fc.size().getInfo() == 0:
        return {"value": 0.0, "source": "no_sample", "raw": None}
    s = fc.first().getInfo()
    val = (s.get("properties") or {}).get("total_GHI_kWh_m2") if s else None
    if val is None:
        return {"value": 0.0, "source": "null_band", "raw": s}
    return {"value": float(val), "source": "centroid_sample", "raw": s}


# ---------------------------------------------------------------------------
# Public: roof-masked baselines
# ---------------------------------------------------------------------------


def sample_era5_beam_fraction_at_point(
    point: ee.Geometry,
    start_date: str,
    end_date_exclusive: str,
    scale_m: float = _ERA5_HOURLY_SCALE_M,
) -> dict[str, Any]:
    """
    Period-mean beam fraction (direct / GHI) from ERA5 HOURLY at a point.

    Both numerator (direct horizontal) and denominator (GHI) come from the
    same ERA5 HOURLY collection so the ratio is internally consistent.
    ERA5-Land is NOT used here -- it lacks the direct radiation band.

    Formula
    -------
    beam_fraction = sum(total_sky_direct_solar_radiation_at_surface)
                  / sum(surface_solar_radiation_downwards)
    over [start_date, end_date_exclusive).

    This scalar is used to correct the shadow penalty:
      net_irr = GHI * (1 - shadow_frequency * beam_fraction)
    instead of the naive GHI * (1 - shadow_frequency) which assumed that
    100% of GHI is direct and fully blocked by building shadows.

    Fallback
    --------
    If sampling fails (no ERA5 coverage -- very unlikely for India):
    returns beam_fraction = 0.60, the conservative annual mean for
    cloudy urban India (fraction is lower during monsoon ~0.45,
    higher in dry winter ~0.75; 0.60 is a reasonable annual midpoint).

    Returns dict keys
    -----------------
    beam_fraction          -- direct / GHI  [0, 1]
    diffuse_fraction       -- 1 - beam_fraction
    direct_j_m2_period     -- sum of direct radiation J/m^2
    ghi_j_m2_period        -- sum of GHI J/m^2
    source                 -- "era5_hourly" | "fallback_*"
    """
    col = ee.ImageCollection(_ERA5_HOURLY_COLLECTION).filterDate(start_date, end_date_exclusive)
    ghi_img = col.select(_ERA5_HOURLY_GHI_BAND).sum().rename("ghi_sum")
    direct_img = col.select(_ERA5_HOURLY_DIRECT_BAND).sum().rename("direct_sum")
    combined = ghi_img.addBands(direct_img)

    fc = combined.sample(region=point, scale=scale_m, numPixels=1, geometries=False)
    if fc.size().getInfo() == 0:
        return {
            "beam_fraction": 0.60,
            "diffuse_fraction": 0.40,
            "direct_j_m2_period": None,
            "ghi_j_m2_period": None,
            "source": "fallback_no_sample",
            "collection": _ERA5_HOURLY_COLLECTION,
            "start_date": start_date,
            "end_date_exclusive": end_date_exclusive,
        }

    props = (fc.first().getInfo() or {}).get("properties", {})
    ghi_raw = props.get("ghi_sum")
    direct_raw = props.get("direct_sum")

    if ghi_raw is None or direct_raw is None or float(ghi_raw) <= 0:
        return {
            "beam_fraction": 0.60,
            "diffuse_fraction": 0.40,
            "direct_j_m2_period": direct_raw,
            "ghi_j_m2_period": ghi_raw,
            "source": "fallback_null_band",
            "collection": _ERA5_HOURLY_COLLECTION,
            "start_date": start_date,
            "end_date_exclusive": end_date_exclusive,
        }

    beam = min(float(direct_raw) / float(ghi_raw), 1.0)
    return {
        "beam_fraction": round(beam, 4),
        "diffuse_fraction": round(1.0 - beam, 4),
        "direct_j_m2_period": round(float(direct_raw), 1),
        "ghi_j_m2_period": round(float(ghi_raw), 1),
        "source": "era5_hourly",
        "collection": _ERA5_HOURLY_COLLECTION,
        "start_date": start_date,
        "end_date_exclusive": end_date_exclusive,
        "scale_m": scale_m,
    }


# ---------------------------------------------------------------------------
# Batched point sampling -- grab many sub-periods in one getInfo (for /api/series,
# so the curve isn't one ERA5 request per point).
# ---------------------------------------------------------------------------


def sample_era5_period_ghi_multi(
    point: ee.Geometry,
    windows: list,
    scale_m: float = ERA5_SCALE_M,
) -> list:
    """
    GHI (kWh/m^2) at a point for a whole list of windows, one getInfo for the lot.
    windows are (start, end_exclusive) ISO strings; result lines up with them (0.0 if a
    band comes back empty). Same per-window total as sample_era5_period_ghi_kwh_m2_at_point.
    """
    if not windows:
        return []
    img: ee.Image | None = None
    for i, (s, e) in enumerate(windows):
        band = _era5_total(s, e).rename(f"ghi_{i}")
        img = band if img is None else img.addBands(band)
    feat = img.sample(region=point, scale=scale_m, numPixels=1, geometries=False).first().getInfo()
    props = (feat or {}).get("properties", {}) if feat else {}
    return [float(props.get(f"ghi_{i}") or 0.0) for i in range(len(windows))]


def sample_era5_beam_multi(
    point: ee.Geometry,
    windows: list,
    scale_m: float = _ERA5_HOURLY_SCALE_M,
) -> list:
    """
    Beam fraction (direct / GHI) per window, one getInfo for all of them. Falls back to
    0.60 for any window that has no data, same as sample_era5_beam_fraction_at_point.
    """
    if not windows:
        return []
    img: ee.Image | None = None
    for i, (s, e) in enumerate(windows):
        col = ee.ImageCollection(_ERA5_HOURLY_COLLECTION).filterDate(s, e)
        d = col.select(_ERA5_HOURLY_DIRECT_BAND).sum().rename(f"d_{i}")
        g = col.select(_ERA5_HOURLY_GHI_BAND).sum().rename(f"g_{i}")
        pair = d.addBands(g)
        img = pair if img is None else img.addBands(pair)
    feat = img.sample(region=point, scale=scale_m, numPixels=1, geometries=False).first().getInfo()
    props = (feat or {}).get("properties", {}) if feat else {}
    out = []
    for i in range(len(windows)):
        gg = props.get(f"g_{i}")
        dd = props.get(f"d_{i}")
        if gg is not None and dd is not None and float(gg) > 0:
            out.append(min(float(dd) / float(gg), 1.0))
        else:
            out.append(0.60)
    return out
