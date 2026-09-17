"""
Result caching, and the key design that makes it actually work.

Why caching is the highest-value change for cost
------------------------------------------------
Every ``/api/yield`` makes several blocking ``getInfo()`` round-trips and there
was no cache of any kind. Meanwhile the underlying data is immutable: ERA5,
MODIS and Open Buildings for a *past* window never change. So essentially every
response is cacheable with a long TTL, and the only thing that legitimately
invalidates one is a change to our own algorithm.

The key design matters more than the backend
--------------------------------------------
Three decisions, each of which changes the hit rate by an order of magnitude:

**Resolve the temporal mode before keying.** ``{mode: yearly, year: 2023}`` and
the equivalent explicit date range describe the same computation, so they must
collapse to one entry. Keying on the raw request body would treat them as two.

**Quantise the coordinates.** Areas of interest come from map clicks, so the
raw floats are effectively unique per click and the hit rate would be
approximately **zero**. Rounding to 4 decimal places (~11 m) fixes that, and is
far below both ERA5's 9 km cell and the meaningful sensitivity of an AOI
*centre*. This is a real approximation and is disclosed in
``docs/methodology.md`` rather than hidden.

**Version the key.** Prefixing with ``ALGO_VERSION`` and ``DATASET_VERSION``
means a deploy that changes the physics automatically invalidates every stale
entry. The alternative -- remembering to flush a cache after a model change --
is the kind of step that gets forgotten exactly once and then silently serves
pre-fix numbers.

What is never cached
--------------------
Non-2xx responses, and any window whose end date is today or later: that
window's data is still being published, so a result computed now would be
partial and caching it would pin the partial answer for the whole TTL.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

from solaris.core import constants as C

#: Decimal places for coordinate quantisation. 4 dp is ~11 m at these
#: latitudes -- see the module docstring for why this is necessary.
COORD_PRECISION = 4

#: Request fields that describe the temporal mode rather than the resolved
#: window. Dropped from the key once the window is resolved, so equivalent
#: requests collapse to one entry.
MODE_FIELDS = frozenset({"baseline_mode", "year", "quarter", "month"})

#: Fields that never affect the computation.
IGNORED_FIELDS = frozenset({"project_id"})


@dataclass
class CacheStats:
    hits: int = 0
    misses: int = 0
    stores: int = 0
    bypasses: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "stores": self.stores,
            "bypasses": self.bypasses,
            "hit_rate": round(self.hit_rate, 4),
        }


def _quantise(value: Any) -> Any:
    """Round floats so near-identical requests share a key."""
    if isinstance(value, float):
        return round(value, COORD_PRECISION)
    if isinstance(value, list):
        return [_quantise(item) for item in value]
    if isinstance(value, dict):
        return {k: _quantise(v) for k, v in value.items()}
    return value


def canonical_payload(
    payload: dict[str, Any],
    *,
    start_date: str,
    end_date_exclusive: str,
) -> dict[str, Any]:
    """
    Reduce a request body to the fields that determine the answer.

    Mode fields are replaced by the resolved window, ignored fields are
    dropped, floats are quantised, and keys are sorted.
    """
    cleaned = {
        key: _quantise(value)
        for key, value in payload.items()
        if key not in MODE_FIELDS and key not in IGNORED_FIELDS and value is not None
    }
    cleaned["_window"] = [start_date, end_date_exclusive]
    return dict(sorted(cleaned.items()))


def cache_key(
    endpoint: str,
    payload: dict[str, Any],
    *,
    start_date: str,
    end_date_exclusive: str,
) -> str:
    """
    A stable key for one computation.

    Version-prefixed so a physics change invalidates old entries without any
    manual flush.
    """
    canonical = canonical_payload(
        payload, start_date=start_date, end_date_exclusive=end_date_exclusive
    )
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]
    return (
        f"a{C.ALGO_VERSION}.d{C.DATASET_VERSION}.{endpoint.strip('/').replace('/', '_')}.{digest}"
    )


def is_cacheable_window(end_date_exclusive: str, today: date | None = None) -> bool:
    """
    False for any window that is not yet fully in the past.

    A window ending today or later is still being published, so a result
    computed now is partial. Caching it would pin that partial answer for the
    whole TTL, which is worse than not caching at all.
    """
    today = today or date.today()
    try:
        end = date.fromisoformat(end_date_exclusive)
    except (TypeError, ValueError):
        return False
    return end <= today


class CacheBackend(Protocol):
    """Minimal backend interface, so the store can be swapped without callers changing."""

    def get(self, key: str) -> dict[str, Any] | None: ...

    def set(self, key: str, value: dict[str, Any], ttl_s: int) -> None: ...

    def clear(self) -> None: ...


class MemoryCache:
    """
    In-process TTL cache.

    Dies on Cloud Run's scale-to-zero, which is expected: this is tier one, and
    it absorbs the common "click the same building twice / toggle a layer"
    pattern for free. A persistent tier behind it survives cold starts.
    """

    def __init__(self, max_entries: int = 512):
        from cachetools import TTLCache

        self._max_entries = max_entries
        # One cache per TTL, because TTLCache fixes its TTL at construction.
        self._caches: dict[int, TTLCache] = {}
        self._lock = threading.Lock()
        self.stats = CacheStats()

    def _cache_for(self, ttl_s: int):
        from cachetools import TTLCache

        if ttl_s not in self._caches:
            self._caches[ttl_s] = TTLCache(maxsize=self._max_entries, ttl=max(ttl_s, 1))
        return self._caches[ttl_s]

    def get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            for cache in self._caches.values():
                if key in cache:
                    self.stats.hits += 1
                    return cache[key]
            self.stats.misses += 1
            return None

    def set(self, key: str, value: dict[str, Any], ttl_s: int) -> None:
        if ttl_s <= 0:
            return
        with self._lock:
            self._cache_for(ttl_s)[key] = value
            self.stats.stores += 1

    def clear(self) -> None:
        with self._lock:
            self._caches.clear()


class NullCache:
    """Disabled cache, for tests and for local debugging of a cold path."""

    def __init__(self) -> None:
        self.stats = CacheStats()

    def get(self, key: str) -> dict[str, Any] | None:
        self.stats.bypasses += 1
        return None

    def set(self, key: str, value: dict[str, Any], ttl_s: int) -> None:
        return None

    def clear(self) -> None:
        return None


_BACKEND: CacheBackend | None = None
_BACKEND_LOCK = threading.Lock()


def get_cache() -> CacheBackend:
    """The process-wide cache backend, built from settings on first use."""
    global _BACKEND
    if _BACKEND is None:
        with _BACKEND_LOCK:
            if _BACKEND is None:
                from solaris.core.config import get_settings

                settings = get_settings()
                if settings.cache_backend == "memory":
                    _BACKEND = MemoryCache(max_entries=settings.cache_max_entries)
                else:
                    # Firestore is the intended persistent tier; until it is
                    # wired, an unknown backend degrades to no caching rather
                    # than failing a request.
                    _BACKEND = NullCache()
    return _BACKEND


def reset_cache() -> None:
    """Drop the backend, so the next call rebuilds it. Used by tests."""
    global _BACKEND
    with _BACKEND_LOCK:
        if _BACKEND is not None:
            _BACKEND.clear()
        _BACKEND = None


@dataclass
class CacheOutcome:
    """What the cache did, for the response headers."""

    key: str
    status: str  # HIT | MISS | BYPASS
    value: dict[str, Any] | None = None

    @property
    def headers(self) -> dict[str, str]:
        return {"X-Cache": self.status, "X-Cache-Key": self.key}


def lookup(
    endpoint: str,
    payload: dict[str, Any],
    *,
    start_date: str,
    end_date_exclusive: str,
) -> CacheOutcome:
    """Check the cache for this computation."""
    key = cache_key(endpoint, payload, start_date=start_date, end_date_exclusive=end_date_exclusive)
    if not is_cacheable_window(end_date_exclusive):
        return CacheOutcome(key=key, status="BYPASS")
    value = get_cache().get(key)
    if value is None:
        return CacheOutcome(key=key, status="MISS")
    return CacheOutcome(key=key, status="HIT", value=value)


def store(
    outcome: CacheOutcome,
    value: dict[str, Any],
    *,
    end_date_exclusive: str,
    ttl_s: int,
    coverage_complete: bool = True,
) -> None:
    """
    Store a successful result.

    Two independent reasons to decline, and both are needed:

    ``is_cacheable_window``  the window is not yet closed in calendar terms.
    ``coverage_complete``    the window is closed but the data behind it is
                             not all published yet -- ERA5-Land lags real time,
                             so a window ending last week can still be partial.

    Caching a partial result would pin it for the whole TTL, which is worse
    than not caching: the answer would stay wrong after the data arrived.
    """
    if outcome.status == "BYPASS" or not is_cacheable_window(end_date_exclusive):
        return
    if not coverage_complete:
        return
    get_cache().set(outcome.key, value, ttl_s)


def cache_age_seconds(value: dict[str, Any]) -> float | None:
    """Age of a cached payload, if it carries a timestamp."""
    stamped = value.get("_cached_at")
    return time.time() - stamped if isinstance(stamped, int | float) else None
