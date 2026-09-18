"""
Concurrency and quota limits.

Three layers, cheapest first, because they defend against different things.

**1. Input bounds** (in :mod:`solaris.api.schemas`) are the primary defence and
the only one that helps against the worst case: a single well-formed request
for an oversized area of interest can burn the quota on its own, and no
request-rate limit touches that. They run before any ``ee`` object exists, so a
rejected request costs nothing.

**2. A concurrency semaphore.** Every endpoint is a sync ``def``, so FastAPI
runs it on a threadpool of 40. Without a cap, one instance can hold 40 blocking
``getInfo()`` calls open at once. That is the actual concurrency hazard, and it
is also why the request timeout is not sufficient on its own: an ``anyio``
timeout frees the *client*, but ``getInfo()`` is uninterruptible blocking I/O,
so the worker thread keeps going.

**3. A daily Earth Engine compute budget**, and this is the layer that actually
protects the quota.

It counts **cost units**, not round-trips. That distinction was not academic:
Earth Engine bills EECU-seconds, every ``/api/yield`` makes 12 round-trips
regardless of window, and a yearly window costs more than ten times a
single-day one. A call-counting budget therefore treated the cheapest and
dearest queries as identical.

The ceiling it defends is fixed and cannot be raised from the console. The
noncommercial tier allows 540,000 EECU-seconds per month as a **system limit**,
and the daily figure beside it reads "Unlimited" with no adjustable setting --
so Google offers no lever and this counter is the only guard available. One
measured day of development consumed 39,301 EECU-seconds, about 91% of that
month's usage to date, from a few dozen queries. See
``constants.EE_COST_UNITS``.

Per-identity rate limiting sits alongside these, keyed on an opaque session
token where one is presented and falling back to IP. IP alone is weak in India
specifically: mobile carriers use carrier-grade NAT, so thousands of
subscribers share an egress address -- an IP-keyed limit restricts them
collectively while one determined caller rotates addresses.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date


class BudgetExceededError(RuntimeError):
    """The daily Earth Engine compute budget is exhausted."""


class RateLimitExceededError(RuntimeError):
    """This identity has made too many requests."""

    def __init__(self, message: str, retry_after_s: int = 60):
        super().__init__(message)
        self.retry_after_s = retry_after_s


class ConcurrencyTimeoutError(RuntimeError):
    """Could not acquire an Earth Engine slot in time."""


# ---------------------------------------------------------------------------
# Earth Engine call accounting
# ---------------------------------------------------------------------------


@dataclass
class EarthEngineBudget:
    """
    A daily ceiling on Earth Engine **compute cost**, reset by date.

    In-process here. A multi-instance deployment needs this in a shared store
    (Firestore's ``FieldValue.increment`` is the intended implementation) or
    each instance gets its own allowance. The interface is deliberately narrow
    so swapping the counter is a small change.
    """

    limit: float
    _day: str = ""
    _count: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _roll(self) -> None:
        today = date.today().isoformat()
        if self._day != today:
            self._day, self._count = today, 0

    @property
    def used(self) -> float:
        with self._lock:
            self._roll()
            return self._count

    @property
    def remaining(self) -> float:
        return max(0.0, self.limit - self.used)

    def consume(self, n: float = 1.0) -> None:
        """Record ``n`` calls, raising once the day's allowance is gone."""
        with self._lock:
            self._roll()
            if self.limit and self._count + n > self.limit:
                raise BudgetExceededError(
                    f"Daily Earth Engine compute budget of {self.limit:g} units is "
                    f"exhausted ({self._count:g} used). One unit is roughly one "
                    f"monthly-window query. Resets at UTC midnight."
                )
            self._count += n

    def as_dict(self) -> dict[str, float | str]:
        return {
            "limit": self.limit,
            "used": round(self.used, 2),
            "remaining": round(self.remaining, 2),
            "unit": "cost units; 1 = one monthly-window query",
            "day": self._day or date.today().isoformat(),
        }


# ---------------------------------------------------------------------------
# Per-identity rate limiting
# ---------------------------------------------------------------------------


@dataclass
class SlidingWindowLimiter:
    """
    A sliding-window request counter per identity.

    Sliding rather than fixed-window because a fixed window lets a caller make
    two full allowances back to back across the boundary.
    """

    limit: int
    window_s: int
    _hits: dict[str, deque] = field(default_factory=lambda: defaultdict(deque))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def check(self, identity: str) -> None:
        """Record a request, raising if this identity is over its allowance."""
        if not self.limit:
            return
        now = time.monotonic()
        cutoff = now - self.window_s
        with self._lock:
            hits = self._hits[identity]
            while hits and hits[0] < cutoff:
                hits.popleft()
            if len(hits) >= self.limit:
                retry_after = int(max(1, self.window_s - (now - hits[0])))
                raise RateLimitExceededError(
                    f"Rate limit of {self.limit} requests per {self.window_s}s "
                    f"exceeded. Retry in {retry_after}s.",
                    retry_after_s=retry_after,
                )
            hits.append(now)

    def used(self, identity: str) -> int:
        now = time.monotonic()
        cutoff = now - self.window_s
        with self._lock:
            hits = self._hits[identity]
            while hits and hits[0] < cutoff:
                hits.popleft()
            return len(hits)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


class EarthEngineGate:
    """
    Bounds concurrent Earth Engine calls and counts them against the budget.

    Used as a context manager around any block that makes ``getInfo()`` calls::

        with gate.slot(n_calls=5):
            ...
    """

    def __init__(self, max_concurrent: int, budget: EarthEngineBudget):
        self._semaphore = threading.BoundedSemaphore(max_concurrent)
        self.max_concurrent = max_concurrent
        self.budget = budget
        self._in_flight = 0
        self._lock = threading.Lock()

    @property
    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def slot(self, n_calls: float = 1.0, timeout_s: float = 30.0):
        return _GateSlot(self, n_calls=n_calls, timeout_s=timeout_s)

    def as_dict(self) -> dict[str, object]:
        return {
            "max_concurrent": self.max_concurrent,
            "in_flight": self.in_flight,
            "budget": self.budget.as_dict(),
        }


class _GateSlot:
    def __init__(self, gate: EarthEngineGate, n_calls: float, timeout_s: float):
        self._gate = gate
        self._n_calls = n_calls
        self._timeout_s = timeout_s
        self._acquired = False

    def __enter__(self) -> _GateSlot:
        # Budget first: rejecting on an exhausted budget should not consume a
        # concurrency slot.
        self._gate.budget.consume(self._n_calls)
        if not self._gate._semaphore.acquire(timeout=self._timeout_s):
            raise ConcurrencyTimeoutError(
                f"No Earth Engine slot within {self._timeout_s}s "
                f"({self._gate.max_concurrent} in flight). Retry shortly."
            )
        self._acquired = True
        with self._gate._lock:
            self._gate._in_flight += 1
        return self

    def __exit__(self, *exc_info) -> None:
        if self._acquired:
            with self._gate._lock:
                self._gate._in_flight -= 1
            self._gate._semaphore.release()
        return None


# ---------------------------------------------------------------------------
# Process-wide instances
# ---------------------------------------------------------------------------

_GATE: EarthEngineGate | None = None
_MINUTE_LIMITER: SlidingWindowLimiter | None = None
_DAY_LIMITER: SlidingWindowLimiter | None = None
_LOCK = threading.Lock()


def get_gate() -> EarthEngineGate:
    global _GATE
    if _GATE is None:
        with _LOCK:
            if _GATE is None:
                from solaris.core.config import get_settings

                settings = get_settings()
                budget: object = EarthEngineBudget(limit=settings.daily_ee_cost_budget)
                if settings.cache_backend == "firestore":
                    # The same Firestore dependency that backs the persistent
                    # cache also makes this counter global rather than
                    # per-instance. It wraps the local budget rather than
                    # replacing it, so an unreachable counter degrades to a
                    # per-instance ceiling instead of to none.
                    from solaris.core.firestore_cache import FirestoreBudget

                    budget = FirestoreBudget(
                        local=budget,
                        collection=settings.firestore_collection,
                        project=settings.gee_project_id,
                    )
                _GATE = EarthEngineGate(
                    max_concurrent=settings.max_concurrent_ee_calls,
                    budget=budget,  # type: ignore[arg-type]
                )
    return _GATE


def get_limiters() -> tuple[SlidingWindowLimiter, SlidingWindowLimiter]:
    global _MINUTE_LIMITER, _DAY_LIMITER
    if _MINUTE_LIMITER is None or _DAY_LIMITER is None:
        with _LOCK:
            from solaris.core.config import get_settings

            settings = get_settings()
            _MINUTE_LIMITER = SlidingWindowLimiter(
                limit=settings.rate_limit_per_minute, window_s=60
            )
            _DAY_LIMITER = SlidingWindowLimiter(limit=settings.rate_limit_per_day, window_s=86_400)
    return _MINUTE_LIMITER, _DAY_LIMITER


def check_rate_limit(identity: str) -> None:
    """Apply both the per-minute and per-day allowances."""
    minute, day = get_limiters()
    minute.check(identity)
    day.check(identity)


def reset_limits() -> None:
    """Drop all counters, so the next call rebuilds them. Used by tests."""
    global _GATE, _MINUTE_LIMITER, _DAY_LIMITER
    with _LOCK:
        _GATE = None
        if _MINUTE_LIMITER:
            _MINUTE_LIMITER.reset()
        if _DAY_LIMITER:
            _DAY_LIMITER.reset()
        _MINUTE_LIMITER = None
        _DAY_LIMITER = None
