"""
Tests for guest sessions, the computation allowance, and the persistent cache
tier.

Two properties carry the weight here:

* the allowance must be charged only on a cache **miss**, which is what makes a
  cached result free to revisit, and
* the persistent cache tier must **degrade to computing the answer**. A cache is
  an optimisation; an unreachable one must not fail a request.

Google sign-in was removed. It is asserted absent rather than simply deleted,
because a partially-removed authentication path is worse than either state: the
exhausted-allowance message once recommended signing in with no interface able
to perform it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from solaris.api import auth
from solaris.core import firestore_cache


@pytest.fixture
def client():
    from solaris.api.app import app

    return TestClient(app)


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------


class TestIdentityResolution:
    def test_no_token_falls_back_to_the_client_address(self):
        identity = auth.resolve_identity(guest_token=None, client_ip_address="1.2.3.4")
        assert identity.key == "ip:1.2.3.4"
        assert identity.allowance_key == "ip:1.2.3.4"

    def test_a_token_keys_on_the_session_not_the_address(self):
        """
        The purpose of the token. Behind carrier-grade NAT thousands of
        subscribers share one address, so an IP-keyed allowance would be a
        single allowance for an entire mobile network.
        """
        first = auth.resolve_identity("tok-a", "1.2.3.4")
        second = auth.resolve_identity("tok-b", "1.2.3.4")
        assert first.key != second.key
        assert first.key == "guest:tok-a"

    def test_the_rate_limit_key_is_prefixed_and_the_allowance_key_is_not(self):
        """
        Separate keys, deliberately. The prefix prevents a token colliding with
        an address; the allowance is charged on the bare token, matching what
        ``issue()`` registered. An earlier version mixed the two, so every
        session created a second unregistered record.
        """
        identity = auth.resolve_identity("tok", "1.2.3.4")
        assert identity.key == "guest:tok"
        assert identity.allowance_key == "tok"


class TestSignInIsGone:
    """
    Sign-in was removed; these assert it did not leave fragments behind.

    It verified a Google ID token and keyed rate limiting on the ``sub`` claim.
    It moved no Earth Engine quota -- the OAuth client belongs to this project,
    so calls bill here regardless -- there was no persistence for an identity to
    attach to, and no interface from which to sign in. See the module docstring
    of ``solaris.api.auth``.
    """

    def test_the_verification_helpers_are_absent(self):
        for name in ("verify_google_id_token", "AuthError", "GOOGLE_ISSUERS"):
            assert not hasattr(auth, name), name

    def test_the_identity_carries_no_account_fields(self):
        identity = auth.resolve_identity("tok", "1.2.3.4")
        for name in ("subject", "email", "name", "picture", "is_signed_in", "tier"):
            assert not hasattr(identity, name), name

    def test_no_oauth_client_setting_remains(self):
        from solaris.core.config import Settings

        assert "google_client_id" not in Settings.model_fields

    def test_the_verify_endpoint_is_gone(self, client):
        # 405 rather than 404: the path falls through to the single-page-app
        # static mount, which serves GET only. Either answer means the route
        # no longer exists.
        response = client.post("/api/auth/verify", json={"id_token": "x" * 40})
        assert response.status_code in (404, 405), response.status_code

    def test_a_bearer_token_is_ignored_rather_than_verified(self, client):
        """
        No 401 path: an Authorization header is simply not consulted, so a
        stray one must not affect the response.
        """
        response = client.get("/api/auth/me", headers={"Authorization": "Bearer anything"})
        assert response.status_code == 200

    def test_the_refusal_names_only_remedies_that_exist(self):
        """
        The message previously recommended signing in, which no interface could
        perform. It must now name only reachable remedies.
        """
        allowance = auth.GuestAllowance(allowance=1)
        token = allowance.issue()
        allowance.spend(token)
        with pytest.raises(auth.AllowanceExceededError) as caught:
            allowance.check(token)
        message = str(caught.value)
        assert "Sign in" not in message
        assert "sign in" not in message
        assert "session resets" in message


# ---------------------------------------------------------------------------
# The allowance
# ---------------------------------------------------------------------------


class TestGuestAllowance:
    def test_spends_then_refuses(self):
        allowance = auth.GuestAllowance(allowance=2)
        token = allowance.issue()
        allowance.check(token)
        allowance.spend(token)
        allowance.check(token)
        allowance.spend(token)
        with pytest.raises(auth.AllowanceExceededError) as caught:
            allowance.check(token)
        assert caught.value.used == 2
        assert caught.value.allowance == 2

    def test_sessions_are_independent(self):
        allowance = auth.GuestAllowance(allowance=1)
        a, b = allowance.issue(), allowance.issue()
        allowance.spend(a)
        allowance.check(b)  # must not raise

    def test_zero_allowance_disables_the_check(self):
        """How the test suite and a private deployment turn it off."""
        allowance = auth.GuestAllowance(allowance=0)
        token = allowance.issue()
        for _ in range(10):
            allowance.check(token)
            allowance.spend(token)

    def test_tokens_are_unguessable(self):
        allowance = auth.GuestAllowance(allowance=1)
        tokens = {allowance.issue() for _ in range(50)}
        assert len(tokens) == 50
        assert all(len(t) >= 24 for t in tokens)

    def test_remaining_never_goes_negative(self):
        allowance = auth.GuestAllowance(allowance=2)
        token = allowance.issue()
        allowance.spend(token, 5)
        assert allowance.remaining(token) == 0

    def test_expired_sessions_are_swept(self):
        """The in-process table must not grow without bound."""
        allowance = auth.GuestAllowance(allowance=1, ttl_s=0)
        allowance.issue()
        allowance.issue()
        assert allowance.as_dict()["active_sessions"] == 1


# ---------------------------------------------------------------------------
# The HTTP surface
# ---------------------------------------------------------------------------


class TestSessionEndpoints:
    def test_the_guest_endpoint_issues_a_token_and_names_the_header(self, client):
        body = client.post("/api/auth/guest").json()
        assert body["guest_token"]
        assert body["header"] == auth.GUEST_HEADER
        assert "sign_in_enabled" not in body

    def test_me_reports_the_allowance(self, client):
        body = client.get("/api/auth/me").json()
        assert "allowance" in body
        assert "sign_in_enabled" not in body
        assert "google_client_id" not in body

    def test_the_refusal_carries_the_counts(self, client, monkeypatch):
        """
        The interface renders "used N of M" from these, so they are part of the
        contract rather than debug detail.
        """
        monkeypatch.setenv("SOLARIS_GUEST_COMPUTATION_ALLOWANCE", "1")
        from solaris.core.config import get_settings

        get_settings.cache_clear()
        auth.reset_guest_allowance()
        try:
            allowance = auth.get_guest_allowance()
            token = allowance.issue()
            allowance.spend(token)
            assert allowance.remaining(token) == 0
            response = client.post(
                "/api/yield",
                json={"lat": 28.6, "lon": 77.2, "baseline_mode": "yearly", "year": 2023},
                headers={auth.GUEST_HEADER: token},
            )
            assert response.status_code == 403
            error = response.json()["error"]
            assert error["code"] == "allowance_exhausted"
            assert error["used"] == 1
            assert error["allowance"] == 1
        finally:
            get_settings.cache_clear()
            auth.reset_guest_allowance()

    def test_exempt_paths_need_no_session(self, client):
        for path in ("/api/health", "/api/version", "/api/config"):
            assert client.get(path).status_code == 200


# ---------------------------------------------------------------------------
# The persistent cache tier
# ---------------------------------------------------------------------------


class _FakeSnapshot:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return self._data


class _Increment:
    """Stands in for ``firestore.Increment``, an atomic field transform."""

    def __init__(self, amount):
        self.amount = amount


class _FakeDocument:
    def __init__(self, store, key):
        self._store = store
        self._key = key

    def get(self):
        return _FakeSnapshot(self._store.get(self._key))

    def set(self, value, merge=False):
        target = self._store.setdefault(self._key, {}) if merge else {}
        for field, item in value.items():
            # Increment is resolved here rather than overwriting, which is the
            # behaviour the shared budget depends on. A fake that clobbered the
            # field would make the counter look like it worked while actually
            # resetting it on every write.
            if isinstance(item, _Increment):
                target[field] = int(target.get(field, 0)) + item.amount
            else:
                target[field] = item
        self._store[self._key] = target


class _FakeCollection:
    def __init__(self, store):
        self._store = store

    def document(self, key):
        return _FakeDocument(self._store, key)

    def list_documents(self):
        return []


class _FakeFirestore:
    """
    Stands in for the Firestore client.

    Hand-written rather than a mock because the behaviours under test are
    whether an *expired but still readable* document counts as a miss, and
    whether ``Increment`` actually accumulates. A mock would return whatever it
    was told to and prove neither.
    """

    def __init__(self):
        self.store: dict[str, dict] = {}

    def collection(self, name):
        return _FakeCollection(self.store)


@pytest.fixture
def fake_firestore(monkeypatch):
    """
    Install a fake ``google.cloud.firestore`` module.

    Patching the module rather than injecting a private attribute means the
    code under test takes its **real** path: the lazy import, the client
    construction, and the ``Increment`` write all run as they would in
    production. Injecting ``_client`` directly would skip the import that the
    write path performs again -- which is exactly the line that silently
    disabled the shared counter the first time these tests were written.
    """
    import sys
    import types

    shared = _FakeFirestore()
    module = types.ModuleType("google.cloud.firestore")
    module.Client = lambda project=None: shared
    module.Increment = _Increment
    monkeypatch.setitem(sys.modules, "google.cloud.firestore", module)

    cloud = sys.modules.get("google.cloud")
    if cloud is not None:
        monkeypatch.setattr(cloud, "firestore", module, raising=False)
    return shared


class TestFirestoreCache:
    @pytest.fixture(autouse=True)
    def _install(self, fake_firestore):
        self.shared = fake_firestore

    def _cache(self):
        return firestore_cache.FirestoreCache(collection="test")

    def test_round_trips_a_value(self):
        cache = self._cache()
        cache.set("k", {"answer": 42}, ttl_s=60)
        assert cache.get("k") == {"answer": 42}

    def test_a_missing_key_is_a_miss(self):
        assert self._cache().get("absent") is None

    def test_an_expired_document_is_a_miss_even_though_it_is_readable(self):
        """
        The reason the expiry is checked in Python as well as by the TTL
        policy. Server-side deletion is documented as best-effort within 24
        hours, so without this check every TTL could silently stretch by a day.
        """
        cache = self._cache()
        cache.set("k", {"answer": 42}, ttl_s=60)
        client = self.shared
        client.store["k"][firestore_cache.FIELD_EXPIRES] = datetime.now(UTC) - timedelta(minutes=1)
        assert cache.get("k") is None

    def test_an_unparseable_expiry_is_treated_as_expired(self):
        """
        Fail toward recomputation. A wrongly-missed entry costs a computation;
        a wrongly-hit one serves a stale number.
        """
        cache = self._cache()
        cache.set("k", {"answer": 42}, ttl_s=60)
        self.shared.store["k"][firestore_cache.FIELD_EXPIRES] = "not-a-date"
        assert cache.get("k") is None

    def test_a_zero_ttl_is_not_stored(self):
        cache = self._cache()
        cache.set("k", {"a": 1}, ttl_s=0)
        assert cache.get("k") is None

    def test_an_unavailable_client_degrades_to_a_miss(self):
        """The central requirement: no client means no cache, not an error."""
        cache = firestore_cache.FirestoreCache(collection="test")
        cache._attempted = True  # as if construction had already failed
        assert cache.get("k") is None
        cache.set("k", {"a": 1}, ttl_s=60)  # must not raise
        assert cache.stats.bypasses >= 1

    def test_a_raising_client_degrades_to_a_miss_and_is_counted(self):
        """
        A client that constructs fine but then fails -- a revoked credential, a
        deleted database. This found a real defect: the warm path called
        through the client outside any ``try``, so the exception escaped into
        the request and turned a cache problem into a 500.
        """
        cache = self._cache()
        cache.get("warm-up")  # forces client construction

        class Exploding:
            def collection(self, name):
                raise RuntimeError("firestore is down")

        cache._client = Exploding()
        assert cache.get("k") is None
        assert cache.health.errors >= 1
        assert "firestore is down" in cache.health.last_error

    def test_health_is_reportable(self):
        """A silently dead cache tier should be visible, not merely slow."""
        cache = firestore_cache.FirestoreCache(collection="test")
        assert set(cache.as_dict()) == {"backend", "stats", "health"}


class TestTieredCache:
    @pytest.fixture(autouse=True)
    def _install(self, fake_firestore):
        self.shared = fake_firestore

    def _tiered(self):
        from solaris.core.cache import MemoryCache

        return firestore_cache.TieredCache(
            fast=MemoryCache(max_entries=8),
            slow=firestore_cache.FirestoreCache(collection="test"),
        )

    def test_writes_reach_both_tiers(self):
        tiered = self._tiered()
        tiered.set("k", {"a": 1}, ttl_s=600)
        assert tiered.fast.get("k") == {"a": 1}
        assert tiered.slow.get("k") == {"a": 1}

    def test_a_cold_instance_still_finds_the_entry(self):
        """
        The case that justifies the persistent tier at all. Under
        scale-to-zero most requests land on an instance with an empty memory
        cache, so memory alone would miss nearly always.
        """
        tiered = self._tiered()
        tiered.set("k", {"a": 1}, ttl_s=600)
        tiered.fast.clear()
        assert tiered.get("k") == {"a": 1}

    def test_a_persistent_hit_is_promoted_into_memory(self):
        tiered = self._tiered()
        tiered.set("k", {"a": 1}, ttl_s=600)
        tiered.fast.clear()
        tiered.get("k")
        assert tiered.fast.get("k") == {"a": 1}

    def test_a_miss_in_both_tiers_is_a_miss(self):
        tiered = self._tiered()
        assert tiered.get("absent") is None
        assert tiered.stats.misses == 1


class TestSharedBudget:
    @pytest.fixture(autouse=True)
    def _install(self, fake_firestore):
        self.shared = fake_firestore

    def _budget(self, limit=10):
        from solaris.core.limits import EarthEngineBudget

        return firestore_cache.FirestoreBudget(
            local=EarthEngineBudget(limit=limit), collection="test"
        )

    def test_the_local_ceiling_still_applies_without_a_remote(self):
        """
        The asymmetry that matters: a cache that cannot be reached should stop
        caching, but a *budget* that cannot be reached must not stop limiting.
        """
        from solaris.core.limits import BudgetExceededError, EarthEngineBudget

        budget = firestore_cache.FirestoreBudget(
            local=EarthEngineBudget(limit=3), collection="test"
        )
        budget._attempted = True  # no client, as if Firestore were unavailable
        budget.consume(3)
        with pytest.raises(BudgetExceededError):
            budget.consume(1)

    def test_an_exhausted_shared_counter_stops_an_otherwise_fine_instance(self):
        """
        What makes the counter worth sharing. A fresh instance has spent
        nothing locally, so only the shared count can stop it.
        """
        from solaris.core.limits import BudgetExceededError, EarthEngineBudget

        budget = self._budget(limit=5)
        self.shared.store[budget._doc_id()] = {"count": 99}
        budget.local = EarthEngineBudget(limit=5)
        with pytest.raises(BudgetExceededError, match="across all instances"):
            budget.consume(1)

    def test_the_local_budget_is_charged_before_the_remote_write(self):
        """
        Ordering, deliberately. A request rejected locally must not increment
        the shared counter for work that is not going to happen.
        """
        from solaris.core.limits import BudgetExceededError, EarthEngineBudget

        budget = self._budget(limit=1)
        budget.local = EarthEngineBudget(limit=1)
        budget.consume(1)
        before = dict(self.shared.store.get(budget._doc_id(), {}))
        with pytest.raises(BudgetExceededError):
            budget.consume(1)
        assert self.shared.store.get(budget._doc_id(), {}) == before

    def test_reported_usage_never_understates(self):
        budget = self._budget(limit=100)
        budget.consume(4)
        budget._remote_count = 40
        assert budget.used == 40
        assert budget.remaining == 60

    def test_the_scope_is_reported(self):
        """
        Degrading from a global ceiling to a per-instance one is a real change
        in behaviour, so it is named in the response rather than inferred.
        """
        from solaris.core.limits import EarthEngineBudget

        budget = self._budget()
        budget.consume(1)  # forces client construction
        assert budget.as_dict()["scope"] == "shared"

        offline = firestore_cache.FirestoreBudget(
            local=EarthEngineBudget(limit=5), collection="test"
        )
        offline._attempted = True
        assert offline.as_dict()["scope"] == "instance"


# ---------------------------------------------------------------------------
# Earth Engine configuration failures
# ---------------------------------------------------------------------------


class TestEarthEngineUnavailable:
    """
    A misconfigured Earth Engine project must not read as an application bug.

    This distinction is not cosmetic. The first deployment of this service
    answered every request with a generic 500, which sent the investigation
    into application code for a problem that was entirely in the Cloud project
    -- a missing IAM role. A 503 that names the likely cause is the difference
    between ten minutes and an afternoon.
    """

    def test_a_hint_is_derived_for_each_known_failure(self):
        from solaris.api import deps

        cases = {
            "Caller does not have required permission to use project x": "serviceUsageConsumer",
            "Project is not registered to use Earth Engine": "register",
            "Earth Engine API has not been used in project 123 before": "not enabled",
            "Please authorize access to your Earth Engine account": "authenticate",
        }
        for message, expected in cases.items():
            hint = deps.earth_engine_hint(RuntimeError(message))
            assert expected.lower() in hint.lower(), message

    def test_an_unrecognised_message_still_gets_generic_guidance(self):
        """
        Matching on message text is fragile. The fallback is the honest
        acknowledgement: an unknown message must still produce guidance rather
        than an empty hint that reads as "no problem found".
        """
        from solaris.api import deps

        hint = deps.earth_engine_hint(RuntimeError("something entirely new"))
        assert "GEE_PROJECT_ID" in hint
        assert len(hint) > 40

    def test_the_hint_never_quotes_the_raw_error(self):
        """
        The raw text names the Cloud project, and this reply reaches an
        unauthenticated caller.
        """
        from solaris.api import deps

        raw = "Caller does not have required permission to use project super-secret-proj"
        hint = deps.earth_engine_hint(RuntimeError(raw))
        assert "super-secret-proj" not in hint

    def test_a_compute_request_returns_503_not_500(self, client, monkeypatch):
        from solaris.api import deps

        def _explode(*_args, **_kwargs):
            raise deps.EarthEngineUnavailableError(
                "Earth Engine is not available on this server.",
                hint="Run `earthengine authenticate`.",
            )

        monkeypatch.setattr(deps, "ensure_ee", _explode)
        response = client.post(
            "/api/yield",
            json={"lat": 28.6, "lon": 77.2, "baseline_mode": "yearly", "year": 2023},
        )
        assert response.status_code == 503
        body = response.json()
        assert body["error"]["code"] == "earth_engine_unavailable"
        assert body["error"]["hint"] == "Run `earthengine authenticate`."
        assert response.headers["Retry-After"] == "30"

    def test_the_hint_is_withheld_in_production(self, client, monkeypatch):
        """
        A public deployment should not narrate its own setup to arbitrary
        callers. The hint still goes to the log, where whoever is fixing it
        will be looking.
        """
        from solaris.api import deps
        from solaris.core.config import get_settings

        def _explode(*_args, **_kwargs):
            raise deps.EarthEngineUnavailableError("nope", hint="Grant the role on project x.")

        monkeypatch.setattr(deps, "ensure_ee", _explode)
        monkeypatch.setenv("SOLARIS_ENV", "prod")
        monkeypatch.setenv("SOLARIS_CORS_ORIGINS", "https://example.test")
        get_settings.cache_clear()
        try:
            response = client.post(
                "/api/yield",
                json={"lat": 28.6, "lon": 77.2, "baseline_mode": "yearly", "year": 2023},
            )
            assert response.status_code == 503
            assert "hint" not in response.json()["error"]
            assert "project x" not in response.text
        finally:
            get_settings.cache_clear()

    def test_readiness_reports_the_hint(self, client, monkeypatch):
        from solaris.api import deps

        def _explode(*_args, **_kwargs):
            raise deps.EarthEngineUnavailableError("nope", hint="Enable the API.")

        monkeypatch.setattr(deps, "ensure_ee", _explode)
        body = client.get("/api/ready").json()
        assert body["ready"] is False
        assert body["checks"]["earth_engine"] == "unavailable"
        assert body["hint"] == "Enable the API."

    def test_a_failure_is_not_memoised(self, monkeypatch):
        """
        A transient failure must not pin the process into a broken state for
        its lifetime, and fixing the configuration should take effect on the
        next request rather than needing a restart.
        """
        from solaris.api import deps

        attempts = {"n": 0}

        def _flaky(_project):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("Please authorize access")

        monkeypatch.setattr(deps, "_initialize_ee", _flaky)
        monkeypatch.setattr(deps, "_EE_INIT_PROJECT", None, raising=False)

        with pytest.raises(deps.EarthEngineUnavailableError):
            deps._ensure_ee("proj")
        deps._ensure_ee("proj")  # must not raise the second time
        assert attempts["n"] == 2
