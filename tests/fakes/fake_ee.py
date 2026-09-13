"""
A numpy-backed stand-in for the Earth Engine client library.

Why not ``MagicMock``
---------------------
A MagicMock ``ee`` lets ``penalties.py`` run without raising while every
arithmetic result is itself a mock. It would have happily accepted all ten of
the confirmed defects and proven nothing. Here every operation is **real
arithmetic on real arrays**, so a wrong formula produces a wrong number and the
test fails.

Design rules, in priority order
-------------------------------
1. **Encode real Earth Engine semantics, especially where the code under test
   gets them wrong.** Two matter most:

   * ``Image.translate(x, y, units, proj)`` defaults ``units`` to **metres**.
     The shadow and sky-view models pass pixel counts, so they are translating
     4x too short. This fake reproduces that, which turns the bug into a failing
     test rather than a claim in a commit message.
   * Focal operations with ``units="pixels"`` resolve against the **projection of
     the request**, not the image's own projection. So a radius-100px kernel
     spans 400 m when reduced at ``scale=4`` and kilometres when rendered at a
     low-zoom tile scale -- the shadow layer drawn on the map is not the one
     behind the number. Modelled by evaluating lazily (see below).

2. **Fail loudly.** Module ``__getattr__`` raises ``NotImplementedError`` for
   anything not implemented. Never a permissive stub, so coverage gaps are
   visible instead of silently passing.

3. **Analytically checkable fixtures.** See ``synthetic.py`` for rasters whose
   shadow, sky-view and anomaly values can be worked out by hand.

Lazy evaluation
---------------
A ``FakeImage`` is an expression tree, not an array. Each node knows how to
``evaluate(scale_m)``. ``reduceRegion(scale=S)`` evaluates at ``S``;
``reproject(scale=R)`` pins everything beneath it to ``R``. That is what lets
scale-dependent kernel behaviour be observed at all.

Stated simplification
---------------------
Arrays always live on one fixed native grid; evaluating at a coarser scale does
**not** resample them. Instead, pixel-denominated neighbourhood operations scale
their radius by ``eval_scale / native_scale``. This reproduces the observable
consequence we care about -- a kernel's physical footprint growing with the
request scale -- without implementing full pyramid resampling. Metre-denominated
operations are unaffected. Anything depending on genuine resampling is out of
scope for this fake and belongs in the live ``gee``-marked probe tests.
"""

from __future__ import annotations

import math
import warnings
from collections.abc import Callable
from typing import Any

import numpy as np
from scipy import ndimage

#: Pixel size of the synthetic grid, in metres. Matches Open Buildings 2.5D.
NATIVE_SCALE_M = 4.0

#: Grid shape used by the default fixtures, (rows, cols).
DEFAULT_SHAPE = (64, 64)

#: Asset / collection registries, populated by tests via the helpers below.
_ASSETS: dict[str, FakeImage] = {}
_COLLECTIONS: dict[str, Callable[..., list[FakeImage]]] = {}

_INITIALIZED = False


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------


def register_asset(asset_id: str, image: FakeImage) -> None:
    _ASSETS[asset_id] = image


def register_collection(collection_id: str, builder: Callable[..., list[FakeImage]]) -> None:
    """
    ``builder(start, end) -> list[FakeImage]``.

    Each returned image should carry a ``system:time_start`` property so
    ``filterDate`` can select among them.
    """
    _COLLECTIONS[collection_id] = builder


def reset_registries() -> None:
    _ASSETS.clear()
    _COLLECTIONS.clear()


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


class FakeProjection:
    def __init__(self, crs: str = "EPSG:4326", scale_m: float = NATIVE_SCALE_M):
        self.crs = crs
        self._scale_m = float(scale_m)

    def nominalScale(self):
        return FakeNumber(self._scale_m)

    def getInfo(self):
        return {"crs": self.crs, "scale": self._scale_m}


class FakeNumber:
    def __init__(self, value: float):
        self._value = value

    def getInfo(self):
        return self._value

    def divide(self, other):
        return FakeNumber(self._value / _scalar(other))

    def multiply(self, other):
        return FakeNumber(self._value * _scalar(other))


def _scalar(x: Any) -> float:
    if isinstance(x, FakeNumber):
        return x._value
    return float(x)


# ---------------------------------------------------------------------------
# Image
# ---------------------------------------------------------------------------

BandDict = dict[str, np.ndarray]


class FakeImage:
    """
    An expression tree over named 2-D float arrays.

    ``_evaluate(scale_m)`` returns ``{band_name: ndarray}``. ``nan`` marks a
    masked pixel, matching Earth Engine's behaviour of skipping masked pixels in
    reductions (so a band of all-nan reduces to ``None``, which is what drives
    the fallback paths in the code under test).
    """

    def __init__(
        self,
        evaluator: Callable[[float], BandDict],
        *,
        native_scale_m: float = NATIVE_SCALE_M,
        pinned_scale_m: float | None = None,
        properties: dict[str, Any] | None = None,
    ):
        self._evaluate_fn = evaluator
        self.native_scale_m = native_scale_m
        self.pinned_scale_m = pinned_scale_m
        self.properties = properties or {}

    # -- construction -----------------------------------------------------

    @classmethod
    def from_bands(cls, bands: BandDict, **kw) -> FakeImage:
        frozen = {k: np.asarray(v, dtype=float) for k, v in bands.items()}
        return cls(lambda _scale: {k: v.copy() for k, v in frozen.items()}, **kw)

    @classmethod
    def from_constant(cls, value: float, name: str = "constant", **kw) -> FakeImage:
        """
        Constants are 0-d arrays so they broadcast against any grid shape,
        matching Earth Engine, where ``ee.Image(1.0)`` has no intrinsic extent.
        """
        return cls(lambda _scale, v=float(value): {name: np.array(v)}, **kw)

    # -- evaluation -------------------------------------------------------

    def evaluate(self, scale_m: float) -> BandDict:
        effective = self.pinned_scale_m if self.pinned_scale_m is not None else scale_m
        return self._evaluate_fn(effective)

    def _derive(self, evaluator, **overrides) -> FakeImage:
        kw = {
            "native_scale_m": self.native_scale_m,
            "pinned_scale_m": self.pinned_scale_m,
            "properties": self.properties,
        }
        kw.update(overrides)
        return FakeImage(evaluator, **kw)

    def band_names(self, scale_m: float = NATIVE_SCALE_M) -> list[str]:
        return list(self.evaluate(scale_m).keys())

    def bandNames(self):
        return FakeList(self.band_names())

    # -- band manipulation ------------------------------------------------

    def select(self, selector, *rest) -> FakeImage:
        if rest:
            names = [selector, *rest]
        elif isinstance(selector, list | tuple):
            names = list(selector)
        else:
            names = [selector]

        def ev(scale, names=names):
            bands = self._evaluate_fn(
                self.pinned_scale_m if self.pinned_scale_m is not None else scale
            )
            out = {}
            for n in names:
                if isinstance(n, int):
                    key = list(bands.keys())[n]
                    out[key] = bands[key]
                else:
                    if n not in bands:
                        raise KeyError(f"fake_ee: band {n!r} not found; have {list(bands)}")
                    out[n] = bands[n]
            return out

        return self._derive(ev)

    def rename(self, *names) -> FakeImage:
        flat = (
            list(names[0])
            if len(names) == 1 and isinstance(names[0], list | tuple)
            else list(names)
        )

        def ev(scale, flat=flat):
            bands = self.evaluate(scale)
            if len(flat) != len(bands):
                raise ValueError(f"fake_ee: rename got {len(flat)} names for {len(bands)} bands")
            return dict(zip(flat, bands.values(), strict=True))

        return self._derive(ev)

    def addBands(self, other: FakeImage) -> FakeImage:
        def ev(scale):
            merged = dict(self.evaluate(scale))
            merged.update(other.evaluate(scale))
            return merged

        return self._derive(ev)

    def toFloat(self) -> FakeImage:
        return self

    def toUint8(self) -> FakeImage:
        return self._unary(lambda a: np.clip(np.nan_to_num(a), 0, 255).astype(float))

    def toInt(self) -> FakeImage:
        return self._unary(lambda a: np.trunc(a))

    # -- masking ----------------------------------------------------------

    def updateMask(self, mask: FakeImage) -> FakeImage:
        def ev(scale):
            bands = self.evaluate(scale)
            m = _first_array(mask.evaluate(scale))
            keep = np.where((m > 0) & ~np.isnan(m), 1.0, np.nan)
            return {k: v * keep for k, v in bands.items()}

        return self._derive(ev)

    def selfMask(self) -> FakeImage:
        return self._unary(lambda a: np.where(a > 0, a, np.nan))

    def unmask(self, value: float = 0.0) -> FakeImage:
        return self._unary(lambda a, v=float(value): np.where(np.isnan(a), v, a))

    # -- arithmetic -------------------------------------------------------

    def _unary(self, fn) -> FakeImage:
        return self._derive(lambda scale: {k: fn(v) for k, v in self.evaluate(scale).items()})

    def _binary(self, other, fn) -> FakeImage:
        def ev(scale):
            left = self.evaluate(scale)
            if isinstance(other, FakeImage):
                right = other.evaluate(scale)
                rvals = list(right.values())
                if len(rvals) == 1:
                    return {k: fn(v, rvals[0]) for k, v in left.items()}
                if len(rvals) != len(left):
                    raise ValueError(
                        f"fake_ee: band-count mismatch ({len(left)} vs {len(rvals)}) in a binary op"
                    )
                return {k: fn(v, r) for (k, v), r in zip(left.items(), rvals, strict=True)}
            return {k: fn(v, float(other)) for k, v in left.items()}

        return self._derive(ev)

    def add(self, other):
        return self._binary(other, lambda a, b: a + b)

    def subtract(self, other):
        return self._binary(other, lambda a, b: a - b)

    def multiply(self, other):
        return self._binary(other, lambda a, b: a * b)

    def divide(self, other):
        return self._binary(other, lambda a, b: a / b)

    def pow(self, other):
        return self._binary(other, lambda a, b: np.power(a, b))

    def max(self, other):
        return self._binary(other, np.maximum)

    def min(self, other):
        return self._binary(other, np.minimum)

    def clamp(self, low: float, high: float):
        return self._unary(lambda a: np.clip(a, float(low), float(high)))

    def sin(self):
        return self._unary(np.sin)

    def cos(self):
        return self._unary(np.cos)

    def tan(self):
        return self._unary(np.tan)

    def atan(self):
        return self._unary(np.arctan)

    def asin(self):
        return self._unary(np.arcsin)

    def sqrt(self):
        return self._unary(np.sqrt)

    def abs(self):
        return self._unary(np.abs)

    def exp(self):
        return self._unary(np.exp)

    def log(self):
        return self._unary(np.log)

    # -- comparison (1.0 / 0.0, like Earth Engine) -------------------------

    @staticmethod
    def _b(arr) -> np.ndarray:
        return np.where(arr, 1.0, 0.0)

    def gt(self, other):
        return self._binary(other, lambda a, b: self._b(a > b))

    def gte(self, other):
        return self._binary(other, lambda a, b: self._b(a >= b))

    def lt(self, other):
        return self._binary(other, lambda a, b: self._b(a < b))

    def lte(self, other):
        return self._binary(other, lambda a, b: self._b(a <= b))

    def eq(self, other):
        return self._binary(other, lambda a, b: self._b(a == b))

    def neq(self, other):
        return self._binary(other, lambda a, b: self._b(a != b))

    def And(self, other):
        return self._binary(other, lambda a, b: self._b((a > 0) & (b > 0)))

    def Or(self, other):
        return self._binary(other, lambda a, b: self._b((a > 0) | (b > 0)))

    def Not(self):
        return self._unary(lambda a: self._b(a == 0))

    # -- projection -------------------------------------------------------

    def setDefaultProjection(self, crs=None, crsTransform=None, scale=None):
        """
        Earth Engine divides ``scale`` by the nominal size of a metre in the
        target CRS, so ``scale=4`` means 4 metres even for EPSG:4326. Verified
        against the live API by the gee-marked probe tests.
        """
        native = float(scale) if scale is not None else self.native_scale_m
        return self._derive(self._evaluate_fn, native_scale_m=native)

    def reproject(self, crs=None, crsTransform=None, scale=None):
        """Pins the evaluation scale, making everything beneath scale-invariant."""
        pinned = float(scale) if scale is not None else self.native_scale_m
        return self._derive(self._evaluate_fn, native_scale_m=pinned, pinned_scale_m=pinned)

    def projection(self) -> FakeProjection:
        return FakeProjection("EPSG:4326", self.native_scale_m)

    # -- neighbourhood ----------------------------------------------------

    def translate(self, x, y, units: str | None = None, proj=None) -> FakeImage:
        """
        Shift the image. **``units`` defaults to metres**, matching Earth Engine.

        This is the single most important semantic in this fake: the production
        shadow and sky-view code computes offsets in pixels and passes them here
        without specifying units, so its translations come out a factor of
        ``native_scale_m`` too short.
        """
        unit = (units or "meters").lower()
        if unit not in ("meters", "metres", "pixels"):
            raise ValueError(f"fake_ee: unknown translate units {units!r}")

        def ev(scale):
            bands = self.evaluate(scale)
            if unit == "pixels":
                dx_px, dy_px = float(x), float(y)
            else:
                dx_px = float(x) / self.native_scale_m
                dy_px = float(y) / self.native_scale_m
            # +y is north, so a positive y shift moves content up a row index.
            shift = (-round(dy_px), round(dx_px))
            return {k: (v if v.ndim == 0 else _shift_nan(v, shift)) for k, v in bands.items()}

        return self._derive(ev)

    def _focal(self, reduce_fn, radius, units, kernel_type) -> FakeImage:
        unit = (units or "pixels").lower()

        def ev(scale):
            bands = self.evaluate(scale)
            if unit == "pixels":
                # The kernel is denominated in pixels *of the request
                # projection*, so its physical footprint grows with the
                # request scale. This is defect D9.
                effective = self.pinned_scale_m if self.pinned_scale_m is not None else scale
                r = float(radius) * (effective / self.native_scale_m)
            else:
                r = float(radius) / self.native_scale_m
            r_px = max(round(r), 0)
            return {
                k: (v if v.ndim == 0 else _focal_apply(v, r_px, reduce_fn, kernel_type))
                for k, v in bands.items()
            }

        return self._derive(ev)

    def focal_max(self, radius=1.5, kernelType="circle", units=None, iterations=1, kernel=None):
        if kernel is not None:
            radius, units, kernelType = kernel.radius, kernel.units, kernel.kernel_type
        return self._focal(np.nanmax, radius, units, kernelType)

    def focal_min(self, radius=1.5, kernelType="circle", units=None, iterations=1, kernel=None):
        if kernel is not None:
            radius, units, kernelType = kernel.radius, kernel.units, kernel.kernel_type
        return self._focal(np.nanmin, radius, units, kernelType)

    def focal_mean(self, radius=1.5, kernelType="circle", units=None, iterations=1, kernel=None):
        if kernel is not None:
            radius, units, kernelType = kernel.radius, kernel.units, kernel.kernel_type
        return self._focal(np.nanmean, radius, units, kernelType)

    # -- geometry / clipping ----------------------------------------------

    def clip(self, geometry) -> FakeImage:
        def ev(scale):
            bands = self.evaluate(scale)
            shape = _grid_shape(bands)
            keep = np.where(geometry.pixel_mask(shape), 1.0, np.nan)
            return {k: _as_grid(v, shape) * keep for k, v in bands.items()}

        return self._derive(ev)

    # -- reduction --------------------------------------------------------

    def reduceRegion(
        self,
        reducer=None,
        geometry=None,
        scale=None,
        maxPixels=None,
        tileScale=None,
        bestEffort=False,
        crs=None,
        **_ignored,
    ) -> FakeDictionary:
        scale_m = float(scale) if scale is not None else self.native_scale_m
        bands = self.evaluate(scale_m)
        shape = _grid_shape(bands)
        mask = geometry.pixel_mask(shape) if geometry is not None else np.ones(shape, dtype=bool)
        out: dict[str, Any] = {}
        for name, arr in bands.items():
            vals = _as_grid(arr, shape)[mask]
            vals = vals[~np.isnan(vals)]
            out[name] = None if vals.size == 0 else float(reducer.apply(vals))
        return FakeDictionary(out)

    def sample(self, region=None, scale=None, numPixels=None, geometries=False, **_kw):
        scale_m = float(scale) if scale is not None else self.native_scale_m
        bands = self.evaluate(scale_m)
        shape = _grid_shape(bands)
        idx = (shape[0] // 2, shape[1] // 2) if region is None else region.centroid_index(shape)
        props: dict[str, Any] = {}
        for name, arr in bands.items():
            v = _as_grid(arr, shape)[idx]
            props[name] = None if np.isnan(v) else float(v)
        if all(v is None for v in props.values()):
            return FakeFeatureCollection([])
        return FakeFeatureCollection([FakeFeature(props)])

    # -- serving ----------------------------------------------------------

    def getMapId(self, vis_params=None):
        return {
            "mapid": "fake-mapid",
            "tile_fetcher": _FakeTileFetcher(
                "https://earthengine.example/v1/fake-mapid/tiles/{z}/{x}/{y}"
            ),
        }

    def getInfo(self):
        bands = self.evaluate(self.native_scale_m)
        return {
            "type": "Image",
            "bands": [{"id": k} for k in bands],
            "properties": self.properties,
        }


def _first_array(bands: BandDict) -> np.ndarray:
    return bands[next(iter(bands))]


def _grid_shape(bands: BandDict) -> tuple[int, int]:
    """The concrete grid shape of a band dict, ignoring 0-d constants."""
    for arr in bands.values():
        if arr.ndim == 2:
            return arr.shape
    return DEFAULT_SHAPE


def _as_grid(arr: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    return np.broadcast_to(arr, shape) if arr.ndim == 0 else arr


def _shift_nan(arr: np.ndarray, shift: tuple[int, int]) -> np.ndarray:
    """Shift with nan fill (out-of-frame data is unknown, not zero)."""
    dy, dx = shift
    out = np.full_like(arr, np.nan)
    ny, nx = arr.shape
    ys_src = slice(max(0, -dy), min(ny, ny - dy))
    ys_dst = slice(max(0, dy), min(ny, ny + dy))
    xs_src = slice(max(0, -dx), min(nx, nx - dx))
    xs_dst = slice(max(0, dx), min(nx, nx + dx))
    out[ys_dst, xs_dst] = arr[ys_src, xs_src]
    return out


def _kernel_offsets(r_px: int, kernel_type: str) -> list[tuple[int, int]]:
    kt = (kernel_type or "circle").lower()
    offsets = []
    for dy in range(-r_px, r_px + 1):
        for dx in range(-r_px, r_px + 1):
            if kt == "circle" and (dx * dx + dy * dy) > r_px * r_px:
                continue
            offsets.append((dy, dx))
    return offsets


def _half_widths(r_px: int, kernel_type: str) -> list[tuple[int, int]]:
    """
    Decompose a 2-D footprint into per-row half-widths ``(dy, w)``.

    A disc of radius ``r`` is the union over ``dy`` of horizontal runs of
    half-width ``floor(sqrt(r^2 - dy^2))``. That turns an O(r^2) footprint into
    ``2r+1`` one-dimensional passes -- necessary because the production shadow
    model uses a radius-100 kernel, and both a sliding-window stack and scipy's
    footprint filters run out of memory at that size.
    """
    square = (kernel_type or "circle").lower() == "square"
    rows = []
    for dy in range(-r_px, r_px + 1):
        w = r_px if square else math.isqrt(max(r_px * r_px - dy * dy, 0))
        rows.append((dy, w))
    return rows


def _shift_rows(arr: np.ndarray, dy: int, fill: float) -> np.ndarray:
    """``out[y] = arr[y + dy]``, padded with ``fill``."""
    out = np.full_like(arr, fill)
    ny = arr.shape[0]
    if dy >= 0:
        if dy < ny:
            out[: ny - dy] = arr[dy:]
    else:
        if -dy < ny:
            out[-dy:] = arr[: ny + dy]
    return out


def _box_sum_1d(arr: np.ndarray, width: int) -> np.ndarray:
    k = 2 * width + 1
    return ndimage.uniform_filter1d(arr, size=k, axis=1, mode="constant", cval=0.0) * k


def _focal_apply(arr: np.ndarray, r_px: int, reduce_fn, kernel_type: str) -> np.ndarray:
    """
    Neighbourhood reduction over a circular or square footprint.

    Deliberately no "radius exceeds the grid, so reduce globally" shortcut: that
    would make kernels look scale-invariant on small test grids and hide defect
    D9, which is precisely what these tests exist to detect.
    """
    if r_px <= 0:
        return arr.copy()

    finite = np.isfinite(arr)
    if not finite.any():
        return np.full_like(arr, np.nan)

    ny, nx = arr.shape
    # Rows further away than the grid is tall contribute nothing, and a
    # half-width wider than the grid is just a whole-row reduction. Without
    # these bounds a coarse request scale inflates the radius into the hundreds
    # and the pass count explodes.
    rows = [(dy, min(w, nx)) for dy, w in _half_widths(r_px, kernel_type) if abs(dy) < ny]

    if reduce_fn is np.nanmax or reduce_fn is np.nanmin:
        is_max = reduce_fn is np.nanmax
        fill = -np.inf if is_max else np.inf
        filled = np.where(finite, arr, fill)
        combine = np.maximum if is_max else np.minimum
        f1d = ndimage.maximum_filter1d if is_max else ndimage.minimum_filter1d
        out = np.full_like(arr, fill)
        for dy, w in rows:
            if w >= nx:
                row = combine.reduce(filled, axis=1, keepdims=True)
                band = np.broadcast_to(row, arr.shape)
            else:
                band = f1d(filled, size=2 * w + 1, axis=1, mode="constant", cval=fill)
            out = combine(out, _shift_rows(band, dy, fill))
        return np.where(np.isfinite(out), out, np.nan)

    if reduce_fn is np.nanmean:
        values = np.where(finite, arr, 0.0)
        valid = finite.astype(float)
        total = np.zeros_like(arr)
        count = np.zeros_like(arr)
        for dy, w in rows:
            if w >= nx:
                v_band = np.broadcast_to(values.sum(axis=1, keepdims=True), arr.shape)
                c_band = np.broadcast_to(valid.sum(axis=1, keepdims=True), arr.shape)
            else:
                v_band = _box_sum_1d(values, w)
                c_band = _box_sum_1d(valid, w)
            total += _shift_rows(v_band, dy, 0.0)
            count += _shift_rows(c_band, dy, 0.0)
        with np.errstate(invalid="ignore", divide="ignore"):
            return np.where(count > 0.5, total / np.maximum(count, 1e-12), np.nan)

    raise NotImplementedError(
        f"fake_ee: focal reduction {getattr(reduce_fn, '__name__', reduce_fn)!r} is not implemented"
    )


class _FakeTileFetcher:
    def __init__(self, url_format: str):
        self.url_format = url_format


class FakeList:
    def __init__(self, values):
        self._values = list(values)

    def getInfo(self):
        return list(self._values)


class FakeDictionary:
    def __init__(self, mapping: dict[str, Any]):
        self._mapping = dict(mapping)

    def getInfo(self):
        return dict(self._mapping)

    def get(self, key):
        return self._mapping.get(key)


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


class FakeGeometry:
    """
    A rectangle in pixel-index space.

    The fake works in pixel space rather than lon/lat because every test that
    needs geometry is testing raster maths, not map projection. ``from_bbox_px``
    is the constructor tests should use; ``ee.Geometry.Polygon`` accepts lon/lat
    rings and covers the whole grid, which is what the production code paths
    want when they pass an AOI straight through.
    """

    def __init__(self, y0=None, y1=None, x0=None, x1=None, coords=None, point=None):
        self._y0, self._y1, self._x0, self._x1 = y0, y1, x0, x1
        self.coords = coords
        self.point = point

    @classmethod
    def from_bbox_px(cls, y0: int, y1: int, x0: int, x1: int) -> FakeGeometry:
        return cls(y0=y0, y1=y1, x0=x0, x1=x1)

    def pixel_mask(self, shape: tuple[int, int]) -> np.ndarray:
        mask = np.zeros(shape, dtype=bool)
        y0 = 0 if self._y0 is None else max(0, self._y0)
        y1 = shape[0] if self._y1 is None else min(shape[0], self._y1)
        x0 = 0 if self._x0 is None else max(0, self._x0)
        x1 = shape[1] if self._x1 is None else min(shape[1], self._x1)
        mask[y0:y1, x0:x1] = True
        return mask

    def centroid_index(self, shape: tuple[int, int]) -> tuple[int, int]:
        mask = self.pixel_mask(shape)
        ys, xs = np.nonzero(mask)
        if ys.size == 0:
            return (shape[0] // 2, shape[1] // 2)
        return (int(ys.mean()), int(xs.mean()))

    # -- Earth Engine surface ---------------------------------------------

    def centroid(self, maxError=None):
        """
        A point geometry.

        ``coordinates`` must come back as a flat ``[lon, lat]`` pair rather than
        a ring: the API reads ``coords[0]`` and ``coords[1]`` as floats, so
        returning a polygon here fails with a confusing TypeError. The pixel
        bbox is carried over so reductions against the centroid still work.
        """
        if self.point is not None:
            return self
        if self.coords:
            lons = [float(c[0]) for c in self.coords]
            lats = [float(c[1]) for c in self.coords]
            centre = (sum(lons) / len(lons), sum(lats) / len(lats))
        else:
            centre = (0.0, 0.0)
        return FakeGeometry(y0=self._y0, y1=self._y1, x0=self._x0, x1=self._x1, point=centre)

    def buffer(self, distance, maxError=None):
        px = round(float(distance) / NATIVE_SCALE_M)
        if self._y0 is None:
            return self
        return FakeGeometry(y0=self._y0 - px, y1=self._y1 + px, x0=self._x0 - px, x1=self._x1 + px)

    def bounds(self, maxError=None):
        return self

    def area(self, maxError=None):
        mask_cells = (
            (self._y1 - self._y0) * (self._x1 - self._x0)
            if self._y0 is not None
            else DEFAULT_SHAPE[0] * DEFAULT_SHAPE[1]
        )
        return FakeNumber(mask_cells * NATIVE_SCALE_M**2)

    def coordinates(self):
        return FakeList(self.coords or [])

    def getInfo(self):
        if self.point is not None:
            return {"type": "Point", "coordinates": [self.point[0], self.point[1]]}
        if self.coords:
            return {"type": "Polygon", "coordinates": [self.coords]}
        return {"type": "Point", "coordinates": [0.0, 0.0]}


class _GeometryNamespace:
    @staticmethod
    def Polygon(coords, *args, **kwargs):
        ring = coords[0] if coords and isinstance(coords[0][0], list | tuple) else coords
        return FakeGeometry(coords=ring)

    @staticmethod
    def Point(coords, *args, **kwargs):
        return FakeGeometry(point=(float(coords[0]), float(coords[1])))

    @staticmethod
    def Rectangle(coords, *args, **kwargs):
        return FakeGeometry(coords=coords)


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------


class FakeFeature:
    def __init__(self, properties: dict[str, Any], geometry: FakeGeometry | None = None):
        self._properties = dict(properties)
        self._geometry = geometry or FakeGeometry()

    def geometry(self):
        return self._geometry

    def get(self, key):
        return self._properties.get(key)

    def getInfo(self):
        return {"type": "Feature", "properties": dict(self._properties)}


class FakeFeatureCollection:
    def __init__(self, features: list[FakeFeature]):
        self._features = list(features)

    def filterBounds(self, geometry):
        return self

    def filterDate(self, start, end):
        return self

    def filter(self, _f):
        return self

    def limit(self, n, *_a, **_kw):
        return FakeFeatureCollection(self._features[: int(n)])

    def first(self):
        return self._features[0] if self._features else _NullFeature()

    def size(self):
        return FakeNumber(len(self._features))

    def toList(self, n, offset=0):
        return FakeList(self._features[offset : offset + int(n)])

    def getInfo(self):
        return {
            "type": "FeatureCollection",
            "features": [f.getInfo() for f in self._features],
        }


class _NullFeature:
    """``first()`` on an empty collection: ``getInfo()`` yields None, as in EE."""

    def getInfo(self):
        return None

    def geometry(self):
        return FakeGeometry()

    def get(self, _key):
        return None


# ---------------------------------------------------------------------------
# Image collections
# ---------------------------------------------------------------------------


class FakeImageCollection:
    def __init__(self, source):
        if isinstance(source, str):
            if source not in _COLLECTIONS:
                raise NotImplementedError(
                    f"fake_ee: collection {source!r} is not registered. "
                    "Call fake_ee.register_collection() in your fixture."
                )
            self._builder = _COLLECTIONS[source]
            self._images: list[FakeImage] | None = None
            self._id = source
        else:
            self._builder = None
            self._images = list(source)
            self._id = "<inline>"
        self._start: str | None = None
        self._end: str | None = None
        self._selected: list[str] | None = None

    def _materialize(self) -> list[FakeImage]:
        imgs = self._images if self._images is not None else self._builder(self._start, self._end)
        if self._selected:
            imgs = [im.select(self._selected) for im in imgs]
        return imgs

    def filterDate(self, start, end=None):
        self._start, self._end = start, end
        return self

    def filterBounds(self, geometry):
        return self

    def filter(self, _f):
        return self

    def select(self, selector, *rest):
        if rest:
            self._selected = [selector, *rest]
        elif isinstance(selector, list | tuple):
            self._selected = list(selector)
        else:
            self._selected = [selector]
        return self

    def map(self, fn):
        return FakeImageCollection([fn(im) for im in self._materialize()])

    # -- aggregation ------------------------------------------------------

    def _aggregate(self, fn) -> FakeImage:
        imgs = self._materialize()
        if not imgs:
            raise ValueError(f"fake_ee: no images in {self._id} for the given filters")

        def ev(scale):
            per_band: dict[str, list[np.ndarray]] = {}
            for im in imgs:
                for k, v in im.evaluate(scale).items():
                    per_band.setdefault(k, []).append(v)
            # An all-masked band legitimately aggregates to nan (which then
            # reduces to None and drives the fallback paths under test), so the
            # "All-NaN slice" warning is expected rather than diagnostic.
            with np.errstate(invalid="ignore", divide="ignore"), warnings.catch_warnings():
                warnings.simplefilter("ignore", RuntimeWarning)
                return {k: fn(np.stack(v, axis=0), axis=0) for k, v in per_band.items()}

        first = imgs[0]
        return FakeImage(
            ev,
            native_scale_m=first.native_scale_m,
            pinned_scale_m=first.pinned_scale_m,
        )

    def sum(self):
        return self._aggregate(np.nansum)

    def mean(self):
        return self._aggregate(np.nanmean)

    def median(self):
        return self._aggregate(np.nanmedian)

    def max(self):
        return self._aggregate(np.nanmax)

    def min(self):
        return self._aggregate(np.nanmin)

    def mosaic(self):
        imgs = self._materialize()
        if not imgs:
            raise ValueError(f"fake_ee: no images to mosaic in {self._id}")
        return imgs[0] if len(imgs) == 1 else self._aggregate(np.nanmax)

    def first(self):
        imgs = self._materialize()
        return imgs[0] if imgs else None

    def size(self):
        return FakeNumber(len(self._materialize()))

    def toList(self, n, offset=0):
        return FakeList(self._materialize()[offset : offset + int(n)])

    def getRegion(self, geometry, scale=None):
        raise NotImplementedError("fake_ee: getRegion is not implemented")


# ---------------------------------------------------------------------------
# Namespaces
# ---------------------------------------------------------------------------


class _ImageFactory:
    """``ee.Image`` is both a constructor and a namespace."""

    def __call__(self, arg=None, *args, **kwargs):
        if isinstance(arg, FakeImage):
            return arg
        if isinstance(arg, int | float):
            return FakeImage.from_constant(float(arg))
        if isinstance(arg, str):
            if arg not in _ASSETS:
                raise NotImplementedError(
                    f"fake_ee: asset {arg!r} is not registered. "
                    "Call fake_ee.register_asset() in your fixture."
                )
            return _ASSETS[arg]
        if isinstance(arg, dict):
            return FakeImage.from_bands(arg)
        if arg is None:
            return FakeImage.from_constant(0.0)
        raise NotImplementedError(f"fake_ee: ee.Image({type(arg).__name__}) unsupported")

    @staticmethod
    def constant(value):
        return FakeImage.from_constant(float(value))

    @staticmethod
    def from_bands(bands, **kw):
        """Convenience so tests can write ``ee.Image.from_bands(...)``."""
        return FakeImage.from_bands(bands, **kw)

    @staticmethod
    def pixelArea():
        return FakeImage(lambda _scale: {"area": np.full(DEFAULT_SHAPE, NATIVE_SCALE_M**2)})

    @staticmethod
    def pixelLonLat():
        ny, nx = DEFAULT_SHAPE
        lon, lat = np.meshgrid(np.arange(nx, dtype=float), np.arange(ny, dtype=float))
        return FakeImage.from_bands({"longitude": lon, "latitude": lat})

    @staticmethod
    def random(seed=0):
        rng = np.random.default_rng(seed)
        return FakeImage.from_bands({"random": rng.random(DEFAULT_SHAPE)})


class _ReducerNamespace:
    @staticmethod
    def mean():
        return _Reducer("mean", lambda v: float(np.mean(v)))

    @staticmethod
    def sum():
        return _Reducer("sum", lambda v: float(np.sum(v)))

    @staticmethod
    def min():
        return _Reducer("min", lambda v: float(np.min(v)))

    @staticmethod
    def max():
        return _Reducer("max", lambda v: float(np.max(v)))

    @staticmethod
    def stdDev():
        return _Reducer("stdDev", lambda v: float(np.std(v)))

    @staticmethod
    def median():
        return _Reducer("median", lambda v: float(np.median(v)))

    @staticmethod
    def count():
        return _Reducer("count", lambda v: float(v.size))


class _Reducer:
    def __init__(self, name: str, fn):
        self.name = name
        self._fn = fn

    def apply(self, values: np.ndarray) -> float:
        return self._fn(values)


class _FakeKernel:
    def __init__(self, radius, units, normalize, kernel_type):
        self.radius = radius
        self.units = units
        self.normalize = normalize
        self.kernel_type = kernel_type


class _KernelNamespace:
    @staticmethod
    def circle(radius=1.5, units="pixels", normalize=True, magnitude=1.0):
        return _FakeKernel(radius, units, normalize, "circle")

    @staticmethod
    def square(radius=1.5, units="pixels", normalize=True, magnitude=1.0):
        return _FakeKernel(radius, units, normalize, "square")


class _TerrainNamespace:
    @staticmethod
    def products(dem: FakeImage) -> FakeImage:
        """
        Slope in degrees from a finite-difference gradient on the DEM, plus the
        elevation band. Aspect and hillshade are not modelled.
        """

        def ev(scale):
            bands = dem.evaluate(scale)
            elev = _first_array(bands)
            gy, gx = np.gradient(np.nan_to_num(elev), dem.native_scale_m)
            slope = np.degrees(np.arctan(np.hypot(gx, gy)))
            return {"elevation": elev, "slope": slope}

        return FakeImage(ev, native_scale_m=dem.native_scale_m)


class _FilterNamespace:
    @staticmethod
    def gte(name, value):
        return {"op": "gte", "name": name, "value": value}

    @staticmethod
    def lt(name, value):
        return {"op": "lt", "name": name, "value": value}

    @staticmethod
    def eq(name, value):
        return {"op": "eq", "name": name, "value": value}

    @staticmethod
    def And(*filters):
        return {"op": "and", "filters": list(filters)}

    @staticmethod
    def date(start, end=None):
        return {"op": "date", "start": start, "end": end}


class ServiceAccountCredentials:
    def __init__(self, email=None, key_file=None, key_data=None):
        self.email = email
        self.key_file = key_file
        self.key_data = key_data


def Initialize(credentials=None, project=None, **kwargs):
    global _INITIALIZED
    _INITIALIZED = True


def Authenticate(**kwargs):
    return None


def is_initialized() -> bool:
    return _INITIALIZED


def Dictionary(mapping=None):
    return FakeDictionary(mapping or {})


def Feature(source, geometry=None):
    if isinstance(source, FakeFeature):
        return source
    if isinstance(source, dict):
        return FakeFeature(source.get("properties", {}), geometry)
    return FakeFeature({}, geometry)


def FeatureCollection(source, *args, **kwargs):
    if isinstance(source, FakeFeatureCollection):
        return source
    if isinstance(source, str):
        if source not in _COLLECTIONS:
            raise NotImplementedError(f"fake_ee: feature collection {source!r} is not registered.")
        return _COLLECTIONS[source]()
    if isinstance(source, list):
        return FakeFeatureCollection(source)
    return FakeFeatureCollection([])


def Number(value):
    return FakeNumber(_scalar(value))


def Algorithms(*_a, **_kw):
    raise NotImplementedError("fake_ee: ee.Algorithms is not implemented")


# Public namespace, mirroring the real module's shape.
Image = _ImageFactory()
ImageCollection = FakeImageCollection
Geometry = _GeometryNamespace
Reducer = _ReducerNamespace
Kernel = _KernelNamespace
Terrain = _TerrainNamespace
Filter = _FilterNamespace


class EEException(Exception):
    pass


def __getattr__(name: str):
    """
    Fail loudly for anything unimplemented.

    A permissive stub would let a test pass while exercising nothing, which is
    the whole failure mode this fake exists to avoid.

    Dunder and private names raise ``AttributeError`` instead: Python's import
    machinery probes ``__path__``, ``__all__`` and friends, and a
    ``NotImplementedError`` from those breaks importing the module at all.
    """
    if name.startswith("_"):
        raise AttributeError(name)
    raise NotImplementedError(
        f"fake_ee: ee.{name} is not implemented. Add it to tests/fakes/fake_ee.py "
        "if the code under test genuinely needs it."
    )


__all__ = [
    "DEFAULT_SHAPE",
    "NATIVE_SCALE_M",
    "EEException",
    "FakeGeometry",
    "FakeImage",
    "Filter",
    "Geometry",
    "Image",
    "ImageCollection",
    "Initialize",
    "Kernel",
    "Reducer",
    "ServiceAccountCredentials",
    "Terrain",
    "math",
    "register_asset",
    "register_collection",
    "reset_registries",
]
