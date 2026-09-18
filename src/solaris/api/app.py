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

import contextlib
import os
from datetime import date
from pathlib import Path
from typing import Any

import ee
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from solaris.api import deps, middleware
from solaris.api.auth import GUEST_HEADER, AllowanceExceededError, get_guest_allowance
from solaris.api.deps import EarthEngineUnavailableError
from solaris.api.schemas import (
    BaselineRequest,
    BuildingsRequest,
    TilesRequest,
    YieldRequest,
)
from solaris.api.windows import (
    _series_layout,
    resolve_temporal_window,
)
from solaris.core import constants as C
from solaris.core.cache import lookup as cache_lookup
from solaris.core.cache import store as cache_store
from solaris.core.config import get_settings
from solaris.core.limits import (
    BudgetExceededError,
    ConcurrencyTimeoutError,
    RateLimitExceededError,
)
from solaris.core.quality import DataQuality
from solaris.gee.coverage import (
    conservative_latest_date,
    latest_available_date,
    max_selectable_year,
    window_coverage,
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

#: Upper end of the shadow-frequency colour stretch.
#:
#: Rooftop shadow frequency rarely exceeds this. Stretching 0-1 instead pushed
#: every real value into the darkest tenth of the palette, which is why that
#: overlay appeared blank.
SHADOW_VIS_MAX = 0.35

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

_settings = get_settings()

# The dashboard is served same-origin from this same app, so CORS only matters
# for local development and any separately hosted frontend. Origins come from
# validated settings, which reject a wildcard outright -- the old
# allow_origins=["*"] with allow_credentials=True was invalid per the CORS spec
# anyway (browsers reject a wildcard origin when credentials are sent), so it
# never granted what it appeared to. Nothing here uses cookies or auth headers.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_settings.cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type", "X-Request-ID"],
)

# Request id, rate limiting, structured logging, and error handling that does
# not leak Earth Engine internals to the caller.
middleware.install(app, _settings)


#: India Standard Time offset from UTC, in hours.
IST_OFFSET_HOURS = 5.5


def _utc_bucket_to_ist(label: str) -> str:
    """
    Re-label a UTC hour bucket in IST.

    The shade matrix buckets are UTC because the pipeline is UTC-aligned to
    ERA5, but an Indian user reads "08-12" as morning when it is really
    13:30-17:30 local. Reporting both removes the ambiguity rather than
    silently mislabelling.
    """
    start_utc, end_utc = (int(part) for part in label.split("-"))

    def shift(hour_utc: int) -> str:
        total = hour_utc + IST_OFFSET_HOURS
        wrapped = total % 24
        hours = int(wrapped)
        minutes = round((wrapped - hours) * 60)
        return f"{hours:02d}:{minutes:02d}"

    return f"{shift(start_utc)}-{shift(end_utc)} IST"


@app.exception_handler(HTTPException)
async def _sanitise_http_exception(request: Request, exc: HTTPException):
    """
    Keep server-side detail server-side.

    Handlers raise ``HTTPException(500, detail=str(exc))``, and FastAPI's own
    handler serialises that straight to the client -- so a deployed instance
    answered with "Caller does not have required permission to use project
    pv-mapping-india", leaking the project id to anyone who asked. FastAPI
    handles HTTPException before any middleware sees it, so the sanitising has
    to happen here.

    4xx details are kept: those are caller-facing validation messages and are
    the whole point of the status code.
    """
    request_id = middleware.request_id_var.get()
    if exc.status_code >= 500:
        middleware.logger.error(
            "handler error",
            extra={
                "extra_fields": {
                    "path": request.url.path,
                    "status": exc.status_code,
                    "detail": str(exc.detail),
                }
            },
        )
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": "internal_error",
                    "message": (
                        "An internal error occurred. Quote the request id when reporting it."
                    ),
                },
                "request_id": request_id,
            },
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {"code": "bad_request", "message": exc.detail},
            "request_id": request_id,
        },
        headers=getattr(exc, "headers", None) or {},
    )


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/presets")
def presets() -> dict[str, Any]:
    """
    Supported modes, the selectable year range, and the model constants.

    The year ceiling is **data-derived**: it comes from the last date
    ERA5-Land actually has, not from the calendar. The previous
    ``date.today().year - 1`` refused a quarter of the current year that had
    finished months earlier, purely because the calendar year had not ended.

    Also serves the physics constants, so the dashboard stops carrying its own
    copies of the PV parameters and the grid emission factor -- which was the
    "keep in sync" comment that nothing enforced.
    """
    latest = _cached_latest_available_date()
    max_year = max_selectable_year(latest)
    return {
        "baseline": {
            "modes": ["yearly", "quarterly", "monthly", "daily"],
            "year_bounds": {"min": 2000, "max": max_year, "default": max_year},
            "quarter_default": 2,
            "month_default": 1,
            "daily_note": (
                "Use start_date and end_date_exclusive in ISO format; end must "
                "be start + 1 day (exclusive)."
            ),
            "data_availability": {
                "latest_available_date": latest.isoformat() if latest else None,
                "source": "probed" if _LATEST_PROBE["ok"] else "conservative_fallback",
                "note": (
                    "Derived from the last available ERA5-Land image. A window "
                    "reaching past this date returns a partial result, flagged "
                    "under data_quality.coverage."
                ),
            },
        },
        "constants": _public_constants(),
    }


#: Probe result, cached for the day. /api/presets should not cost an Earth
#: Engine round-trip on every call, and coverage advances at most daily.
_LATEST_PROBE: dict[str, Any] = {"day": None, "value": None, "ok": False}


def _cached_latest_available_date():
    """
    The last date with ERA5-Land data, probed once per day per process.

    Falls back to a conservative bound when the probe fails, so a probe failure
    narrows the year ceiling rather than taking the endpoint down.
    """
    today = date.today().isoformat()
    if _LATEST_PROBE["day"] == today:
        return _LATEST_PROBE["value"]
    try:
        deps.ensure_ee()
        value = latest_available_date()
        ok = value is not None
    except Exception:
        value, ok = None, False
    if value is None:
        value = conservative_latest_date()
    _LATEST_PROBE.update({"day": today, "value": value, "ok": ok})
    return value


def _public_constants() -> dict[str, Any]:
    """
    Model constants the dashboard needs.

    Served rather than duplicated: the PV parameters and the grid emission
    factor were hardcoded in both the backend and the dashboard JavaScript,
    with a comment asking future editors to keep them in sync.
    """
    return {
        "panel_efficiency": C.PANEL_EFFICIENCY,
        "performance_ratio": C.PERFORMANCE_RATIO,
        "packing_factor": C.PACKING_FACTOR,
        "building_confidence": C.BUILDING_CONFIDENCE,
        "grid_emission_factor_kg_per_kwh": C.GRID_EMISSION_FACTOR_KG_PER_KWH,
        "stc_irradiance_w_m2": C.STC_IRRADIANCE_W_M2,
        "max_slope_deg": C.MAX_SLOPE_DEG,
        "roof_scale_m": C.ROOF_SCALE_M,
        "max_aoi_km2": C.MAX_AOI_KM2,
        "max_half_size_deg": C.MAX_HALF_SIZE_DEG,
        "open_buildings_years": [
            C.OPEN_BUILDINGS_MIN_YEAR,
            C.OPEN_BUILDINGS_MAX_YEAR,
        ],
        "min_days_for_annualisation": C.MIN_DAYS_FOR_ANNUALISATION,
        "algo_version": C.ALGO_VERSION,
        "dataset_version": C.DATASET_VERSION,
    }


@app.get("/api/version")
def version() -> dict[str, Any]:
    """
    Build and algorithm identity.

    Makes "which code produced this figure?" answerable, which matters when a
    validation report is compared against an earlier one.
    """
    settings = get_settings()
    return {
        "version": "0.2.0",
        "git_sha": settings.git_sha,
        "env": settings.env,
        "algo_version": C.ALGO_VERSION,
        "dataset_version": C.DATASET_VERSION,
    }


@app.get("/api/config")
def config() -> dict[str, Any]:
    """Non-secret runtime configuration, for debugging a deployed instance."""
    return get_settings().redacted()


# ---------------------------------------------------------------------------
# Guest sessions
# ---------------------------------------------------------------------------


@app.post("/api/auth/guest")
def auth_guest() -> dict[str, Any]:
    """
    Mint an opaque guest session token.

    The interface calls this once and returns the token in
    ``X-Solaris-Guest``. It is not a credential and grants nothing beyond the
    session allowance; it exists so that allowance is per browser session
    rather than per IP address, which behind carrier-grade NAT would mean one
    allowance shared across thousands of subscribers.
    """
    allowance = get_guest_allowance()
    return {
        "guest_token": allowance.issue(),
        "allowance": allowance.allowance,
        "header": GUEST_HEADER,
    }


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    """
    What this session has left.

    Lets the interface show a remaining count rather than leaving a visitor to
    discover the limit by reaching it.
    """
    identity = getattr(request.state, "identity", None)
    return {
        "identity": identity.as_dict() if identity else {"kind": "unknown"},
        "allowance": deps.allowance_state(request),
    }


@app.get("/api/ready")
def ready() -> dict[str, Any]:
    """
    Readiness: can this instance actually serve a computation?

    Distinct from /api/health, which is liveness and touches nothing. Used by
    the post-deploy smoke test. Deliberately cheap -- it establishes the Earth
    Engine session but runs no reduction.
    """
    settings = get_settings()
    checks: dict[str, Any] = {"config": "ok", "earth_engine": "unknown"}
    hint = ""
    try:
        deps.ensure_ee()
        checks["earth_engine"] = "ok"
    except deps.EarthEngineUnavailableError as exc:
        checks["earth_engine"] = "unavailable"
        hint = exc.hint
    except Exception as exc:
        checks["earth_engine"] = f"error: {type(exc).__name__}"

    out: dict[str, Any] = {
        "ready": checks["earth_engine"] == "ok",
        "checks": checks,
        "project": settings.gee_project_id,
        "auth_mode": settings.gee_auth_mode,
    }
    # An actionable hint, because "error: EEException" tells whoever is setting
    # this up nothing at all. This endpoint already reports configuration by
    # design -- it is what the post-deploy smoke test reads -- so the hint adds
    # no exposure that the project id has not already.
    if hint:
        out["hint"] = hint
    return out


@app.post("/api/baseline")
def compute_baseline(req: BaselineRequest, request: Request) -> dict[str, Any]:
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
    except (
        HTTPException,
        AllowanceExceededError,
        EarthEngineUnavailableError,
        BudgetExceededError,
        RateLimitExceededError,
        ConcurrencyTimeoutError,
    ):
        # Limit failures are translated by the middleware into 429/503
        # with a Retry-After; swallowing them here would report a quota
        # problem as an internal error.
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

        net_irr = net_irradiance_image(
            regional_ghi_kwh_m2_period,
            shadow_freq,
            beam_fraction=beam_fraction,
            uhi_derate=float(uhi_info["uhi_derate_factor"]),
            soiling_retention=float(soiling_info["soiling_retention_factor"]),
            sky_view_factor=svf_img,
        )

        # Every per-pixel layer is masked to the roof. Shadow and sky-view are
        # only defined on a roof, and rendering them across the whole area of
        # interest put a wash of colour over streets and parks where the model
        # makes no claim at all.
        roofs_only = roof_mask.selfMask()

        if req.layer == "roof_mask":
            img = roofs_only
            vis = {"min": 0, "max": 1, "palette": ["00e5ff"]}
        elif req.layer == "shadow_frequency":
            # Stretched over the range roofs actually occupy, not 0-1.
            #
            # Rooftop shadow frequency is typically 0.02-0.10 and rarely passes
            # 0.3, so a 0-1 stretch mapped every real value into the first tenth
            # of the palette and the layer rendered as a uniform dark wash --
            # indistinguishable from "no data" and the reason this overlay
            # looked useless. Measured for June over Delhi: mean 0.024.
            img = shadow_freq.updateMask(roofs_only).clamp(0, SHADOW_VIS_MAX)
            vis = {
                "min": 0.0,
                "max": SHADOW_VIS_MAX,
                "palette": ["1e3a5f", "3b82f6", "fbbf24", "ef4444"],
            }
        elif req.layer == "sky_view_factor":
            img = svf_img.updateMask(roofs_only).clamp(0, 1)
            # Low SVF (sky blocked) -> warm; high SVF (open sky) -> cool/green.
            vis = {"min": 0.6, "max": 1.0, "palette": ["ef4444", "f59e0b", "22c55e"]}
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
            # Metres, matching UHIPenalty.stats. A pixel-denominated kernel
            # resolves against the *request* projection, so at tile-pyramid
            # scales this window spanned hundreds of kilometres rather than 30
            # -- the same defect that was fixed in the reduction path but left
            # behind here, which is why the map and the number disagreed.
            background = lst.focal_mean(
                radius=UHIPenalty.BACKGROUND_KERNEL_M,
                kernelType="circle",
                units="meters",
            )
            img = lst.subtract(background).rename("delta_t_uhi_celsius").clip(aoi).clamp(-3.0, 8.0)
            # Typical Indian UHI anomalies: ~2-6 degC (but allow a bit wider).
            # Avoid the bright yellow/orange used by irradiance visualizations; keep it cleaner.
            vis = {"min": -3.0, "max": 8.0, "palette": ["2563eb", "22c55e", "a855f7", "ef4444"]}
        elif req.layer == "combined_derate":
            # Removed from the layer list, and rejected rather than rendered.
            #
            # The heat-island and soiling derates are AOI-wide *scalars*, so
            # this layer could only ever draw a single flat colour over the
            # whole box. It was not a poor visualisation, it was a map of a
            # number that has no spatial variation -- and drawing it implied a
            # per-pixel result the model does not produce. The spatially varying
            # part of the same physics is `temperature_delta`.
            raise HTTPException(
                status_code=400,
                detail=(
                    "combined_derate is an area-wide scalar, not a per-pixel "
                    "layer, so it cannot be mapped. Use temperature_delta for "
                    "the heat-island field, or read the derate from /api/yield."
                ),
            )
        else:
            # Net irradiance, stretched across the range roofs occupy.
            #
            # A 0-to-GHI stretch put every roof in a narrow band near the top of
            # the palette, because net retention is ~0.7-0.9 of the baseline
            # everywhere. Anchoring the low end at half the baseline spends the
            # palette on the variation that exists.
            img = net_irr.updateMask(roofs_only)
            vis = {
                "min": max(1.0, regional_ghi_kwh_m2_period * 0.5),
                "max": max(2.0, regional_ghi_kwh_m2_period * 0.95),
                "palette": ["1e3a5f", "2563eb", "22c55e", "fbbf24"],
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
    except (
        HTTPException,
        AllowanceExceededError,
        EarthEngineUnavailableError,
        BudgetExceededError,
        RateLimitExceededError,
        ConcurrencyTimeoutError,
    ):
        # Limit failures are translated by the middleware into 429/503
        # with a Retry-After; swallowing them here would report a quota
        # problem as an internal error.
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
    except (
        HTTPException,
        AllowanceExceededError,
        EarthEngineUnavailableError,
        BudgetExceededError,
        RateLimitExceededError,
        ConcurrencyTimeoutError,
    ):
        # Limit failures are translated by the middleware into 429/503
        # with a Retry-After; swallowing them here would report a quota
        # problem as an internal error.
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/api/yield")
def compute_yield(req: YieldRequest, request: Request) -> Response:
    """
    Single-building PV energy for the same temporal window as /api/baseline.

    ERA5 GHI is summed over [start_date, end_date_exclusive) at the AOI centroid.
    Shadow retention uses sun positions aligned with that window (year / quarter / day)
    at the centroid latitude and longitude.

    Cached on the resolved window, and the Earth Engine work runs inside a
    bounded slot counted against the daily call budget.
    """
    # Created before the try so the finally below can always close it, whether
    # we return from a cache hit, fail window validation, or complete normally.
    ee_stack = contextlib.ExitStack()
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

        s, e = win["start_date"], win["end_date_exclusive"]

        # Cache lookup happens *after* the window is resolved, so
        # {mode: yearly, year: 2023} and the equivalent explicit range collapse
        # to one entry rather than two.
        cached = cache_lookup("/api/yield", req.model_dump(), start_date=s, end_date_exclusive=e)
        if cached.value is not None:
            return JSONResponse(
                content=cached.value,
                headers={**cached.headers, **deps.allowance_headers(request)},
            )

        # A miss means real work, so this is where a guest allowance is spent.
        # Deliberately after the lookup: a cached answer is free to serve, so
        # charging for it would punish exactly the requests that cost nothing.
        deps.charge_computation(request)

        # Bound concurrent Earth Engine work and count it against the daily
        # budget. /api/yield makes roughly a dozen round-trips.
        ee_stack.enter_context(deps.ee_gate(n_calls=12))
        deps.ensure_ee()
        coords, aoi = deps.aoi_from_req(req)
        centroid = aoi.centroid(1)
        lon_deg, lat_deg = deps.centroid_lon_lat(centroid)

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
        # The windowed model: AOD sets the deposition rate, ERA5-Land rainfall
        # sets the cleaning events, and the loss is averaged over the actual
        # window. The legacy path returned one annual number whatever the
        # window, which for a single monsoon day was wrong by roughly an order
        # of magnitude.
        soiling_info = SoilingPenalty.stats_windowed(
            aoi,
            s,
            e,
            cleaning_interval_days=req.cleaning_interval_days,
            point=centroid,
        )

        # The fully-derated image is no longer built here. It existed only to be
        # reduced as the fifth band of the energy stack, and that stage is now
        # derived from the sky-view stage by scalar multiplication -- see the note
        # on sum_stack below. The reported net irradiance comes from the mean
        # reduction and the same scalars, so nothing is lost.

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

        # Stack the stages' energy (irr * roof_mask * area) plus the roof area into one
        # image and sum it in a single getInfo -- one round-trip instead of six.
        #
        # Only the *structurally distinct* stages go in. The heat-island and soiling
        # stages are the sky-view stage multiplied by positive scalars, so they are
        # derived in Python below instead of being reduced separately. The algebra is
        # exact: net_irradiance_image folds the derates into a scalar coefficient and
        # then floors at zero, and max(0, k*r) == k*max(0, r) for k > 0.
        #
        # This is not a micro-optimisation. Each geometric stage embeds the whole
        # directional shadow trace -- roughly 31 sun positions by up to 12 sample
        # distances -- plus the sky-view fan of 8 azimuths by 5 radii. Stacking five
        # copies of that tree made Earth Engine answer **"User memory limit
        # exceeded"** for an ordinary Delhi query, so the endpoint failed outright
        # rather than being slow. Three bands instead of five, and the two derived
        # stages cost nothing.
        area_img = roof_mask.toFloat().multiply(ee.Image.pixelArea())
        sum_stack = (
            baseline_irr.multiply(area_img)
            .rename("e_baseline")
            .addBands(shadow_only_irr.multiply(area_img).rename("e_shadow"))
            .addBands(svf_only_irr.multiply(area_img).rename("e_svf"))
            .addBands(area_img.rename("roof_area"))
        )
        sum_raw = (
            sum_stack.reduceRegion(
                reducer=ee.Reducer.sum(),
                geometry=building_geom,
                scale=4.0,
                maxPixels=int(1e7),
                # Splits the region into smaller tiles, trading round-trip time for
                # peak memory. The canonical remedy for the limit above, and cheap
                # insurance for a dense AOI where the tree is at its largest.
                tileScale=4,
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
                maxPixels=int(1e7),
                tileScale=4,
            ).getInfo()
            or {}
        )

        baseline_roof_kwh = float(sum_raw.get("e_baseline") or 0.0)
        after_shadow_roof_kwh = float(sum_raw.get("e_shadow") or 0.0)
        after_svf_roof_kwh = float(sum_raw.get("e_svf") or 0.0)
        # Derived, not reduced: these two stages are the sky-view stage scaled by
        # the derates, which are plain positive scalars. See the note on sum_stack.
        after_uhi_roof_kwh = after_svf_roof_kwh * float(uhi_info["uhi_derate_factor"])
        after_soiling_roof_kwh = after_uhi_roof_kwh * float(
            soiling_info["soiling_retention_factor"]
        )
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
        # Buckets are UTC because the whole pipeline is UTC-aligned to ERA5,
        # but a user in India reads "08-12" as morning when it is actually
        # 13:30-17:30 local. Each bucket therefore carries both labels.
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
                    maxPixels=int(1e7),
                ).getInfo()
                or {}
            )
            if shade_stack is not None
            else {}
        )

        shade_intervals = []
        for label, _h0, _h1 in bucket_specs:
            # Named distinctly from the `band` built in the loop above: reusing
            # that name made this an Optional assignment to a str.
            bucket_band_name = bucket_band.get(label)
            raw_bucket = shade_raw.get(bucket_band_name) if bucket_band_name is not None else None
            shade_fraction = float(raw_bucket) if raw_bucket is not None else 0.0
            shade_area_m2 = float(roof_area_m2) * float(shade_fraction)
            shade_intervals.append(
                {
                    "label": label,
                    "label_utc": f"{label} UTC",
                    "label_ist": _utc_bucket_to_ist(label),
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

        # D8: every data path here has a fallback that returns a *plausible*
        # number, distinguishable only by a `source` string buried in a nested
        # dict. Collect them so a degraded result is visibly degraded instead
        # of quietly wrong. See solaris.core.quality.
        quality = DataQuality()
        quality.record_source("regional_ghi", ghi_info.get("source"))
        quality.record_source("beam_fraction", beam_info.get("source"))
        quality.record_stats("uhi", uhi_info)
        quality.record_stats("soiling", soiling_info)
        quality.record_source("target_building", building_selection_source)
        if soiling_info.get("soiling_retention_clamped"):
            quality.record_clamp(
                "soiling_retention",
                "Soiling retention hit its floor, which means the measured "
                "aerosol optical depth was outside anything the literature "
                "supports. Check the input rather than trusting the loss.",
            )
        empty_buckets = [
            interval["label"]
            for interval in shade_intervals
            if interval["shade_fraction"] == 0.0 and interval["label"] not in bucket_band
        ]
        quality.record_empty_shade_buckets([str(label) for label in empty_buckets])
        try:
            quality.set_coverage(
                window_coverage(s, e, latest=_cached_latest_available_date()).as_dict()
            )
        except Exception:
            quality.note("Temporal coverage could not be determined.")

        out = {
            "status": "ok",
            "data_quality": quality.as_dict(),
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
        # Only cache a result whose window is fully covered: caching a partial
        # answer would pin it for the whole TTL and leave it wrong after the
        # data arrived.
        coverage = quality.coverage or {}
        cache_store(
            cached,
            out,
            end_date_exclusive=e,
            ttl_s=get_settings().cache_ttl_result_s,
            coverage_complete=coverage.get("status") == "complete",
        )
        return JSONResponse(
            content=out, headers={**cached.headers, **deps.allowance_headers(request)}
        )
    except (
        HTTPException,
        AllowanceExceededError,
        EarthEngineUnavailableError,
        BudgetExceededError,
        RateLimitExceededError,
        ConcurrencyTimeoutError,
    ):
        # Limit failures are translated by the middleware into 429/503
        # with a Retry-After; swallowing them here would report a quota
        # problem as an internal error.
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        # Releases the Earth Engine slot on every path. A no-op when the slot
        # was never taken, which is the cache-hit case.
        ee_stack.close()


@app.post("/api/series")
def compute_series(req: YieldRequest, request: Request) -> dict[str, Any]:
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
                .reduceRegion(ee.Reducer.sum(), building_geom, 4.0, maxPixels=int(1e7))
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
                    maxPixels=int(1e7),
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
    except (
        HTTPException,
        AllowanceExceededError,
        EarthEngineUnavailableError,
        BudgetExceededError,
        RateLimitExceededError,
        ConcurrencyTimeoutError,
    ):
        # Limit failures are translated by the middleware into 429/503
        # with a Retry-After; swallowing them here would report a quota
        # problem as an internal error.
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e


# ---------------------------------------------------------------------------
# The single-page app
# ---------------------------------------------------------------------------

# Resolved relative to this file, not the process cwd -- a relative path here
# meant the server only worked when launched from the repo root and 500'd
# otherwise. Guarded so a checkout with no built frontend can still import the
# app, which is what lets the test suite run without an npm install.
_STATIC_DIR = Path(__file__).resolve().parent / "static"
_INDEX_HTML = _STATIC_DIR / "index.html"


class _SpaStaticFiles(StaticFiles):
    """
    Static files, with a fallback to ``index.html`` for unknown paths.

    The frontend is a single-page app with client-side routing, so a request for
    ``/explore`` has no corresponding file: the browser needs the app shell,
    which then renders that route. Plain ``StaticFiles`` returns 404 and the
    deep link is broken -- which matters here because permalinks are a feature,
    and a shared link that only works if you navigate to it from the home page
    is not a permalink.

    Only 404s are rewritten. A 403 or a genuine server error is passed through,
    because turning those into a 200 with an HTML body would hide them.
    """

    async def get_response(self, path: str, scope):
        from starlette.exceptions import HTTPException as StarletteHTTPException

        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404 or not _INDEX_HTML.is_file():
                raise
            # An unknown *asset* should stay a 404 rather than being answered
            # with HTML: a missing script returning index.html produces a
            # baffling syntax error in the console instead of a clear 404.
            if path.startswith("assets/") or "." in path.rsplit("/", 1)[-1]:
                raise
            return await super().get_response("index.html", scope)


if _STATIC_DIR.is_dir():
    app.mount("/", _SpaStaticFiles(directory=str(_STATIC_DIR), html=True), name="static")


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
