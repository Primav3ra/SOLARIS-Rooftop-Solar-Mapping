"""
Shared request-handling dependencies: the Earth Engine session, AOI
construction, rooftop layers and target-building selection.

Routers import this module (``from solaris.api import deps``) and call through
it rather than importing the functions by name, so that patching
``solaris.api.deps.ensure_ee`` in a test takes effect at every call site.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import date
from typing import Any

import ee
from fastapi import HTTPException

from solaris.api.windows import square_aoi_from_point
from solaris.gee.datasets import get_open_buildings_vector
from solaris.gee.layers import build_roof_layers as _layers_build_roof_layers
from solaris.gee.solar_geometry import (
    solar_positions_monthly,
    solar_positions_quarterly,
    solar_positions_single_day,
    solar_positions_yearly,
)

_EE_INIT_PROJECT: str | None = None
_EE_INIT_LOCK = threading.Lock()


def _centroid_lon_lat(centroid: ee.Geometry) -> tuple[float, float]:
    g = centroid.getInfo()
    coords = g.get("coordinates")
    if not coords or len(coords) < 2:
        raise RuntimeError("Could not read centroid coordinates")
    return float(coords[0]), float(coords[1])


def _solar_positions_for_window(
    lat_deg: float,
    lon_deg: float,
    win: dict[str, Any],
) -> list[tuple[float, float, float]]:
    mode = win["mode"]
    if mode == "yearly":
        y = int(win["calendar_year"])
        pos = solar_positions_yearly(lat_deg, lon_deg, y)
    elif mode == "quarterly":
        pos = solar_positions_quarterly(
            lat_deg, lon_deg, int(win["calendar_year"]), int(win["quarter"])
        )
    elif mode == "monthly":
        pos = solar_positions_monthly(
            lat_deg, lon_deg, int(win["calendar_year"]), int(win["month"])
        )
    else:
        d0 = date.fromisoformat(win["start_date"])
        pos = solar_positions_single_day(lat_deg, lon_deg, d0)
    # No thinning: halving the list would drop half the insolation weight
    # without renormalising, silently scaling shadow_frequency. See
    # windows._cap_positions.
    return pos


# ---------------------------------------------------------------------------
# Request bounds -- these are the primary defence against Earth Engine quota
# exhaustion. Without them a single well-formed request can ask EE for a region
# the size of a continent, which no rate limiter can protect against.
# ---------------------------------------------------------------------------

MAX_AOI_KM2: float = 30.0  # keeps AOIs inside the 4 m reduce-scale tier
MAX_HALF_SIZE_DEG: float = 0.025  # ~2.8 km half-side => ~30 km2 at Delhi latitude
MAX_AOI_VERTICES: int = 100
OPEN_BUILDINGS_MIN_YEAR: int = 2016  # Open Buildings 2.5D Temporal v1 vintages
OPEN_BUILDINGS_MAX_YEAR: int = 2023

_DEG_KM = 111.32  # km per degree of latitude


def _aoi_from_req(req: Any) -> tuple[list[list[float]], ee.Geometry]:
    if getattr(req, "coordinates", None) is None:
        if getattr(req, "lat", None) is None or getattr(req, "lon", None) is None:
            raise HTTPException(status_code=400, detail="Provide either coordinates or lat/lon.")
        coords = square_aoi_from_point(float(req.lat), float(req.lon), float(req.half_size_deg))
    else:
        coords = req.coordinates
    return coords, ee.Geometry.Polygon(coords)


_EE_INIT_PROJECT: str | None = None
_EE_INIT_LOCK = threading.Lock()


def gee_project_id() -> str:
    """
    The Earth Engine project, from server configuration only.

    Deliberately NOT accepted from the request body: a client-supplied project id
    would let any caller choose which GCP project this server initialises Earth
    Engine against, which is both an enumeration oracle and a way to attribute
    someone else's quota and billing.
    """
    return os.environ.get("GEE_PROJECT_ID", "pv-mapping-india")


class EarthEngineUnavailableError(RuntimeError):
    """
    Earth Engine could not be initialised.

    Distinct from a generic failure on purpose. A 500 tells the caller "our
    bug"; this is almost always configuration -- missing credentials, a project
    without the API enabled, a project that is not Earth Engine *registered*,
    or a service account without ``serviceusage.services.use``. Reporting it as
    an internal error sends someone hunting through application code for a
    problem that is entirely in their Cloud project, which is exactly what
    happened on the first deploy of this service.

    Carries a curated hint rather than the raw exception text. The raw text
    names the Cloud project, and this reply can reach an unauthenticated
    caller.
    """

    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.hint = hint


#: Fragments of Earth Engine error text mapped to something actionable.
#:
#: Matching on message text is fragile, and the fallback below is the honest
#: acknowledgement of that: an unrecognised message still produces a 503 with
#: generic guidance rather than being mistaken for a healthy state.
_EE_ERROR_HINTS = (
    (
        "does not have required permission",
        "The credentials in use cannot access this Cloud project. Grant the "
        "account roles/serviceusage.serviceUsageConsumer on it, and check that "
        "GEE_PROJECT_ID names a project you actually have access to.",
    ),
    (
        "not registered",
        "The Cloud project is not registered with Earth Engine. Enabling the "
        "API is not sufficient -- register it (noncommercial or commercial) at "
        "https://code.earthengine.google.com/register.",
    ),
    (
        "has not been used in project",
        "The Earth Engine API is not enabled on this Cloud project. Enable it, "
        "then allow a few minutes for the change to propagate.",
    ),
    (
        "Please authorize",
        "No usable credentials were found. Run `earthengine authenticate` for "
        "local development, or attach a service account in production.",
    ),
    (
        "credentials",
        "No usable credentials were found. Run `earthengine authenticate` for "
        "local development, or attach a service account in production.",
    ),
)


def earth_engine_hint(exc: Exception) -> str:
    """An actionable hint for an Earth Engine failure, or a generic fallback."""
    text = str(exc)
    for fragment, hint in _EE_ERROR_HINTS:
        if fragment in text:
            return hint
    return (
        "Earth Engine could not be initialised. Check that GEE_PROJECT_ID names "
        "a project with the Earth Engine API enabled and registered, and that "
        "the running identity has access to it. See docs/deployment.md."
    )


def _ensure_ee(project_id: str | None = None) -> None:
    """
    Spin EE up once per process so we're not paying for ee.Initialize on every
    request. The project always comes from server config; the argument exists
    only for tests.

    Credential hunt, in order: GEE_SA_JSON (whole key pasted into an env var --
    easiest on Render/Fly), GEE_SA_KEY_FILE (a key file path), Application
    Default Credentials (keyless -- covers Cloud Run, GCE and Workload Identity
    Federation), and finally local `earthengine authenticate` creds for dev.
    """
    global _EE_INIT_PROJECT
    project_id = project_id or gee_project_id()
    if project_id == _EE_INIT_PROJECT:
        return

    # Endpoints are sync `def`, so FastAPI runs them on a threadpool; without this
    # lock two concurrent cold requests can both call ee.Initialize.
    with _EE_INIT_LOCK:
        if project_id == _EE_INIT_PROJECT:
            return
        try:
            _initialize_ee(project_id)
        except Exception as exc:
            # Deliberately not memoised: a transient failure must not pin the
            # process into a broken state for its lifetime, and a
            # configuration fix should take effect on the next request rather
            # than needing a restart.
            raise EarthEngineUnavailableError(
                "Earth Engine is not available on this server.",
                hint=earth_engine_hint(exc),
            ) from exc
        _EE_INIT_PROJECT = project_id


def _initialize_ee(project_id: str) -> None:
    sa_email = os.environ.get("GEE_SERVICE_ACCOUNT")
    sa_json = os.environ.get("GEE_SA_JSON")
    sa_key_file = os.environ.get("GEE_SA_KEY_FILE")

    if sa_json:
        email = sa_email or json.loads(sa_json).get("client_email")
        ee.Initialize(ee.ServiceAccountCredentials(email, key_data=sa_json), project=project_id)
    elif sa_key_file:
        ee.Initialize(
            ee.ServiceAccountCredentials(sa_email, key_file=sa_key_file), project=project_id
        )
    else:
        # Application Default Credentials first: this covers Cloud Run's attached
        # service account (keyless, what prod uses), GCE, Cloud Build and Workload
        # Identity Federation. The previous K_SERVICE sniff only matched Cloud Run.
        # Falls through to local `earthengine authenticate` creds when ADC is absent.
        try:
            import google.auth

            adc, _ = google.auth.default(
                scopes=[
                    "https://www.googleapis.com/auth/earthengine",
                    "https://www.googleapis.com/auth/cloud-platform",
                ]
            )
            ee.Initialize(adc, project=project_id)
            return
        except Exception:
            pass
        ee.Initialize(project=project_id)


def _build_roof_layers(
    aoi: ee.Geometry,
    roof_year: int | None,
    presence_threshold: float,
    min_height_m: float,
) -> tuple[ee.Image, ee.Image, ee.Image]:
    """
    Thin adapter over solaris.gee.layers.build_roof_layers, kept so the existing
    call sites keep their tuple-unpacking shape.
    """
    return tuple(
        _layers_build_roof_layers(
            aoi,
            roof_year=roof_year,
            presence_threshold=presence_threshold,
            min_height_m=min_height_m,
        )
    )


def _select_target_building(
    aoi: ee.Geometry,
    coords: list[list[float]],
    centroid: ee.Geometry,
    confidence: float,
) -> tuple[ee.Geometry, dict[str, Any], dict[str, Any], str, str | None]:
    """
    Grab the building footprint under the click. Returns
    (building_geom, building_props, building_geojson_feature, source, warning).
    """
    tb = (
        get_open_buildings_vector(aoi, confidence_threshold=confidence)
        .filterBounds(centroid)
        .first()
        .getInfo()
    )
    source = "vector_centroid_point"
    warning: str | None = None

    # a bare point misses when the click lands on an edge / low-confidence footprint,
    # so nudge out 30 m, and if that still finds nothing just use the whole AOI
    if tb is None:
        try:
            tb = (
                get_open_buildings_vector(aoi, confidence_threshold=confidence)
                .filterBounds(centroid.buffer(30))
                .first()
                .getInfo()
            )
            if tb is not None:
                source = "vector_centroid_buffer30m"
        except Exception:
            tb = None

    if tb is None:
        source = "aoi_fallback"
        warning = "No Open Buildings polygon found at the selected point; using the AOI roof mask for calculations."
        building_geom = aoi
        building_props = {"confidence": confidence, "area_in_meters": None}
        building_geojson_feature = {
            "type": "Feature",
            "geometry": {"type": "Polygon", "coordinates": [coords]},
            "properties": {},
        }
    else:
        building_geom = ee.Feature(tb).geometry()
        building_props = tb.get("properties", {})
        building_geojson_feature = tb

    return building_geom, building_props, building_geojson_feature, source, warning


def _ee_tile_template(image: ee.Image, vis: dict[str, Any]) -> str:
    """
    Return Map ID tile template URL for an EE image.
    This yields a URL like: https://earthengine.googleapis.com/v1alpha/projects/.../maps/{mapid}/tiles/{z}/{x}/{y}
    """
    m = image.getMapId(vis)
    return m["tile_fetcher"].url_format


def ee_gate(n_calls: int = 1):
    """
    A bounded Earth Engine slot, counted against the daily budget.

    Wrap any block that makes ``getInfo()`` calls::

        with deps.ee_gate(n_calls=6):
            ...

    Two things this does that a request timeout cannot. It bounds concurrency:
    every endpoint is a sync ``def``, so FastAPI runs it on a threadpool of 40,
    and without a cap one instance can hold 40 blocking ``getInfo()`` calls
    open at once. And it counts round-trips against a daily budget denominated
    in the resource actually being consumed, rather than in HTTP requests.
    """
    from solaris.api.middleware import record_ee_calls
    from solaris.core.limits import get_gate

    record_ee_calls(n_calls)
    return get_gate().slot(n_calls=n_calls)


def charge_computation(request: Any) -> None:
    """
    Spend one guest computation, or raise if the allowance is gone.

    Called only **after** a cache lookup has missed. That ordering is the whole
    design: a cached answer costs nothing to serve, so charging for it would
    make repeat visits feel punitive and would spend an allowance on work that
    never happened. It also makes the cache a user-facing feature rather than
    an invisible optimisation.

    """
    from solaris.api.auth import get_guest_allowance

    identity = getattr(getattr(request, "state", None), "identity", None)
    if identity is None:
        return

    allowance = get_guest_allowance()
    allowance.check(identity.allowance_key)
    allowance.spend(identity.allowance_key)


def allowance_headers(request: Any) -> dict[str, str]:
    """
    The remaining guest allowance, as response headers.

    Headers rather than a body field, deliberately. The allowance is per
    caller and changes with every computation, while the body is what gets
    **cached** -- so putting the count in the payload would freeze one
    caller's remaining count into every later reader's response. Headers are
    assembled per response and sit outside the cached value.
    """
    state = allowance_state(request)
    headers: dict[str, str] = {}
    if "remaining" in state:
        headers["X-Guest-Remaining"] = str(state["remaining"])
        headers["X-Guest-Allowance"] = str(state["allowance"])
    return headers


def allowance_state(request: Any) -> dict[str, Any]:
    """
    What the caller has left, for the response envelope.

    Returned on every computation so the frontend can show the remaining count
    without a second round-trip, and so a guest is never surprised by the
    refusal that arrives when the allowance runs out.
    """
    from solaris.api.auth import get_guest_allowance

    identity = getattr(getattr(request, "state", None), "identity", None)
    if identity is None:
        return {"allowance": None}

    allowance = get_guest_allowance()
    return {
        "allowance": allowance.allowance,
        "used": allowance.used(identity.allowance_key),
        "remaining": allowance.remaining(identity.allowance_key),
    }


# Public aliases. The underscore names remain the implementations; these are
# what routers and tests should reference, so that monkeypatching this module
# affects every caller.
ensure_ee = _ensure_ee
aoi_from_req = _aoi_from_req
centroid_lon_lat = _centroid_lon_lat
solar_positions_for_window = _solar_positions_for_window
build_roof_layers = _build_roof_layers
select_target_building = _select_target_building
ee_tile_template = _ee_tile_template
