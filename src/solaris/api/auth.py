"""
Guest sessions and the per-session computation allowance.

Why there is no sign-in
-----------------------
Google sign-in was implemented and then removed. It verified an ID token and
used the ``sub`` claim as the rate-limit key in place of the client IP. The
removal is recorded here because the reasoning generalises: the feature came
from the plan rather than from a demonstrated need, and on inspection it
delivered nothing a visitor could use.

It did not do the thing it appeared to do. The OAuth client belongs to this
project, so ``ee.Initialize(project=ours)`` bills Earth Engine calls here
whether or not the caller is authenticated -- signing in moved no quota.
Shifting the cost would require the visitor to supply their own Earth
Engine-*registered* Cloud project, and Earth Engine access is not conferred by
owning a Google account: a project needs the API enabled plus a noncommercial
or commercial registration.

What remained was a better rate-limit key. IP is a poor key in India, where
carrier-grade NAT places thousands of subscribers behind one egress address, so
an IP-keyed limit restricts them collectively while one determined caller
rotates addresses. That argument only bites at traffic this service does not
receive, and the opaque guest token below already provides per-session keying
without asking anyone to authenticate.

There was also no persistence to attach an identity to -- no stored sites, no
profile, nothing surviving a session -- and no interface element from which to
sign in, so the exhausted-allowance message recommended an action the
application could not perform.

Reinstating it would be justified alongside a persistence tier, or on evidence
that IP-keyed limiting was misfiring. The ``earthengine`` scope required for
the only variant that *would* move quota is classed sensitive, requiring Google
verification review and capping an unverified application at roughly a hundred
users.

The guest allowance
-------------------
Computations per browser session, tracked against an opaque server-issued
token. Cached results do not decrement it, so revisiting an already-computed
site is free -- which makes the cache a visible feature rather than only a cost
control.

The token is not a credential. It grants nothing beyond the allowance; its sole
purpose is to make that allowance per-session rather than per-address.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("solaris.auth")

#: How long a guest session record lives. Long enough to survive a browsing
#: session, short enough that the in-process table cannot grow without bound.
GUEST_SESSION_TTL_S = 24 * 3600

#: Header carrying the opaque guest token issued by ``POST /api/auth/guest``.
GUEST_HEADER = "x-solaris-guest"


class AllowanceExceededError(RuntimeError):
    """A session has used up its computation allowance."""

    def __init__(self, message: str, used: int, allowance: int):
        super().__init__(message)
        self.used = used
        self.allowance = allowance


@dataclass
class Identity:
    """
    Who is making this request, for rate limiting and the allowance.

    ``key`` carries a type prefix so a guest token cannot collide with an IP
    address. ``allowance_key`` is the bare token, matching what
    :meth:`GuestAllowance.issue` returned.

    The two are separate deliberately. An earlier version charged the prefixed
    key while ``issue`` registered the bare one, so every session silently
    started a second, unregistered record and the expiry sweep collected
    nothing.
    """

    key: str
    allowance_key: str

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.key.split(":", 1)[0]}


@dataclass
class _GuestRecord:
    created_at: float
    used: int = 0


@dataclass
class GuestAllowance:
    """
    Counts the computations a session has spent.

    In-process, and honestly so: with ``--max-instances=2`` a determined
    visitor could obtain two allowances by being routed to both instances. That
    is accepted. The allowance paces a casual visitor; the actual spend guard is
    the global daily Earth Engine call budget in :mod:`solaris.core.limits`,
    which *is* shared across instances. Putting this counter in Firestore would
    add a write to every request to tighten a limit whose worst case is already
    bounded at twice the intended value.

    Expired records are swept on access rather than by a background task; the
    table is small and the sweep is cheap.
    """

    allowance: int
    ttl_s: int = GUEST_SESSION_TTL_S
    _sessions: dict[str, _GuestRecord] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _sweep(self, now: float) -> None:
        stale = [k for k, r in self._sessions.items() if now - r.created_at > self.ttl_s]
        for key in stale:
            del self._sessions[key]

    def issue(self) -> str:
        """Mint a new opaque guest token."""
        token = secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            self._sweep(now)
            self._sessions[token] = _GuestRecord(created_at=now)
        return token

    def known(self, token: str) -> bool:
        with self._lock:
            return token in self._sessions

    def used(self, token: str) -> int:
        with self._lock:
            record = self._sessions.get(token)
            return record.used if record else 0

    def remaining(self, token: str) -> int:
        return max(0, self.allowance - self.used(token))

    def check(self, token: str) -> None:
        """
        Raise if this session has no computations left.

        Checked *before* the work, so a session over its allowance costs no
        Earth Engine calls.
        """
        if not self.allowance:
            return
        with self._lock:
            record = self._sessions.get(token)
            used = record.used if record else 0
        if used >= self.allowance:
            # The remedies named are the ones that exist. An earlier version
            # recommended signing in, which no interface could perform.
            raise AllowanceExceededError(
                f"This session has used its {self.allowance} computations. "
                f"Previously computed sites remain available and cost nothing "
                f"to revisit; a new session resets the allowance.",
                used=used,
                allowance=self.allowance,
            )

    def spend(self, token: str, n: int = 1) -> int:
        """
        Record ``n`` computations. Returns the new used count.

        Called only on a cache **miss**, which is what makes a cached result
        free.
        """
        now = time.time()
        with self._lock:
            record = self._sessions.get(token)
            if record is None:
                record = _GuestRecord(created_at=now)
                self._sessions[token] = record
            record.used += n
            return record.used

    def reset(self) -> None:
        with self._lock:
            self._sessions.clear()

    def as_dict(self) -> dict[str, Any]:
        with self._lock:
            return {"allowance": self.allowance, "active_sessions": len(self._sessions)}


_ALLOWANCE: GuestAllowance | None = None
_ALLOWANCE_LOCK = threading.Lock()


def get_guest_allowance() -> GuestAllowance:
    global _ALLOWANCE
    if _ALLOWANCE is None:
        with _ALLOWANCE_LOCK:
            if _ALLOWANCE is None:
                from solaris.core.config import get_settings

                _ALLOWANCE = GuestAllowance(allowance=get_settings().guest_computation_allowance)
    return _ALLOWANCE


def reset_guest_allowance() -> None:
    """Drop all sessions, so the next call rebuilds the store. Used by tests."""
    global _ALLOWANCE
    with _ALLOWANCE_LOCK:
        if _ALLOWANCE is not None:
            _ALLOWANCE.reset()
        _ALLOWANCE = None


def resolve_identity(guest_token: str | None, client_ip_address: str) -> Identity:
    """
    Turn request headers into an :class:`Identity`.

    A presented guest token keys the session. Without one the request falls
    back to the client address -- the weaker key, per the module docstring on
    carrier-grade NAT, but one that requires nothing of the visitor.
    """
    if guest_token:
        return Identity(key=f"guest:{guest_token}", allowance_key=guest_token)
    return Identity(key=f"ip:{client_ip_address}", allowance_key=f"ip:{client_ip_address}")


__all__ = [
    "GUEST_HEADER",
    "AllowanceExceededError",
    "GuestAllowance",
    "Identity",
    "get_guest_allowance",
    "reset_guest_allowance",
    "resolve_identity",
]
