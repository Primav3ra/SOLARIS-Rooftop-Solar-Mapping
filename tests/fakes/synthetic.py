"""
Synthetic rasters whose expected results can be worked out by hand.

The point of every fixture here is that the right answer is known *analytically*,
not by running the code and recording what it produced. A golden-file fixture
captured from the implementation would have happily locked in the 4x translate
bug; a wall at a known height and distance has a sky-view factor you can compute
with a calculator.
"""

from __future__ import annotations

import numpy as np
from tests.fakes import fake_ee
from tests.fakes.fake_ee import DEFAULT_SHAPE, NATIVE_SCALE_M, FakeImage

# ---------------------------------------------------------------------------
# Building-height rasters
# ---------------------------------------------------------------------------


def flat_ground(shape: tuple[int, int] = DEFAULT_SHAPE, height: float = 0.0):
    """Uniform height. Shadow frequency must be exactly 0 everywhere."""
    return np.full(shape, float(height))


def uniform_block(shape: tuple[int, int] = DEFAULT_SHAPE, height: float = 10.0) -> np.ndarray:
    """
    Uniform non-zero height.

    Key invariant: the shadow model requires a *taller* neighbour, so a uniform
    surface must yield shadow_frequency == 0 and sky_view_factor == 1 no matter
    how tall it is. This is the invariant most likely to break while fixing the
    translate units, which is why it gets its own fixture.
    """
    return np.full(shape, float(height))


def single_tower(
    shape: tuple[int, int] = DEFAULT_SHAPE,
    height: float = 40.0,
    centre: tuple[int, int] | None = None,
    size_px: int = 1,
) -> np.ndarray:
    """
    One tall block on flat ground.

    With the sun at altitude ``alt``, the shadow reaches ``height / tan(alt)``
    metres, i.e. ``height / (tan(alt) * NATIVE_SCALE_M)`` pixels, in the
    direction ``azimuth + 180``. At 40 m and alt=45 that is 40 m = 10 pixels.
    """
    grid = np.zeros(shape)
    cy, cx = centre or (shape[0] // 2, shape[1] // 2)
    half = size_px // 2
    grid[cy - half : cy + half + 1, cx - half : cx + half + 1] = float(height)
    return grid


def wall_at_distance(
    shape: tuple[int, int] = DEFAULT_SHAPE,
    wall_height: float = 10.0,
    distance_px: int = 1,
    direction: str = "east",
) -> tuple[np.ndarray, tuple[int, int]]:
    """
    A single wall pixel at a known pixel distance from a probe pixel.

    Returns ``(height_grid, probe_index)``. The sky-view factor at the probe is
    analytically::

        SVF = 1 - sin^2(atan(wall_height / (distance_px * NATIVE_SCALE_M))) / N_AZIMUTH

    because only one of the eight sampled azimuths sees an obstruction.
    """
    grid = np.zeros(shape)
    py, px = shape[0] // 2, shape[1] // 2
    offsets = {
        "east": (0, distance_px),
        "west": (0, -distance_px),
        "north": (-distance_px, 0),
        "south": (distance_px, 0),
    }
    dy, dx = offsets[direction]
    grid[py + dy, px + dx] = float(wall_height)
    return grid, (py, px)


def street_canyon(
    shape: tuple[int, int] = DEFAULT_SHAPE,
    height: float = 30.0,
    gap_px: int = 3,
) -> np.ndarray:
    """Two parallel rows of tall buildings with a gap: low sky-view in between."""
    grid = np.zeros(shape)
    mid = shape[0] // 2
    grid[mid - gap_px - 2 : mid - gap_px, :] = float(height)
    grid[mid + gap_px : mid + gap_px + 2, :] = float(height)
    return grid


# ---------------------------------------------------------------------------
# Registered Earth Engine images / collections
# ---------------------------------------------------------------------------


def building_image(
    height_grid: np.ndarray,
    presence: float | np.ndarray = 1.0,
    fractional_count: float = 1.0,
) -> FakeImage:
    """An Open Buildings 2.5D-shaped image with the three bands the code selects."""
    shape = height_grid.shape
    pres = (
        np.full(shape, float(presence))
        if np.isscalar(presence)
        else np.asarray(presence, dtype=float)
    )
    return FakeImage.from_bands(
        {
            "building_presence": pres,
            "building_height": np.asarray(height_grid, dtype=float),
            "building_fractional_count": np.full(shape, float(fractional_count)),
        },
        native_scale_m=NATIVE_SCALE_M,
    )


def gaussian_lst_hotspot(
    shape: tuple[int, int] = DEFAULT_SHAPE,
    background_c: float = 30.0,
    peak_excess_c: float = 6.0,
    sigma_px: float = 8.0,
) -> np.ndarray:
    """
    MODIS-style raw LST integers with a Gaussian urban hot spot.

    Returned in *raw* units so the code's ``* 0.02 - 273.15`` conversion is
    exercised rather than bypassed. Peak anomaly over background is
    ``peak_excess_c``.
    """
    ny, nx = shape
    y, x = np.mgrid[0:ny, 0:nx]
    cy, cx = ny / 2.0, nx / 2.0
    excess = peak_excess_c * np.exp(-(((y - cy) ** 2 + (x - cx) ** 2) / (2.0 * sigma_px**2)))
    celsius = background_c + excess
    kelvin = celsius + 273.15
    return kelvin / 0.02


def aod_with_bad_quality_pixels(
    shape: tuple[int, int] = DEFAULT_SHAPE,
    clean_aod: float = 0.60,
    corrupt_aod: float = 5.00,
    corrupt_fraction: float = 0.25,
    seed: int = 7,
) -> tuple[np.ndarray, np.ndarray]:
    """
    MAIAC-style raw AOD plus a QA band, with deliberately absurd values on the
    QA-flagged pixels.

    Returns ``(raw_aod, qa)``. The production code takes ``.mean()`` with **no**
    QA masking, so the unmasked mean is dragged far above the clean value --
    which is how defect D4 becomes a measurable failure rather than a docstring
    discrepancy.
    """
    rng = np.random.default_rng(seed)
    qa_bad = rng.random(shape) < corrupt_fraction
    aod = np.where(qa_bad, corrupt_aod, clean_aod)
    # AOD_QA bits 0-2 are MCD19A2's cloud mask: 001 = clear, 011 = cloudy.
    # Note "clear" is a set bit, not a cleared one -- an easy thing to invert.
    qa = np.where(qa_bad, 0b011, 0b001).astype(float)
    return aod / 0.001, qa


def dem_flat(shape: tuple[int, int] = DEFAULT_SHAPE, elevation: float = 216.0):
    """Flat terrain: slope 0, so the slope<30 exclusion keeps everything."""
    return np.full(shape, float(elevation))


def dem_with_steep_slope(
    shape: tuple[int, int] = DEFAULT_SHAPE, rise_per_px: float = 10.0
) -> np.ndarray:
    """A ramp steep enough to trip the slope exclusion over half the grid."""
    ny, nx = shape
    ramp = np.tile(np.arange(nx, dtype=float) * rise_per_px, (ny, 1))
    ramp[:, : nx // 2] = 0.0
    return ramp


# ---------------------------------------------------------------------------
# Collection registration
# ---------------------------------------------------------------------------


def register_constant_hourly_collection(
    collection_id: str,
    band: str,
    value_per_image: float,
    n_images: int = 24,
    shape: tuple[int, int] = DEFAULT_SHAPE,
    scale_m: float = NATIVE_SCALE_M,
) -> None:
    """
    Register a collection of ``n_images`` identical images.

    Used to pin unit conversions: with ``value_per_image`` in J/m^2 per hour and
    ``n_images`` hours, ``.sum() / 3.6e6`` must equal
    ``n_images * value_per_image / 3.6e6`` kWh/m^2 exactly.
    """

    def builder(_start=None, _end=None):
        return [
            FakeImage.from_bands(
                {band: np.full(shape, float(value_per_image))}, native_scale_m=scale_m
            )
            for _ in range(n_images)
        ]

    fake_ee.register_collection(collection_id, builder)


def register_image_collection(collection_id: str, images: list[FakeImage]) -> None:
    fake_ee.register_collection(collection_id, lambda _s=None, _e=None: list(images))


def register_single_image_collection(collection_id: str, image: FakeImage) -> None:
    register_image_collection(collection_id, [image])
