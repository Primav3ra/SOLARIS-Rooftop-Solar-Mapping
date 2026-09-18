"""
Temporal window resolution -- pure logic, with no Earth Engine dependency.

Deliberately free of any ``ee`` import. This is the branching that decides the
date range every Earth Engine query uses, and keeping it separate means it can
be tested with no credentials, no network and no mocking at all.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

# solar_geometry is pure stdlib trigonometry, so importing it keeps this
# module free of any Earth Engine dependency.
from solaris.gee.solar_geometry import (
    solar_positions_monthly,
    solar_positions_single_day,
)

MONTH_ABBR = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]


def square_aoi_from_point(lat: float, lon: float, half_size_deg: float = 0.01) -> list[list[float]]:
    return [
        [lon - half_size_deg, lat - half_size_deg],
        [lon + half_size_deg, lat - half_size_deg],
        [lon + half_size_deg, lat + half_size_deg],
        [lon - half_size_deg, lat + half_size_deg],
        [lon - half_size_deg, lat - half_size_deg],
    ]


def _last_complete_calendar_year() -> int:
    return date.today().year - 1


def _quarter_bounds(year: int, quarter: int) -> tuple[str, str]:
    if quarter == 1:
        return f"{year}-01-01", f"{year}-04-01"
    if quarter == 2:
        return f"{year}-04-01", f"{year}-07-01"
    if quarter == 3:
        return f"{year}-07-01", f"{year}-10-01"
    if quarter == 4:
        return f"{year}-10-01", f"{year + 1}-01-01"
    raise ValueError("quarter must be 1..4")


def _parse_daily_window(start_date: str, end_date_exclusive: str) -> int:
    d0 = date.fromisoformat(start_date)
    d1 = date.fromisoformat(end_date_exclusive)
    if d1 <= d0:
        raise ValueError("end_date_exclusive must be after start_date")
    return (d1 - d0).days


def resolve_temporal_window(
    baseline_mode: str,
    year: int | None,
    quarter: int | None,
    month: int | None,
    start_date: str | None,
    end_date_exclusive: str | None,
    max_year: int | None = None,
) -> dict[str, Any]:
    """
    Map a UI mode to ``[start_date, end_date_exclusive)`` for ERA5 and solar
    alignment. ``monthly`` is one UTC calendar month; ``daily`` is exactly one
    UTC calendar day.

    ``max_year`` is the latest selectable year. Callers should pass a
    **data-derived** bound from :mod:`solaris.gee.coverage`; it defaults to the
    last complete calendar year only so this module stays free of any Earth
    Engine dependency and remains testable offline.

    Why that matters: the ceiling used to be ``date.today().year - 1``
    unconditionally, so a quarter of the current year that finished months ago
    was refused purely because the calendar year had not ended. Whether a
    window is computable depends on whether the data exists, not on the date.
    """
    ly = max_year if max_year is not None else _last_complete_calendar_year()
    mode = (baseline_mode or "yearly").lower()
    if mode not in ("yearly", "quarterly", "monthly", "daily"):
        raise ValueError("baseline_mode must be yearly, quarterly, monthly, or daily")
    if mode == "yearly":
        y = year if year is not None else ly
        if y < 2000 or y > ly:
            raise ValueError(
                f"year must be between 2000 and {ly} (the latest year with available data)"
            )
        s, e = f"{y}-01-01", f"{y + 1}-01-01"
        return {
            "mode": "yearly",
            "start_date": s,
            "end_date_exclusive": e,
            "calendar_year": y,
            "quarter": None,
        }
    if mode == "quarterly":
        y = year if year is not None else ly
        q = quarter if quarter is not None else 2
        if y < 2000 or y > ly:
            raise ValueError(f"year must be between 2000 and {ly}")
        if q < 1 or q > 4:
            raise ValueError("quarter must be 1..4")
        s, e = _quarter_bounds(y, q)
        return {
            "mode": "quarterly",
            "start_date": s,
            "end_date_exclusive": e,
            "calendar_year": y,
            "quarter": q,
            "month": None,
        }
    if mode == "monthly":
        y = year if year is not None else ly
        m = month if month is not None else 1
        if y < 2000 or y > ly:
            raise ValueError(f"year must be between 2000 and {ly}")
        if m < 1 or m > 12:
            raise ValueError("month must be 1..12")
        s = f"{y}-{m:02d}-01"
        e = f"{y + 1}-01-01" if m == 12 else f"{y}-{m + 1:02d}-01"
        return {
            "mode": "monthly",
            "start_date": s,
            "end_date_exclusive": e,
            "calendar_year": y,
            "quarter": None,
            "month": m,
        }
    if not start_date or not end_date_exclusive:
        raise ValueError("daily mode requires start_date and end_date_exclusive (ISO YYYY-MM-DD)")
    # _parse_daily_window already raises ValueError with a caller-facing
    # message, so let it propagate rather than re-wrapping it.
    nd = _parse_daily_window(start_date, end_date_exclusive)
    if nd != 1:
        raise ValueError(
            "daily mode requires exactly one calendar day: end_date_exclusive must be start_date + 1 day"
        )
    return {
        "mode": "daily",
        "start_date": start_date,
        "end_date_exclusive": end_date_exclusive,
        "calendar_year": None,
        "quarter": None,
        "month": None,
    }


def _cap_positions(pos: list[tuple]) -> list[tuple]:
    """
    Identity. Kept so call sites need no change.

    This used to return ``pos[::2]`` when the list exceeded 42 entries, which
    dropped half the insolation weight **without renormalising** and so would
    have silently halved shadow_frequency. It never fired: the largest set any
    builder produces is 39 (monthly), so the branch was dead code concealing a
    live bug. If a cap is ever genuinely needed it belongs in solar_geometry,
    alongside merge_weighted_position_sets, which renormalises.

    tests/unit/test_solar_geometry.py asserts every builder stays under 42, and
    that weights always sum to 1.
    """
    return pos


def _series_layout(
    mode: str,
    win: dict[str, Any],
    lat_deg: float,
    lon_deg: float,
) -> tuple[list[str], list[tuple[str, str, list[tuple]]], list[int]]:
    """
    Work out the points on the curve. Returns (labels, items, bin_of): the x labels,
    the (start, end, sun-positions) chunks to evaluate, and which output bucket each
    chunk lands in -- that mapping is 1:1 except in monthly mode, where days collapse
    into weeks.
    """
    # Narrowed once here rather than at each use. Every mode below indexes or
    # does arithmetic on the year, and resolve_temporal_window always sets it --
    # so a missing value is a programming error in the caller, not a runtime
    # condition to tolerate silently.
    raw_year = win.get("calendar_year")
    if raw_year is None:
        raise ValueError("window is missing calendar_year; resolve it before building a series")
    year = int(raw_year)

    if mode == "yearly":
        items = []
        for m in range(1, 13):
            s = f"{year}-{m:02d}-01"
            e = f"{year + 1}-01-01" if m == 12 else f"{year}-{m + 1:02d}-01"
            items.append((s, e, _cap_positions(solar_positions_monthly(lat_deg, lon_deg, year, m))))
        return list(MONTH_ABBR), items, list(range(12))

    if mode == "quarterly":
        q = int(win["quarter"])
        months = {1: [1, 2, 3], 2: [4, 5, 6], 3: [7, 8, 9], 4: [10, 11, 12]}[q]
        items, labels = [], []
        for m in months:
            s = f"{year}-{m:02d}-01"
            e = f"{year + 1}-01-01" if m == 12 else f"{year}-{m + 1:02d}-01"
            items.append((s, e, _cap_positions(solar_positions_monthly(lat_deg, lon_deg, year, m))))
            labels.append(MONTH_ABBR[m - 1])
        return labels, items, list(range(len(items)))

    if mode == "monthly":
        m = int(win["month"])
        first = date(year, m, 1)
        nxt = date(year + 1, 1, 1) if m == 12 else date(year, m + 1, 1)
        ndays = (nxt - first).days
        items, bin_of = [], []
        for d in range(1, ndays + 1):
            day = date(year, m, d)
            s = day.isoformat()
            e = (day + timedelta(days=1)).isoformat()
            items.append((s, e, _cap_positions(solar_positions_single_day(lat_deg, lon_deg, day))))
            bin_of.append(min(4, (d - 1) // 7))
        return ["W1", "W2", "W3", "W4", "W5"], items, bin_of

    # daily: a single point
    s, e = win["start_date"], win["end_date_exclusive"]
    d0 = date.fromisoformat(s)
    return [s], [(s, e, _cap_positions(solar_positions_single_day(lat_deg, lon_deg, d0)))], [0]
