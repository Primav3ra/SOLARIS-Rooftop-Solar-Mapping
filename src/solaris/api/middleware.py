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
Rate limiting is keyed on the signed-in subject when present, falling back to
the client IP. IP alone is weak in India specifically: mobile carriers use
carrier-grade NAT, so thousands of users share one egress address. An IP-keyed
limit punishes them collectively while one determined user simply rotates
addresses.
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

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
ee_calls_var: contextvars.ContextVar[int] = contextvars.ContextVar("ee_calls", default=0)

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

    Prefers a verified subject over an IP. See the module docstring for why IP
    alone is a poor key here.
    """
    subject = getattr(request.state, "subject", None)
    if subject:
        return f"sub:{subject}", "subject"
    return f"ip:{client_ip(request)}", "ip"


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
        calls_token = ee_calls_var.set(0)
        started = time.perf_counter()
        trace = cloud_trace(request)

        identity, identity_kind = request_identity(request)

        try:
            if request.url.path not in EXEMPT_PATHS:
                check_rate_limit(identity)
            response = await call_next(request)
            status = response.status_code
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
                    "ee_calls": ee_calls_var.get(),
                    "cache": response.headers.get("X-Cache", "-"),
                },
                "trace": trace,
            },
        )

        request_id_var.reset(token)
        ee_calls_var.reset(calls_token)
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
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": {"code": code, "message": message},
            "request_id": request_id,
        },
        headers=headers or {},
    )


def record_ee_calls(n: int = 1) -> None:
    """Add to this request's Earth Engine call count."""
    ee_calls_var.set(ee_calls_var.get() + n)


def install(app: FastAPI, settings: Settings) -> None:
    """Configure logging and attach the middleware stack."""
    configure_logging(settings)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(ObservabilityMiddleware)
    logger.info("solaris starting", extra={"extra_fields": {"config": settings.redacted()}})
