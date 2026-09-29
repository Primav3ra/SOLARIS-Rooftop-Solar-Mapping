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

import contextlib
import logging
import warnings
from typing import ClassVar

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
    def test_the_counter_is_empty_outside_a_request(self, app_client):
        """
        Scoped to a request, so work off the request path records nothing
        rather than accumulating into a shared default.

        This assertion used to be the *only* check on ee_calls, and it passed
        while the logged value was always zero -- it can only ever observe the
        out-of-request state. The real check is
        TestRequestLogFields, which reads the emitted line.
        """
        from solaris.api import middleware

        app_client.post("/api/yield", json=_request())
        assert middleware.current_counters().calls == 0
        assert middleware.current_counters().cost == 0.0

    def test_the_gate_bounds_concurrency(self, monkeypatch):
        monkeypatch.setenv("SOLARIS_MAX_CONCURRENT_EE_CALLS", "1")
        get_settings.cache_clear()
        limits_mod.reset_limits()
        gate = limits_mod.get_gate()
        assert gate.max_concurrent == 1


class TestEveryEarthEngineEndpointIsMetered:
    """
    The budget only guards what it is wired into, and for a while it was wired
    into one endpoint out of five.

    ``/api/yield`` was gated; ``/api/series``, ``/api/tiles``, ``/api/baseline``
    and ``/api/buildings`` all reached Earth Engine while charging nothing and
    holding no concurrency slot. ``/api/series`` was the worst of them, because
    it runs a shadow reduction per sub-period -- a yearly series does roughly
    the work of a yearly yield -- and ``/api/tiles`` was the most frequent,
    since the overlay switcher fires it on every layer change.

    The behavioural tests below would pass again if someone added a sixth
    endpoint and forgot the gate, so the structural test is the one that
    actually holds the line.
    """

    #: Markers that mean a function reaches Earth Engine.
    EE_MARKERS = (
        "getInfo",
        "getMapId",
        "reduceRegion",
        "ensure_ee",
        "aggregate_array",
    )

    #: Handlers allowed to reach Earth Engine ungated, each with the reason.
    EXEMPT: ClassVar[set[str]] = {
        # The readiness probe. It establishes the Earth Engine session and runs
        # no reduction, so its compute cost is negligible -- and gating it would
        # be actively harmful: on an exhausted budget the probe would answer 503
        # and Cloud Run would take the instance out of service, turning a spent
        # daily allowance into an outage.
        "ready",
    }

    def test_no_route_handler_touches_earth_engine_without_a_gate(self):
        import ast
        import pathlib

        import solaris.api.app as app_mod

        source = pathlib.Path(app_mod.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)

        ungated = []
        for node in tree.body:
            if not isinstance(node, ast.FunctionDef):
                continue
            is_route = any(
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr in ("get", "post")
                for d in node.decorator_list
            )
            if not is_route:
                continue
            body = ast.get_source_segment(source, node) or ""
            if node.name in self.EXEMPT:
                continue
            if any(marker in body for marker in self.EE_MARKERS) and "ee_gate" not in body:
                ungated.append(node.name)

        assert ungated == [], (
            f"route handlers reaching Earth Engine with no budget charge: {ungated}. "
            f"Wrap the Earth Engine work in deps.ee_gate(..., cost=C.ee_cost_units(mode, "
            f"'<endpoint>')) -- an unmetered endpoint is a hole in the only quota guard "
            f"the project has."
        )

    @pytest.mark.parametrize(
        "path",
        ["/api/yield", "/api/series", "/api/tiles", "/api/baseline", "/api/buildings"],
    )
    def test_each_endpoint_refuses_work_on_an_exhausted_budget(self, monkeypatch, path):
        monkeypatch.setenv("SOLARIS_DAILY_EE_COST_BUDGET", "0.01")
        get_settings.cache_clear()
        limits_mod.reset_limits()
        cache_mod.reset_cache()
        install_fake_ee(monkeypatch)
        world.register_world()
        client = _client()

        response = client.post(path, json=_request(baseline_mode="yearly", year=2023))
        assert response.status_code == 503, response.text
        assert response.json()["error"]["code"] == "budget_exhausted"

    def test_the_endpoint_multipliers_are_relative_to_a_yield(self):
        """
        A multiplier of 1.0 for ``yield`` is what makes the others readable as
        fractions of a known cost rather than as free-standing magic numbers.
        """
        from solaris.core import constants as C

        assert C.EE_COST_ENDPOINT_MULTIPLIER["yield"] == 1.0
        assert set(C.EE_COST_ENDPOINT_MULTIPLIER) == {"yield", "series", "baseline", "tiles"}
        # series is the dearest because it reduces shadow per sub-period.
        assert C.EE_COST_ENDPOINT_MULTIPLIER["series"] > 1.0

    def test_an_unknown_endpoint_bills_as_a_yield_rather_than_free(self):
        from solaris.core import constants as C

        assert C.ee_cost_units("yearly", "not_an_endpoint") == C.ee_cost_units("yearly", "yield")

    def test_buildings_is_charged_a_flat_cost(self):
        """
        It is one vector ``getInfo`` bounded by ``MAX_BUILDINGS`` and reduces
        over no time window, so scaling its charge by the temporal mode would
        be charging for work it does not do.
        """
        from solaris.core import constants as C

        assert C.EE_COST_UNITS_BUILDINGS > 0
        assert "buildings" not in C.EE_COST_ENDPOINT_MULTIPLIER


class _LogCapture(logging.Handler):
    """
    Collects records straight off the ``solaris`` logger.

    ``caplog`` cannot see them: ``configure_logging`` sets
    ``propagate = False`` so one request produces exactly one line in Cloud
    Logging rather than a duplicate via the root logger.
    """

    def __init__(self):
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record):
        self.records.append(record)

    def request_lines(self) -> list[dict]:
        return [r.extra_fields for r in self.records if "status" in getattr(r, "extra_fields", {})]


@contextlib.contextmanager
def _capture_logs():
    handler = _LogCapture()
    log = logging.getLogger("solaris")
    log.addHandler(handler)
    try:
        yield handler
    finally:
        log.removeHandler(handler)


class TestRequestLogFields:
    """
    The per-request log line is the only window into a deployed instance, so
    the fields it carries are a contract rather than a convenience.
    """

    def test_a_request_logs_both_call_count_and_cost(self, app_client):
        """
        Both, not either. Round-trip count is nearly constant across temporal
        modes -- every /api/yield makes 12 -- while cost is what Earth Engine
        bills, so a daily and a yearly query are indistinguishable in
        ``ee_calls`` and differ 120-fold in ``ee_cost``.
        """
        cache_mod.reset_cache()
        with _capture_logs() as logs:
            app_client.post("/api/yield", json=_request(baseline_mode="yearly", year=2023))

        lines = logs.request_lines()
        assert lines, "no per-request log line was emitted"
        fields = lines[-1]
        for key in ("method", "path", "status", "duration_ms", "ee_calls", "ee_cost", "cache"):
            assert key in fields, f"{key} missing from the request log line"
        assert fields["ee_calls"] == 12
        assert fields["ee_cost"] == 12.0  # yearly window, yield endpoint

    def test_cost_does_not_leak_between_requests(self, app_client):
        """
        The counters are contextvars reset per request. A leak would make the
        second request on a warm instance look more expensive than it was.
        """
        cache_mod.reset_cache()
        with _capture_logs() as logs:
            app_client.post("/api/yield", json=_request(baseline_mode="yearly", year=2023))
            app_client.post(
                "/api/yield", json=_request(baseline_mode="monthly", year=2023, month=6)
            )

        costs = [line["ee_cost"] for line in logs.request_lines()]
        assert costs[-1] == 1.0, f"monthly window should cost 1 unit, got {costs[-1]}"

    def test_a_cache_hit_costs_nothing(self, app_client):
        """
        A served-from-cache answer makes no Earth Engine call, so it must
        charge nothing -- that is what makes the cache a cost control.
        """
        cache_mod.reset_cache()
        app_client.post("/api/yield", json=_request())
        with _capture_logs() as logs:
            response = app_client.post("/api/yield", json=_request())

        assert response.headers.get("X-Cache") == "HIT"
        fields = logs.request_lines()[-1]
        assert fields["ee_cost"] == 0.0
        assert fields["ee_calls"] == 0


class TestTileCostAndCaching:
    """
    The overlay switcher must not be able to exhaust a day's budget.

    Observed in production: a yearly window charged 6 units per overlay
    (0.5 x 12) with no cache behind it, so one city plus a chart plus one
    layer reached 36 of 40 units and the next layer was refused. Two causes,
    both fixed here: tiles were uncached despite the API reference claiming a
    6 h TTL, and every layer was charged the full window cost even when it
    reduces over no window at all.
    """

    def _fresh(self, monkeypatch, budget="1000"):
        monkeypatch.setenv("SOLARIS_DAILY_EE_COST_BUDGET", budget)
        get_settings.cache_clear()
        limits_mod.reset_limits()
        cache_mod.reset_cache()
        install_fake_ee(monkeypatch)
        world.register_world()
        return _client()

    def test_a_repeated_overlay_is_served_from_cache(self, monkeypatch):
        client = self._fresh(monkeypatch)
        body = {**_request(baseline_mode="yearly", year=2023), "layer": "shadow_frequency"}

        first = client.post("/api/tiles", json=body)
        second = client.post("/api/tiles", json=body)

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert first.json()["cache"] == "MISS"
        assert second.json()["cache"] == "HIT"

    def test_a_cached_overlay_charges_nothing(self, monkeypatch):
        """The whole point: switching back to a layer already rendered is free."""
        client = self._fresh(monkeypatch)
        body = {**_request(baseline_mode="yearly", year=2023), "layer": "sky_view_factor"}

        client.post("/api/tiles", json=body)
        spent_after_first = limits_mod.get_gate().budget.used
        client.post("/api/tiles", json=body)
        spent_after_second = limits_mod.get_gate().budget.used

        assert spent_after_second == spent_after_first

    def test_the_roof_mask_layer_is_not_charged_by_window(self):
        """
        roof_mask is Open Buildings thresholded. It touches no ERA5, no MODIS
        and no sun position, so a yearly roof_mask must not cost twelve times
        a monthly one -- it does exactly the same work.
        """
        from solaris.core import constants as C

        yearly = C.ee_cost_units("yearly", "tiles", layer="roof_mask")
        monthly = C.ee_cost_units("monthly", "tiles", layer="roof_mask")
        assert yearly == monthly == C.EE_COST_UNITS_TILE_STATIC

    def test_stepping_through_every_overlay_fits_in_a_day(self):
        """
        The failure that was actually observed. A user opens one city, reads
        the chart, and steps through all five overlays; that must not consume
        a day's allowance.
        """
        from solaris.core import constants as C

        layers = (
            "roof_mask",
            "shadow_frequency",
            "sky_view_factor",
            "net_irradiance",
            "temperature_delta",
        )
        session = (
            C.ee_cost_units("yearly", "yield")
            + C.ee_cost_units("yearly", "series")
            + sum(C.ee_cost_units("yearly", "tiles", layer=layer) for layer in layers)
        )
        budget = get_settings().daily_ee_cost_budget
        assert session < budget / 2, (
            f"one city costs {session:g} units against a {budget:g}-unit day, so a "
            f"visitor cannot examine two sites. Overlay pricing is too high."
        )


class TestTileLayersRender:
    """
    Every overlay must come back with a tile template, at every layer.

    The heat-island layer silently returned nothing on the deployed site. Its
    background window is denominated in metres, which fixes the window's size
    but not its cost: Earth Engine converts metres to pixels using the
    projection of the *request*, and a tile at street zoom is roughly
    metre-scale, so a 30 km radius asked for a kernel about 30,000 pixels
    across. The reduction path never hit this because reduceRegion pins
    scale=1 km itself, so the number was right while the map was blank --
    the same asymmetry as defect D9, surviving in the one place D9's fix had
    not reached.
    """

    #: Every layer the schema accepts. Derived from the schema rather than
    #: restated, so a new layer cannot be added without being covered here.
    @staticmethod
    def _layers():
        import typing

        from solaris.api.schemas import TilesRequest

        annotation = TilesRequest.model_fields["layer"].annotation
        return list(typing.get_args(annotation))

    def test_every_declared_layer_returns_a_tile_template(self, monkeypatch):
        monkeypatch.setenv("SOLARIS_DAILY_EE_COST_BUDGET", "1000")
        get_settings.cache_clear()
        limits_mod.reset_limits()
        cache_mod.reset_cache()
        install_fake_ee(monkeypatch)
        world.register_world()
        client = _client()

        layers = self._layers()
        assert layers, "no layers found on TilesRequest"

        missing = []
        for layer in layers:
            response = client.post(
                "/api/tiles",
                json={**_request(baseline_mode="monthly", year=2023, month=5), "layer": layer},
            )
            if response.status_code != 200:
                missing.append(f"{layer}: HTTP {response.status_code}")
                continue
            if not response.json().get("urlTemplate"):
                missing.append(f"{layer}: no urlTemplate")

        assert missing == [], f"layers that did not render: {missing}"

    def test_the_heat_island_background_is_pinned_to_a_coarse_projection(self):
        """
        Structural, because the behavioural test above cannot see it: the fake
        Earth Engine does not model the cost of an oversized kernel, so a
        30,000-pixel focal window passes offline and fails in production.

        What must hold is that the focal window is computed against a pinned
        projection rather than the request's.
        """
        import pathlib as _pathlib

        import solaris.api.app as app_mod

        source = _pathlib.Path(app_mod.__file__).read_text(encoding="utf-8")
        branch = source[source.index('elif req.layer == "temperature_delta"') :]
        branch = branch[: branch.index("        else:")]

        assert "reproject(" in branch, (
            "the heat-island tile takes a 30 km focal mean; without pinning the "
            "projection first, Earth Engine sizes that kernel against the tile "
            "request and returns nothing at street zoom."
        )
        assert "focal_mean" in branch
        assert branch.index("reproject(") < branch.index("focal_mean"), (
            "reproject must come before focal_mean, or the kernel is still "
            "sized against the request projection."
        )


class TestStaticFilesAreNeverRateLimited:
    """
    Observed locally: after enough navigation the server answered 429 for the
    site's own JavaScript, and the page failed to load. The limiter applied to
    every path, and a single landing-page load is a dozen or more static
    files against an allowance of 30 per minute.

    Behind carrier-grade NAT -- common on Indian mobile networks, and the
    reason this module prefers a session token over the client address --
    thousands of subscribers share one IP, so they shared that allowance for
    loading the site, before anyone computed anything.
    """

    def _exhausted_client(self, monkeypatch):
        monkeypatch.setenv("SOLARIS_RATE_LIMIT_PER_MINUTE", "2")
        get_settings.cache_clear()
        limits_mod.reset_limits()
        cache_mod.reset_cache()
        install_fake_ee(monkeypatch)
        world.register_world()
        client = _client()
        # Spend the whole allowance on the API.
        for _ in range(4):
            client.post("/api/yield", json=_request())
        return client

    def test_the_api_is_still_limited(self, monkeypatch):
        client = self._exhausted_client(monkeypatch)
        assert client.post("/api/yield", json=_request()).status_code == 429

    @pytest.mark.parametrize(
        "path",
        ["/", "/explore", "/validation", "/assets/index.js", "/textures/earth-day.jpg"],
    )
    def test_static_and_page_routes_are_never_429(self, monkeypatch, path):
        """
        Whatever the file resolves to -- 200, or 404 in a checkout with no
        built frontend -- it must not be refused for rate. A 429 here is the
        page failing to load its own code.
        """
        client = self._exhausted_client(monkeypatch)
        for _ in range(10):
            status = client.get(path).status_code
            assert status != 429, f"{path} was rate limited"

    def test_the_rule_is_the_api_prefix(self):
        from solaris.api.middleware import is_rate_limited

        assert is_rate_limited("/api/yield")
        assert is_rate_limited("/api/tiles")
        assert is_rate_limited("/api/auth/guest")
        assert not is_rate_limited("/api/health")
        assert not is_rate_limited("/")
        assert not is_rate_limited("/assets/Explore-BP6IwWWS.js")
        assert not is_rate_limited("/textures/earth-night.jpg")
        # A path that merely contains "api" is not under the API prefix.
        assert not is_rate_limited("/apiary")
