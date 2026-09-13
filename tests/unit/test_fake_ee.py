"""
Tests for the fake Earth Engine itself.

The fake is test infrastructure, so if it is wrong every penalty test built on
it is quietly wrong too. These tests pin the three things that actually matter:

1. **Arithmetic is real** -- the whole reason not to use ``MagicMock``.
2. **The two Earth Engine semantics the production code gets wrong are
   faithfully reproduced** -- ``translate`` defaulting to metres, and focal
   kernels resolving at the request projection. If the fake got these *right*
   in the sense of being forgiving, the defect tests would pass vacuously.
3. **Neighbourhood reductions are exact**, checked against brute force, because
   they are implemented with a 1-D decomposition for tractability at the
   production radius of 100 pixels.

The corresponding assertions about the *real* API live in the ``gee``-marked
probe tests; this file only checks the fake is self-consistent.
"""

from __future__ import annotations

import numpy as np
import pytest
from tests.fakes import fake_ee as fake
from tests.fakes import synthetic as syn


@pytest.fixture(autouse=True)
def _clean_registries():
    fake.reset_registries()
    yield
    fake.reset_registries()


def _one(image, scale: float = 4.0) -> np.ndarray:
    bands = image.evaluate(scale)
    return bands[next(iter(bands))]


def _reduce(image, reducer=None, **kw) -> float:
    reducer = reducer or fake.Reducer.mean()
    got = image.reduceRegion(reducer=reducer, scale=kw.pop("scale", 4.0), **kw).getInfo()
    return next(iter(got.values()))


class TestArithmeticIsReal:
    def test_scalar_arithmetic(self):
        expr = fake.Image(3.0).rename("a").multiply(4.0).add(1.0)
        assert _reduce(expr) == 13.0

    def test_image_arithmetic(self):
        a = fake.Image({"v": np.full((8, 8), 6.0)})
        b = fake.Image({"v": np.full((8, 8), 3.0)})
        assert _reduce(a.divide(b)) == 2.0
        assert _reduce(a.subtract(b)) == 3.0

    @pytest.mark.parametrize(
        "op,left,right,expected",
        [
            ("gt", 5.0, 3.0, 1.0),
            ("gt", 3.0, 5.0, 0.0),
            ("gte", 5.0, 5.0, 1.0),
            ("lt", 3.0, 5.0, 1.0),
            ("And", 1.0, 1.0, 1.0),
            ("And", 1.0, 0.0, 0.0),
        ],
    )
    def test_comparisons_yield_one_or_zero(self, op, left, right, expected):
        a = fake.Image({"v": np.full((4, 4), left)})
        b = fake.Image({"v": np.full((4, 4), right)})
        assert _reduce(getattr(a, op)(b)) == expected

    def test_trigonometry(self):
        img = fake.Image({"v": np.full((4, 4), np.pi / 2)})
        assert _reduce(img.sin()) == pytest.approx(1.0)
        assert _reduce(img.cos()) == pytest.approx(0.0, abs=1e-12)

    def test_clamp(self):
        img = fake.Image({"v": np.array([[-5.0, 0.5, 7.0, 0.2]])})
        assert _reduce(img.clamp(0.0, 1.0)) == pytest.approx((0.0 + 0.5 + 1.0 + 0.2) / 4)

    def test_constants_broadcast_against_any_grid(self):
        """``ee.Image(1.0)`` has no intrinsic extent, so it must broadcast."""
        grid = fake.Image({"v": np.full((7, 3), 2.0)})
        assert _reduce(fake.Image(1.0).subtract(grid)) == -1.0
        assert _reduce(grid.multiply(fake.Image(0.5))) == 1.0


class TestBands:
    def test_select_rename_addbands(self):
        img = fake.Image({"a": np.ones((4, 4)), "b": np.full((4, 4), 2.0)})
        assert img.select("b").band_names() == ["b"]
        assert img.select(["a", "b"]).band_names() == ["a", "b"]
        assert img.select("a").rename("z").band_names() == ["z"]
        merged = img.select("a").addBands(img.select("b").rename("c"))
        assert merged.band_names() == ["a", "c"]

    def test_selecting_a_missing_band_raises(self):
        img = fake.Image({"a": np.ones((4, 4))})
        with pytest.raises(KeyError, match="nope"):
            img.select("nope").band_names()

    def test_rename_arity_is_checked(self):
        img = fake.Image({"a": np.ones((4, 4)), "b": np.ones((4, 4))})
        with pytest.raises(ValueError, match="rename"):
            img.rename("only_one").band_names()


class TestMaskingAndReduction:
    def test_masked_pixels_are_skipped(self):
        arr = np.full((4, 4), 2.0)
        arr[0, :] = np.nan
        assert _reduce(fake.Image({"v": arr})) == 2.0
        assert _reduce(fake.Image({"v": arr}), fake.Reducer.sum()) == 24.0

    def test_fully_masked_band_reduces_to_none(self):
        """
        The behaviour that drives every fallback path in the production code:
        a band with no valid pixels comes back as ``None``, not 0.
        """
        got = (
            fake.Image({"v": np.full((4, 4), np.nan)})
            .reduceRegion(reducer=fake.Reducer.mean(), scale=4.0)
            .getInfo()
        )
        assert got == {"v": None}

    def test_geometry_restricts_the_reduction(self):
        arr = np.zeros((10, 10))
        arr[0:5, :] = 10.0
        img = fake.Image({"v": arr})
        assert _reduce(img) == 5.0
        top = fake.FakeGeometry.from_bbox_px(0, 5, 0, 10)
        assert _reduce(img, geometry=top) == 10.0
        bottom = fake.FakeGeometry.from_bbox_px(5, 10, 0, 10)
        assert _reduce(img, geometry=bottom) == 0.0

    def test_pixel_area_uses_the_native_scale(self):
        area = fake.Image.pixelArea()
        assert _reduce(area) == fake.NATIVE_SCALE_M**2

    def test_sample_reads_the_geometry_centroid(self):
        arr = np.zeros((10, 10))
        arr[2, 3] = 42.0
        img = fake.Image({"v": arr})
        at_point = fake.FakeGeometry.from_bbox_px(2, 3, 3, 4)
        feature = img.sample(region=at_point, scale=4.0).first().getInfo()
        assert feature["properties"]["v"] == 42.0

    def test_sample_of_masked_data_yields_an_empty_collection(self):
        img = fake.Image({"v": np.full((10, 10), np.nan)})
        assert img.sample(region=fake.FakeGeometry(), scale=4.0).size().getInfo() == 0


class TestTranslateSemantics:
    """
    ``Image.translate(x, y, units, proj)`` defaults ``units`` to **metres**.

    This is the semantic behind defect D1. If the fake treated bare offsets as
    pixels, the shadow and sky-view defect tests would pass and the bug would
    stay invisible.
    """

    def _moved_to(self, image, scale: float = 4.0) -> tuple[int, int]:
        arr = _one(image, scale)
        idx = np.argwhere(arr == 1.0)
        return tuple(int(v) for v in idx[0])

    @pytest.fixture
    def spike(self):
        grid = np.zeros((32, 32))
        grid[16, 16] = 1.0
        return fake.Image({"v": grid})

    def test_bare_offsets_are_metres(self, spike):
        """8 metres is 2 pixels at a 4 m native scale."""
        assert self._moved_to(spike.translate(8.0, 0.0)) == (16, 18)

    def test_explicit_pixel_units(self, spike):
        assert self._moved_to(spike.translate(8.0, 0.0, units="pixels")) == (16, 24)

    def test_metres_and_pixels_differ_by_the_native_scale(self, spike):
        """A pixel count passed without units comes out 4x too short."""
        as_metres = self._moved_to(spike.translate(8.0, 0.0))
        as_pixels = self._moved_to(spike.translate(8.0, 0.0, units="pixels"))
        assert (as_pixels[1] - 16) == (as_metres[1] - 16) * fake.NATIVE_SCALE_M

    def test_positive_y_is_north(self):
        """North is a decreasing row index, matching a north-up raster."""
        grid = np.zeros((32, 32))
        grid[16, 16] = 1.0
        moved = self._moved_to(fake.Image({"v": grid}).translate(0.0, 8.0))
        assert moved[0] < 16

    def test_unknown_units_are_rejected(self, spike):
        with pytest.raises(ValueError, match="units"):
            spike.translate(1.0, 0.0, units="furlongs").evaluate(4.0)

    def test_shifted_in_data_is_masked_not_zeroed(self, spike):
        """Out-of-frame data is unknown, so it must be nan rather than 0."""
        arr = _one(spike.translate(40.0, 0.0))
        assert np.isnan(arr[16, 0])


class TestFocalOperations:
    """
    Neighbourhood reductions are implemented as a 1-D decomposition, because the
    production shadow model uses a radius-100 kernel and neither a
    sliding-window stack nor scipy's footprint filters fit in memory at that
    size. Correctness is therefore checked against brute force.
    """

    @staticmethod
    def _brute_force_max(arr: np.ndarray, r: int) -> np.ndarray:
        ny, nx = arr.shape
        dy, dx = np.mgrid[-r : r + 1, -r : r + 1]
        foot = (dy * dy + dx * dx) <= r * r
        out = np.full_like(arr, -np.inf)
        for j in range(-r, r + 1):
            for i in range(-r, r + 1):
                if not foot[j + r, i + r]:
                    continue
                shifted = np.full_like(arr, -np.inf)
                shifted[max(0, -j) : min(ny, ny - j), max(0, -i) : min(nx, nx - i)] = arr[
                    max(0, j) : min(ny, ny + j), max(0, i) : min(nx, nx + i)
                ]
                out = np.maximum(out, shifted)
        return out

    @pytest.mark.parametrize("radius", [1, 2, 3, 5])
    def test_focal_max_matches_brute_force(self, radius):
        rng = np.random.default_rng(3)
        arr = rng.random((30, 30)) * 10.0
        got = _one(
            fake.Image({"v": arr}).focal_max(radius=radius, units="pixels"),
        )
        np.testing.assert_allclose(got, self._brute_force_max(arr, radius))

    @pytest.mark.parametrize("radius", [1, 2, 4])
    def test_focal_mean_matches_brute_force(self, radius):
        rng = np.random.default_rng(5)
        arr = rng.random((24, 24)) * 10.0
        ny, nx = arr.shape
        dy, dx = np.mgrid[-radius : radius + 1, -radius : radius + 1]
        foot = (dy * dy + dx * dx) <= radius * radius
        total = np.zeros_like(arr)
        count = np.zeros_like(arr)
        for j in range(-radius, radius + 1):
            for i in range(-radius, radius + 1):
                if not foot[j + radius, i + radius]:
                    continue
                sv = np.zeros_like(arr)
                sc = np.zeros_like(arr)
                sl = (
                    slice(max(0, -j), min(ny, ny - j)),
                    slice(max(0, -i), min(nx, nx - i)),
                )
                src = (
                    slice(max(0, j), min(ny, ny + j)),
                    slice(max(0, i), min(nx, nx + i)),
                )
                sv[sl] = arr[src]
                sc[sl] = 1.0
                total += sv
                count += sc
        got = _one(fake.Image({"v": arr}).focal_mean(radius=radius, units="pixels"))
        np.testing.assert_allclose(got, total / count)

    def test_circle_footprint_area(self):
        grid = np.zeros((32, 32))
        grid[16, 16] = 1.0
        got = _one(fake.Image({"v": grid}).focal_max(radius=2, units="pixels"))
        assert int((got == 1.0).sum()) == 13  # |{(dy,dx): dy^2+dx^2 <= 4}|

    def test_square_footprint_area(self):
        grid = np.zeros((32, 32))
        grid[16, 16] = 1.0
        got = _one(fake.Image({"v": grid}).focal_max(radius=2, kernelType="square", units="pixels"))
        assert int((got == 1.0).sum()) == 25

    def test_kernel_object_is_honoured(self):
        grid = np.zeros((32, 32))
        grid[16, 16] = 1.0
        kern = fake.Kernel.circle(radius=2, units="pixels", normalize=False)
        got = _one(fake.Image({"v": grid}).focal_max(kernel=kern))
        assert int((got == 1.0).sum()) == 13

    def test_uniform_input_is_unchanged_by_focal_mean(self):
        arr = np.full((16, 16), 7.0)
        got = _one(fake.Image({"v": arr}).focal_mean(radius=3, units="pixels"))
        np.testing.assert_allclose(got, 7.0)

    def test_zero_radius_is_a_no_op(self):
        rng = np.random.default_rng(9)
        arr = rng.random((8, 8))
        got = _one(fake.Image({"v": arr}).focal_max(radius=0, units="pixels"))
        np.testing.assert_allclose(got, arr)


class TestRequestScaleDependence:
    """
    Pixel-denominated kernels resolve against the request projection, which is
    defect D9. Reproducing it is the point; ``reproject`` is the escape hatch.
    """

    @pytest.fixture
    def spike(self):
        grid = np.zeros((128, 128))
        grid[64, 64] = 1.0
        return fake.Image({"v": grid})

    def test_footprint_grows_with_the_request_scale(self, spike):
        focal = spike.focal_max(radius=10, units="pixels")
        counts = [int(np.nansum(_one(focal, s))) for s in (4.0, 8.0, 16.0)]
        assert counts[0] < counts[1] < counts[2], counts

    def test_reproject_pins_the_scale(self, spike):
        focal = spike.reproject(crs="EPSG:4326", scale=4.0).focal_max(radius=10, units="pixels")
        counts = {int(np.nansum(_one(focal, s))) for s in (4.0, 8.0, 16.0)}
        assert len(counts) == 1, counts

    def test_metre_denominated_kernels_are_already_invariant(self, spike):
        focal = spike.focal_max(radius=40.0, units="meters")
        counts = {int(np.nansum(_one(focal, s))) for s in (4.0, 8.0, 16.0)}
        assert len(counts) == 1, counts

    def test_set_default_projection_scale_is_metres(self):
        """
        Earth Engine divides ``scale`` by the nominal size of a metre in the
        target CRS, so ``scale=4`` means 4 m even for EPSG:4326. Asserted
        against the live API by the gee-marked probe tests.
        """
        img = fake.Image({"v": np.zeros((8, 8))}).setDefaultProjection(crs="EPSG:4326", scale=4)
        assert img.projection().nominalScale().getInfo() == 4.0


class TestCollections:
    def test_sum_and_mean(self):
        syn.register_constant_hourly_collection("T/HOURLY", "ghi", 1.0e6, n_images=24, shape=(8, 8))
        col = fake.ImageCollection("T/HOURLY").select("ghi")
        assert _reduce(col.sum()) == pytest.approx(24.0e6)
        assert _reduce(fake.ImageCollection("T/HOURLY").select("ghi").mean()) == pytest.approx(
            1.0e6
        )

    def test_joule_to_kwh_conversion(self):
        """Pins the 3.6e6 divisor and the 'per hour' band semantics."""
        syn.register_constant_hourly_collection("T/HOURLY", "ghi", 1.0e6, n_images=24, shape=(8, 8))
        total = fake.ImageCollection("T/HOURLY").select("ghi").sum().divide(3_600_000.0)
        assert _reduce(total) == pytest.approx(24.0e6 / 3.6e6)

    def test_median(self):
        imgs = [fake.Image.from_bands({"v": np.full((4, 4), float(x))}) for x in (1.0, 5.0, 9.0)]
        syn.register_image_collection("T/MED", imgs)
        assert _reduce(fake.ImageCollection("T/MED").select("v").median()) == 5.0

    def test_filters_are_chainable(self):
        syn.register_constant_hourly_collection("T/HOURLY", "ghi", 1.0, n_images=3, shape=(4, 4))
        col = (
            fake.ImageCollection("T/HOURLY")
            .filterBounds(fake.FakeGeometry())
            .filterDate("2023-01-01", "2024-01-01")
            .select("ghi")
        )
        assert col.size().getInfo() == 3

    def test_unregistered_collection_fails_loudly(self):
        with pytest.raises(NotImplementedError, match="not registered"):
            fake.ImageCollection("NOT/REGISTERED")

    def test_unregistered_asset_fails_loudly(self):
        with pytest.raises(NotImplementedError, match="not registered"):
            fake.Image("NOT/REGISTERED")


class TestFailLoudly:
    def test_unimplemented_attribute_raises(self):
        with pytest.raises(NotImplementedError, match="not implemented"):
            fake.SomethingNobodyImplemented  # noqa: B018

    def test_dunders_raise_attribute_error_so_imports_work(self):
        """
        Python's import machinery probes ``__path__`` and friends; raising
        ``NotImplementedError`` from those would make the module unimportable.
        """
        with pytest.raises(AttributeError):
            fake.__nonexistent_dunder__  # noqa: B018

    def test_unsupported_focal_reducer_raises(self):
        img = fake.Image({"v": np.ones((8, 8))})
        with pytest.raises(NotImplementedError, match="focal reduction"):
            img._focal(np.nanstd, 2, "pixels", "circle").evaluate(4.0)


class TestTerrainAndGeometry:
    def test_flat_dem_has_zero_slope(self):
        dem = fake.Image({"elevation": syn.dem_flat(shape=(16, 16))})
        slope = fake.Terrain.products(dem).select("slope")
        assert _reduce(slope) == pytest.approx(0.0, abs=1e-12)

    def test_ramp_has_positive_slope(self):
        dem = fake.Image({"elevation": syn.dem_with_steep_slope(shape=(16, 16))})
        slope = fake.Terrain.products(dem).select("slope")
        assert _reduce(slope, fake.Reducer.max()) > 30.0

    def test_buffer_grows_the_bbox(self):
        geom = fake.FakeGeometry.from_bbox_px(10, 12, 10, 12)
        buffered = geom.buffer(8.0)  # 8 m = 2 px
        assert int(buffered.pixel_mask((32, 32)).sum()) > int(geom.pixel_mask((32, 32)).sum())

    def test_polygon_covers_the_whole_grid(self):
        """A lon/lat AOI passed straight through covers the synthetic grid."""
        geom = fake.Geometry.Polygon([[[77.2, 28.6], [77.3, 28.6], [77.3, 28.7]]])
        assert geom.pixel_mask((16, 16)).all()

    def test_clip_masks_outside_the_geometry(self):
        img = fake.Image({"v": np.ones((10, 10))})
        clipped = img.clip(fake.FakeGeometry.from_bbox_px(0, 5, 0, 10))
        arr = _one(clipped)
        assert not np.isnan(arr[0, 0])
        assert np.isnan(arr[9, 0])


class TestSyntheticFixtures:
    def test_uniform_block_is_uniform(self):
        assert np.ptp(syn.uniform_block(height=12.0)) == 0.0

    def test_single_tower_has_one_tall_pixel(self):
        grid = syn.single_tower(shape=(32, 32), height=40.0, centre=(16, 16))
        assert grid[16, 16] == 40.0
        assert int((grid > 0).sum()) == 1

    def test_wall_fixture_places_the_wall_where_documented(self):
        grid, probe = syn.wall_at_distance(
            shape=(32, 32), wall_height=10.0, distance_px=3, direction="east"
        )
        assert grid[probe] == 0.0
        assert grid[probe[0], probe[1] + 3] == 10.0

    def test_gaussian_hotspot_peak_excess_is_as_requested(self):
        raw = syn.gaussian_lst_hotspot(
            shape=(64, 64), background_c=30.0, peak_excess_c=6.0, sigma_px=8.0
        )
        celsius = raw * 0.02 - 273.15
        assert celsius.max() == pytest.approx(36.0, abs=0.05)
        assert celsius.min() == pytest.approx(30.0, abs=0.3)

    def test_aod_fixture_corrupts_only_qa_flagged_pixels(self):
        """
        AOD_QA bits 0-2 are MCD19A2's cloud mask, where 001 means *clear* --
        a set bit, not a cleared one, which is easy to invert.
        """
        raw, qa = syn.aod_with_bad_quality_pixels(
            shape=(64, 64), clean_aod=0.6, corrupt_aod=5.0, corrupt_fraction=0.25
        )
        aod = raw * 0.001
        clear = (qa.astype(int) & 0b111) == 0b001
        assert np.allclose(aod[clear], 0.6)
        assert np.allclose(aod[~clear], 5.0)
        # Averaging without the mask drags the mean far off the clear value.
        # That was defect D4; the masked mean now recovers 0.6.
        assert aod.mean() > 1.0
        assert np.isclose(aod[clear].mean(), 0.6)
