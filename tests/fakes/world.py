"""
A complete synthetic Earth Engine "world": every asset and collection the API
touches, registered in one call.

This is what lets ``/api/yield`` and ``/api/series`` run end to end with no
credentials and no network. Two things fall out of that:

* the 1191-line request handler becomes testable at all, and
* there is a deterministic golden output to compare against, so the handler can
  be split into routers and *proved* not to have changed behaviour.

Everything is deliberately simple and fixed (no randomness, no date
dependence), so golden values are stable. The numbers are physically plausible
for Delhi but are **not** meant to be accurate -- these are contract and
regression tests, not validation. Validation against real references is the
job of the ``evals`` harness.
"""

from __future__ import annotations

import numpy as np
from tests.fakes import fake_ee as fake
from tests.fakes import synthetic as syn
from tests.fakes.fake_ee import FakeFeature, FakeFeatureCollection, FakeImage

#: Grid used for the synthetic city.
SHAPE = (64, 64)


#: A mixed urban block: flat ground, a low-rise terrace and one tower, so the
#: shadow and sky-view layers have something to bite on.
def city_heights() -> np.ndarray:
    grid = np.zeros(SHAPE)
    grid[8:56, 8:56] = 9.0  # low-rise fabric
    grid[20:26, 20:26] = 24.0  # mid-rise block
    grid[40:44, 40:44] = 48.0  # tower
    return grid


#: Open Buildings presence: high over the built fabric, low elsewhere.
def city_presence() -> np.ndarray:
    presence = np.full(SHAPE, 0.05)
    presence[8:56, 8:56] = 0.92
    return presence


# ---------------------------------------------------------------------------
# Fixed per-dataset values. Chosen to be plausible for Delhi.
# ---------------------------------------------------------------------------

#: Daily GHI accumulation, J/m^2, chosen so a full non-leap year sums to the
#: same annual total the hourly fixture produced. Sub-year windows now scale
#: with their length, which the previous constant-collection fixture did not do
#: -- it returned the annual total for a one-month window, so nothing tested
#: that a shorter window yields less energy.
ERA5_LAND_J_PER_DAY = 1900.0 / 365.0 * 3_600_000.0

#: ERA5-Land GHI, J/m^2 per hour. 8760 hourly images would be slow, so the
#: collection returns N images whose sum reproduces a realistic annual total.
ERA5_LAND_IMAGES = 12
ERA5_LAND_J_PER_IMAGE = 1900.0 / 12 * 3_600_000.0  # -> 1900 kWh/m2/yr

#: ERA5 hourly direct / GHI, giving a beam fraction of exactly 0.62.
ERA5_HOURLY_IMAGES = 12
ERA5_HOURLY_GHI_J = 1900.0 / 12 * 3_600_000.0
ERA5_HOURLY_DIRECT_J = ERA5_HOURLY_GHI_J * 0.62

#: MODIS LST: 30 C background with a 4 C urban hot spot.
LST_BACKGROUND_C = 30.0
LST_PEAK_EXCESS_C = 4.0

#: MAIAC AOD, raw units (x0.001). 0.70 is a plausible Delhi annual mean.
AOD_VALUE = 0.70

#: SRTM elevation: flat, so the slope exclusion keeps the whole grid.
ELEVATION_M = 216.0

#: ERA5-Land daily precipitation, in **metres** as the real band is.
#:
#: Shaped rather than constant, because the soiling model is driven by dry-spell
#: length and a constant series has no spells at all.
#:
#: The pattern is the **measured** Delhi 2021 monthly rain-day count from NASA
#: POWER, not an invented one. That matters: the first version of this fixture
#: was a stylised two-month monsoon giving 20 cleaning-rain days a year, against
#: Delhi's real 91. The synthetic year was therefore arid enough to saturate the
#: soiling model at its 30% ceiling, which made soiling 89% of all modelled loss
#: and broke the penalty-balance assertions -- not because the assertions were
#: wrong, but because the world was.
#:
#: Days per month on which it rains, keyed by month. Real total: 91.
PRECIP_RAIN_DAYS_BY_MONTH = {
    1: 4,
    2: 1,
    3: 1,
    4: 1,
    5: 8,
    6: 11,
    7: 21,
    8: 17,
    9: 20,
    10: 6,
    11: 0,
    12: 1,
}

#: Rainfall on a wet day, mm. Well clear of the 1 mm cleaning threshold, so the
#: test is about spell structure rather than about threshold arithmetic.
PRECIP_WET_DAY_MM = 8.0
PRECIP_DRY_DAY_MM = 0.0

#: The Open Buildings v3 footprint returned for a clicked point.
BUILDING_AREA_M2 = 240.0
BUILDING_CONFIDENCE = 0.86


def _constant_collection(bands: dict[str, float], n_images: int):
    def builder(_start=None, _end=None):
        return [
            FakeImage.from_bands(
                {k: np.full(SHAPE, float(v)) for k, v in bands.items()},
                native_scale_m=fake.NATIVE_SCALE_M,
            )
            for _ in range(n_images)
        ]

    return builder


def _daily_precip_builder(start=None, end=None):
    """
    Daily precipitation images covering the requested window, in metres.

    This **honours the dates**, unlike the constant collections in this world.
    It has to: the soiling model divides accumulated loss by the length of the
    series, so a builder that returned a whole year for a one-day window would
    make every sub-year window look catastrophically soiled. That is exactly
    what happened first time round -- a single November day came back at a 50%
    loss -- and it found a real defect in the production code, which was taking
    its denominator from the request rather than from the data.
    """
    import calendar
    import datetime as _dt

    first = _dt.date(2023, 1, 1) if not start else _dt.date.fromisoformat(str(start)[:10])
    last = _dt.date(2024, 1, 1) if not end else _dt.date.fromisoformat(str(end)[:10])

    images = []
    for month in range(1, 13):
        days_in_month = calendar.monthrange(2023, month)[1]
        rain_days = PRECIP_RAIN_DAYS_BY_MONTH.get(month, 0)
        # Spread the month's rain days evenly. Even spacing understates the
        # real clustering, so the synthetic soiling loss is a little lower than
        # the real Delhi figure -- an error in the safe direction for a fixture
        # whose job is to be representative rather than adversarial.
        wet_days = (
            {round((i + 0.5) * days_in_month / rain_days) for i in range(rain_days)}
            if rain_days
            else set()
        )
        for day in range(1, days_in_month + 1):
            when = _dt.date(first.year, month, day)
            if not (first <= when < last):
                continue
            mm = PRECIP_WET_DAY_MM if day in wet_days else PRECIP_DRY_DAY_MM
            images.append(
                FakeImage.from_bands(
                    {
                        # Metres, as ERA5-Land publishes it. Converting is the
                        # production code's job, and that is the point.
                        "total_precipitation_sum": np.full(SHAPE, mm / 1000.0),
                        # Radiation lives in the same daily collection, because
                        # in the real catalogue it does. GHI moved here from the
                        # hourly collection for a 23x speedup, and the band is
                        # an accumulation so the period total is unchanged.
                        "surface_solar_radiation_downwards_sum": np.full(
                            SHAPE, ERA5_LAND_J_PER_DAY
                        ),
                    },
                    native_scale_m=fake.NATIVE_SCALE_M,
                )
            )
    return images


def expected_cleaning_rain_days() -> int:
    """The cleaning-rain day count the world is built to produce."""
    return sum(PRECIP_RAIN_DAYS_BY_MONTH.values())


def register_world() -> None:
    """Register every asset and collection the API reads. Idempotent."""
    fake.reset_registries()

    # -- terrain ----------------------------------------------------------
    fake.register_asset(
        "USGS/SRTMGL1_003",
        FakeImage.from_bands({"elevation": np.full(SHAPE, ELEVATION_M)}),
    )

    # -- buildings (raster) ----------------------------------------------
    buildings = FakeImage.from_bands(
        {
            "building_presence": city_presence(),
            "building_height": city_heights(),
            "building_fractional_count": np.ones(SHAPE),
        },
        native_scale_m=fake.NATIVE_SCALE_M,
    )
    fake.register_collection(
        "GOOGLE/Research/open-buildings-temporal/v1",
        lambda _s=None, _e=None: [buildings],
    )

    # -- buildings (vector) ----------------------------------------------
    # A footprint covering the mid-rise block, so the per-building reduction
    # has a non-trivial region.
    footprint = FakeFeature(
        {"confidence": BUILDING_CONFIDENCE, "area_in_meters": BUILDING_AREA_M2},
        geometry=fake.FakeGeometry.from_bbox_px(20, 26, 20, 26),
    )
    fake.register_collection(
        "GOOGLE/Research/open-buildings/v3/polygons",
        lambda *_a, **_kw: FakeFeatureCollection([footprint]),
    )

    # -- irradiance -------------------------------------------------------
    fake.register_collection(
        "ECMWF/ERA5_LAND/HOURLY",
        _constant_collection(
            {"surface_solar_radiation_downwards_hourly": ERA5_LAND_J_PER_IMAGE},
            ERA5_LAND_IMAGES,
        ),
    )
    fake.register_collection("ECMWF/ERA5_LAND/DAILY_AGGR", _daily_precip_builder)
    fake.register_collection(
        "ECMWF/ERA5/HOURLY",
        _constant_collection(
            {
                "surface_solar_radiation_downwards": ERA5_HOURLY_GHI_J,
                "total_sky_direct_solar_radiation_at_surface": ERA5_HOURLY_DIRECT_J,
            },
            ERA5_HOURLY_IMAGES,
        ),
    )

    # -- temperature / aerosol -------------------------------------------
    fake.register_collection(
        "MODIS/061/MOD11A2",
        lambda _s=None, _e=None: [
            FakeImage.from_bands(
                {
                    "LST_Day_1km": syn.gaussian_lst_hotspot(
                        shape=SHAPE,
                        background_c=LST_BACKGROUND_C,
                        peak_excess_c=LST_PEAK_EXCESS_C,
                        sigma_px=12.0,
                    )
                },
                native_scale_m=fake.NATIVE_SCALE_M,
            )
        ],
    )
    # AOD_QA bits 0-2 are the cloud mask; 001 = clear. Every pixel here is
    # clear, so the QA-masked mean equals AOD_VALUE exactly.
    fake.register_collection(
        "MODIS/061/MCD19A2_GRANULES",
        _constant_collection({"Optical_Depth_055": AOD_VALUE / 0.001, "AOD_QA": 0b001}, 1),
    )


#: The AOI every golden test uses. Centred on the synthetic city.
AOI_REQUEST = {
    "lat": 28.6139,
    "lon": 77.2090,
    "half_size_deg": 0.01,
    "roof_year": 2022,
    "baseline_mode": "yearly",
    "year": 2023,
}


def expected_beam_fraction() -> float:
    """The beam fraction the world is constructed to produce."""
    return ERA5_HOURLY_DIRECT_J / ERA5_HOURLY_GHI_J


def expected_annual_ghi_kwh_m2() -> float:
    """The ERA5-Land annual GHI the world is constructed to produce."""
    return ERA5_LAND_IMAGES * ERA5_LAND_J_PER_IMAGE / 3_600_000.0
