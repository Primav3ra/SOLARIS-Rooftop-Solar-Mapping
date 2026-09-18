"""
The persistent cache tier, and the shared Earth Engine budget counter.

Why Firestore and not Redis
---------------------------
The in-process :class:`~solaris.core.cache.MemoryCache` dies with the instance,
and Cloud Run is configured to scale to zero -- so with memory alone the first
request after any idle period is always a miss, which is most requests for a
project with this traffic profile. A persistent tier is what makes the cache
worth having at all.

Memorystore (Redis) would need a Serverless VPC connector and bills roughly
$35/month always-on, which directly contradicts the scale-to-zero cost goal.
Firestore's free tier covers this workload, needs no VPC, and has two
properties that happen to be exactly what is needed here:

* a **native TTL policy** -- expired documents are deleted server-side, so
  there is no cleanup job and no cron to forget about, and
* ``Increment`` as an atomic field transform, which is what the daily Earth
  Engine budget needs in order to be genuinely global across instances rather
  than per-instance.

One dependency serving both is why it wins on more than price.

Degradation, deliberately total
-------------------------------
Every operation here is wrapped. A cache is an optimisation; if Firestore is
unreachable, misconfigured, or the client library is absent, the correct
behaviour is to compute the answer and serve it, not to fail the request. The
failures are counted and surfaced through ``/api/config`` so a silently dead
cache tier is visible rather than merely slow -- the same honesty constraint
this project applies to every other fallback.

The budget counter degrades the *other* way round. If the shared counter cannot
be read, the local in-process budget still applies: losing the global ceiling
must not remove the local one, because a budget is a spend guard rather than an
optimisation.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

log = logging.getLogger("solaris.cache")

#: Document field holding the cached payload.
FIELD_VALUE = "value"

#: Document field holding the expiry instant. This is the field a Firestore TTL
#: policy must be configured against (see ``docs/deployment.md``). Without that
#: policy the documents are still *logically* expired by the check in
#: :meth:`FirestoreCache.get`, they are simply never deleted -- so a missing
#: policy costs storage, not correctness.
FIELD_EXPIRES = "expires_at"

FIELD_STORED = "stored_at"


@dataclass
class _Health:
    """Whether the persistent tier is actually working, and why not."""

    available: bool = False
    reason: str = "not initialised"
    reads: int = 0
    writes: int = 0
    errors: int = 0
    last_error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "reads": self.reads,
            "writes": self.writes,
            "errors": self.errors,
            "last_error": self.last_error,
        }


class FirestoreCache:
    """
    A persistent cache backed by one Firestore collection.

    Implements the same narrow protocol as :class:`MemoryCache` -- ``get``,
    ``set``, ``clear``, plus a ``stats`` object -- so it is a drop-in
    replacement and the calling code never learns which tier answered.

    The client is constructed lazily on first use rather than at import: a
    process that never serves a request (a test run, a ``--help`` invocation)
    should not open a network connection, and a construction failure should
    surface as a degraded cache rather than an import error.
    """

    def __init__(self, collection: str, project: str | None = None):
        from solaris.core.cache import CacheStats

        self.collection_name = collection
        self.project = project
        self.stats = CacheStats()
        self.health = _Health()
        self._client: Any = None
        self._lock = threading.Lock()
        self._attempted = False

    # -- client -----------------------------------------------------------

    def _collection(self):
        """
        The collection handle, or ``None`` if the tier is unavailable.

        Note the ``try`` around the *warm* path as well as construction. A
        client that constructs fine but whose ``collection()`` raises -- a
        revoked credential, a deleted database -- would otherwise escape into
        the request and turn a cache problem into a 500.
        """
        if self._client is not None:
            try:
                return self._client.collection(self.collection_name)
            except Exception as exc:
                self._record_error(exc)
                return None
        if self._attempted:
            return None
        with self._lock:
            if self._attempted:
                return None
            self._attempted = True
            try:
                from google.cloud import firestore

                self._client = (
                    firestore.Client(project=self.project) if self.project else firestore.Client()
                )
                self.health.available = True
                self.health.reason = "ok"
            except ImportError:
                self.health.reason = (
                    "google-cloud-firestore is not installed; install the 'firestore' "
                    "extra to enable the persistent cache tier"
                )
                log.info("firestore cache unavailable: %s", self.health.reason)
                return None
            except Exception as exc:  # pragma: no cover - needs a real project
                self.health.reason = f"client construction failed: {type(exc).__name__}"
                self.health.last_error = str(exc)[:200]
                log.warning("firestore cache unavailable: %s", self.health.reason)
                return None
        return self._client.collection(self.collection_name)

    # -- protocol ---------------------------------------------------------

    def get(self, key: str) -> dict[str, Any] | None:
        collection = self._collection()
        if collection is None:
            self.stats.bypasses += 1
            return None
        try:
            snapshot = collection.document(key).get()
        except Exception as exc:
            self._record_error(exc)
            self.stats.bypasses += 1
            return None

        if not snapshot.exists:
            self.stats.misses += 1
            return None

        payload = snapshot.to_dict() or {}
        expires = payload.get(FIELD_EXPIRES)
        # Checked here as well as by the TTL policy: server-side deletion is
        # asynchronous and documented as best-effort within 24 hours, so an
        # expired document can still be readable. Serving it would silently
        # extend every TTL by up to a day.
        if expires is not None and _as_datetime(expires) <= datetime.now(UTC):
            self.stats.misses += 1
            return None

        self.health.reads += 1
        self.stats.hits += 1
        value = payload.get(FIELD_VALUE)
        return value if isinstance(value, dict) else None

    def set(self, key: str, value: dict[str, Any], ttl_s: int) -> None:
        if ttl_s <= 0:
            return
        collection = self._collection()
        if collection is None:
            return
        document = {
            FIELD_VALUE: value,
            FIELD_STORED: datetime.now(UTC),
            FIELD_EXPIRES: datetime.now(UTC) + timedelta(seconds=ttl_s),
        }
        try:
            collection.document(key).set(document)
        except Exception as exc:
            self._record_error(exc)
            return
        self.health.writes += 1
        self.stats.stores += 1

    def clear(self) -> None:
        """
        Delete every document in the collection.

        Only ever called by tests and by an explicit administrative flush; a
        normal deploy invalidates through the versioned key prefix instead,
        which is both cheaper and safer than a bulk delete.
        """
        collection = self._collection()
        if collection is None:
            return
        try:
            for snapshot in collection.list_documents():
                snapshot.delete()
        except Exception as exc:  # pragma: no cover - needs a real project
            self._record_error(exc)

    def _record_error(self, exc: Exception) -> None:
        self.health.errors += 1
        self.health.last_error = f"{type(exc).__name__}: {exc}"[:200]
        log.warning("firestore cache error: %s", self.health.last_error)

    def as_dict(self) -> dict[str, Any]:
        return {
            "backend": "firestore",
            "stats": self.stats.as_dict(),
            "health": self.health.as_dict(),
        }


class TieredCache:
    """
    An in-process cache in front of a persistent one.

    The ordering is what makes this worth the extra type: a repeated request on
    a warm instance is answered from memory with no network hop at all, while a
    cold instance -- the common case under scale-to-zero -- still finds the
    entry in Firestore. A Firestore hit is promoted into memory so the second
    identical request on that instance is free.
    """

    #: Promotion TTL for an entry pulled up from the persistent tier. Short,
    #: because the authoritative expiry lives in Firestore and the memory copy
    #: is only a hop-saver -- it must not be able to outlive the real entry by
    #: much.
    PROMOTION_TTL_S = 3600

    def __init__(self, fast: Any, slow: Any):
        from solaris.core.cache import CacheStats

        self.fast = fast
        self.slow = slow
        self.stats = CacheStats()

    def get(self, key: str) -> dict[str, Any] | None:
        value = self.fast.get(key)
        if value is not None:
            self.stats.hits += 1
            return value
        value = self.slow.get(key)
        if value is not None:
            self.fast.set(key, value, self.PROMOTION_TTL_S)
            self.stats.hits += 1
            return value
        self.stats.misses += 1
        return None

    def set(self, key: str, value: dict[str, Any], ttl_s: int) -> None:
        self.fast.set(key, value, ttl_s)
        self.slow.set(key, value, ttl_s)
        self.stats.stores += 1

    def clear(self) -> None:
        self.fast.clear()
        self.slow.clear()

    def as_dict(self) -> dict[str, Any]:
        slow_detail = self.slow.as_dict() if hasattr(self.slow, "as_dict") else None
        return {
            "backend": "tiered",
            "stats": self.stats.as_dict(),
            "fast": self.fast.stats.as_dict() if hasattr(self.fast, "stats") else None,
            "slow": slow_detail,
        }


# ---------------------------------------------------------------------------
# The shared Earth Engine budget
# ---------------------------------------------------------------------------


@dataclass
class FirestoreBudget:
    """
    A daily Earth Engine call ceiling shared across instances.

    Wraps an in-process :class:`~solaris.core.limits.EarthEngineBudget` and adds
    a Firestore counter incremented atomically with ``Increment``. The local
    budget is checked **first** and is never skipped, so if the shared counter
    is unavailable the instance still has a ceiling -- degrading from a global
    limit to a per-instance one rather than to no limit at all. With
    ``--max-instances=2`` that worst case is bounded at twice the intended
    budget, which is an acceptable failure mode for a spend guard.

    The remote read is cached for :attr:`refresh_s` seconds. Reading the counter
    on every call would add a Firestore round-trip to every Earth Engine
    round-trip, which is a poor trade for a limit that only needs to be
    approximately right near its boundary.
    """

    local: Any
    collection: str
    project: str | None = None
    refresh_s: float = 30.0
    _client: Any = None
    _attempted: bool = False
    _remote_count: int = 0
    _remote_read_at: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _health: _Health = field(default_factory=_Health)

    @property
    def limit(self) -> int:
        return self.local.limit

    def _doc_id(self) -> str:
        return f"budget-{datetime.now(UTC).date().isoformat()}"

    def _document(self):
        if self._client is not None:
            return self._client.collection(self.collection).document(self._doc_id())
        if self._attempted:
            return None
        self._attempted = True
        try:
            from google.cloud import firestore

            self._client = (
                firestore.Client(project=self.project) if self.project else firestore.Client()
            )
            self._health.available = True
            self._health.reason = "ok"
        except Exception as exc:
            self._health.reason = f"unavailable: {type(exc).__name__}"
            self._health.last_error = str(exc)[:200]
            return None
        return self._client.collection(self.collection).document(self._doc_id())

    def consume(self, n: int = 1) -> None:
        """Record ``n`` calls against both the local and the shared counter."""
        # Local first: an exhausted local budget must reject without a network
        # call, and without incrementing the shared counter for work that is
        # not going to happen.
        self.local.consume(n)

        document = self._document()
        if document is None:
            return
        try:
            from google.cloud import firestore

            document.set({"count": firestore.Increment(n), "day": self._doc_id()}, merge=True)
            self._health.writes += 1
        except Exception as exc:
            self._health.errors += 1
            self._health.last_error = f"{type(exc).__name__}: {exc}"[:200]
            return

        remote = self._remote()
        if self.limit and remote > self.limit:
            from solaris.core.limits import BudgetExceededError

            raise BudgetExceededError(
                f"Shared daily Earth Engine budget of {self.limit} calls is exhausted "
                f"({remote} used across all instances). Resets at UTC midnight."
            )

    def _remote(self) -> int:
        """The shared count, refreshed at most every :attr:`refresh_s`."""
        now = time.monotonic()
        with self._lock:
            if self._remote_read_at and now - self._remote_read_at < self.refresh_s:
                return self._remote_count
            self._remote_read_at = now
        document = self._document()
        if document is None:
            return self._remote_count
        try:
            snapshot = document.get()
            self._remote_count = int((snapshot.to_dict() or {}).get("count", 0))
            self._health.reads += 1
        except Exception as exc:
            self._health.errors += 1
            self._health.last_error = f"{type(exc).__name__}: {exc}"[:200]
        return self._remote_count

    @property
    def used(self) -> int:
        """
        The larger of the local and last-known shared counts.

        Reporting the maximum rather than the remote value means a stale or
        unavailable remote read can never *understate* usage.
        """
        return max(self.local.used, self._remote_count)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def as_dict(self) -> dict[str, Any]:
        return {
            "limit": self.limit,
            "used": self.used,
            "remaining": self.remaining,
            "day": datetime.now(UTC).date().isoformat(),
            "scope": "shared" if self._health.available else "instance",
            "shared_counter": self._health.as_dict(),
        }


def _as_datetime(value: Any) -> datetime:
    """Coerce whatever Firestore hands back into an aware datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return datetime.now(UTC) - timedelta(seconds=1)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    # Unrecognised: treat as expired rather than as fresh. A cache that wrongly
    # misses costs a recomputation; one that wrongly hits serves a stale
    # number, which is the failure mode this project exists to remove.
    return datetime.now(UTC) - timedelta(seconds=1)


__all__ = ["FIELD_EXPIRES", "FirestoreBudget", "FirestoreCache", "TieredCache"]
