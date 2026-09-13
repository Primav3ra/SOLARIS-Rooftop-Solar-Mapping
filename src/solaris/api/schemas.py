"""
Request models.

The four endpoints previously carried four near-identical copies of the same
fields, so a bound added to one silently missed the others. They now compose
three mixins, and every field carries an explicit range.

These bounds are the primary defence against Earth Engine quota exhaustion:
validation runs before any ``ee`` object is constructed, so an oversized AOI is
rejected without spending a request. No amount of per-IP rate limiting helps
against one well-formed request for a region the size of a continent.
"""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_AOI_KM2: float = 30.0  # keeps AOIs inside the 4 m reduce-scale tier
MAX_HALF_SIZE_DEG: float = 0.025  # ~2.8 km half-side => ~30 km2 at Delhi latitude
MAX_AOI_VERTICES: int = 100
OPEN_BUILDINGS_MIN_YEAR: int = 2016  # Open Buildings 2.5D Temporal v1 vintages
OPEN_BUILDINGS_MAX_YEAR: int = 2023

_DEG_KM = 111.32  # km per degree of latitude


def polygon_area_km2(coords: list[list[float]]) -> float:
    """
    Approximate polygon area in km^2 via the shoelace formula on an
    equirectangular projection scaled at the polygon's mean latitude.
    Good enough as an input guard; not used for any reported quantity.
    """
    if not coords or len(coords) < 3:
        return 0.0
    lats = [float(p[1]) for p in coords]
    mean_lat_rad = math.radians(sum(lats) / len(lats))
    ring = coords[:-1] if coords[0] == coords[-1] else coords
    n = len(ring)
    if n < 3:
        return 0.0
    acc = 0.0
    for i in range(n):
        x1, y1 = float(ring[i][0]), float(ring[i][1])
        x2, y2 = float(ring[(i + 1) % n][0]), float(ring[(i + 1) % n][1])
        acc += x1 * y2 - x2 * y1
    area_deg2 = abs(acc) / 2.0
    return area_deg2 * (_DEG_KM**2) * math.cos(mean_lat_rad)


class AoiMixin(BaseModel):
    """AOI selection: either an explicit polygon or a lat/lon centre + half-size."""

    coordinates: list[list[float]] | None = None
    lat: float | None = Field(default=None, ge=-90.0, le=90.0)
    lon: float | None = Field(default=None, ge=-180.0, le=180.0)
    half_size_deg: float = Field(default=0.01, gt=0.0, le=MAX_HALF_SIZE_DEG)

    @field_validator("coordinates")
    @classmethod
    def _check_ring(cls, v: list[list[float]] | None) -> list[list[float]] | None:
        if v is None:
            return v
        if len(v) < 4:
            raise ValueError("coordinates must be a closed ring of at least 4 positions")
        if len(v) > MAX_AOI_VERTICES:
            raise ValueError(f"coordinates must have at most {MAX_AOI_VERTICES} positions")
        for p in v:
            if len(p) < 2:
                raise ValueError("each coordinate must be [lon, lat]")
            lon, lat = float(p[0]), float(p[1])
            if not (-180.0 <= lon <= 180.0 and -90.0 <= lat <= 90.0):
                raise ValueError(f"coordinate out of range: [{lon}, {lat}]")
        area = polygon_area_km2(v)
        if area > MAX_AOI_KM2:
            raise ValueError(
                f"AOI area {area:.1f} km2 exceeds the {MAX_AOI_KM2} km2 limit; "
                "request a smaller area"
            )
        return v

    @model_validator(mode="after")
    def _require_aoi(self) -> AoiMixin:
        if self.coordinates is None and (self.lat is None or self.lon is None):
            raise ValueError("Provide either coordinates or both lat and lon.")
        return self


class RoofMixin(BaseModel):
    """Rooftop mask parameters."""

    roof_year: int = Field(default=2022, ge=OPEN_BUILDINGS_MIN_YEAR, le=OPEN_BUILDINGS_MAX_YEAR)
    presence_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    min_height_m: float = Field(default=0.0, ge=0.0, le=500.0)


class TemporalMixin(BaseModel):
    """Temporal window selection."""

    baseline_mode: str = "yearly"  # yearly | quarterly | monthly | daily
    year: int | None = Field(default=None, ge=2000, le=2100)
    quarter: int | None = Field(default=None, ge=1, le=4)
    month: int | None = Field(default=None, ge=1, le=12)
    start_date: str | None = None
    end_date_exclusive: str | None = None


class BaselineRequest(AoiMixin, RoofMixin, TemporalMixin):
    pass


class YieldRequest(AoiMixin, RoofMixin, TemporalMixin):
    panel_efficiency: float = Field(default=0.18, gt=0.0, le=0.40)
    performance_ratio: float = Field(default=0.80, gt=0.0, le=1.0)
    # usable-roof coverage fraction: panels never tile 100% of a roof
    # (setbacks, obstructions, water tanks, access). Typical 0.6-0.75.
    packing_factor: float = Field(default=0.7, gt=0.0, le=1.0)
    building_confidence: float = Field(default=0.7, ge=0.0, le=1.0)


class TilesRequest(AoiMixin, RoofMixin, TemporalMixin):
    layer: Literal[
        "roof_mask",
        "shadow_frequency",
        "sky_view_factor",
        "net_irradiance",
        "combined_derate",
        "temperature_delta",
    ] = "roof_mask"


class BuildingsRequest(AoiMixin):
    building_confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    limit: int = Field(default=400, ge=1, le=2000)
