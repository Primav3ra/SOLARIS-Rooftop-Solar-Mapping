"""
Daily precipitation over the selected window, for the soiling model.

Why this exists, and why it is worth a round-trip
-------------------------------------------------
The soiling model needs dry-spell lengths, not a rain total and not a rain-day
count. The difference is not marginal: accumulation is convex in spell length,
so substituting the *mean* spell understates the annual loss by a factor of
1.8x to 12.6x across the ten evaluation cities -- and worst in the arid cities
where soiling matters most. See :mod:`solaris.physics.soiling`.

So the window's actual daily series is sampled. It costs **one** Earth Engine
round-trip regardless of window length, because the reduction is built as a
server-side ``aggregate_array`` over the filtered collection and pulled back in
a single ``getInfo()``. A naive loop over days would be 365 round-trips for a
year and would dominate the whole request.

Units, which are the usual trap
-------------------------------
ERA5-Land's ``total_precipitation_sum`` is in **metres**, not millimetres. The
factor of 1000 is applied here and asserted in the tests, because a soiling
model silently fed metres would see 0.005 mm of rain on a 5 mm day, conclude it
never rains anywhere in India, and return a plausible-looking annual loss that
is far too high.
"""

from __future__ import annotations

from typing import Any

import ee

from solaris.core.constants import ERA5_SCALE_M

#: ERA5-Land daily aggregates. The daily collection rather than the hourly one:
#: the soiling model needs a daily accumulation to compare against a daily
#: threshold, and aggregating 8760 hourly images server-side to get it would be
#: needless work.
ERA5_LAND_DAILY = "ECMWF/ERA5_LAND/DAILY_AGGR"

#: Total precipitation, metres per day.
PRECIP_BAND = "total_precipitation_sum"

#: Metres to millimetres.
M_TO_MM = 1000.0


def daily_precip_image_collection(start_date: str, end_date_exclusive: str):
    """The ERA5-Land daily precipitation images covering the window."""
    return (
        ee.ImageCollection(ERA5_LAND_DAILY)
        .filterDate(start_date, end_date_exclusive)
        .select(PRECIP_BAND)
    )


def sample_era5_daily_precip(
    point: ee.Geometry,
    start_date: str,
    end_date_exclusive: str,
    scale_m: float = ERA5_SCALE_M,
) -> dict[str, Any]:
    """
    The daily rainfall series in mm at a point, in one round-trip.

    Returns ``{"precip_mm": [...], "n_days": int, "source": str}``. The source
    tag is always present and always honest, because a silent fallback here
    would degrade the soiling model by up to an order of magnitude without
    saying so:

    ``era5_land_daily``
        A real series. The accurate path.

    ``no_data``
        The collection returned nothing for this window -- most likely a window
        past ERA5-Land's publication lag, or a point outside its land mask
        (ERA5-*Land* is masked over open water, so a coastal AOI centroid can
        land on a masked cell). The caller must fall back to the rain-day
        approximation and say so in ``data_quality``.

    No exception is raised on an empty result: soiling is one term of many, and
    losing it should degrade the estimate rather than fail the request. What it
    must not do is degrade quietly.
    """
    collection = daily_precip_image_collection(start_date, end_date_exclusive)

    def _reduce(image: ee.Image) -> ee.Feature:
        value = image.reduceRegion(
            reducer=ee.Reducer.first(),
            geometry=point,
            scale=scale_m,
        ).get(PRECIP_BAND)
        return ee.Feature(None, {"p": value})

    # Built entirely server-side, then fetched once. aggregate_array over a
    # FeatureCollection is what keeps this at a single round-trip; mapping
    # getInfo() over the days would be one per day.
    try:
        values = ee.FeatureCollection(collection.map(_reduce)).aggregate_array("p").getInfo()
    except Exception:
        return {"precip_mm": [], "n_days": 0, "source": "no_data"}

    if not values:
        return {"precip_mm": [], "n_days": 0, "source": "no_data"}

    # A masked cell yields None rather than zero. Dropping it is right and
    # treating it as zero is not: a None is an unknown day, and calling it dry
    # would invent a dry spell and overstate the soiling.
    precip_mm = [float(v) * M_TO_MM for v in values if v is not None]
    if not precip_mm:
        return {"precip_mm": [], "n_days": 0, "source": "no_data"}

    return {
        "precip_mm": precip_mm,
        "n_days": len(precip_mm),
        "n_masked_days": len(values) - len(precip_mm),
        "total_mm": round(sum(precip_mm), 2),
        "source": "era5_land_daily",
    }


__all__ = [
    "ERA5_LAND_DAILY",
    "M_TO_MM",
    "PRECIP_BAND",
    "daily_precip_image_collection",
    "sample_era5_daily_precip",
]
