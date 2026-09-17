"""
Tests for the cross-cutting layer: config, cache keys, limits, data quality and
coverage.

Most of these assert *policy*, not arithmetic, so they read as a specification
of the decisions taken:

* the cache key must collapse equivalent requests and must version-invalidate
  on an algorithm change,
* coordinates must be quantised or the hit rate is ~0,
* a window that is not yet fully in the past must never be cached,
* the year ceiling must come from data availability, not the calendar,
* a substituted value must be reported, not silently returned.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import ClassVar

import pytest
from pydantic import ValidationError

from solaris.core import cache as cache_mod
from solaris.core import constants as C
from solaris.core import limits as limits_mod
from solaris.core.config import Settings, get_settings
from solaris.core.quality import DataQuality, Severity
from solaris.gee import coverage as coverage_mod


@pytest.fixture(autouse=True)
def _clean_state():
    cache_mod.reset_cache()
    limits_mod.reset_limits()
    get_settings.cache_clear()
    yield
    cache_mod.reset_cache()
    limits_mod.reset_limits()
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestSettings:
    def test_defaults_are_usable(self):
        settings = Settings(_env_file=None)
        assert settings.env == "local"
        assert settings.gee_project_id
        assert settings.max_concurrent_ee_calls >= 1

    def test_wildcard_origin_is_rejected(self):
        """
        The old config paired `allow_origins=["*"]` with
        `allow_credentials=True`, which is invalid per the CORS spec, so it
        never granted what it appeared to. Making it unrepresentable is
        cheaper than re-explaining it.
        """
        with pytest.raises(ValueError, match="must not contain"):
            Settings(_env_file=None, cors_origins=["*"])

    def test_origins_accept_a_comma_separated_string(self):
        """Environment variables arrive as strings, not lists."""
        settings = Settings(_env_file=None, cors_origins="http://a.test, http://b.test")
        assert settings.cors_origins == ["http://a.test", "http://b.test"]

    def test_invalid_log_level_is_rejected(self):
        with pytest.raises(ValueError, match="log_level"):
            Settings(_env_file=None, log_level="CHATTY")

    def test_is_frozen(self):
        """Configuration must not be mutable mid-request."""
        settings = Settings(_env_file=None)
        with pytest.raises(ValidationError):
            settings.env = "prod"  # type: ignore[misc]

    def test_redacted_never_leaks_a_secret(self):
        settings = Settings(_env_file=None, gee_sa_json="super-secret-key-material")
        payload = settings.redacted()
        assert payload["gee_credentials_configured"] is True
        assert "super-secret-key-material" not in repr(payload)

    def test_get_settings_is_cached(self):
        assert get_settings() is get_settings()


# ---------------------------------------------------------------------------
# Cache keys
# ---------------------------------------------------------------------------


class TestCacheKey:
    BASE: ClassVar[dict] = {"lat": 28.6139, "lon": 77.2090, "half_size_deg": 0.01}
    WINDOW: ClassVar[dict] = {
        "start_date": "2023-01-01",
        "end_date_exclusive": "2024-01-01",
    }

    def _key(self, payload=None, **window):
        return cache_mod.cache_key(
            "/api/yield", payload or dict(self.BASE), **{**self.WINDOW, **window}
        )

    def test_is_stable(self):
        assert self._key() == self._key()

    def test_equivalent_temporal_modes_collapse(self):
        """
        `{mode: yearly, year: 2023}` and the explicit range describe the same
        computation. Keying the raw body would treat them as two entries and
        halve the hit rate for no reason.
        """
        via_mode = self._key({**self.BASE, "baseline_mode": "yearly", "year": 2023})
        via_range = self._key({**self.BASE, "baseline_mode": "daily"})
        assert via_mode == via_range

    def test_coordinates_are_quantised(self):
        """
        Without this the hit rate is ~0: AOIs come from map clicks, so the raw
        floats are unique per click. 4 dp is ~11 m, far below ERA5's 9 km cell.
        """
        nudged = {**self.BASE, "lat": 28.61390001}
        assert self._key(nudged) == self._key()

    def test_a_materially_different_location_differs(self):
        assert self._key({**self.BASE, "lat": 19.0760}) != self._key()

    def test_a_different_window_differs(self):
        assert self._key(end_date_exclusive="2023-07-01") != self._key()

    def test_pv_parameters_affect_the_key(self):
        assert self._key({**self.BASE, "performance_ratio": 0.75}) != self._key()

    def test_project_id_does_not_affect_the_key(self):
        """It is server config and is ignored on input; it must not fragment the cache."""
        assert self._key({**self.BASE, "project_id": "anything"}) == self._key()

    def test_key_is_version_prefixed(self):
        """
        A deploy that changes the physics must invalidate stale entries
        automatically. Relying on someone remembering to flush a cache after a
        model change is the step that gets forgotten exactly once.
        """
        key = self._key()
        assert key.startswith(f"a{C.ALGO_VERSION}.d{C.DATASET_VERSION}.")

    def test_algorithm_version_change_invalidates(self, monkeypatch):
        before = self._key()
        monkeypatch.setattr(C, "ALGO_VERSION", "999")
        assert self._key() != before

    def test_endpoints_do_not_collide(self):
        yield_key = cache_mod.cache_key("/api/yield", dict(self.BASE), **self.WINDOW)
        series_key = cache_mod.cache_key("/api/series", dict(self.BASE), **self.WINDOW)
        assert yield_key != series_key

    def test_none_values_are_dropped(self):
        assert self._key({**self.BASE, "quarter": None}) == self._key()


class TestCacheability:
    def test_a_past_window_is_cacheable(self):
        assert cache_mod.is_cacheable_window("2023-01-01", today=date(2026, 1, 1))

    def test_a_window_ending_today_is_closed(self):
        """
        ``end_date_exclusive`` is exclusive, so a window ending today covers
        through yesterday and *is* closed in calendar terms.
        """
        assert cache_mod.is_cacheable_window("2026-06-01", today=date(2026, 6, 1))

    def test_a_window_ending_tomorrow_is_not(self):
        assert not cache_mod.is_cacheable_window("2026-06-02", today=date(2026, 6, 1))

    def test_incomplete_coverage_blocks_a_store(self):
        """
        The calendar being closed is not sufficient. ERA5-Land lags real time,
        so a window that ended last week can still be partial -- and caching a
        partial result would pin it for the whole TTL, leaving it wrong after
        the data arrived.
        """
        payload = {"lat": 28.6, "lon": 77.2}
        window = {"start_date": "2023-01-01", "end_date_exclusive": "2024-01-01"}
        outcome = cache_mod.lookup("/api/yield", payload, **window)
        cache_mod.store(
            outcome,
            {"answer": 1},
            end_date_exclusive=window["end_date_exclusive"],
            ttl_s=60,
            coverage_complete=False,
        )
        assert cache_mod.lookup("/api/yield", payload, **window).status == "MISS"

    def test_a_future_window_is_not(self):
        assert not cache_mod.is_cacheable_window("2027-01-01", today=date(2026, 1, 1))

    def test_a_malformed_date_is_not(self):
        assert not cache_mod.is_cacheable_window("not-a-date")


class TestMemoryCache:
    def test_round_trip(self):
        cache = cache_mod.MemoryCache(max_entries=8)
        cache.set("k", {"v": 1}, ttl_s=60)
        assert cache.get("k") == {"v": 1}
        assert cache.stats.hits == 1

    def test_miss_is_counted(self):
        cache = cache_mod.MemoryCache()
        assert cache.get("absent") is None
        assert cache.stats.misses == 1

    def test_zero_ttl_does_not_store(self):
        cache = cache_mod.MemoryCache()
        cache.set("k", {"v": 1}, ttl_s=0)
        assert cache.get("k") is None

    def test_clear(self):
        cache = cache_mod.MemoryCache()
        cache.set("k", {"v": 1}, ttl_s=60)
        cache.clear()
        assert cache.get("k") is None

    def test_null_cache_never_stores(self):
        cache = cache_mod.NullCache()
        cache.set("k", {"v": 1}, ttl_s=60)
        assert cache.get("k") is None
        assert cache.stats.bypasses == 1

    def test_lookup_and_store_round_trip(self):
        payload = {"lat": 28.6, "lon": 77.2}
        window = {"start_date": "2023-01-01", "end_date_exclusive": "2024-01-01"}
        first = cache_mod.lookup("/api/yield", payload, **window)
        assert first.status == "MISS"
        cache_mod.store(
            first, {"answer": 42}, end_date_exclusive=window["end_date_exclusive"], ttl_s=60
        )
        second = cache_mod.lookup("/api/yield", payload, **window)
        assert second.status == "HIT"
        assert second.value == {"answer": 42}

    def test_incomplete_window_bypasses(self):
        future = (date.today() + timedelta(days=30)).isoformat()
        outcome = cache_mod.lookup(
            "/api/yield",
            {"lat": 28.6, "lon": 77.2},
            start_date=date.today().isoformat(),
            end_date_exclusive=future,
        )
        assert outcome.status == "BYPASS"

    def test_headers_expose_the_outcome(self):
        outcome = cache_mod.CacheOutcome(key="abc", status="HIT")
        assert outcome.headers["X-Cache"] == "HIT"
        assert outcome.headers["X-Cache-Key"] == "abc"


# ---------------------------------------------------------------------------
# Limits
# ---------------------------------------------------------------------------


class TestEarthEngineBudget:
    def test_consumes(self):
        budget = limits_mod.EarthEngineBudget(limit=10)
        budget.consume(3)
        assert budget.used == 3
        assert budget.remaining == 7

    def test_raises_when_exhausted(self):
        budget = limits_mod.EarthEngineBudget(limit=5)
        budget.consume(5)
        with pytest.raises(limits_mod.BudgetExceededError, match="exhausted"):
            budget.consume(1)

    def test_a_partial_overrun_is_refused_atomically(self):
        """Consuming 3 against a remaining 2 must take none of them."""
        budget = limits_mod.EarthEngineBudget(limit=5)
        budget.consume(3)
        with pytest.raises(limits_mod.BudgetExceededError):
            budget.consume(3)
        assert budget.used == 3

    def test_zero_limit_means_unlimited(self):
        budget = limits_mod.EarthEngineBudget(limit=0)
        budget.consume(1_000_000)

    def test_rolls_over_on_a_new_day(self):
        budget = limits_mod.EarthEngineBudget(limit=5)
        budget.consume(5)
        budget._day = "1999-01-01"  # force a stale day
        assert budget.used == 0


class TestSlidingWindowLimiter:
    def test_allows_up_to_the_limit(self):
        limiter = limits_mod.SlidingWindowLimiter(limit=3, window_s=60)
        for _ in range(3):
            limiter.check("alice")

    def test_blocks_past_the_limit(self):
        limiter = limits_mod.SlidingWindowLimiter(limit=2, window_s=60)
        limiter.check("alice")
        limiter.check("alice")
        with pytest.raises(limits_mod.RateLimitExceededError) as exc:
            limiter.check("alice")
        assert exc.value.retry_after_s >= 1

    def test_identities_are_independent(self):
        limiter = limits_mod.SlidingWindowLimiter(limit=1, window_s=60)
        limiter.check("alice")
        limiter.check("bob")

    def test_zero_limit_disables(self):
        limiter = limits_mod.SlidingWindowLimiter(limit=0, window_s=60)
        for _ in range(100):
            limiter.check("alice")

    def test_window_expiry_frees_capacity(self):
        limiter = limits_mod.SlidingWindowLimiter(limit=1, window_s=0)
        limiter.check("alice")
        limiter.check("alice")  # the previous hit has already aged out


class TestEarthEngineGate:
    def test_slot_bounds_concurrency(self):
        gate = limits_mod.EarthEngineGate(
            max_concurrent=2, budget=limits_mod.EarthEngineBudget(limit=100)
        )
        with gate.slot(), gate.slot():
            assert gate.in_flight == 2
        assert gate.in_flight == 0

    def test_a_slot_counts_against_the_budget(self):
        budget = limits_mod.EarthEngineBudget(limit=10)
        gate = limits_mod.EarthEngineGate(max_concurrent=4, budget=budget)
        with gate.slot(n_calls=5):
            pass
        assert budget.used == 5

    def test_budget_is_checked_before_a_slot_is_taken(self):
        """
        Rejecting on an exhausted budget must not consume a concurrency slot,
        or a burst of rejected requests would starve the ones that could run.
        """
        budget = limits_mod.EarthEngineBudget(limit=1)
        gate = limits_mod.EarthEngineGate(max_concurrent=1, budget=budget)
        budget.consume(1)
        with pytest.raises(limits_mod.BudgetExceededError), gate.slot():
            pass
        assert gate.in_flight == 0

    def test_timeout_when_no_slot_is_available(self):
        gate = limits_mod.EarthEngineGate(
            max_concurrent=1, budget=limits_mod.EarthEngineBudget(limit=100)
        )
        with (
            gate.slot(),
            pytest.raises(limits_mod.ConcurrencyTimeoutError),
            gate.slot(timeout_s=0.05),
        ):
            pass

    def test_slot_released_on_exception(self):
        gate = limits_mod.EarthEngineGate(
            max_concurrent=1, budget=limits_mod.EarthEngineBudget(limit=100)
        )
        with pytest.raises(RuntimeError), gate.slot():
            raise RuntimeError("boom")
        assert gate.in_flight == 0


# ---------------------------------------------------------------------------
# Data quality
# ---------------------------------------------------------------------------


class TestDataQuality:
    def test_a_clean_result_records_nothing(self):
        quality = DataQuality()
        quality.record_source("regional_ghi", "reduceRegion")
        assert quality.severity is Severity.OK
        assert quality.is_fully_computed

    def test_a_zero_fallback_is_unreliable(self):
        """
        The sharpest case: the reduction found no pixels and returned 0.0, so
        every energy figure derived from it is zero for the wrong reason.
        """
        quality = DataQuality()
        quality.record_source("regional_ghi", "fallback_zero")
        assert quality.severity is Severity.UNRELIABLE
        assert not quality.is_fully_computed

    def test_a_substituted_beam_fraction_is_degraded_not_unreliable(self):
        quality = DataQuality()
        quality.record_source("beam_fraction", "fallback_no_sample")
        assert quality.severity is Severity.DEGRADED

    def test_the_aoi_fallback_is_unreliable(self):
        """
        It silently swaps one roof for a ~2 km box, changing the denominator of
        every per-building figure by orders of magnitude.
        """
        quality = DataQuality()
        quality.record_source("target_building", "aoi_fallback")
        assert quality.severity is Severity.UNRELIABLE
        assert "entire AOI" in quality.findings[0].detail

    def test_empty_shade_buckets_are_flagged(self):
        """
        A bucket with no sun reports shade_fraction 0.0, which reads as "fully
        sunlit" -- the opposite of what it means.
        """
        quality = DataQuality()
        quality.record_empty_shade_buckets(["16-20", "20-24"])
        assert quality.severity is Severity.DEGRADED
        assert "not 'no shade'" in quality.findings[0].detail

    def test_no_empty_buckets_records_nothing(self):
        quality = DataQuality()
        quality.record_empty_shade_buckets([])
        assert quality.is_fully_computed

    def test_worst_severity_wins(self):
        quality = DataQuality()
        quality.record_source("beam_fraction", "fallback_no_sample")
        quality.record_source("regional_ghi", "fallback_zero")
        assert quality.severity is Severity.UNRELIABLE

    def test_partial_coverage_is_unreliable(self):
        quality = DataQuality()
        quality.set_coverage({"status": "partial", "warning": "only 75 of 91 days"})
        assert quality.severity is Severity.UNRELIABLE

    def test_complete_coverage_records_nothing(self):
        quality = DataQuality()
        quality.set_coverage({"status": "complete"})
        assert quality.is_fully_computed

    def test_record_stats_reads_the_nested_source(self):
        quality = DataQuality()
        quality.record_stats("soiling", {"source": "fallback_urban_midpoint"})
        assert quality.severity is Severity.DEGRADED

    def test_unknown_sources_are_treated_as_real(self):
        quality = DataQuality()
        quality.record_source("x", "some_new_successful_path")
        assert quality.is_fully_computed

    def test_serialises(self):
        import json

        quality = DataQuality()
        quality.record_source("regional_ghi", "fallback_zero")
        payload = quality.as_dict()
        json.dumps(payload)
        assert payload["severity"] == "unreliable"
        assert "Do not quote" in payload["summary"]


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


class TestCoverage:
    LATEST = date(2026, 6, 14)

    def test_a_fully_covered_window_is_complete(self):
        cov = coverage_mod.window_coverage("2023-01-01", "2024-01-01", latest=self.LATEST)
        assert cov.status == "complete"
        assert cov.fraction == 1.0
        assert cov.warning is None

    def test_a_window_past_the_edge_is_partial(self):
        """
        The case that used to return a plausible small number with no
        indication anything was wrong.
        """
        cov = coverage_mod.window_coverage("2026-04-01", "2026-07-01", latest=self.LATEST)
        assert cov.status == "partial"
        assert cov.days_available == 75
        assert cov.days_requested == 91
        assert "partial" in (cov.warning or "")

    def test_a_window_entirely_past_the_edge_has_no_data(self):
        cov = coverage_mod.window_coverage("2026-07-01", "2026-10-01", latest=self.LATEST)
        assert cov.status == "no_data"
        assert cov.days_available == 0

    def test_max_selectable_year_is_data_derived(self):
        """
        The R5 fix. The ceiling used to be `date.today().year - 1`, which
        refused a quarter of the current year that had finished months
        earlier, purely because the calendar year had not ended.
        """
        assert coverage_mod.max_selectable_year(latest=self.LATEST) == 2026

    def test_the_conservative_fallback_is_pessimistic(self):
        """
        Understating availability refuses a window that might have worked --
        visible and recoverable. Overstating it returns a silently partial
        number, which is neither.
        """
        today = date(2026, 9, 14)
        fallback = coverage_mod.conservative_latest_date(today)
        assert fallback < today
        assert (today - fallback).days >= 90

    def test_serialises(self):
        import json

        payload = coverage_mod.window_coverage(
            "2026-04-01", "2026-07-01", latest=self.LATEST
        ).as_dict()
        json.dumps(payload)
        assert payload["status"] == "partial"


class TestQ2Twenty26:
    """
    The specific case that prompted this work: a quarter of 2026 that finished
    months ago was refused because the calendar year had not ended.
    """

    def test_is_now_accepted(self):
        from solaris.api.windows import resolve_temporal_window

        latest = date(2026, 6, 14)
        max_year = coverage_mod.max_selectable_year(latest=latest)
        window = resolve_temporal_window("quarterly", 2026, 2, None, None, None, max_year=max_year)
        assert window["start_date"] == "2026-04-01"
        assert window["end_date_exclusive"] == "2026-07-01"

    def test_was_previously_refused(self):
        """Pins the old behaviour so the regression is recognisable."""
        from solaris.api.windows import resolve_temporal_window

        with pytest.raises(ValueError, match="year must be between"):
            resolve_temporal_window("quarterly", 2026, 2, None, None, None, max_year=2025)

    def test_and_its_partial_coverage_is_reported(self):
        cov = coverage_mod.window_coverage("2026-04-01", "2026-07-01", latest=date(2026, 6, 14))
        assert cov.status == "partial"
        assert cov.warning and "past the end" in cov.warning
