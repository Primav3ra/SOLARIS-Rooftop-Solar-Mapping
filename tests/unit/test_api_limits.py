"""
End-to-end tests for caching, rate limiting and the Earth Engine gate.

These exist because the rest of the suite *disables* rate limiting — the
limiter is process-global and every TestClient request shares one identity, so
leaving it on made unrelated modules fail with 429s partway through. Disabling
it there means the only place it gets exercised is here, deliberately.

The same argument applies to the cache: it is reset between tests everywhere
else, so this is where hit/miss behaviour is actually asserted.
"""

from __future__ import annotations

import warnings

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)

from tests.fakes import world
from tests.fakes.install import install_fake_ee

from solaris.core import cache as cache_mod
from solaris.core import limits as limits_mod
from solaris.core.config import get_settings


def _client():
    from fastapi.testclient import TestClient

    import solaris.api.app as app_mod

    return TestClient(app_mod.app, raise_server_exceptions=False)


@pytest.fixture
def app_client(monkeypatch):
    install_fake_ee(monkeypatch)
    world.register_world()
    return _client()


def _request(**overrides):
    return {**world.AOI_REQUEST, **overrides}


class TestCaching:
    def test_first_call_misses_and_second_hits(self, app_client):
        first = app_client.post("/api/yield", json=_request())
        second = app_client.post("/api/yield", json=_request())
        assert first.headers["X-Cache"] == "MISS"
        assert second.headers["X-Cache"] == "HIT"
        assert first.json() == second.json()

    def test_the_cache_key_is_exposed(self, app_client):
        response = app_client.post("/api/yield", json=_request())
        assert response.headers["X-Cache-Key"].startswith("a")

    def test_equivalent_temporal_modes_share_an_entry(self, app_client):
        """
        `{mode: yearly, year: 2023}` and the equivalent explicit date range are
        the same computation, so the second must hit.
        """
        app_client.post("/api/yield", json=_request(baseline_mode="yearly", year=2023))
        second = app_client.post(
            "/api/yield",
            json=_request(
                baseline_mode="daily",
                start_date="2023-01-01",
                end_date_exclusive="2023-01-02",
            ),
        )
        # Different window, so this one must not collide.
        assert second.headers["X-Cache"] == "MISS"

    def test_a_nudged_coordinate_still_hits(self, app_client):
        """
        Coordinates are quantised to ~11 m. Without that, AOIs from map clicks
        are unique per click and the hit rate is approximately zero.
        """
        app_client.post("/api/yield", json=_request())
        nudged = app_client.post("/api/yield", json=_request(lat=world.AOI_REQUEST["lat"] + 1e-7))
        assert nudged.headers["X-Cache"] == "HIT"

    def test_a_different_location_misses(self, app_client):
        app_client.post("/api/yield", json=_request())
        elsewhere = app_client.post("/api/yield", json=_request(lat=19.0760, lon=72.8777))
        assert elsewhere.headers["X-Cache"] == "MISS"

    def test_changed_pv_parameters_miss(self, app_client):
        app_client.post("/api/yield", json=_request())
        changed = app_client.post("/api/yield", json=_request(performance_ratio=0.75))
        assert changed.headers["X-Cache"] == "MISS"

    def test_an_algorithm_version_bump_invalidates(self, app_client, monkeypatch):
        """
        A deploy that changes the physics must invalidate stale entries without
        anyone remembering to flush a cache.
        """
        from solaris.core import constants as C

        app_client.post("/api/yield", json=_request())
        monkeypatch.setattr(C, "ALGO_VERSION", "999")
        after = app_client.post("/api/yield", json=_request())
        assert after.headers["X-Cache"] == "MISS"

    def test_a_cache_hit_costs_no_earth_engine_calls(self, app_client, monkeypatch):
        """
        The point of the cache. Also what makes a guest allowance workable:
        a cached result can be served without spending one.
        """
        app_client.post("/api/yield", json=_request())

        from solaris.api import deps

        def _fail(*_a, **_kw):
            raise AssertionError("a cache hit must not reach Earth Engine")

        monkeypatch.setattr(deps, "ensure_ee", _fail)
        hit = app_client.post("/api/yield", json=_request())
        assert hit.headers["X-Cache"] == "HIT"
        assert hit.status_code == 200


class TestRateLimiting:
    def test_requests_past_the_limit_get_429(self, monkeypatch):
        monkeypatch.setenv("SOLARIS_RATE_LIMIT_PER_MINUTE", "3")
        get_settings.cache_clear()
        limits_mod.reset_limits()
        cache_mod.reset_cache()
        install_fake_ee(monkeypatch)
        world.register_world()
        client = _client()

        statuses = [
            client.post("/api/yield", json=_request(lat=28.6 + i * 0.01)).status_code
            for i in range(5)
        ]
        assert 429 in statuses, statuses

    def test_a_429_explains_when_to_retry(self, monkeypatch):
        monkeypatch.setenv("SOLARIS_RATE_LIMIT_PER_MINUTE", "1")
        get_settings.cache_clear()
        limits_mod.reset_limits()
        install_fake_ee(monkeypatch)
        world.register_world()
        client = _client()

        client.post("/api/yield", json=_request())
        blocked = client.post("/api/yield", json=_request(lat=19.0))
        assert blocked.status_code == 429
        assert "Retry-After" in blocked.headers
        body = blocked.json()
        assert body["error"]["code"] == "rate_limited"
        assert body["request_id"]

    def test_health_checks_are_exempt(self, monkeypatch):
        """A load balancer polling /api/health must never be rate limited."""
        monkeypatch.setenv("SOLARIS_RATE_LIMIT_PER_MINUTE", "1")
        get_settings.cache_clear()
        limits_mod.reset_limits()
        client = _client()
        for _ in range(10):
            assert client.get("/api/health").status_code == 200


class TestBudget:
    """
    The budget is denominated in Earth Engine **compute cost**, not round-trips
    and not HTTP requests.

    That is what makes it protect the quota. Earth Engine bills EECU-seconds;
    every ``/api/yield`` makes 12 round-trips regardless of window; and a yearly
    window costs more than ten times a single-day one. A call-counting budget
    therefore charged the cheapest and dearest queries identically, while the
    monthly EECU ceiling is a system limit with no console-side setting -- so
    this counter is the only guard available.
    """

    def _prepare(self, monkeypatch, budget):
        monkeypatch.setenv("SOLARIS_DAILY_EE_COST_BUDGET", str(budget))
        get_settings.cache_clear()
        limits_mod.reset_limits()
        cache_mod.reset_cache()
        install_fake_ee(monkeypatch)
        world.register_world()
        return _client()

    def test_an_exhausted_budget_returns_503(self, monkeypatch):
        # A yearly window costs 12 units, so it alone exceeds a budget of 5.
        client = self._prepare(monkeypatch, 5)
        response = client.post("/api/yield", json=_request(baseline_mode="yearly", year=2023))
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "budget_exhausted"
        assert response.headers.get("Retry-After")

    def test_a_generous_budget_allows_the_request(self, monkeypatch):
        client = self._prepare(monkeypatch, 10_000)
        assert client.post("/api/yield", json=_request()).status_code == 200

    def test_a_cheap_window_passes_a_budget_a_dear_one_would_not(self, monkeypatch):
        """
        The behaviour the old unit could not express.

        A budget of 5 units admits a single-day query (0.1 units) and refuses a
        yearly one (12), although both make the same 12 round-trips.
        """
        client = self._prepare(monkeypatch, 5)
        daily = client.post(
            "/api/yield",
            json=_request(
                baseline_mode="daily",
                start_date="2023-06-15",
                end_date_exclusive="2023-06-16",
            ),
        )
        assert daily.status_code == 200, daily.text

        yearly = client.post("/api/yield", json=_request(baseline_mode="yearly", year=2023))
        assert yearly.status_code == 503

    def test_the_cost_of_a_window_rises_with_its_length(self):
        """
        Cost tracks the number of daily images the window reduces over, which
        is what the irradiance, precipitation and shadow reductions scale with.
        """
        from solaris.core import constants as C

        costs = [C.ee_cost_units(m) for m in ("daily", "monthly", "quarterly", "yearly")]
        assert costs == sorted(costs)
        assert costs[-1] >= 10 * costs[1], "a year should cost far more than a month"

    def test_an_unknown_mode_is_charged_the_dearest(self):
        """Fail expensive: an unrecognised window must not be charged as cheap."""
        from solaris.core import constants as C

        assert C.ee_cost_units("something-new") == max(C.EE_COST_UNITS.values())

    def test_the_monthly_ceiling_is_recorded(self):
        """
        540,000 EECU-seconds, a system limit with no adjustable setting. The
        default daily budget is sized against it, so the number belongs in the
        code rather than in a runbook.
        """
        from solaris.core import constants as C

        assert C.NONCOMMERCIAL_EECU_SECONDS_PER_MONTH == 540_000
        settings = get_settings()
        # A month at the default budget must fit inside the ceiling, using the
        # per-unit cost recorded in constants rather than a number invented
        # here. This assertion caught the default being set from the cost of a
        # yearly query rather than of a unit -- a factor-of-twelve error that
        # put the budget at more than twice the ceiling.
        monthly_spend = settings.daily_ee_cost_budget * 30 * C.EECU_SECONDS_PER_COST_UNIT
        assert monthly_spend <= C.NONCOMMERCIAL_EECU_SECONDS_PER_MONTH, (
            f"default budget implies {monthly_spend:,.0f} EECU-s/month against a "
            f"{C.NONCOMMERCIAL_EECU_SECONDS_PER_MONTH:,} ceiling"
        )


class TestErrorEnvelope:
    def test_internal_errors_do_not_leak_earth_engine_detail(self, monkeypatch):
        """
        Handlers used to end in `HTTPException(500, detail=str(e))`, which put
        raw Earth Engine text into the body -- the deployed instance answered
        with "Caller does not have required permission to use project
        pv-mapping-india", leaking the project id to anyone.
        """
        install_fake_ee(monkeypatch)
        world.register_world()

        from solaris.api import deps

        def _boom(*_a, **_kw):
            raise RuntimeError(
                "Caller does not have required permission to use project secret-project-id"
            )

        monkeypatch.setattr(deps, "ensure_ee", _boom)
        response = _client().post("/api/yield", json=_request())
        assert response.status_code == 500
        body = response.text
        assert "secret-project-id" not in body
        assert "request_id" in body

    def test_every_response_carries_a_request_id(self, app_client):
        """So a user can quote one and it can be found in the logs."""
        response = app_client.get("/api/health")
        assert response.headers["X-Request-ID"]

    def test_security_headers_are_present(self, app_client):
        response = app_client.get("/api/health")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"


class TestEarthEngineCallAccounting:
    def test_calls_are_counted_per_request(self, app_client):
        """
        The ee_calls counter is the quota currency, and the most useful number
        in the request log: it turns "why is this slow or expensive" into one
        query.
        """
        from solaris.api import middleware

        app_client.post("/api/yield", json=_request())
        # The counter is per-request via a contextvar, so it resets to the
        # default outside a request.
        assert middleware.ee_calls_var.get() == 0

    def test_the_gate_bounds_concurrency(self, monkeypatch):
        monkeypatch.setenv("SOLARIS_MAX_CONCURRENT_EE_CALLS", "1")
        get_settings.cache_clear()
        limits_mod.reset_limits()
        gate = limits_mod.get_gate()
        assert gate.max_concurrent == 1
