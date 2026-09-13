"""
Penalty-layer tests, run against the numpy-backed fake Earth Engine.

``penalties.py`` is 489 lines carrying every coefficient in the model and had no
tests at all. It is also where the confirmed defects live.

Tests marked ``xfail(strict=True)`` are **executable specifications of known
defects**. They assert the physically correct answer, which the current code
does not produce. ``strict=True`` matters: once a defect is fixed the test
starts passing, the strict xfail turns that into a failure, and whoever fixed it
is forced to remove the marker. So the defect list cannot silently drift out of
date in either direction.

Defect references match docs/limitations.md and the plan:

* **D1** ``ee.Image.translate`` defaults to metres; the shadow and sky-view code
  passes pixel counts, so its offsets are 4x too short.
* **D2** the sky-view horizon angle divides the rise by ``d * 4`` metres while
  the translate only moved ``d`` metres, so obstruction is understated.
* **D4** MAIAC AOD is averaged with no ``AOD_QA`` masking.
* **D5** soiling loss is uncapped, so retention can go negative.
* **D9** focal kernels in pixel units resolve at the request projection.
* **D10** ``frequency`` raises ``IndexError`` on an empty position list.
"""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest
from tests.fakes import fake_ee as fake
from tests.fakes import synthetic as syn

PIXEL_M = fake.NATIVE_SCALE_M  # 4 m


@pytest.fixture(autouse=True)
def _use_fake_ee(monkeypatch):
    """Swap the fake in for the real client library in the module under test."""
    import solaris.gee.penalties as penalties

    monkeypatch.setattr(penalties, "ee", fake)
    fake.reset_registries()
    return penalties


@pytest.fixture
def penalties(_use_fake_ee):
    return _use_fake_ee


def _height_image(grid: np.ndarray):
    return fake.Image({"building_height": grid}).select("building_height")


def _band(image, scale: float = PIXEL_M, name: str | None = None) -> np.ndarray:
    bands = image.evaluate(scale)
    return bands[name] if name else bands[next(iter(bands))]


# ---------------------------------------------------------------------------
# ShadowPenalty
# ---------------------------------------------------------------------------


class TestShadowSelfShadowGuard:
    """A roof is never shadowed by a neighbour of equal height."""

    @pytest.mark.parametrize("height", [0.0, 3.0, 10.0, 40.0, 120.0])
    def test_uniform_height_casts_no_shadow(self, penalties, height):
        img = _height_image(syn.uniform_block(height=height))
        freq = penalties.ShadowPenalty.frequency(img, solar_positions=[(45.0, 180.0, 1.0)])
        arr = _band(freq)
        assert np.nanmax(arr) == 0.0, f"uniform height {height} m produced shadow"

    def test_flat_ground_casts_no_shadow(self, penalties):
        img = _height_image(syn.flat_ground())
        freq = penalties.ShadowPenalty.frequency(img, solar_positions=[(30.0, 150.0, 1.0)])
        assert np.nanmax(_band(freq)) == 0.0


class TestShadowFrequencyWeighting:
    def test_single_position_matches_its_own_mask(self, penalties):
        img = _height_image(syn.single_tower(height=40.0))
        mask = penalties.ShadowPenalty._mask_for_position(img, 45.0, 180.0, PIXEL_M)
        freq = penalties.ShadowPenalty.frequency(img, solar_positions=[(45.0, 180.0, 1.0)])
        np.testing.assert_allclose(np.nan_to_num(_band(freq)), np.nan_to_num(_band(mask)))

    def test_weights_combine_linearly(self, penalties):
        img = _height_image(syn.single_tower(height=40.0))
        p1, p2 = (45.0, 180.0), (30.0, 90.0)
        m1 = np.nan_to_num(_band(penalties.ShadowPenalty._mask_for_position(img, *p1, PIXEL_M)))
        m2 = np.nan_to_num(_band(penalties.ShadowPenalty._mask_for_position(img, *p2, PIXEL_M)))
        freq = penalties.ShadowPenalty.frequency(img, solar_positions=[(*p1, 0.3), (*p2, 0.7)])
        np.testing.assert_allclose(np.nan_to_num(_band(freq)), 0.3 * m1 + 0.7 * m2, atol=1e-12)

    def test_frequency_is_bounded(self, penalties):
        img = _height_image(syn.street_canyon(height=30.0))
        freq = penalties.ShadowPenalty.frequency(
            img, solar_positions=[(20.0, 120.0, 0.5), (60.0, 200.0, 0.5)]
        )
        arr = np.nan_to_num(_band(freq))
        assert arr.min() >= 0.0
        assert arr.max() <= 1.0 + 1e-12

    def test_weightless_two_tuples_get_uniform_weight(self, penalties):
        img = _height_image(syn.single_tower(height=40.0))
        freq = penalties.ShadowPenalty.frequency(img, solar_positions=[(45.0, 180.0), (30.0, 90.0)])
        assert np.nanmax(np.nan_to_num(_band(freq))) <= 1.0

    # Fixed: D10 -- frequency() now raises ValueError on an empty position list.
    def test_empty_position_list_raises_a_clear_error(self, penalties):
        img = _height_image(syn.single_tower())
        with pytest.raises(ValueError):
            penalties.ShadowPenalty.frequency(img, solar_positions=[])


class TestShadowGeometry:
    """
    Shadow length and direction against hand-computed geometry.

    A building of height ``H`` with the sun at altitude ``alt`` casts a shadow
    ``H / tan(alt)`` metres long, pointing towards ``azimuth + 180``. At 40 m
    and alt=45 that is 40 m, i.e. 10 pixels at 4 m/px.
    """

    TOWER_H = 40.0
    CENTRE = (32, 32)

    def _mask(self, penalties, alt, az):
        img = _height_image(syn.single_tower(height=self.TOWER_H, centre=self.CENTRE, size_px=1))
        return np.nan_to_num(
            _band(penalties.ShadowPenalty._mask_for_position(img, alt, az, PIXEL_M))
        )

    # Fixed: D1/D2 -- the shadow trace is directional and metre-denominated, so
    # length now tracks H/tan(alt).
    def test_shadow_extends_the_correct_distance(self, penalties):
        mask = self._mask(penalties, 45.0, 180.0)
        cy, cx = self.CENTRE
        expected_px = round(self.TOWER_H / math.tan(math.radians(45.0)) / PIXEL_M)
        shadowed = [r for r in range(cy - expected_px, cy) if mask[r, cx] > 0]
        assert len(shadowed) == expected_px

    # Fixed: D1/D2 -- near-field occlusion is tested at every pixel, so the shadow
    # is contiguous with the building that casts it.
    def test_shadow_is_adjacent_to_its_caster(self, penalties):
        mask = self._mask(penalties, 45.0, 180.0)
        cy, cx = self.CENTRE
        assert mask[cy - 1, cx] > 0, "pixel immediately north of the tower is sunlit"

    # Fixed: D1/D2 -- shadow reach is now H/tan(alt) rather than a fixed offset.
    def test_shadow_area_responds_to_sun_altitude(self, penalties):
        low = int(self._mask(penalties, 10.0, 180.0).sum())
        high = int(self._mask(penalties, 80.0, 180.0).sum())
        assert low > high, (
            f"a low sun must cast a larger shadow: alt=10 gave {low} px, alt=80 gave {high} px"
        )

    def test_shadow_points_away_from_the_sun(self, penalties):
        """
        The bearing convention (sin -> x, cos -> y, clockwise from north) is
        correct even though the distance is not: the single flagged pixel does
        land on the correct side of the tower. Worth pinning separately so the
        D1 fix cannot silently invert the direction while correcting the range.
        """
        cy, cx = self.CENTRE
        for az, (dy, dx) in {
            180.0: (-1, 0),  # sun south -> shadow north
            0.0: (1, 0),  # sun north -> shadow south
            90.0: (0, -1),  # sun east  -> shadow west
            270.0: (0, 1),  # sun west  -> shadow east
        }.items():
            mask = self._mask(penalties, 45.0, az)
            ys, xs = np.nonzero(mask)
            assert ys.size, f"no shadow at azimuth {az}"
            off_y, off_x = ys.mean() - cy, xs.mean() - cx
            assert np.sign(off_y) == dy or dy == 0, f"azimuth {az}: wrong N/S bearing"
            assert np.sign(off_x) == dx or dx == 0, f"azimuth {az}: wrong E/W bearing"


class TestScaleDependence:
    """
    Defect D9: focal kernels denominated in pixels resolve against the
    projection of the *request*, not the image. Nothing reprojects first, so the
    same expression can mean different things to /api/yield (reduced at
    scale=4) and /api/tiles (rendered at a coarse pyramid scale).
    """

    def test_shadow_frequency_is_currently_scale_invariant_but_only_by_accident(self, penalties):
        """
        Measured: the ``focal_max`` term genuinely does vary with request scale
        (31,417 px covered at scale=4 against 65,536 at scale=30 on a 256 px
        grid) -- that is D9. But the ``caster_h.gt(building_height)`` term admits
        exactly **one** pixel at either scale, so the conjunction comes out
        invariant regardless.

        In other words D2 currently masks D9 here. That has a direct sequencing
        consequence: fixing the caster-height test **without** also pinning the
        projection would turn the shadow layer scale-dependent for the first
        time. This test records today's behaviour so that regression is visible.
        """
        img = _height_image(syn.street_canyon(shape=(256, 256), height=30.0))
        freq = penalties.ShadowPenalty.frequency(img, solar_positions=[(30.0, 150.0, 1.0)])
        means = {
            s: round(float(np.nanmean(np.nan_to_num(_band(freq, scale=s)))), 9)
            for s in (4.0, 10.0, 30.0)
        }
        assert len(set(means.values())) == 1, (
            f"shadow frequency became scale-dependent: {means}. If D2 was just "
            "fixed, D9 now needs fixing too -- reproject the height raster "
            "before the focal operation."
        )

    # Fixed: D9 -- the background window is now 30 km in metres, not 30 pixels,
    # so it no longer moves with the request scale.
    def test_uhi_anomaly_is_independent_of_request_scale(self, penalties):
        lst = syn.gaussian_lst_hotspot(shape=(128, 128), peak_excess_c=6.0, sigma_px=16.0)
        syn.register_single_image_collection(
            "MODIS/061/MOD11A2", fake.Image.from_bands({"LST_Day_1km": lst})
        )
        aoi = fake.FakeGeometry.from_bbox_px(56, 72, 56, 72)
        deltas = {
            s: round(
                penalties.UHIPenalty.stats(aoi, "2023-01-01", scale_m=s)["delta_t_uhi_celsius"],
                3,
            )
            for s in (4.0, 100.0, 1000.0)
        }
        assert len(set(deltas.values())) == 1, f"UHI anomaly varies with request scale: {deltas}"


# ---------------------------------------------------------------------------
# SkyViewFactor
# ---------------------------------------------------------------------------


class TestSkyViewFactor:
    N_AZIMUTH = 8

    @pytest.mark.parametrize("height", [0.0, 10.0, 40.0])
    def test_uniform_height_sees_the_whole_sky(self, penalties, height):
        """Only taller neighbours occlude, so a flat surface must give SVF == 1."""
        img = _height_image(syn.uniform_block(height=height))
        arr = _band(penalties.SkyViewFactor.image(img))
        np.testing.assert_allclose(np.nan_to_num(arr, nan=1.0), 1.0, atol=1e-12)

    def test_svf_is_bounded(self, penalties):
        rng = np.random.default_rng(11)
        img = _height_image(rng.random((48, 48)) * 50.0)
        arr = np.nan_to_num(_band(penalties.SkyViewFactor.image(img)), nan=1.0)
        assert arr.min() >= 0.0
        assert arr.max() <= 1.0

    def test_svf_decreases_as_a_neighbour_grows(self, penalties):
        values = []
        for h in (0.0, 5.0, 20.0, 60.0):
            grid, probe = syn.wall_at_distance(wall_height=h, distance_px=4)
            arr = _band(penalties.SkyViewFactor.image(_height_image(grid)))
            values.append(float(np.nan_to_num(arr, nan=1.0)[probe]))
        assert all(a >= b for a, b in itertools.pairwise(values)), values

    def test_svf_increases_with_distance(self, penalties):
        values = []
        for d in (1, 2, 4, 8, 16):
            grid, probe = syn.wall_at_distance(wall_height=30.0, distance_px=d)
            arr = _band(penalties.SkyViewFactor.image(_height_image(grid)))
            values.append(float(np.nan_to_num(arr, nan=1.0)[probe]))
        assert values[-1] >= values[0], values

    def test_street_canyon_is_more_occluded_than_open_ground(self, penalties):
        canyon = _band(penalties.SkyViewFactor.image(_height_image(syn.street_canyon(height=30.0))))
        flat = _band(penalties.SkyViewFactor.image(_height_image(syn.uniform_block(height=0.0))))
        assert np.nanmean(np.nan_to_num(canyon, nan=1.0)) < np.nanmean(np.nan_to_num(flat, nan=1.0))

    @pytest.mark.parametrize("wall_height,distance_px", [(10.0, 1), (40.0, 4), (10.0, 16)])
    # Fixed: D2 -- horizon distances are in metres, matching translate()'s units.
    # Now agrees with the analytic value to 0.0 (was 0.964888 vs 0.892241).
    def test_svf_matches_the_analytic_single_wall_value(self, penalties, wall_height, distance_px):
        grid, probe = syn.wall_at_distance(wall_height=wall_height, distance_px=distance_px)
        arr = _band(penalties.SkyViewFactor.image(_height_image(grid)))
        got = float(np.nan_to_num(arr, nan=1.0)[probe])
        distance_m = distance_px * PIXEL_M
        horizon = math.atan(wall_height / distance_m)
        expected = 1.0 - (math.sin(horizon) ** 2) / self.N_AZIMUTH
        assert got == pytest.approx(expected, abs=1e-6)


# ---------------------------------------------------------------------------
# UHIPenalty
# ---------------------------------------------------------------------------


class TestUhiPenalty:
    def _register_lst(self, raw_grid):
        img = fake.Image.from_bands({"LST_Day_1km": raw_grid}, native_scale_m=fake.NATIVE_SCALE_M)
        syn.register_single_image_collection("MODIS/061/MOD11A2", img)

    @pytest.mark.parametrize(
        "delta_t,expected",
        [(0.0, 1.0), (2.0, 0.992), (4.0, 0.984), (6.0, 0.976)],
    )
    def test_derate_algebra(self, penalties, delta_t, expected):
        """derate = 1 + gamma * dT, with gamma = -0.004 per degC (IEC 60891)."""
        gamma = penalties.UHIPenalty.DEFAULT_TEMP_COEFF
        assert 1.0 + gamma * delta_t == pytest.approx(expected, abs=1e-9)

    def test_uniform_lst_yields_no_anomaly(self, penalties):
        """A city with no hot spot must produce delta_t == 0 and derate == 1."""
        self._register_lst(syn.gaussian_lst_hotspot(peak_excess_c=0.0))
        stats = penalties.UHIPenalty.stats(fake.FakeGeometry(), "2023-01-01")
        assert stats["delta_t_uhi_celsius"] == pytest.approx(0.0, abs=1e-6)
        assert stats["uhi_derate_factor"] == pytest.approx(1.0, abs=1e-6)

    def test_hotspot_produces_a_positive_anomaly_and_a_derate_below_one(self, penalties):
        self._register_lst(syn.gaussian_lst_hotspot(peak_excess_c=6.0, sigma_px=8.0))
        aoi = fake.FakeGeometry.from_bbox_px(28, 36, 28, 36)  # the hot core
        stats = penalties.UHIPenalty.stats(aoi, "2023-01-01")
        assert stats["delta_t_uhi_celsius"] > 0.0
        assert 0.95 < stats["uhi_derate_factor"] < 1.0

    def test_raw_to_celsius_conversion(self, penalties):
        """MODIS LST is raw * 0.02 kelvin, then minus 273.15."""
        self._register_lst(syn.gaussian_lst_hotspot(background_c=30.0, peak_excess_c=0.0))
        stats = penalties.UHIPenalty.stats(fake.FakeGeometry(), "2023-01-01")
        assert stats["mean_lst_day_celsius"] == pytest.approx(30.0, abs=0.05)

    def test_accounting_year_comes_from_the_start_date(self, penalties):
        self._register_lst(syn.gaussian_lst_hotspot(peak_excess_c=0.0))
        stats = penalties.UHIPenalty.stats(fake.FakeGeometry(), "2019-07-15")
        assert stats["accounting_year"] == 2019

    def test_all_masked_lst_falls_back_and_says_so(self, penalties):
        """A silent fallback must be distinguishable via the source tag."""
        self._register_lst(np.full(fake.DEFAULT_SHAPE, np.nan))
        stats = penalties.UHIPenalty.stats(fake.FakeGeometry(), "2023-01-01")
        assert stats["source"] == "fallback_zero"
        assert stats["delta_t_uhi_celsius"] == 0.0
        assert stats["uhi_derate_factor"] == 1.0
        assert stats["mean_lst_day_celsius"] == 35.0  # documented urban stand-in

    def test_reported_metadata_is_complete(self, penalties):
        self._register_lst(syn.gaussian_lst_hotspot(peak_excess_c=3.0))
        stats = penalties.UHIPenalty.stats(fake.FakeGeometry(), "2023-01-01")
        for key in (
            "delta_t_uhi_celsius",
            "mean_lst_day_celsius",
            "background_lst_celsius",
            "uhi_derate_factor",
            "temp_coeff_per_c",
            "source",
            "modis_collection",
            "accounting_year",
        ):
            assert key in stats
        assert stats["background_lst_celsius"] == pytest.approx(
            stats["mean_lst_day_celsius"] - stats["delta_t_uhi_celsius"], abs=1e-6
        )


# ---------------------------------------------------------------------------
# SoilingPenalty
# ---------------------------------------------------------------------------


class TestSoilingPenalty:
    def _register_aod(self, raw_aod, qa=None):
        bands = {"Optical_Depth_055": raw_aod}
        # The QA band is now always required, since aod_image() masks on it.
        # Default to "clear" (cloud-mask bits 001) so tests that do not care
        # about quality get every pixel.
        bands["AOD_QA"] = qa if qa is not None else np.full_like(raw_aod, 0b001)
        img = fake.Image.from_bands(bands, native_scale_m=fake.NATIVE_SCALE_M)
        syn.register_single_image_collection("MODIS/061/MCD19A2_GRANULES", img)

    @pytest.mark.parametrize("aod", [0.1, 0.5, 0.8, 1.2])
    def test_loss_is_linear_in_aod(self, penalties, aod):
        coeff = penalties.SoilingPenalty.SOILING_COEFFICIENT
        self._register_aod(np.full(fake.DEFAULT_SHAPE, aod / 0.001))
        stats = penalties.SoilingPenalty.stats(fake.FakeGeometry(), "2023-01-01")
        assert stats["mean_aod_550nm"] == pytest.approx(aod, abs=1e-4)
        assert stats["soiling_loss_fraction"] == pytest.approx(aod * coeff, abs=1e-4)
        assert stats["soiling_retention_factor"] == pytest.approx(1.0 - aod * coeff, abs=1e-4)

    def test_scale_factor_is_applied(self, penalties):
        """MCD19A2 Optical_Depth_055 is raw * 0.001."""
        self._register_aod(np.full(fake.DEFAULT_SHAPE, 600.0))
        stats = penalties.SoilingPenalty.stats(fake.FakeGeometry(), "2023-01-01")
        assert stats["mean_aod_550nm"] == pytest.approx(0.6, abs=1e-6)

    def test_all_masked_aod_falls_back_and_says_so(self, penalties):
        self._register_aod(np.full(fake.DEFAULT_SHAPE, np.nan))
        stats = penalties.SoilingPenalty.stats(fake.FakeGeometry(), "2023-01-01")
        assert stats["source"] == "fallback_urban_midpoint"
        assert stats["mean_aod_550nm"] == 0.50

    def test_urban_aod_gives_a_plausible_loss(self, penalties):
        """
        Delhi annual AOD runs 0.6-0.9. Measured soiling there is 0.24-0.47 %/day
        with 7-30 day cleaning, i.e. a few per cent to ~10% annually, so the
        model should land in single digits rather than tens of per cent.
        """
        self._register_aod(np.full(fake.DEFAULT_SHAPE, 750.0))
        stats = penalties.SoilingPenalty.stats(fake.FakeGeometry(), "2023-01-01")
        assert 0.02 < stats["soiling_loss_fraction"] < 0.12

    @pytest.mark.parametrize("aod", [13.0, 25.0])
    # Fixed: D5 -- retention is clamped at MIN_RETENTION and the binding is reported.
    def test_retention_never_goes_negative(self, penalties, aod):
        self._register_aod(np.full(fake.DEFAULT_SHAPE, aod / 0.001))
        stats = penalties.SoilingPenalty.stats(fake.FakeGeometry(), "2023-01-01")
        assert stats["soiling_retention_factor"] >= 0.0

    # Fixed: D4 -- aod_image() masks on the AOD_QA cloud bits before averaging.
    def test_low_quality_retrievals_are_excluded(self, penalties):
        raw, qa = syn.aod_with_bad_quality_pixels(
            clean_aod=0.60, corrupt_aod=5.00, corrupt_fraction=0.25
        )
        self._register_aod(raw, qa)
        stats = penalties.SoilingPenalty.stats(fake.FakeGeometry(), "2023-01-01")
        assert stats["mean_aod_550nm"] == pytest.approx(0.60, rel=0.05)


# ---------------------------------------------------------------------------
# net_irradiance_image
# ---------------------------------------------------------------------------


class TestNetIrradianceAlgebra:
    """
    The combination step, which is pure algebra and the cheapest thing in the
    module to get wrong:

        net = GHI * [diffuse * SVF + beam * (1 - shadow)] * uhi * soiling
    """

    SHAPE = (16, 16)
    BASELINE = 1000.0

    def _shadow(self, value: float):
        return fake.Image({"shadow_frequency": np.full(self.SHAPE, value)}).select(
            "shadow_frequency"
        )

    def _svf(self, value: float):
        return fake.Image({"sky_view_factor": np.full(self.SHAPE, value)}).select("sky_view_factor")

    def _net(self, penalties, **kw):
        img = penalties.net_irradiance_image(self.BASELINE, **kw)
        return float(np.nanmean(_band(img)))

    @pytest.mark.parametrize("beam", [0.0, 0.3, 0.6, 1.0])
    def test_no_penalties_returns_the_baseline(self, penalties, beam):
        got = self._net(
            penalties,
            shadow_frequency=self._shadow(0.0),
            beam_fraction=beam,
            sky_view_factor=self._svf(1.0),
        )
        assert got == pytest.approx(self.BASELINE, rel=1e-12)

    def test_svf_none_reduces_to_the_beam_only_form(self, penalties):
        """Documented behaviour: SVF=None means open sky, i.e. SVF=1."""
        args = {"shadow_frequency": self._shadow(0.4), "beam_fraction": 0.6}
        assert self._net(penalties, sky_view_factor=None, **args) == pytest.approx(
            self._net(penalties, sky_view_factor=self._svf(1.0), **args), rel=1e-12
        )

    def test_scalar_svf_matches_an_image_svf(self, penalties):
        args = {"shadow_frequency": self._shadow(0.4), "beam_fraction": 0.6}
        assert self._net(penalties, sky_view_factor=0.85, **args) == pytest.approx(
            self._net(penalties, sky_view_factor=self._svf(0.85), **args), rel=1e-12
        )

    @pytest.mark.parametrize("shadow", [0.0, 0.5, 1.0])
    def test_zero_beam_fraction_makes_shadow_irrelevant(self, penalties, shadow):
        """
        The test that would catch a beam/diffuse swap: with no direct beam,
        shadowing cannot change the answer.
        """
        got = self._net(
            penalties,
            shadow_frequency=self._shadow(shadow),
            beam_fraction=0.0,
            sky_view_factor=self._svf(1.0),
        )
        assert got == pytest.approx(self.BASELINE, rel=1e-12)

    @pytest.mark.parametrize("svf", [0.5, 0.8, 1.0])
    def test_full_beam_fraction_makes_svf_irrelevant(self, penalties, svf):
        got = self._net(
            penalties,
            shadow_frequency=self._shadow(0.25),
            beam_fraction=1.0,
            sky_view_factor=self._svf(svf),
        )
        assert got == pytest.approx(self.BASELINE * 0.75, rel=1e-12)

    def test_matches_the_documented_formula(self, penalties):
        shadow, svf, beam, uhi, soil = 0.3, 0.85, 0.62, 0.99, 0.95
        expected = self.BASELINE * ((1.0 - beam) * svf + beam * (1.0 - shadow)) * uhi * soil
        got = self._net(
            penalties,
            shadow_frequency=self._shadow(shadow),
            beam_fraction=beam,
            uhi_derate=uhi,
            soiling_retention=soil,
            sky_view_factor=self._svf(svf),
        )
        assert got == pytest.approx(expected, rel=1e-12)

    def test_derates_factorise(self, penalties):
        args = {
            "shadow_frequency": self._shadow(0.2),
            "beam_fraction": 0.6,
            "sky_view_factor": self._svf(0.9),
        }
        both = self._net(penalties, uhi_derate=0.98, soiling_retention=0.94, **args)
        separately = (
            self._net(penalties, uhi_derate=1.0, soiling_retention=1.0, **args) * 0.98 * 0.94
        )
        assert both == pytest.approx(separately, rel=1e-12)

    def test_linear_in_the_baseline(self, penalties):
        args = {
            "shadow_frequency": self._shadow(0.3),
            "beam_fraction": 0.6,
            "sky_view_factor": self._svf(0.9),
        }
        one = penalties.net_irradiance_image(1.0, **args)
        thousand = penalties.net_irradiance_image(1000.0, **args)
        assert float(np.nanmean(_band(thousand))) == pytest.approx(
            1000.0 * float(np.nanmean(_band(one))), rel=1e-12
        )

    def test_monotone_decreasing_in_shadow(self, penalties):
        vals = [
            self._net(
                penalties,
                shadow_frequency=self._shadow(s),
                beam_fraction=0.7,
                sky_view_factor=self._svf(0.9),
            )
            for s in (0.0, 0.25, 0.5, 0.75, 1.0)
        ]
        assert all(a > b for a, b in itertools.pairwise(vals)), vals

    def test_monotone_increasing_in_svf(self, penalties):
        vals = [
            self._net(
                penalties,
                shadow_frequency=self._shadow(0.3),
                beam_fraction=0.4,
                sky_view_factor=self._svf(v),
            )
            for v in (0.0, 0.25, 0.5, 0.75, 1.0)
        ]
        assert all(a < b for a, b in itertools.pairwise(vals)), vals

    def test_band_name_is_stable(self, penalties):
        img = penalties.net_irradiance_image(self.BASELINE, self._shadow(0.2), beam_fraction=0.6)
        assert "net_irradiance_kwh_m2_period" in img.evaluate(PIXEL_M)

    @pytest.mark.parametrize("shadow,svf,beam", [(0.0, 1.0, 0.6), (1.0, 0.0, 0.6), (0.5, 0.5, 0.5)])
    def test_output_never_exceeds_the_baseline(self, penalties, shadow, svf, beam):
        got = self._net(
            penalties,
            shadow_frequency=self._shadow(shadow),
            beam_fraction=beam,
            sky_view_factor=self._svf(svf),
        )
        assert 0.0 <= got <= self.BASELINE + 1e-9

    # Fixed: D5 -- net_irradiance_image() floors its output at zero.
    def test_output_is_non_negative_even_with_an_extreme_soiling_derate(self, penalties):
        retention = 1.0 - 13.0 * 0.08  # AOD 13 at the production coefficient
        got = self._net(
            penalties,
            shadow_frequency=self._shadow(0.2),
            beam_fraction=0.6,
            soiling_retention=retention,
            sky_view_factor=self._svf(0.9),
        )
        assert got >= 0.0


# ---------------------------------------------------------------------------
# Default position table
# ---------------------------------------------------------------------------


class TestDefaultSolarPositions:
    def test_weights_sum_to_one(self, penalties):
        positions = penalties._make_solar_positions()
        assert sum(w for _a, _z, w in positions) == pytest.approx(1.0, abs=1e-12)

    def test_low_altitude_entries_are_filtered(self, penalties):
        assert all(alt >= 2.0 for alt, _z, _w in penalties._make_solar_positions())

    def test_equinox_is_double_weighted(self, penalties):
        """The equinox stands in for both spring and autumn."""
        positions = penalties._make_solar_positions()
        equinox_noon = [w for alt, az, w in positions if az == 180.0 and 61 < alt < 63]
        summer_noon = [w for alt, az, w in positions if az == 180.0 and 83 < alt < 85]
        assert equinox_noon and summer_noon
        # sin(62) * 2 vs sin(84) * 1, before normalisation
        ratio = equinox_noon[0] / summer_noon[0]
        expected = 2 * math.sin(math.radians(62.0)) / math.sin(math.radians(84.0))
        assert ratio == pytest.approx(expected, rel=1e-9)

    def test_table_size_contradicts_its_own_docstring(self, penalties):
        """
        The docstring claims "18 representative (alt_deg, az_deg, weight)
        positions", but the table holds 21 entries and the lowest authored
        altitude is 3 deg, so the ``alt < 2.0`` filter removes none of them.
        A documentation defect rather than a behavioural one -- pinned here so
        the docstring gets corrected rather than the count quietly changing.
        """
        positions = penalties._make_solar_positions()
        assert len(positions) == 21
        assert min(alt for alt, _z, _w in positions) == 3.0
