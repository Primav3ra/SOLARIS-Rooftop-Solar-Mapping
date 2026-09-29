"""
Rooftop candidate mask + area, off the Open Buildings 2.5D layer.

A few defaults worth knowing: presence > 0.5 is just a prior (the model's confidence
isn't calibrated, so tune it if needed); min_height_m lets you drop low/noise structures
but is off by default; and we reduce at 4 m to match Open Buildings' effective resolution.
"""

from __future__ import annotations

import ee


def build_rooftop_candidate_mask(
    buildings: ee.Image,
    presence_threshold: float = 0.5,
    min_height_m: float = 0.0,
) -> ee.Image:
    """
    0/1 mask of likely-rooftop pixels: presence over the threshold, and (optionally)
    tall enough. `buildings` needs the presence + height bands. Band out: 'roof_candidate'.
    """
    presence = buildings.select("building_presence")
    height = buildings.select("building_height")
    cand = presence.gt(presence_threshold)
    if min_height_m and min_height_m > 0:
        cand = cand.And(height.gte(min_height_m))
    return cand.rename("roof_candidate").toUint8()


def apply_terrain_exclusion(
    roof_mask: ee.Image,
    exclusion_mask: ee.Image,
    buildings: ee.Image,
    scale_m: float = 4.0,
) -> ee.Image:
    """
    AND the roof mask with the terrain keep-mask (drops steep slopes). Reprojects the
    exclusion onto the building layer's grid first so the pixels line up.
    """
    ref = buildings.select("building_presence")
    proj = ref.projection()
    exclusion_repr = exclusion_mask.reproject(crs=proj, scale=scale_m).toFloat()
    exclusion_bin = exclusion_repr.gt(0.5)  # re-binarize after the resample
    combined = roof_mask.multiply(exclusion_bin.toUint8())
    return combined.rename("roof_candidate").toUint8()
