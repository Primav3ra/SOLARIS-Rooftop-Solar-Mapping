"""
API contract tests that need no Earth Engine access.

Every request is validated by pydantic *before* any ``ee.*`` object is built, so
the whole rejection surface is testable offline. That matters more than it
sounds: these bounds are the primary defence against Earth Engine quota
exhaustion, because a single oversized AOI can burn the quota regardless of any
request-rate limit.

Also pinned here: that the Earth Engine project is server configuration and is
no longer accepted from the request body.
"""

from __future__ import annotations

import warnings
from typing import ClassVar

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)

from fastapi.testclient import TestClient

from solaris.api.app import (
    TilesRequest,
    YieldRequest,
    app,
    gee_project_id,
    resolve_temporal_window,
)
from solaris.core import constants as C

#: Endpoints that take an AOI and therefore share the bounds validators.
AOI_ENDPOINTS = [
    "/api/baseline",
    "/api/yield",
    "/api/tiles",
    "/api/buildings",
    "/api/series",
]


@pytest.fixture(autouse=True)
def _forbid_earth_engine(monkeypatch):
    """
    Hard guarantee that this module never touches the network.

    Earlier versions of these tests asserted "not 422" by POSTing and letting
    the request fall through to Earth Engine, which cost 5-8 s per test in real
    auth round-trips. Anything that would reach EE now fails loudly instead, and
    positive cases assert via direct model construction.
    """

    def _boom(*_args, **_kwargs):
        raise AssertionError(
            "this test reached Earth Engine; offline tests must not make network calls"
        )

    monkeypatch.setattr("solaris.api.app._ensure_ee", _boom)


@pytest.fixture(scope="module")
def client():
    return TestClient(app, raise_server_exceptions=False)


def _square(lat: float, lon: float, half: float):
    return [
        [lon - half, lat - half],
        [lon + half, lat - half],
        [lon + half, lat + half],
        [lon - half, lat + half],
        [lon - half, lat - half],
    ]


class TestMetaEndpoints:
    def test_health(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}

    def test_presets_shape(self, client):
        r = client.get("/api/presets")
        assert r.status_code == 200
        baseline = r.json()["baseline"]
        assert set(baseline["modes"]) == {"yearly", "quarterly", "monthly", "daily"}
        bounds = baseline["year_bounds"]
        assert bounds["min"] == 2000
        assert bounds["default"] == bounds["max"]

    def test_presets_advertises_the_monthly_mode(self, client):
        """`monthly` is fully implemented and was missing from the docs."""
        assert "monthly" in client.get("/api/presets").json()["baseline"]["modes"]

    def test_static_dashboard_is_served(self, client):
        assert client.get("/index.html").status_code == 200

    def test_openapi_documents_every_endpoint(self, client):
        paths = client.get("/openapi.json").json()["paths"]
        for endpoint in [*AOI_ENDPOINTS, "/api/health", "/api/presets"]:
            assert endpoint in paths, f"{endpoint} missing from the OpenAPI schema"


class TestAoiBoundsRejectedBeforeEarthEngine:
    """
    Each case must return 422 from pydantic. A 500 here would mean the request
    reached Earth Engine, i.e. the guard leaked.
    """

    @pytest.mark.parametrize("endpoint", AOI_ENDPOINTS)
    def test_missing_aoi(self, client, endpoint):
        assert client.post(endpoint, json={}).status_code == 422

    @pytest.mark.parametrize("endpoint", AOI_ENDPOINTS)
    def test_lat_without_lon(self, client, endpoint):
        assert client.post(endpoint, json={"lat": 28.6}).status_code == 422

    @pytest.mark.parametrize("endpoint", AOI_ENDPOINTS)
    def test_oversized_half_size(self, client, endpoint):
        """Without this bound one request could ask EE for a whole continent."""
        body = {"lat": 28.6, "lon": 77.2, "half_size_deg": 10.0}
        assert client.post(endpoint, json=body).status_code == 422

    @pytest.mark.parametrize("endpoint", AOI_ENDPOINTS)
    def test_oversized_polygon(self, client, endpoint):
        body = {"coordinates": _square(28.6, 77.2, 5.0)}
        assert client.post(endpoint, json=body).status_code == 422

    @pytest.mark.parametrize("half", [0.0015, 0.01, C.MAX_HALF_SIZE_DEG])
    def test_sizes_at_or_below_the_cap_validate(self, half):
        """At or below the cap the body must validate."""
        YieldRequest(lat=28.6, lon=77.2, half_size_deg=half)

    def test_just_above_the_cap_is_rejected(self, client):
        body = {"lat": 28.6, "lon": 77.2, "half_size_deg": C.MAX_HALF_SIZE_DEG * 1.01}
        assert client.post("/api/yield", json=body).status_code == 422

    def test_too_many_vertices(self, client):
        ring = [[77.2 + i * 1e-5, 28.6] for i in range(C.MAX_AOI_VERTICES + 5)]
        assert client.post("/api/yield", json={"coordinates": ring}).status_code == 422

    def test_degenerate_ring(self, client):
        body = {"coordinates": [[77.2, 28.6], [77.21, 28.6], [77.2, 28.61]]}
        assert client.post("/api/yield", json=body).status_code == 422

    @pytest.mark.parametrize("bad", [{"lat": 999.0, "lon": 77.2}, {"lat": 28.6, "lon": 999.0}])
    def test_out_of_range_coordinates(self, client, bad):
        assert client.post("/api/yield", json=bad).status_code == 422

    def test_out_of_range_polygon_vertex(self, client):
        body = {"coordinates": [[77.2, 28.6], [400.0, 28.6], [77.2, 28.61], [77.2, 28.6]]}
        assert client.post("/api/yield", json=body).status_code == 422


class TestParameterBounds:
    BASE: ClassVar[dict] = {"lat": 28.6, "lon": 77.2}

    @pytest.mark.parametrize(
        "field,value",
        [
            ("roof_year", C.OPEN_BUILDINGS_MIN_YEAR - 1),
            ("roof_year", C.OPEN_BUILDINGS_MAX_YEAR + 1),
            ("presence_threshold", -0.1),
            ("presence_threshold", 1.5),
            ("min_height_m", -5.0),
            ("quarter", 0),
            ("quarter", 7),
            ("month", 0),
            ("month", 13),
            ("panel_efficiency", 0.0),
            ("panel_efficiency", 0.95),
            ("performance_ratio", 1.5),
            ("packing_factor", 0.0),
            ("packing_factor", 1.5),
            ("building_confidence", 1.5),
        ],
    )
    def test_out_of_range_parameter_rejected(self, client, field, value):
        r = client.post("/api/yield", json={**self.BASE, field: value})
        assert r.status_code == 422, f"{field}={value} should have been rejected"
        assert any(field in str(e.get("loc", "")) for e in r.json()["detail"])

    @pytest.mark.parametrize(
        "field,value",
        [
            ("roof_year", C.OPEN_BUILDINGS_MAX_YEAR),
            ("presence_threshold", 0.0),
            ("presence_threshold", 1.0),
            ("panel_efficiency", C.PANEL_EFFICIENCY),
            ("performance_ratio", C.PERFORMANCE_RATIO),
            ("packing_factor", C.PACKING_FACTOR),
        ],
    )
    def test_in_range_parameter_accepted(self, field, value):
        YieldRequest(**{**self.BASE, field: value})

    def test_buildings_limit_bounds(self, client):
        assert client.post("/api/buildings", json={**self.BASE, "limit": 0}).status_code == 422
        over = {**self.BASE, "limit": C.MAX_BUILDINGS + 1}
        assert client.post("/api/buildings", json=over).status_code == 422

    def test_tiles_layer_is_an_enum(self, client):
        bad = client.post("/api/tiles", json={**self.BASE, "layer": "not_a_layer"})
        assert bad.status_code == 422

    @pytest.mark.parametrize(
        "layer",
        [
            "roof_mask",
            "shadow_frequency",
            "sky_view_factor",
            "net_irradiance",
            "combined_derate",
            "temperature_delta",
        ],
    )
    def test_every_documented_tile_layer_validates(self, layer):
        assert TilesRequest(**{**self.BASE, "layer": layer}).layer == layer


class TestTemporalValidationIsACallerError:
    """
    Window problems are caller errors, not server faults.

    Asserted against the resolver rather than over HTTP because the handler
    calls _ensure_ee() before resolving the window, so an HTTP-level assertion
    here would require a live Earth Engine session. The handler wraps these in
    HTTPException(400); see test_temporal_windows.py for full window coverage.
    """

    def test_unknown_mode(self):
        with pytest.raises(ValueError, match="baseline_mode must be"):
            resolve_temporal_window("hourly", 2023, None, None, None, None)

    def test_multi_day_daily_window(self):
        with pytest.raises(ValueError, match="one calendar day"):
            resolve_temporal_window("daily", None, None, None, "2023-06-01", "2023-06-30")

    def test_daily_without_dates(self):
        with pytest.raises(ValueError, match="daily mode requires"):
            resolve_temporal_window("daily", None, None, None, None, None)

    def test_future_year(self):
        from datetime import date

        with pytest.raises(ValueError, match="year must be between"):
            resolve_temporal_window("yearly", date.today().year + 5, None, None, None, None)


class TestProjectIdIsServerConfiguration:
    def test_project_id_comes_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("GEE_PROJECT_ID", "some-configured-project")
        assert gee_project_id() == "some-configured-project"

    def test_project_id_has_a_default(self, monkeypatch):
        monkeypatch.delenv("GEE_PROJECT_ID", raising=False)
        assert gee_project_id()

    @pytest.mark.parametrize("endpoint", AOI_ENDPOINTS)
    def test_request_models_do_not_expose_project_id(self, endpoint, client):
        """
        A caller could previously choose which GCP project this server
        initialises Earth Engine against -- an enumeration oracle and a way to
        attribute someone else's quota and billing.
        """
        schema = client.get("/openapi.json").json()
        for name, model in schema["components"]["schemas"].items():
            if name.endswith("Request"):
                assert "project_id" not in model.get("properties", {}), (
                    f"{name} still accepts project_id"
                )

    def test_stale_client_sending_project_id_is_ignored_not_rejected(self):
        """Soft cutover: an old client must not start getting 422s."""
        req = YieldRequest(lat=28.6, lon=77.2, project_id="someone-elses-project")
        assert not hasattr(req, "project_id")
