"""
Live Earth Engine tests. All marked ``gee`` and skipped without credentials.

Replaces four ``def main() -> int`` scripts that pytest never collected, two of
which returned 0 unconditionally and so could not fail at all.

Two distinct jobs here, and the second is the important one:

**Smoke tests** confirm the catalog IDs, band names and unit conversions still
hold. Dataset renames are the most likely cause of a silent production break
and nothing guarded them.

**Semantics probes** assert what the real Earth Engine API actually *does*, for
the two behaviours the production code depends on and the offline fake
reproduces. If a probe fails, the fake is wrong and every offline test built on
it is suspect. These probes are also the evidence base for the D1 and D9 fixes:
without them, "translate defaults to metres" is a claim in a commit message
rather than a property asserted against the live service.

Run with::

    pytest -m gee
"""

from __future__ import annotations

import math

import pytest

pytestmark = pytest.mark.gee

#: A small central-Delhi box, shared by every test here to keep requests cheap.
DELHI_SMALL = [
    [77.20, 28.58],
    [77.25, 28.58],
    [77.25, 28.62],
    [77.20, 28.62],
    [77.20, 28.58],
]

#: Global Solar Atlas (Solargis) long-term average GHI for Delhi.
#: NASA POWER gives 1753 kWh/m2/yr for 2020, so the two references disagree by
#: roughly 10%. The band below is deliberately wide enough to contain both --
#: no assertion here can be tighter than the spread between the references.
#: Narrow accuracy claims belong in the evals harness, with the spread reported.
GSA_DELHI_GHI = 1930.0
GHI_PLAUSIBLE_MIN = 1600.0
GHI_PLAUSIBLE_MAX = 2200.0


@pytest.fixture(scope="module")
def ee_session():
    import ee

    from solaris.api import deps

    deps.ensure_ee()
    return ee


@pytest.fixture(scope="module")
def aoi(ee_session):
    return ee_session.Geometry.Polygon(DELHI_SMALL)


# ---------------------------------------------------------------------------
# Dataset availability and schema
# ---------------------------------------------------------------------------


class TestDatasetSchema:
    """
    Catalog drift is the most likely cause of a silent production break, and
    nothing guarded against it before.
    """

    def test_srtm_dem_loads(self, ee_session, aoi):
        from solaris.gee.datasets import get_dem

        bands = get_dem(aoi, dem_type="srtm").bandNames().getInfo()
        assert "elevation" in bands

    def test_open_buildings_temporal_bands(self, ee_session, aoi):
        from solaris.gee.datasets import get_open_buildings_temporal

        bands = get_open_buildings_temporal(aoi, year=2022).bandNames().getInfo()
        for expected in (
            "building_presence",
            "building_height",
            "building_fractional_count",
        ):
            assert expected in bands

    def test_open_buildings_vector_has_footprints(self, ee_session, aoi):
        from solaris.gee.datasets import get_open_buildings_vector

        collection = get_open_buildings_vector(aoi, confidence_threshold=0.7)
        assert collection.size().getInfo() > 0
        properties = collection.first().getInfo()["properties"]
        assert "confidence" in properties
        assert "area_in_meters" in properties

    @pytest.mark.parametrize(
        "collection_id,band",
        [
            ("ECMWF/ERA5_LAND/HOURLY", "surface_solar_radiation_downwards_hourly"),
            ("ECMWF/ERA5/HOURLY", "surface_solar_radiation_downwards"),
            ("ECMWF/ERA5/HOURLY", "total_sky_direct_solar_radiation_at_surface"),
            ("MODIS/061/MOD11A2", "LST_Day_1km"),
            ("MODIS/061/MCD19A2_GRANULES", "Optical_Depth_055"),
        ],
    )
    def test_expected_band_exists(self, ee_session, aoi, collection_id, band):
        image = (
            ee_session.ImageCollection(collection_id)
            .filterBounds(aoi)
            .filterDate("2022-01-01", "2022-02-01")
            .first()
        )
        assert band in image.bandNames().getInfo(), f"{collection_id} no longer has a {band!r} band"

    def test_maiac_exposes_a_quality_band(self, ee_session, aoi):
        """
        Defect D4 is that the soiling layer never applies this mask. The band
        has to exist for the fix to be possible, so pin it.
        """
        image = (
            ee_session.ImageCollection("MODIS/061/MCD19A2_GRANULES")
            .filterBounds(aoi)
            .filterDate("2022-01-01", "2022-02-01")
            .first()
        )
        assert "AOD_QA" in image.bandNames().getInfo()


# ---------------------------------------------------------------------------
# Physical plausibility
# ---------------------------------------------------------------------------


class TestIrradianceMagnitudes:
    def test_annual_ghi_is_plausible_for_delhi(self, ee_session, aoi):
        from solaris.gee.irradiance import get_era5_baseline_info

        info = get_era5_baseline_info(aoi, start_year=2020, end_year=2024)
        ghi = info["mean_annual_ghi_kwh_m2_year"]
        assert GHI_PLAUSIBLE_MIN <= ghi <= GHI_PLAUSIBLE_MAX, (
            f"ERA5-Land annual GHI {ghi:.0f} kWh/m2/yr is outside the plausible "
            f"band [{GHI_PLAUSIBLE_MIN}, {GHI_PLAUSIBLE_MAX}] -- check the unit "
            "conversion or the collection id"
        )
        assert info["value_source"] != "fallback_zero", (
            "the reduction fell through to its silent-zero fallback"
        )

    @pytest.mark.parametrize(
        "start,end,lo,hi",
        [
            ("2022-06-01", "2022-06-02", 4.0, 9.0),  # clear pre-monsoon day
            ("2022-12-01", "2022-12-02", 2.0, 6.0),  # low winter sun
        ],
    )
    def test_daily_ghi_magnitude(self, ee_session, aoi, start, end, lo, hi):
        """Pins the J/m2 -> kWh/m2 divisor and the 'per hour' band semantics."""
        from solaris.gee.irradiance import get_era5_range_info

        total = get_era5_range_info(aoi, start, end)["range_total_ghi_kwh_m2"]
        assert lo <= total <= hi, f"{start}: {total:.2f} kWh/m2 outside [{lo}, {hi}]"

    def test_summer_exceeds_winter(self, ee_session, aoi):
        from solaris.gee.irradiance import get_era5_range_info

        june = get_era5_range_info(aoi, "2022-06-01", "2022-07-01")
        december = get_era5_range_info(aoi, "2022-12-01", "2023-01-01")
        assert june["range_total_ghi_kwh_m2"] > december["range_total_ghi_kwh_m2"]

    @pytest.mark.parametrize(
        "start,end,lo,hi",
        [
            ("2022-12-01", "2023-01-01", 0.45, 0.85),  # dry season, high beam
            ("2022-07-01", "2022-08-01", 0.20, 0.65),  # monsoon cloud, low beam
        ],
    )
    def test_beam_fraction_range(self, ee_session, aoi, start, end, lo, hi):
        """
        Loose bounds, but they would catch a direct/GHI inversion. NASA POWER
        puts Delhi's annual beam fraction near 0.53 and Q2-2026 near 0.74, so
        real seasonal spread is wide.
        """
        from solaris.gee.irradiance import sample_era5_beam_fraction_at_point

        info = sample_era5_beam_fraction_at_point(aoi.centroid(1), start, end)
        assert info["source"] == "era5_hourly", "fell back instead of sampling"
        assert lo <= info["beam_fraction"] <= hi
        assert info["beam_fraction"] + info["diffuse_fraction"] == pytest.approx(1.0, abs=1e-6)


class TestRooftopExtraction:
    def test_rooftop_area_is_positive_and_below_the_aoi(self, ee_session, aoi):
        from solaris.gee.rooftops import get_rooftop_area_m2_info

        info = get_rooftop_area_m2_info(aoi, year=2022)
        area = info["rooftop_candidate_area_m2"]
        aoi_area = info["aoi_area_km2"] * 1e6
        assert area > 0, "no rooftop pixels found in central Delhi"
        assert area < aoi_area, "rooftop area exceeds the AOI"

    def test_higher_presence_threshold_yields_less_area(self, ee_session, aoi):
        from solaris.gee.rooftops import get_rooftop_area_m2_info

        loose = get_rooftop_area_m2_info(aoi, year=2022, presence_threshold=0.3)
        strict = get_rooftop_area_m2_info(aoi, year=2022, presence_threshold=0.8)
        assert strict["rooftop_candidate_area_m2"] <= loose["rooftop_candidate_area_m2"]

    def test_roof_masked_baseline_is_consistent(self, ee_session, aoi):
        from solaris.gee.irradiance import get_roof_masked_era5_baseline_info
        from solaris.gee.layers import build_roof_layers

        layers = build_roof_layers(aoi, roof_year=2022, presence_threshold=0.5, min_height_m=0.0)
        info = get_roof_masked_era5_baseline_info(
            aoi, layers.roof_mask, start_year=2022, end_year=2022
        )
        assert info["roof_area_m2"] > 0
        assert GHI_PLAUSIBLE_MIN <= info["regional_irradiance_kwh_m2_year"] <= GHI_PLAUSIBLE_MAX
        assert info["pre_penalty_total_kwh_year"] == pytest.approx(
            info["regional_irradiance_kwh_m2_year"] * info["roof_area_m2"], rel=1e-6
        )


# ---------------------------------------------------------------------------
# API semantics probes -- the evidence base for the D1 and D9 fixes
# ---------------------------------------------------------------------------


class TestEarthEngineSemantics:
    """
    These assert what real Earth Engine does, not what SOLARIS does.

    They exist so that the offline fake's two most important behaviours are
    grounded in observed API behaviour rather than in documentation reading.
    """

    SCALE_M = 4.0

    @pytest.fixture
    def probe_image(self, ee_session):
        """Longitude in degrees, pinned to a 4 m grid."""
        return (
            ee_session.Image.pixelLonLat()
            .select("longitude")
            .setDefaultProjection(crs="EPSG:4326", scale=self.SCALE_M)
        )

    def test_set_default_projection_scale_is_metres(self, ee_session, probe_image):
        """
        Earth Engine divides ``scale`` by the nominal size of a metre in the
        target CRS, so ``scale=4`` means 4 m even for a geographic CRS. If this
        failed, the 4 m analysis grid would be degrees and everything downstream
        would be meaningless.
        """
        nominal = probe_image.projection().nominalScale().getInfo()
        assert nominal == pytest.approx(self.SCALE_M, rel=0.05)

    def test_translate_defaults_to_metres(self, ee_session, probe_image):
        """
        **The probe behind defect D1.**

        The shadow and sky-view models compute offsets in pixels and pass them
        to ``translate`` without units. If the default is metres, every one of
        those offsets is short by a factor of the pixel size.
        """
        point = ee_session.Geometry.Point([77.2090, 28.6139])
        lat_rad = math.radians(28.6139)
        metres_per_degree_lon = 111_320.0 * math.cos(lat_rad)

        def sample(image):
            feature = image.sample(region=point, scale=self.SCALE_M, numPixels=1).first()
            return feature.getInfo()["properties"]["longitude"]

        base = sample(probe_image)
        shifted_400m = sample(probe_image.translate(400.0, 0.0))
        moved_m = abs(shifted_400m - base) * metres_per_degree_lon

        assert moved_m == pytest.approx(400.0, rel=0.15), (
            f"translate(400, 0) moved {moved_m:.0f} m. If it moved ~1600 m the "
            "default is pixels, and the premise of the D1 fix is wrong."
        )

    def test_translate_with_explicit_pixel_units_differs(self, ee_session, probe_image):
        """100 pixels at 4 m must be 400 m, i.e. 4x a bare ``translate(100, 0)``."""
        point = ee_session.Geometry.Point([77.2090, 28.6139])

        def sample(image):
            feature = image.sample(region=point, scale=self.SCALE_M, numPixels=1).first()
            return feature.getInfo()["properties"]["longitude"]

        base = sample(probe_image)
        as_metres = abs(sample(probe_image.translate(100.0, 0.0)) - base)
        as_pixels = abs(sample(probe_image.translate(100.0, 0.0, units="pixels")) - base)
        assert as_pixels == pytest.approx(as_metres * self.SCALE_M, rel=0.15), (
            f"pixel offset {as_pixels:.6f} deg vs metre offset {as_metres:.6f} deg; "
            f"expected a factor of {self.SCALE_M}"
        )

    def test_pixel_denominated_focal_kernels_depend_on_request_scale(self, ee_session, aoi):
        """
        **The probe behind defect D9.**

        A kernel in pixel units resolves against the projection of the request,
        so the same expression covers a different physical area depending on the
        scale it is evaluated at. The production shadow and UHI layers never
        reproject first.
        """
        from solaris.gee.datasets import get_open_buildings_temporal

        height = (
            get_open_buildings_temporal(aoi, year=2022)
            .select("building_height")
            .setDefaultProjection(crs="EPSG:4326", scale=self.SCALE_M)
        )
        dilated = height.focal_max(radius=10, kernelType="circle", units="pixels")

        def mean_at(scale):
            return dilated.reduceRegion(
                reducer=ee_session.Reducer.mean(),
                geometry=aoi,
                scale=scale,
                maxPixels=1e8,
                bestEffort=True,
            ).getInfo()["building_height"]

        fine, coarse = mean_at(4.0), mean_at(40.0)
        assert fine is not None and coarse is not None
        assert fine != pytest.approx(coarse, rel=1e-3), (
            "the focal result did not change with request scale; if Earth Engine "
            "no longer behaves this way, the D9 fix and the fake both need revising"
        )

    def test_reproject_makes_focal_scale_invariant(self, ee_session, aoi):
        """The fix for D9: pin the projection before the neighbourhood op."""
        from solaris.gee.datasets import get_open_buildings_temporal

        height = (
            get_open_buildings_temporal(aoi, year=2022)
            .select("building_height")
            .reproject(crs="EPSG:4326", scale=self.SCALE_M)
        )
        dilated = height.focal_max(radius=10, kernelType="circle", units="pixels")

        def mean_at(scale):
            return dilated.reduceRegion(
                reducer=ee_session.Reducer.mean(),
                geometry=aoi,
                scale=scale,
                maxPixels=1e8,
                bestEffort=True,
            ).getInfo()["building_height"]

        assert mean_at(4.0) == pytest.approx(mean_at(40.0), rel=0.02)
