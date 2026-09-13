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
    fake.register_collection(
        "MODIS/061/MCD19A2_GRANULES",
        _constant_collection({"Optical_Depth_055": AOD_VALUE / 0.001}, 1),
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
