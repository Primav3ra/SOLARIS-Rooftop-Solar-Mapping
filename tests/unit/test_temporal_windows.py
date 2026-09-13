"""
Temporal-window resolution tests.

``resolve_temporal_window`` is ~80 lines of branching that decides the date
range every Earth Engine query uses, and it had no coverage. It needs no Earth
Engine access, so all of this runs offline.

Also pins the *current* recency behaviour: the year ceiling is derived from the
calendar (``today.year - 1``) rather than from actual data availability, so a
finished quarter of the current year is rejected. That is a known defect; the
tests below assert today's behaviour and are marked so they fail loudly when it
is fixed, rather than silently passing afterwards.
"""

from __future__ import annotations

from datetime import date

import pytest

from solaris.api.windows import (
    _last_complete_calendar_year,
    _parse_daily_window,
    _quarter_bounds,
    resolve_temporal_window,
    square_aoi_from_point,
)


def _resolve(mode, **kw):
    return resolve_temporal_window(
        mode,
        kw.get("year"),
        kw.get("quarter"),
        kw.get("month"),
        kw.get("start_date"),
        kw.get("end_date_exclusive"),
    )


class TestQuarterBounds:
    @pytest.mark.parametrize(
        "year,quarter,expected",
        [
            (2023, 1, ("2023-01-01", "2023-04-01")),
            (2023, 2, ("2023-04-01", "2023-07-01")),
            (2023, 3, ("2023-07-01", "2023-10-01")),
            (2023, 4, ("2023-10-01", "2024-01-01")),  # rolls the year
        ],
    )
    def test_bounds(self, year, quarter, expected):
        assert _quarter_bounds(year, quarter) == expected

    def test_invalid_quarter(self):
        with pytest.raises(ValueError, match="quarter"):
            _quarter_bounds(2023, 5)


class TestYearlyMode:
    def test_full_year(self):
        win = _resolve("yearly", year=2023)
        assert win["mode"] == "yearly"
        assert win["start_date"] == "2023-01-01"
        assert win["end_date_exclusive"] == "2024-01-01"
        assert win["calendar_year"] == 2023

    def test_defaults_to_last_complete_year(self):
        assert _resolve("yearly")["calendar_year"] == _last_complete_calendar_year()

    def test_year_below_floor_rejected(self):
        with pytest.raises(ValueError, match="year must be between"):
            _resolve("yearly", year=1999)

    def test_future_year_rejected(self):
        with pytest.raises(ValueError, match="year must be between"):
            _resolve("yearly", year=_last_complete_calendar_year() + 1)


class TestQuarterlyAndMonthlyModes:
    def test_quarterly(self):
        win = _resolve("quarterly", year=2023, quarter=3)
        assert (win["start_date"], win["end_date_exclusive"]) == (
            "2023-07-01",
            "2023-10-01",
        )
        assert win["quarter"] == 3

    def test_quarterly_defaults_to_q2(self):
        assert _resolve("quarterly", year=2023)["quarter"] == 2

    def test_monthly(self):
        win = _resolve("monthly", year=2023, month=6)
        assert (win["start_date"], win["end_date_exclusive"]) == (
            "2023-06-01",
            "2023-07-01",
        )
        assert win["month"] == 6

    def test_december_monthly_rolls_the_year(self):
        win = _resolve("monthly", year=2023, month=12)
        assert (win["start_date"], win["end_date_exclusive"]) == (
            "2023-12-01",
            "2024-01-01",
        )

    def test_february_leap_year(self):
        win = _resolve("monthly", year=2020, month=2)
        assert win["end_date_exclusive"] == "2020-03-01"
        span = date.fromisoformat(win["end_date_exclusive"]) - date.fromisoformat(win["start_date"])
        assert span.days == 29

    def test_february_non_leap_year(self):
        win = _resolve("monthly", year=2023, month=2)
        span = date.fromisoformat(win["end_date_exclusive"]) - date.fromisoformat(win["start_date"])
        assert span.days == 28

    def test_invalid_quarter_rejected(self):
        with pytest.raises(ValueError, match="quarter must be"):
            _resolve("quarterly", year=2023, quarter=7)

    def test_invalid_month_rejected(self):
        with pytest.raises(ValueError, match="month must be"):
            _resolve("monthly", year=2023, month=13)


class TestDailyMode:
    def test_single_day(self):
        win = _resolve("daily", start_date="2023-06-21", end_date_exclusive="2023-06-22")
        assert win["mode"] == "daily"
        assert win["calendar_year"] is None

    def test_multi_day_rejected(self):
        with pytest.raises(ValueError, match="exactly one calendar day"):
            _resolve("daily", start_date="2023-06-21", end_date_exclusive="2023-06-25")

    def test_reversed_range_rejected(self):
        with pytest.raises(ValueError, match="must be after"):
            _resolve("daily", start_date="2023-06-21", end_date_exclusive="2023-06-20")

    def test_missing_dates_rejected(self):
        with pytest.raises(ValueError, match="daily mode requires"):
            _resolve("daily")

    def test_parse_daily_window_counts_days(self):
        assert _parse_daily_window("2023-06-21", "2023-06-22") == 1
        assert _parse_daily_window("2023-06-01", "2023-07-01") == 30

    def test_parse_daily_window_rejects_equal_dates(self):
        with pytest.raises(ValueError, match="must be after"):
            _parse_daily_window("2023-06-21", "2023-06-21")


class TestModeValidation:
    def test_unknown_mode_rejected(self):
        with pytest.raises(ValueError, match="baseline_mode must be"):
            _resolve("hourly", year=2023)

    def test_mode_is_case_insensitive(self):
        assert _resolve("YEARLY", year=2023)["mode"] == "yearly"

    def test_empty_mode_defaults_to_yearly(self):
        assert _resolve("")["mode"] == "yearly"

    @pytest.mark.parametrize("mode", ["yearly", "quarterly", "monthly"])
    def test_every_mode_returns_a_parseable_ordered_range(self, mode):
        win = _resolve(mode, year=2023, quarter=2, month=6)
        start = date.fromisoformat(win["start_date"])
        end = date.fromisoformat(win["end_date_exclusive"])
        assert start < end

    @pytest.mark.parametrize("mode", ["yearly", "quarterly", "monthly", "daily"])
    def test_response_keys_are_stable_across_modes(self, mode):
        """
        The API reads ``win.get("quarter")`` / ``win.get("month")`` for every
        mode, so each must be present (possibly None) rather than absent.
        """
        win = _resolve(
            mode,
            year=2023,
            quarter=2,
            month=6,
            start_date="2023-06-21",
            end_date_exclusive="2023-06-22",
        )
        for key in ("mode", "start_date", "end_date_exclusive", "calendar_year"):
            assert key in win


class TestCurrentYearCeiling:
    """
    Documents a known defect: the ceiling is calendar-derived, not data-derived.

    A quarter of the current year that finished months ago is rejected purely
    because the calendar year has not ended. The right test is whether the
    underlying ERA5-Land data exists. When that is fixed, these tests should be
    updated rather than deleted.
    """

    def test_current_year_is_refused(self):
        current = date.today().year
        assert _last_complete_calendar_year() == current - 1
        with pytest.raises(ValueError, match="year must be between"):
            _resolve("quarterly", year=current, quarter=1)

    def test_ceiling_is_calendar_derived_not_data_derived(self):
        assert _last_complete_calendar_year() == date.today().year - 1


class TestSquareAoi:
    def test_ring_is_closed(self):
        ring = square_aoi_from_point(28.6, 77.2, 0.01)
        assert len(ring) == 5
        assert ring[0] == ring[-1]

    def test_ring_is_lon_lat_ordered(self):
        ring = square_aoi_from_point(28.6, 77.2, 0.01)
        for lon, lat in ring:
            assert 77.18 <= lon <= 77.22
            assert 28.58 <= lat <= 28.62

    def test_square_in_degrees_is_not_square_in_metres(self):
        """
        The AOI is a degree-square, so at Delhi's latitude it is ~14% taller
        than it is wide. Documented rather than fixed; pinned so the geometry
        does not change unnoticed.
        """
        import math

        half = 0.01
        ring = square_aoi_from_point(28.6, 77.2, half)
        lon_span = (ring[1][0] - ring[0][0]) * math.cos(math.radians(28.6))
        lat_span = ring[2][1] - ring[1][1]
        assert lat_span > lon_span
        assert lat_span / lon_span == pytest.approx(1.0 / math.cos(math.radians(28.6)), rel=1e-6)
