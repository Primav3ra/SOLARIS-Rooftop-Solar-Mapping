"""
Data availability: what windows can actually be computed, and how much of a
requested window really has data behind it.

The defect this fixes
---------------------
The temporal ceiling was calendar-derived: ``date.today().year - 1``. So a
quarter of the current year that finished months ago was refused, with the
message "year must be between 2000 and <last year>", purely because the
calendar year had not ended. The right question is whether the *data* exists,
not whether the year is over.

Worse, the failure past the edge was silent rather than loud. ``_mean_over_aoi``
degrades through ``bestEffort`` to a centroid sample and finally to
``fallback_zero``, so a window extending past the end of ERA5-Land returned a
plausible small number instead of an error -- and ``/api/yield`` multiplied it
by roof area and under-reported the result with no indication anything was
wrong.

So there are two jobs here:

1. :func:`latest_available_date` finds where the data actually ends, and
   ``/api/presets`` advertises that instead of a calendar bound.
2. :func:`window_coverage` reports how many days of a requested window have
   data, so a partially-covered answer is *visibly* partial.

ERA5-Land publication lag is real but not a fixed number: the GEE catalog says
"1950 to three months from real-time" while simultaneously listing coverage
well inside three months. Rather than trust either, probe the collection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import ee

from solaris.gee.irradiance import ERA5_BAND, ERA5_COLLECTION

#: How far back to look when probing for the end of a collection.
PROBE_HORIZON_DAYS = 400

#: Earliest date any supported collection covers.
EARLIEST_SUPPORTED = date(1981, 1, 1)


@dataclass(frozen=True)
class Coverage:
    """How much of a requested window has data behind it."""

    start_date: str
    end_date_exclusive: str
    days_requested: int
    days_available: int
    latest_available_date: str | None

    @property
    def fraction(self) -> float:
        if self.days_requested <= 0:
            return 0.0
        return min(1.0, self.days_available / self.days_requested)

    @property
    def is_complete(self) -> bool:
        return self.days_available >= self.days_requested

    @property
    def status(self) -> str:
        if self.is_complete:
            return "complete"
        if self.days_available <= 0:
            return "no_data"
        return "partial"

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "days_requested": self.days_requested,
            "days_available": self.days_available,
            "fraction": round(self.fraction, 4),
            "latest_available_date": self.latest_available_date,
            "warning": self.warning,
        }

    @property
    def warning(self) -> str | None:
        """A caller-facing explanation when the window is not fully covered."""
        if self.is_complete:
            return None
        if self.days_available <= 0:
            return (
                f"No {ERA5_COLLECTION} data for {self.start_date} to "
                f"{self.end_date_exclusive}. Latest available is "
                f"{self.latest_available_date}."
            )
        missing = self.days_requested - self.days_available
        return (
            f"Only {self.days_available} of {self.days_requested} days have data "
            f"({self.fraction * 100:.0f}%); {missing} day(s) fall past the end of "
            f"{ERA5_COLLECTION} (latest {self.latest_available_date}). Totals are "
            "correspondingly low -- treat this result as partial."
        )


def _collection_end(collection_id: str, band: str) -> date | None:
    """
    The last date the collection has an image for.

    Uses the collection's own ``system:time_start`` rather than the catalog's
    advertised range, because the two disagree: the GEE catalog page for
    ERA5-Land claims a three-month lag while listing coverage far more recent
    than that.
    """
    today = date.today()
    window_start = today - timedelta(days=PROBE_HORIZON_DAYS)
    collection = (
        ee.ImageCollection(collection_id)
        .select(band)
        .filterDate(window_start.isoformat(), (today + timedelta(days=2)).isoformat())
        .sort("system:time_start", False)
    )
    first = collection.first()
    stamp = first.get("system:time_start").getInfo()
    if stamp is None:
        return None
    # system:time_start is epoch milliseconds, UTC.
    return date.fromtimestamp(stamp / 1000.0)


def latest_available_date(
    collection_id: str = ERA5_COLLECTION, band: str = ERA5_BAND
) -> date | None:
    """
    The most recent date with data, or None if the probe fails.

    Callers should treat None as "unknown" and fall back to a conservative
    bound rather than assuming the data is current.
    """
    try:
        return _collection_end(collection_id, band)
    except Exception:
        # A probe failure must not take an endpoint down: the caller falls
        # back to a conservative bound.
        return None


def conservative_latest_date(today: date | None = None) -> date:
    """
    Fallback bound when the live probe is unavailable.

    Three months back, matching the lag ERA5-Land documents. Deliberately
    pessimistic: understating availability refuses a window that might have
    worked, which is a visible and recoverable error, whereas overstating it
    returns a silently partial number.
    """
    today = today or date.today()
    return today - timedelta(days=92)


def window_coverage(
    start_date: str,
    end_date_exclusive: str,
    latest: date | None = None,
) -> Coverage:
    """
    How much of ``[start_date, end_date_exclusive)`` has data.

    ``latest`` may be supplied to avoid re-probing; when omitted the probe
    runs, falling back to :func:`conservative_latest_date`.
    """
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date_exclusive)
    days_requested = max((end - start).days, 0)

    if latest is None:
        latest = latest_available_date() or conservative_latest_date()

    # latest is inclusive, so the first uncovered day is latest + 1.
    covered_end = min(end, latest + timedelta(days=1))
    days_available = max((covered_end - start).days, 0)

    return Coverage(
        start_date=start_date,
        end_date_exclusive=end_date_exclusive,
        days_requested=days_requested,
        days_available=days_available,
        latest_available_date=latest.isoformat() if latest else None,
    )


def max_selectable_year(latest: date | None = None) -> int:
    """
    The latest year a user may select.

    Data-derived rather than calendar-derived: a year is selectable as soon as
    *any* of it has data, because quarterly and monthly windows inside it are
    then computable. The per-request coverage check is what catches a window
    that reaches past the end.
    """
    if latest is None:
        latest = latest_available_date() or conservative_latest_date()
    return latest.year
