"""
HTTP middleware: request identity, rate limiting, structured logging, and
error handling.

Error handling is the part worth reading
----------------------------------------
Every handler previously ended in
``except Exception as e: raise HTTPException(500, detail=str(e))``. That put raw
Earth Engine exception text into the response body, which leaks project ids and
asset paths to any caller -- the deployed instance answered a bad request with
``"Caller does not have required permission to use project pv-mapping-india"``.
Handlers now return a typed error with a request id; the detail goes to the log.

Identity, and why not IP alone
------------------------------
Rate limiting is keyed on an opaque per-session token where one is presented,
falling back to the client IP. IP alone is weak in India specifically: mobile
carriers use carrier-grade NAT, so thousands of subscribers share one egress
address. An IP-keyed limit restricts them collectively while one determined
caller rotates addresses.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from solaris.api.auth import AllowanceExceededError
from solaris.api.deps import EarthEngineUnavailableError
from solaris.core.config import Settings
from solaris.core.limits import (
    BudgetExceededError,
    ConcurrencyTimeoutError,
    RateLimitExceededError,
    check_rate_limit,
)

#: Per-request context, so log lines can be correlated without threading an
#: argument through every function.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("request_id", default="-")


@dataclass
class RequestCounters:
    """
    Earth Engine usage for one request.

    A **mutable object** held in a contextvar, rather than two plain numbers,
    and that is the whole point of the type.

    ``BaseHTTPMiddleware`` runs the downstream app in a separate task, so it
    gets a *copy* of the context. A handler calling ``var.set(n)`` rebinds the
    name in its own copy and the middleware never sees it -- which is exactly
    what happened: ``ee_calls`` was logged as ``0`` on every request from the
    moment it was added, so the field advertised as the quota currency always
    read zero.

    Rebinding does not cross the boundary, but *mutation* does: the middleware
    binds one instance before ``call_next`` and both sides hold a reference to
    the same object.
    """

    calls: int = 0
    #: Charged against the daily budget, in the units of
    #: ``constants.EE_COST_UNITS``. Logged beside ``calls`` because the two
    #: answer different questions: round-trip count is nearly constant across
    #: temporal modes, while cost is what Earth Engine bills, so a yearly and a
    #: daily query are identical in ``calls`` and differ 120-fold in ``cost``.
    cost: float = 0.0


#: ``None`` outside a request, so work done off the request path -- a CLI, a
#: warm-up -- records nothing rather than accumulating into a shared default.
ee_counters_var: contextvars.ContextVar[RequestCounters | None] = contextvars.ContextVar(
    "ee_counters", default=None
)

logger = logging.getLogger("solaris")


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


class CloudLoggingFormatter(logging.Formatter):
    """
    JSON lines in the shape Cloud Logging expects.

    Two details that are easy to get wrong and both matter: the level field
    must be ``severity``, not ``level``, and the trace field must be
    ``logging.googleapis.com/trace`` -- that is what makes Cloud Logging group
    every line from one request together in the console.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "request_id": request_id_var.get(),
        }
        trace = getattr(record, "trace", None)
        if trace:
            payload["logging.googleapis.com/trace"] = trace
        for key, value in getattr(record, "extra_fields", {}).items():
            payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(settings: Settings) -> None:
    """Install a single handler on the ``solaris`` logger."""
    handler = logging.StreamHandler(sys.stdout)
    if settings.log_format == "json":
        handler.setFormatter(CloudLoggingFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(levelname)-8s %(name)s: %(message)s"))
    logger.handlers = [handler]
    logger.setLevel(settings.log_level)
    logger.propagate = False


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


def client_ip(request: Request) -> str:
    """
    The client address.

    ``X-Forwarded-For`` is trusted only because Cloud Run overwrites any
    client-supplied value; the first hop is the real client. Behind a different
    proxy this assumption needs revisiting.
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def request_identity(request: Request) -> tuple[str, str]:
    """
    ``(identity, kind)`` for rate limiting.

    Prefers the opaque guest token over the client address. See the module
    docstring for why IP alone is a poor key here.
    """
    identity = getattr(request.state, "identity", None)
    if identity is not None:
        return identity.key, identity.key.split(":", 1)[0]
    return f"ip:{client_ip(request)}", "ip"


def attach_identity(request: Request) -> None:
    """
    Resolve the caller and record it on ``request.state``.

    Done in middleware rather than as a route dependency so every path gets a
    consistent identity, including the rate limiter, which runs before any
    route function.
    """
    from solaris.api.auth import GUEST_HEADER, resolve_identity

    request.state.identity = resolve_identity(
        guest_token=request.headers.get(GUEST_HEADER),
        client_ip_address=client_ip(request),
    )


def cloud_trace(request: Request) -> str | None:
    """Extract the Cloud Trace id, so log lines group per request."""
    header = request.headers.get("x-cloud-trace-context")
    if not header:
        return None
    return header.split("/")[0]


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

#: Paths exempt from rate limiting: they are cheap, touch no Earth Engine, and
#: are what a load balancer or uptime check polls.
EXEMPT_PATHS = frozenset(
    {"/api/health", "/api/ready", "/api/version", "/api/config", "/api/presets"}
)


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Assigns a request id, applies rate limits, and logs one line per request."""

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable]):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)
        counters = RequestCounters()
        counters_token = ee_counters_var.set(counters)
        started = time.perf_counter()
        trace = cloud_trace(request)

        identity, identity_kind = "ip:pending", "ip"

        try:
            attach_identity(request)
            identity, identity_kind = request_identity(request)
            if request.url.path not in EXEMPT_PATHS:
                check_rate_limit(identity)
            response = await call_next(request)
            status = response.status_code
        except AllowanceExceededError as exc:
            status = 403
            response = _error_response(
                status,
                "allowance_exhausted",
                str(exc),
                request_id,
                extra={"used": exc.used, "allowance": exc.allowance},
            )
        except RateLimitExceededError as exc:
            status = 429
            response = _error_response(
                status,
                "rate_limited",
                str(exc),
                request_id,
                headers={"Retry-After": str(exc.retry_after_s)},
            )
        except BudgetExceededError as exc:
            # Deliberately logged at ERROR: this is the signal that the day's
            # Earth Engine allowance is gone, and it is one of the two things
            # worth alerting on.
            status = 503
            logger.error(
                "earth engine budget exhausted",
                extra={"extra_fields": {"identity": identity}},
            )
            response = _error_response(
                status,
                "budget_exhausted",
                str(exc),
                request_id,
                headers={"Retry-After": "3600"},
            )
        except ConcurrencyTimeoutError as exc:
            status = 503
            response = _error_response(
                status, "busy", str(exc), request_id, headers={"Retry-After": "5"}
            )
        except EarthEngineUnavailableError as exc:
            # 503, not 500: the server is fine, its Earth Engine configuration
            # is not. The hint is included only outside production, because it
            # is written for whoever is configuring the service and a public
            # deployment should not narrate its own setup to arbitrary callers.
            status = 503
            logger.error(
                "earth engine unavailable",
                extra={"extra_fields": {"hint": exc.hint}},
            )
            from solaris.core.config import get_settings

            extra = {} if get_settings().is_production else {"hint": exc.hint}
            response = _error_response(
                status,
                "earth_engine_unavailable",
                str(exc),
                request_id,
                headers={"Retry-After": "30"},
                extra=extra,
            )
        except Exception:
            # The detail goes to the log, never to the client: raw Earth Engine
            # messages carry project ids and asset paths.
            status = 500
            logger.exception("unhandled error", extra={"extra_fields": {"path": request.url.path}})
            response = _error_response(
                status,
                "internal_error",
                "An internal error occurred. Quote the request id when reporting it.",
                request_id,
            )

        duration_ms = (time.perf_counter() - started) * 1000.0
        response.headers["X-Request-ID"] = request_id

        logger.info(
            "%s %s -> %s",
            request.method,
            request.url.path,
            status,
            extra={
                "extra_fields": {
                    "method": request.method,
                    "path": request.url.path,
                    "status": status,
                    "duration_ms": round(duration_ms, 1),
                    "identity_kind": identity_kind,
                    # The quota currency. This is the most useful number in the
                    # whole system: it turns "why is this slow or expensive"
                    # into a single query.
                    "ee_calls": counters.calls,
                    # What the day's budget was actually charged. This is the
                    # field to correlate against the Earth Engine console
                    # meter, because it is denominated the way the quota is.
                    "ee_cost": round(counters.cost, 3),
                    "cache": response.headers.get("X-Cache", "-"),
                },
                "trace": trace,
            },
        )

        request_id_var.reset(token)
        ee_counters_var.reset(counters_token)
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline response headers. Cheap, and each one closes a real gap."""

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable]):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response


def _error_response(
    status: int,
    code: str,
    message: str,
    request_id: str,
    headers: dict[str, str] | None = None,
    extra: Mapping[str, object] | None = None,
) -> JSONResponse:
    """
    One error shape for every failure mode.

    ``extra`` carries machine-readable detail the frontend acts on -- the guest
    allowance counts, for instance, so the sign-in prompt can say how many
    computations were used rather than just that something was refused.
    """
    error: dict[str, object] = {"code": code, "message": message}
    if extra:
        error.update(extra)
    return JSONResponse(
        status_code=status,
        content={"error": error, "request_id": request_id},
        headers=headers or {},
    )


def record_ee_calls(n: int = 1) -> None:
    """Add to this request's Earth Engine round-trip count."""
    counters = ee_counters_var.get()
    if counters is not None:
        counters.calls += n


def record_ee_cost(units: float) -> None:
    """Add to this request's charge against the daily compute budget."""
    counters = ee_counters_var.get()
    if counters is not None:
        counters.cost += units


def current_counters() -> RequestCounters:
    """This request's usage so far, or an empty tally outside a request."""
    return ee_counters_var.get() or RequestCounters()


def install(app: FastAPI, settings: Settings) -> None:
    """Configure logging and attach the middleware stack."""
    configure_logging(settings)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(ObservabilityMiddleware)
    logger.info("solaris starting", extra={"extra_fields": {"config": settings.redacted()}})
