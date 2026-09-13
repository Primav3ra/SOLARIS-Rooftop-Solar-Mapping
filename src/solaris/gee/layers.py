"""
Shared rooftop layer construction.

This exists because the roof mask was previously built two different ways:
``_build_roof_layers`` in the FastAPI module (used by /api/yield, /api/tiles and
/api/series) and ``SolarMappingUtils._build_roof_mask`` (used by /api/baseline),
each with its own copy of the slope-exclusion threshold. Two code paths meant
/api/baseline and /api/yield could disagree about what counted as a rooftop for
the same AOI. There is now one builder.
"""

from __future__ import annotations

from typing import NamedTuple

import ee

from solaris.core.constants import MAX_SLOPE_DEG, ROOF_SCALE_M
from solaris.gee.datasets import get_dem, get_open_buildings_temporal
from solaris.gee.rooftops import apply_terrain_exclusion, build_rooftop_candidate_mask


class RoofLayers(NamedTuple):
    """The three rasters every rooftop computation needs."""

    buildings: ee.Image  # presence + height + fractional_count
    building_height: ee.Image  # metres, projection pinned to ROOF_SCALE_M
    roof_mask: ee.Image  # 0/1 rooftop candidates, terrain-excluded


def build_exclusion_mask(aoi: ee.Geometry, dem_type: str = "srtm") -> ee.Image:
    """
    Terrain keep-mask: 1 where slope < MAX_SLOPE_DEG, 0 elsewhere.

    Caveat worth knowing: the DEM is 30 m, so this excludes buildings sitting on
    steep *terrain*, not buildings with steep *roofs*. In flat cities it rarely
    fires at all. Tracked as limitation P7.
    """
    dem = get_dem(aoi, dem_type=dem_type)
    return ee.Terrain.products(dem).select("slope").lt(MAX_SLOPE_DEG)


def build_roof_layers(
    aoi: ee.Geometry,
    roof_year: int | None,
    presence_threshold: float,
    min_height_m: float,
    exclusion_mask: ee.Image | None = None,
    apply_terrain: bool = True,
) -> RoofLayers:
    """
    Build the rooftop mask and height raster once, so every endpoint works off
    the exact same rooftop definition.

    ``building_height`` gets an explicit default projection because the shadow
    and sky-view models do pixel-denominated neighbourhood operations on it and
    need a known pixel size to be meaningful.

    Pass ``exclusion_mask`` to reuse one you already built; otherwise it is
    derived here when ``apply_terrain`` is set.
    """
    buildings = get_open_buildings_temporal(aoi, year=roof_year)
    building_height = buildings.select("building_height").setDefaultProjection(
        crs="EPSG:4326", scale=ROOF_SCALE_M
    )
    roof_mask = build_rooftop_candidate_mask(
        buildings,
        presence_threshold=presence_threshold,
        min_height_m=min_height_m,
    )
    if apply_terrain:
        if exclusion_mask is None:
            exclusion_mask = build_exclusion_mask(aoi)
        roof_mask = apply_terrain_exclusion(
            roof_mask, exclusion_mask, buildings, scale_m=ROOF_SCALE_M
        )
    return RoofLayers(buildings, building_height, roof_mask)
