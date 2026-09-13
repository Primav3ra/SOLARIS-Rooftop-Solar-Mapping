"""
SOLARIS HTTP API.

The request handlers live here; everything they lean on has been factored out:

* :mod:`solaris.api.windows`  -- temporal window resolution, no Earth Engine
* :mod:`solaris.api.schemas`  -- request models and their bounds
* :mod:`solaris.api.deps`     -- Earth Engine session, AOI, rooftop layers
* :mod:`solaris.core.constants` -- every physical and dataset constant
* :mod:`solaris.gee.*`        -- Earth Engine accessors and physics layers

Handlers call Earth Engine helpers through the ``deps`` module rather than
importing them by name, so patching ``solaris.api.deps.ensure_ee`` in a test
takes effect everywhere.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import ee
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from solaris.api import deps
from solaris.api.schemas import (
    BaselineRequest,
    BuildingsRequest,
    TilesRequest,
    YieldRequest,
)
from solaris.api.windows import (
    _last_complete_calendar_year,
    _series_layout,
    resolve_temporal_window,
)
from solaris.gee.datasets import get_open_buildings_vector
from solaris.gee.irradiance import (
    _ERA5_HOURLY_SCALE_M,
    ERA5_SCALE_M,
    get_era5_baseline_info,
    get_era5_range_info,
    get_roof_masked_era5_baseline_for_date_range,
    get_roof_masked_era5_baseline_info,
    sample_era5_beam_fraction_at_point,
    sample_era5_beam_multi,
    sample_era5_period_ghi_kwh_m2_at_point,
    sample_era5_period_ghi_multi,
)
from solaris.gee.layers import build_exclusion_mask, build_roof_layers
from solaris.gee.penalties import (
    ShadowPenalty,
    SkyViewFactor,
    SoilingPenalty,
    UHIPenalty,
    net_irradiance_image,
)
from solaris.gee.rooftops import get_rooftop_area_m2_info

# Re-exported for callers and tests that reach for these on this module.
gee_project_id = deps.gee_project_id
_ensure_ee = deps.ensure_ee
_aoi_from_req = deps.aoi_from_req
_centroid_lon_lat = deps.centroid_lon_lat
_solar_positions_for_window = deps.solar_positions_for_window
_build_roof_layers = deps.build_roof_layers
_select_target_building = deps.select_target_building
_ee_tile_template = deps.ee_tile_template


app = FastAPI(title="SOLARIS API", version="0.2.0")

# The UI is served same-origin from this same app, so CORS is only needed for
# local development and any future separately-hosted frontend. Note that
# allow_origins=["*"] together with allow_credentials=True is invalid per the
# CORS spec (browsers reject a wildcard origin when credentials are sent), so
# the previous configuration offered no real capability -- and nothing here
# uses cookies or auth headers, so credentials are simply off.
_cors_origins = [
    o.strip()
    for o in os.environ.get(
        "SOLARIS_CORS_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000"
    ).split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/presets")
def presets() -> dict[str, Any]:
    ly = _last_complete_calendar_year()
    return {
        "baseline": {
            "modes": ["yearly", "quarterly", "monthly", "daily"],
            "year_bounds": {"min": 2000, "max": ly, "default": ly},
            "quarter_default": 2,
            "month_default": 1,
            "daily_note": "Use start_date and end_date_exclusive in ISO format; end must be start + 1 day (exclusive).",
        },
    }


@app.post("/api/baseline")
def compute_baseline(req: BaselineRequest) -> dict[str, Any]:
    """
    AOI rooftop area plus an ERA5 irradiance summary for the selected window.

    Previously built a SolarMappingUtils instance per request, which called
    ee.Initialize() every time and used its own copy of the roof-mask builder.
    It now shares _ensure_ee and solaris.gee.layers with every other endpoint.
    """
    try:
        # EE must be initialised before any ee.* object is constructed --
        # _aoi_from_req builds an ee.Geometry, so it cannot come first.
        deps.ensure_ee()
        coords, aoi = deps.aoi_from_req(req)

        exclusion = build_exclusion_mask(aoi)
        rooftop = get_rooftop_area_m2_info(
            aoi,
            year=req.roof_year,
            presence_threshold=req.presence_threshold,
            min_height_m=req.min_height_m,
            exclusion_mask=exclusion,
        )

        try:
            win = resolve_temporal_window(
                req.baseline_mode,
                req.year,
                req.quarter,
                req.month,
                req.start_date,
                req.end_date_exclusive,
            )
        except ValueError as ex:
            raise HTTPException(status_code=400, detail=str(ex)) from ex

        mode = win["mode"]
        s_date, e_date = win["start_date"], win["end_date_exclusive"]

        roof_mask = build_roof_layers(
            aoi,
            roof_year=req.roof_year,
            presence_threshold=req.presence_threshold,
            min_height_m=req.min_height_m,
            exclusion_mask=exclusion,
        ).roof_mask

        aoi_baseline = None
        range_info = None

        if mode == "yearly":
            year = int(win["calendar_year"])
            roof_baseline = get_roof_masked_era5_baseline_info(
                aoi=aoi, roof_mask=roof_mask, start_year=year, end_year=year
            )
            aoi_baseline = get_era5_baseline_info(aoi, start_year=year, end_year=year)
        else:
            # quarterly / monthly / daily all take the same date-range path;
            # only the labels differ.
            roof_baseline = get_roof_masked_era5_baseline_for_date_range(
                aoi=aoi,
                roof_mask=roof_mask,
                start_date=s_date,
                end_date_exclusive=e_date,
            )
            range_info = get_era5_range_info(aoi, start_date=s_date, end_date_exclusive=e_date)

        roof_baseline.update(
            {
                "baseline_time_mode": mode,
                "start_date": s_date,
                "end_date_exclusive": e_date,
                "calendar_year": win.get("calendar_year"),
                "quarter": win.get("quarter"),
                "month": win.get("month"),
            }
        )

        return {
            "status": "ok",
            "baseline_time_mode": mode,
            "temporal_window": {"start_date": s_date, "end_date_exclusive": e_date},
            "aoi_coordinates": coords,
            "rooftop": rooftop,
            "roof_baseline": roof_baseline,
            "aoi_baseline": aoi_baseline,
            "range_baseline": range_info,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/tiles")
def tiles(req: TilesRequest) -> dict[str, Any]:
    """
    Generate Earth Engine tile URL templates (XYZ) for raster overlays within the AOI.
    Layers:
      - roof_mask: rooftop candidate mask (0/1)
      - shadow_frequency: shadow frequency (0..1)
      - sky_view_factor: fraction of diffuse sky visible from the rooftop (0..1)
      - net_irradiance: net irradiance (kWh/m^2 over window)
      - combined_derate: uhi_derate * soiling_retention (scalar image)
      - temperature_delta: UHI delta temperature (MODIS LST daytime anomaly; degC)
    """
    try:
        try:
            win = resolve_temporal_window(
                req.baseline_mode,
                req.year,
                req.quarter,
                req.month,
                req.start_date,
                req.end_date_exclusive,
            )
        except ValueError as ex:
            raise HTTPException(status_code=400, detail=str(ex)) from ex

        deps.ensure_ee()
        coords, aoi = deps.aoi_from_req(req)
        centroid = aoi.centroid(1)
        lon_deg, lat_deg = deps.centroid_lon_lat(centroid)
        s, e = win["start_date"], win["end_date_exclusive"]

        _, building_height, roof_mask = deps.build_roof_layers(
            aoi, req.roof_year, req.presence_threshold, req.min_height_m
        )

        solar_positions = deps.solar_positions_for_window(lat_deg, lon_deg, win)
        shadow_freq = ShadowPenalty.frequency(building_height, solar_positions=solar_positions)
        svf_img = SkyViewFactor.image(building_height)

        # Scalars needed for net irradiance (same as /api/yield)
        ghi_info = sample_era5_period_ghi_kwh_m2_at_point(centroid, s, e, scale_m=ERA5_SCALE_M)
        regional_ghi_kwh_m2_period = float(ghi_info["value"])
        beam_info = sample_era5_beam_fraction_at_point(centroid, s, e)
        beam_fraction = float(beam_info["beam_fraction"])
        uhi_info = UHIPenalty.stats(aoi, s)
        soiling_info = SoilingPenalty.stats(aoi, s)
        combined_derate = float(uhi_info["uhi_derate_factor"]) * float(
            soiling_info["soiling_retention_factor"]
        )

        net_irr = net_irradiance_image(
            regional_ghi_kwh_m2_period,
            shadow_freq,
            beam_fraction=beam_fraction,
            uhi_derate=float(uhi_info["uhi_derate_factor"]),
            soiling_retention=float(soiling_info["soiling_retention_factor"]),
            sky_view_factor=svf_img,
        )

        if req.layer == "roof_mask":
            img = roof_mask.selfMask()
            vis = {"min": 0, "max": 1, "palette": ["00e5ff"]}
        elif req.layer == "shadow_frequency":
            img = shadow_freq.clamp(0, 1)
            vis = {"min": 0, "max": 1, "palette": ["0b1020", "f97316"]}
        elif req.layer == "sky_view_factor":
            img = svf_img.clip(aoi).clamp(0, 1)
            # Low SVF (sky blocked) -> warm; high SVF (open sky) -> cool/green.
            vis = {"min": 0.5, "max": 1.0, "palette": ["ef4444", "f59e0b", "22c55e"]}
        elif req.layer == "temperature_delta":
            # UHI = urban mean LST - ~30km background focal mean (see UHIPenalty.stats).
            uhi_year = int(s[:4])
            lst = (
                ee.ImageCollection(UHIPenalty.MODIS_COLLECTION)
                .filterBounds(aoi)
                .filterDate(f"{uhi_year}-01-01", f"{uhi_year + 1}-01-01")
                .select(UHIPenalty.LST_DAY_BAND)
                .median()
                .multiply(UHIPenalty.LST_SCALE)
                .subtract(UHIPenalty.K_TO_C_OFFSET)
                .rename("LST_celsius")
            )
            background = lst.focal_mean(
                radius=UHIPenalty.BACKGROUND_KERNEL_PX,
                kernelType="circle",
                units="pixels",
            )
            img = lst.subtract(background).rename("delta_t_uhi_celsius").clip(aoi).clamp(-3.0, 8.0)
            # Typical Indian UHI anomalies: ~2-6 degC (but allow a bit wider).
            # Avoid the bright yellow/orange used by irradiance visualizations; keep it cleaner.
            vis = {"min": -3.0, "max": 8.0, "palette": ["2563eb", "22c55e", "a855f7", "ef4444"]}
        elif req.layer == "combined_derate":
            img = ee.Image.constant(combined_derate).rename("combined_derate").clip(aoi)
            vis = {"min": 0.9, "max": 1.0, "palette": ["ef4444", "f59e0b", "22c55e"]}
        else:
            img = net_irr.clip(aoi)
            # Dynamic max for visibility: assume max ~ 1.1x baseline as rough upper bound.
            vis = {
                "min": 0,
                "max": max(50.0, regional_ghi_kwh_m2_period * 1.05),
                "palette": ["0b1020", "2563eb", "22c55e", "f59e0b"],
            }

        url = deps.ee_tile_template(img, vis)
        # Approx bounds from request polygon (lon,lat)
        lons = [p[0] for p in coords]
        lats = [p[1] for p in coords]
        bounds = [[min(lons), min(lats)], [max(lons), max(lats)]]

        return {
            "status": "ok",
            "layer": req.layer,
            "baseline_time_mode": win["mode"],
            "start_date": s,
            "end_date_exclusive": e,
            "urlTemplate": url,
            "tileSize": 256,
            "minZoom": 0,
            "maxZoom": 19,
            "bounds": bounds,
            "attribution": "Google Earth Engine",
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/buildings")
def buildings(req: BuildingsRequest) -> dict[str, Any]:
    """
    Return Open Buildings v3 polygons within the AOI as GeoJSON.
    Intended for map rendering / selection (open-data-only).
    """
    try:
        deps.ensure_ee()
        coords, aoi = deps.aoi_from_req(req)
        fc = get_open_buildings_vector(aoi, confidence_threshold=req.building_confidence).limit(
            req.limit
        )
        gj = fc.getInfo()
        # Keep payload reasonable: strip any huge property blobs, keep key fields only.
        features = []
        for f in (gj or {}).get("features", []) or []:
            props = f.get("properties") or {}
            features.append(
                {
                    "type": "Feature",
                    "id": f.get("id"),
                    "geometry": f.get("geometry"),
                    "properties": {
                        "confidence": props.get("confidence"),
                        "area_in_meters": props.get("area_in_meters"),
                        "full_id": props.get("full_id") or props.get("id"),
                    },
                }
            )
        return {
            "status": "ok",
            "aoi_coordinates": coords,
            "count": len(features),
            "limit": req.limit,
            "building_confidence": req.building_confidence,
            "geojson": {"type": "FeatureCollection", "features": features},
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/yield")
def compute_yield(req: YieldRequest) -> dict[str, Any]:
    """
    Single-building PV energy for the same temporal window as /api/baseline.

    ERA5 GHI is summed over [start_date, end_date_exclusive) at the AOI centroid.
    Shadow retention uses sun positions aligned with that window (year / quarter / day)
    at the centroid latitude and longitude.
    """
    try:
        try:
            win = resolve_temporal_window(
                req.baseline_mode,
                req.year,
                req.quarter,
                req.month,
                req.start_date,
                req.end_date_exclusive,
            )
        except ValueError as ex:
            raise HTTPException(status_code=400, detail=str(ex)) from ex

        deps.ensure_ee()
        coords, aoi = deps.aoi_from_req(req)
        centroid = aoi.centroid(1)
        lon_deg, lat_deg = deps.centroid_lon_lat(centroid)
        s, e = win["start_date"], win["end_date_exclusive"]

        ghi_info = sample_era5_period_ghi_kwh_m2_at_point(centroid, s, e, scale_m=ERA5_SCALE_M)
        regional_ghi_kwh_m2_period = float(ghi_info["value"])
        if ghi_info["source"] in ("no_sample", "null_band"):
            raise HTTPException(
                status_code=500,
                detail="Could not sample ERA5 GHI for the selected period at centroid.",
            )

        solar_positions = deps.solar_positions_for_window(lat_deg, lon_deg, win)

        _, building_height, roof_mask = deps.build_roof_layers(
            aoi, req.roof_year, req.presence_threshold, req.min_height_m
        )

        # Shadow frequency (per-pixel, insolation-weighted, data-driven from building heights)
        shadow_freq = ShadowPenalty.frequency(building_height, solar_positions=solar_positions)

        # Beam fraction: direct / GHI from ERA5 HOURLY -- used to correct shadow losses.
        # Only the beam component is blocked by shadows; diffuse is governed by SVF below.
        beam_info = sample_era5_beam_fraction_at_point(centroid, s, e)
        beam_fraction = float(beam_info["beam_fraction"])

        # Sky View Factor: per-pixel fraction of the diffuse sky still visible from the
        # rooftop after neighbouring buildings occlude part of the hemisphere. Diffuse
        # counterpart of the shadow (beam) penalty; both come from the same height raster.
        # (Mean SVF is reduced once, over the building geometry, further below.)
        svf_img = SkyViewFactor.image(building_height)

        uhi_info = UHIPenalty.stats(aoi, s)
        soiling_info = SoilingPenalty.stats(aoi, s)

        net_irr = net_irradiance_image(
            regional_ghi_kwh_m2_period,
            shadow_freq,
            beam_fraction=beam_fraction,
            uhi_derate=uhi_info["uhi_derate_factor"],
            soiling_retention=soiling_info["soiling_retention_factor"],
            sky_view_factor=svf_img,
        )

        period_label = {
            "yearly": "calendar_year",
            "quarterly": "calendar_quarter",
            "monthly": "calendar_month",
            "daily": "single_day",
        }[win["mode"]]
        (
            building_geom,
            building_props,
            building_geojson_feature,
            building_selection_source,
            selection_warning,
        ) = deps.select_target_building(aoi, coords, centroid, req.building_confidence)

        # Each stage adds one more penalty on top of the last, so the per-stage drops
        # line up: baseline (raw GHI) -> +shadow -> +sky-view -> +uhi -> +soiling(=net).
        # net_irr is the full stack, already built above.
        baseline_irr = ee.Image.constant(regional_ghi_kwh_m2_period).rename("baseline")
        shadow_only_irr = net_irradiance_image(
            regional_ghi_kwh_m2_period,
            shadow_freq,
            beam_fraction=beam_fraction,
            uhi_derate=1.0,
            soiling_retention=1.0,
            sky_view_factor=None,
        )
        svf_only_irr = net_irradiance_image(
            regional_ghi_kwh_m2_period,
            shadow_freq,
            beam_fraction=beam_fraction,
            uhi_derate=1.0,
            soiling_retention=1.0,
            sky_view_factor=svf_img,
        )
        uhi_only_irr = net_irradiance_image(
            regional_ghi_kwh_m2_period,
            shadow_freq,
            beam_fraction=beam_fraction,
            uhi_derate=float(uhi_info["uhi_derate_factor"]),
            soiling_retention=1.0,
            sky_view_factor=svf_img,
        )

        # Stack every stage's energy (irr * roof_mask * area) + the roof area itself into
        # one image and sum it all in a single getInfo -- one round-trip instead of six.
        area_img = roof_mask.toFloat().multiply(ee.Image.pixelArea())
        sum_stack = (
            baseline_irr.multiply(area_img)
            .rename("e_baseline")
            .addBands(shadow_only_irr.multiply(area_img).rename("e_shadow"))
            .addBands(svf_only_irr.multiply(area_img).rename("e_svf"))
            .addBands(uhi_only_irr.multiply(area_img).rename("e_uhi"))
            .addBands(net_irr.multiply(area_img).rename("e_soiling"))
            .addBands(area_img.rename("roof_area"))
        )
        sum_raw = (
            sum_stack.reduceRegion(
                reducer=ee.Reducer.sum(),
                geometry=building_geom,
                scale=4.0,
                maxPixels=1e7,
            ).getInfo()
            or {}
        )

        # and the two means (shadow freq + SVF) share a second reduction.
        mean_stack = shadow_freq.rename("shadow_frequency").addBands(
            svf_img.rename("sky_view_factor")
        )
        mean_raw = (
            mean_stack.reduceRegion(
                reducer=ee.Reducer.mean(),
                geometry=building_geom,
                scale=4.0,
                maxPixels=1e7,
            ).getInfo()
            or {}
        )

        baseline_roof_kwh = float(sum_raw.get("e_baseline") or 0.0)
        after_shadow_roof_kwh = float(sum_raw.get("e_shadow") or 0.0)
        after_svf_roof_kwh = float(sum_raw.get("e_svf") or 0.0)
        after_uhi_roof_kwh = float(sum_raw.get("e_uhi") or 0.0)
        after_soiling_roof_kwh = float(sum_raw.get("e_soiling") or 0.0)
        roof_area_m2 = float(sum_raw.get("roof_area") or 0.0)
        mean_shadow_frequency = mean_raw.get("shadow_frequency")
        mean_sky_view_factor = mean_raw.get("sky_view_factor")
        mean_shadow_fraction = mean_shadow_frequency  # shadow_freq IS the fraction in shadow

        # packing_factor: usable-roof coverage fraction. Applied uniformly to every
        # stage so penalty percentages are unchanged; only absolute kWh scale down to
        # reflect that panels cover ~60-75% of a roof, not 100%.
        yield_scale = req.panel_efficiency * req.performance_ratio * req.packing_factor
        baseline_yield_kwh = baseline_roof_kwh * yield_scale
        after_shadow_yield_kwh = after_shadow_roof_kwh * yield_scale
        after_svf_yield_kwh = after_svf_roof_kwh * yield_scale
        after_uhi_yield_kwh = after_uhi_roof_kwh * yield_scale
        after_soiling_yield_kwh = after_soiling_roof_kwh * yield_scale

        total_energy_kwh = after_soiling_yield_kwh

        penalty_loss_kwh = max(0.0, baseline_yield_kwh - total_energy_kwh)
        penalty_loss_pct = (
            (penalty_loss_kwh / baseline_yield_kwh * 100.0) if baseline_yield_kwh > 0 else 0.0
        )

        shadow_loss_kwh = max(0.0, baseline_yield_kwh - after_shadow_yield_kwh)
        svf_loss_kwh = max(0.0, after_shadow_yield_kwh - after_svf_yield_kwh)
        uhi_loss_kwh = max(0.0, after_svf_yield_kwh - after_uhi_yield_kwh)
        soiling_loss_kwh = max(0.0, after_uhi_yield_kwh - after_soiling_yield_kwh)
        loss_total_for_split = shadow_loss_kwh + svf_loss_kwh + uhi_loss_kwh + soiling_loss_kwh
        if loss_total_for_split <= 0:
            shadow_contrib_pct = 0.0
            svf_contrib_pct = 0.0
            uhi_contrib_pct = 0.0
            soiling_contrib_pct = 0.0
        else:
            shadow_contrib_pct = shadow_loss_kwh / loss_total_for_split * 100.0
            svf_contrib_pct = svf_loss_kwh / loss_total_for_split * 100.0
            uhi_contrib_pct = uhi_loss_kwh / loss_total_for_split * 100.0
            soiling_contrib_pct = soiling_loss_kwh / loss_total_for_split * 100.0

        # Shade matrix: split the day into six 4-hour UTC bins. Same band-stacking trick
        # as above -- one reduction for all six bins rather than six separate calls.
        bucket_specs = [
            ("00-04", 0, 4),
            ("04-08", 4, 8),
            ("08-12", 8, 12),
            ("12-16", 12, 16),
            ("16-20", 16, 20),
            ("20-24", 20, 24),
        ]
        # solar_positions is a list of (alt_deg, az_deg, weight, hour_utc)
        bucket_band = {}  # label -> band name (only for non-empty buckets)
        shade_stack = None
        for label, h0, h1 in bucket_specs:
            bucket_positions = [
                (p[0], p[1], p[2], p[3])
                for p in (solar_positions or [])
                if len(p) >= 4 and p[3] >= h0 and p[3] < h1
            ]
            wsum = sum(float(p[2]) for p in bucket_positions) if bucket_positions else 0.0
            if not bucket_positions or wsum <= 0:
                continue
            norm_positions = [(p[0], p[1], p[2] / wsum, p[3]) for p in bucket_positions]
            band = "shade_" + label.replace("-", "_")
            freq_band = ShadowPenalty.frequency(
                building_height, solar_positions=norm_positions
            ).rename(band)
            bucket_band[label] = band
            shade_stack = freq_band if shade_stack is None else shade_stack.addBands(freq_band)

        shade_raw = (
            (
                shade_stack.reduceRegion(
                    reducer=ee.Reducer.mean(),
                    geometry=building_geom,
                    scale=4.0,
                    maxPixels=1e7,
                ).getInfo()
                or {}
            )
            if shade_stack is not None
            else {}
        )

        shade_intervals = []
        for label, _h0, _h1 in bucket_specs:
            band = bucket_band.get(label)
            raw_bucket = shade_raw.get(band) if band else None
            shade_fraction = float(raw_bucket) if raw_bucket is not None else 0.0
            shade_area_m2 = float(roof_area_m2) * float(shade_fraction)
            shade_intervals.append(
                {
                    "label": label,
                    "shade_fraction": round(shade_fraction, 5),
                    "shade_percent": round(shade_fraction * 100.0, 2),
                    "shade_area_m2": round(shade_area_m2, 2),
                }
            )

        mean_shadow_retention = (
            round(1.0 - mean_shadow_frequency * beam_fraction, 4)
            if mean_shadow_frequency is not None
            else None
        )

        # mean_sky_view_factor already reduced above (batched reduction #2).
        diffuse_fraction = 1.0 - beam_fraction
        # Full retention scalar: diffuse * SVF + beam * (1 - shadow). Falls back to the
        # beam-only shadow retention if SVF could not be sampled.
        if mean_shadow_frequency is not None and mean_sky_view_factor is not None:
            mean_net_retention = round(
                diffuse_fraction * float(mean_sky_view_factor)
                + beam_fraction * (1.0 - mean_shadow_frequency),
                4,
            )
        else:
            mean_net_retention = mean_shadow_retention

        combined_derate = uhi_info["uhi_derate_factor"] * soiling_info["soiling_retention_factor"]
        net_irr_mean = (
            regional_ghi_kwh_m2_period * combined_derate * mean_net_retention
            if mean_net_retention is not None
            else None
        )

        shadow_penalty_percent = (
            round((1.0 - mean_shadow_retention) * 100.0, 2)
            if mean_shadow_retention is not None
            else None
        )
        svf_penalty_percent = (
            round(diffuse_fraction * (1.0 - float(mean_sky_view_factor)) * 100.0, 2)
            if mean_sky_view_factor is not None
            else None
        )
        uhi_penalty_percent = round((1.0 - uhi_info["uhi_derate_factor"]) * 100.0, 2)
        soiling_penalty_percent = round((1.0 - soiling_info["soiling_retention_factor"]) * 100.0, 2)
        combined_penalty_percent = round((1.0 - combined_derate) * 100.0, 2)

        out = {
            "status": "ok",
            "baseline_time_mode": win["mode"],
            "start_date": s,
            "end_date_exclusive": e,
            "accounting_period": period_label,
            "building_selection_source": building_selection_source,
            "selection_warning": selection_warning,
            "regional_ghi_kwh_m2_period": regional_ghi_kwh_m2_period,
            "ghi_sample_source": ghi_info["source"],
            "irradiance_source": "ERA5",
            "panel_efficiency": req.panel_efficiency,
            "performance_ratio": req.performance_ratio,
            "packing_factor": req.packing_factor,
            # Some windows don't define quarter/month keys; keep response stable.
            "calendar_year": win.get("calendar_year"),
            "quarter": win.get("quarter"),
            "month": win.get("month"),
            "building_confidence": building_props.get("confidence"),
            "building_area_in_meters": building_props.get("area_in_meters"),
            "roof_area_m2": roof_area_m2,
            "mean_shadow_fraction": mean_shadow_fraction,
            "mean_shadow_retention": mean_shadow_retention,
            # Authoritative stage yields (PV output, kWh)
            "baseline_yield_kwh": round(float(baseline_yield_kwh), 6),
            "after_shadow_yield_kwh": round(float(after_shadow_yield_kwh), 6),
            "after_svf_yield_kwh": round(float(after_svf_yield_kwh), 6),
            "after_uhi_yield_kwh": round(float(after_uhi_yield_kwh), 6),
            "after_soiling_yield_kwh": round(float(after_soiling_yield_kwh), 6),
            # Loss + contribution (of total loss) in kWh / %
            "penalty_loss_kwh": round(float(penalty_loss_kwh), 6),
            "penalty_loss_pct": round(float(penalty_loss_pct), 4),
            "penalty_contribution": {
                "shadow_loss_kwh": round(float(shadow_loss_kwh), 6),
                "svf_loss_kwh": round(float(svf_loss_kwh), 6),
                "uhi_loss_kwh": round(float(uhi_loss_kwh), 6),
                "soiling_loss_kwh": round(float(soiling_loss_kwh), 6),
                "shadow_contribution_pct": round(float(shadow_contrib_pct), 3),
                "svf_contribution_pct": round(float(svf_contrib_pct), 3),
                "uhi_contribution_pct": round(float(uhi_contrib_pct), 3),
                "soiling_contribution_pct": round(float(soiling_contrib_pct), 3),
            },
            "shade_intervals": shade_intervals,
            "beam_fraction": beam_fraction,
            "diffuse_fraction": beam_info["diffuse_fraction"],
            "beam_fraction_source": beam_info["source"],
            "mean_sky_view_factor": (
                round(float(mean_sky_view_factor), 5) if mean_sky_view_factor is not None else None
            ),
            "svf_penalty_percent": svf_penalty_percent,
            "sky_view_factor_meta": {
                "n_azimuth": SkyViewFactor.N_AZIMUTH,
                "sample_radii_px": list(SkyViewFactor.DIST_PX),
            },
            "uhi_derate_factor": uhi_info["uhi_derate_factor"],
            "delta_t_uhi_celsius": uhi_info["delta_t_uhi_celsius"],
            "soiling_retention_factor": soiling_info["soiling_retention_factor"],
            "mean_aod_550nm": soiling_info["mean_aod_550nm"],
            "combined_derate_factor": round(combined_derate, 5),
            "net_irradiance_kwh_m2_period": net_irr_mean,
            "period_yield_kwh": total_energy_kwh,
            "shadow_penalty_percent": shadow_penalty_percent,
            "uhi_penalty_percent": uhi_penalty_percent,
            "soiling_penalty_percent": soiling_penalty_percent,
            "combined_penalty_percent": combined_penalty_percent,
            "uhi_penalty": uhi_info,
            "soiling_penalty": soiling_info,
            "geojson": {
                "type": "FeatureCollection",
                "features": [
                    {
                        **building_geojson_feature,
                        "properties": {
                            **building_props,
                            "roof_area_m2": roof_area_m2,
                            "mean_shadow_fraction": mean_shadow_fraction,
                            "uhi_derate_factor": uhi_info["uhi_derate_factor"],
                            "soiling_retention_factor": soiling_info["soiling_retention_factor"],
                            "net_irradiance_kwh_m2_period": net_irr_mean,
                            "period_yield_kwh": total_energy_kwh,
                            "shade_intervals": shade_intervals,
                        },
                    }
                ],
            },
        }
        return out
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/series")
def compute_series(req: YieldRequest) -> dict[str, Any]:
    """
    The whole generation curve in one request, instead of firing /api/yield once per
    point (that used to be 12-31 round-trips). GHI and beam come from a couple of
    batched samples; the shadow part is reduced per period so no single EE request has
    to chew through every period's shadow at once -- do that and it OOMs.

        yearly    -> 12 monthly points
        quarterly -> the quarter's 3 months
        monthly   -> daily, binned into weeks W1..W5
        daily     -> one point

    Under the hood it's just net_irradiance_image factored out:
        net = GHI * uhi * soiling * eff*PR*packing
              * [ (1-beam)*SUM(SVF*area) + beam*SUM((1-shadow)*area) ]
    so a point here matches what /api/yield gives for that sub-window.
    """
    try:
        try:
            win = resolve_temporal_window(
                req.baseline_mode,
                req.year,
                req.quarter,
                req.month,
                req.start_date,
                req.end_date_exclusive,
            )
        except ValueError as ex:
            raise HTTPException(status_code=400, detail=str(ex)) from ex

        deps.ensure_ee()
        coords, aoi = deps.aoi_from_req(req)
        centroid = aoi.centroid(1)
        lon_deg, lat_deg = deps.centroid_lon_lat(centroid)
        mode = win["mode"]

        labels, items, bin_of = _series_layout(mode, win, lat_deg, lon_deg)
        if not items:
            return {
                "status": "ok",
                "baseline_time_mode": mode,
                "labels": labels,
                "values": [0.0] * len(labels),
            }

        _, building_height, roof_mask = deps.build_roof_layers(
            aoi, req.roof_year, req.presence_threshold, req.min_height_m
        )
        building_geom, _, _, _, _ = deps.select_target_building(
            aoi, coords, centroid, req.building_confidence
        )
        svf_img = SkyViewFactor.image(building_height)
        area_img = roof_mask.toFloat().multiply(ee.Image.pixelArea())

        # GHI + beam for every sub-period -- two batched point samples.
        windows = [(s, e) for (s, e, _pos) in items]
        ghi_list = sample_era5_period_ghi_multi(centroid, windows, scale_m=ERA5_SCALE_M)
        beam_list = sample_era5_beam_multi(centroid, windows, scale_m=_ERA5_HOURLY_SCALE_M)

        # SVF*area doesn't change month to month, so grab it once.
        svf_area = float(
            (
                svf_img.multiply(area_img)
                .rename("svf_area")
                .reduceRegion(ee.Reducer.sum(), building_geom, 4.0, maxPixels=1e7)
                .getInfo()
                or {}
            ).get("svf_area")
            or 0.0
        )

        # The (1-shadow) part DOES change per period (sun moves), so walk them one by
        # one. Each is about the load /api/yield handles fine; stacking all 12 into a
        # single reduce is what tripped the memory limit and blanked the curve earlier.
        retained_beam_area = []
        for _s, _e, pos in items:
            shadow_freq = ShadowPenalty.frequency(building_height, solar_positions=pos)
            ba = ee.Image(1.0).subtract(shadow_freq).multiply(area_img).rename("ba")
            raw = (
                ba.reduceRegion(
                    reducer=ee.Reducer.sum(),
                    geometry=building_geom,
                    scale=4.0,
                    maxPixels=1e7,
                ).getInfo()
                or {}
            )
            retained_beam_area.append(float(raw.get("ba") or 0.0))

        # uhi + soiling are annual numbers, so compute once and reuse for every point.
        uhi = UHIPenalty.stats(aoi, items[0][0])
        soiling = SoilingPenalty.stats(aoi, items[0][0])
        derate = float(uhi["uhi_derate_factor"]) * float(soiling["soiling_retention_factor"])
        scale = req.panel_efficiency * req.performance_ratio * req.packing_factor

        values = [0.0] * len(labels)
        for i in range(len(items)):
            beam_i = beam_list[i]
            diffuse_i = 1.0 - beam_i
            net_i = (
                ghi_list[i]
                * derate
                * scale
                * (diffuse_i * svf_area + beam_i * retained_beam_area[i])
            )
            values[bin_of[i]] += net_i

        return {
            "status": "ok",
            "baseline_time_mode": mode,
            "labels": labels,
            "values": [round(v, 3) for v in values],
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# Resolve the static directory relative to this file, not the process cwd --
# a relative path here meant the server only worked when launched from the
# repo root and 500'd otherwise. Guarded so that a checkout without a built
# frontend can still import the app (tests, CI).
_STATIC_DIR = Path(__file__).resolve().parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")


# Resolve the static directory relative to this file, not the process cwd --
# a relative path here meant the server only worked when launched from the
# repo root and 500'd otherwise. Guarded so that a checkout without a built
# frontend can still import the app (tests, CI).
_STATIC_DIR = Path(__file__).resolve().parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")


def main() -> None:
    """
    Console entry point (``solaris-api``).

    Binds the port Cloud Run injects via $PORT, falling back to 8000 locally.
    """
    import uvicorn

    uvicorn.run(
        "solaris.api.app:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8000")),
        timeout_keep_alive=5,
    )


if __name__ == "__main__":
    main()
