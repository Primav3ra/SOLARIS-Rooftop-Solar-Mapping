"""
Typed application configuration.

Replaces scattered ``os.environ.get`` calls with one validated object, so a
missing or malformed setting fails at startup with a clear message instead of
surfacing as a 500 on the first request.

Two decisions worth stating outright:

**The Earth Engine project is server configuration, never a request field.**
It used to arrive in every request body, which let any caller choose which GCP
project this server initialises Earth Engine against -- an enumeration oracle,
and a way to attribute someone else's quota and billing.

**Request limits live here, not in the handlers.** They are the primary defence
against Earth Engine quota exhaustion: validation runs before any ``ee`` object
is constructed, so an oversized area of interest costs nothing. No amount of
per-IP rate limiting helps against a single well-formed request for a region
the size of a continent.
"""

from __future__ import annotations

import functools
import pathlib
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Environment = Literal["local", "ci", "prod"]
AuthMode = Literal["auto", "adc", "sa_json", "sa_file", "local"]
CacheBackend = Literal["memory", "firestore", "none"]


def _find_env_file() -> tuple[str, ...]:
    """
    Locate ``.env`` relative to the installed package, not the process cwd.

    A bare ``env_file=".env"`` resolves against the working directory, so
    ``solaris-api`` picked up a ``.env`` only when launched from the repo root
    and **silently ignored it everywhere else** -- falling back to the default
    Earth Engine project and producing a permission error that named a project
    the user had never chosen. Exactly the failure mode the relative
    ``StaticFiles`` path used to have, and equally invisible: nothing warns you
    that your configuration was not read.

    Walks up from this module looking for a directory that holds both a
    ``.env`` and a ``pyproject.toml``, so it finds the *project's* file and not
    some unrelated ``.env`` further up the filesystem. The cwd-relative path is
    kept last, so an explicit local file still wins in a deployment that
    arranges one.
    """
    candidates: list[str] = []
    for parent in pathlib.Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            candidates.append(str(parent / ".env"))
            break
    candidates.append(".env")
    return tuple(candidates)


#: Resolved once at import. A tuple, because pydantic-settings accepts several
#: and applies them in order.
ENV_FILES = _find_env_file()


class Settings(BaseSettings):
    """
    Application settings, read from the environment and ``.env``.

    Frozen, so nothing can mutate configuration mid-request, and cached by
    :func:`get_settings` so validation happens once per process.
    """

    model_config = SettingsConfigDict(
        env_prefix="SOLARIS_",
        env_file=ENV_FILES,
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
        populate_by_name=True,
    )

    # -- identity ---------------------------------------------------------

    env: Environment = "local"

    #: Git SHA of the running build. Surfaced by /api/version so a reported
    #: figure can be traced back to the code that produced it.
    git_sha: str = "unknown"

    # -- Earth Engine -----------------------------------------------------

    #: GCP project for all Earth Engine calls. Must have the Earth Engine API
    #: enabled *and* be registered (noncommercial or commercial).
    gee_project_id: str = Field(default="pv-mapping-india", alias="GEE_PROJECT_ID")

    #: ``auto`` tries, in order: inline key, key file, Application Default
    #: Credentials, then local ``earthengine authenticate`` credentials.
    #: The explicit modes exist so CI can force ADC and fail loudly rather than
    #: silently falling back to a developer's cached credentials.
    gee_auth_mode: AuthMode = "auto"
    gee_service_account: str | None = Field(default=None, alias="GEE_SERVICE_ACCOUNT")
    gee_sa_json: SecretStr | None = Field(default=None, alias="GEE_SA_JSON")
    gee_sa_key_file: str | None = Field(default=None, alias="GEE_SA_KEY_FILE")

    # -- HTTP -------------------------------------------------------------

    #: Allowed CORS origins. The dashboard is served same-origin from this same
    #: app, so this only matters for local development and any separately
    #: hosted frontend.
    #:
    #: ``NoDecode`` is load-bearing, not decoration.
    #:
    #: pydantic-settings treats a ``list`` field as complex and tries to
    #: **JSON-decode** the environment value *before* any validator runs. So
    #: the comma-separated form documented in ``.env.example`` and in the
    #: deployment runbook -- ``SOLARIS_CORS_ORIGINS=https://a,https://b`` --
    #: raised ``SettingsError`` at startup, and the ``mode="before"`` validator
    #: below never got a chance to split it. The failure was invisible locally
    #: because the default is used when the variable is unset, so it would have
    #: surfaced for the first time on the production deploy that set it.
    #:
    #: ``NoDecode`` suppresses that JSON attempt and hands the raw string to the
    #: validator, which is what makes the documented syntax actually work.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:8000", "http://127.0.0.1:8000"]
    )

    #: Per-request ceiling. Note this frees the *client*, not the worker
    #: thread: ``getInfo()`` is uninterruptible blocking I/O, so
    #: ``max_concurrent_ee_calls`` is what actually bounds resource use.
    request_timeout_s: int = Field(default=90, ge=5, le=600)

    # -- limits: the real quota guard -------------------------------------

    #: Maximum concurrent Earth Engine calls per process.
    #:
    #: Every endpoint is a sync ``def``, so FastAPI runs them on a threadpool
    #: of 40. Without this cap one instance can have 40 blocking ``getInfo()``
    #: calls in flight at once, which is the actual concurrency hazard.
    max_concurrent_ee_calls: int = Field(default=4, ge=1, le=40)

    #: Global daily ceiling on Earth Engine round-trips.
    #:
    #: Denominated in the resource actually being consumed rather than in HTTP
    #: requests, which is why it protects the quota where a request-rate limit
    #: would not.
    daily_ee_call_budget: int = Field(default=5000, ge=0)

    #: Requests per identity per window. Keyed on the signed-in subject where
    #: available, falling back to client IP.
    rate_limit_per_minute: int = Field(default=30, ge=0)
    rate_limit_per_day: int = Field(default=300, ge=0)

    #: Computations a guest may run before being asked to sign in. Cached
    #: results do not count, which makes the cache a UX feature as well as a
    #: cost control.
    #:
    #: Ten rather than three. Comparing sites is the primary task -- a single
    #: rooftop figure is close to meaningless without another to compare it
    #: against -- and an allowance of three refused the fourth site, which put
    #: the limit below the smallest useful session. Ten covers a realistic
    #: comparison of five or six roofs with room for re-runs at different
    #: windows, while still bounding a single visitor's cost. The global daily
    #: call budget remains the actual spend guard.
    guest_computation_allowance: int = Field(default=10, ge=0)

    # -- cache ------------------------------------------------------------

    cache_backend: CacheBackend = "memory"
    cache_max_entries: int = Field(default=512, ge=1)

    #: 30 days. Safe because ERA5, MODIS and Open Buildings for a *past*
    #: window are immutable -- the only thing that invalidates a result is a
    #: change to our own algorithm, which the cache key versions handle.
    cache_ttl_result_s: int = Field(default=2_592_000, ge=0)

    #: 6 hours. Shorter because a tile response contains an Earth Engine map
    #: token, which expires.
    cache_ttl_tiles_s: int = Field(default=21_600, ge=0)

    firestore_collection: str = "solaris-cache"

    # -- observability ----------------------------------------------------

    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "text"
    sentry_dsn: SecretStr | None = None

    # -- validators -------------------------------------------------------

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value):
        """
        Accept a comma-separated string, which is how env vars arrive.

        A JSON array is also accepted, since that is what pydantic-settings
        would have expected and someone following its conventions rather than
        ours should not be punished for it.
        """
        if isinstance(value, str):
            text = value.strip()
            if text.startswith("["):
                import json

                try:
                    return json.loads(text)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        "cors_origins looked like JSON but did not parse. Use a "
                        "comma-separated list instead, e.g. "
                        "SOLARIS_CORS_ORIGINS=https://a.example,https://b.example"
                    ) from exc
            return [origin.strip() for origin in text.split(",") if origin.strip()]
        return value

    @field_validator("cors_origins")
    @classmethod
    def _reject_wildcard(cls, value: list[str]) -> list[str]:
        """
        A wildcard origin is rejected outright.

        The previous configuration paired ``allow_origins=["*"]`` with
        ``allow_credentials=True``, which is invalid per the CORS spec --
        browsers reject a wildcard origin when credentials are sent -- so it
        never granted the access it appeared to. Nothing here uses cookies or
        auth headers, so an explicit origin list costs nothing.
        """
        if "*" in value:
            raise ValueError(
                "cors_origins must not contain '*'. List the origins explicitly; "
                "the dashboard is served same-origin and needs no wildcard."
            )
        return value

    @field_validator("log_level")
    @classmethod
    def _valid_log_level(cls, value: str) -> str:
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
        upper = value.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return upper

    # -- derived ----------------------------------------------------------

    @property
    def is_production(self) -> bool:
        return self.env == "prod"

    @property
    def cache_enabled(self) -> bool:
        return self.cache_backend != "none"

    def redacted(self) -> dict[str, object]:
        """
        Settings safe to log or expose. Secrets are reported present/absent,
        never by value.
        """
        return {
            "env": self.env,
            "git_sha": self.git_sha,
            # Which .env was actually read, if any. The most common local
            # confusion is configuration that was never loaded, and this makes
            # that visible instead of leaving it to be inferred from a
            # surprising project id.
            "env_file_loaded": next(
                (path for path in ENV_FILES if pathlib.Path(path).is_file()), None
            ),
            "gee_project_id": self.gee_project_id,
            "gee_auth_mode": self.gee_auth_mode,
            "gee_credentials_configured": bool(self.gee_sa_json or self.gee_sa_key_file),
            "cors_origins": self.cors_origins,
            "cache_backend": self.cache_backend,
            "max_concurrent_ee_calls": self.max_concurrent_ee_calls,
            "daily_ee_call_budget": self.daily_ee_call_budget,
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "guest_computation_allowance": self.guest_computation_allowance,
            "log_level": self.log_level,
            "sentry_configured": self.sentry_dsn is not None,
        }


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """
    The process-wide settings object.

    Cached so validation runs once. Tests override by calling
    ``get_settings.cache_clear()`` after patching the environment.
    """
    return Settings()
