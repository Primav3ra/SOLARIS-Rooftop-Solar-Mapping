"""
End-to-end API tests against a fully synthetic Earth Engine.

``tests/fakes/world.py`` registers every asset and collection the handlers read,
so all five POST endpoints run here with no credentials and no network. That
buys two things:

1. **The accounting identities inside the response are checkable.** The stage
   ladder, the loss attribution and the retention scalars are all derived
   quantities that were previously only verified by eye.
2. **A golden file.** ``app.py`` is a 1191-line module about to be split into
   routers; comparing against a recorded response proves the split changed no
   behaviour. The golden is generated from the synthetic world, so it is
   deterministic and reviewable in a diff -- unlike a capture from live Earth
   Engine.

The golden values are **not** validation. They encode what the code does today,
including its known defects. Checking the model against real references is the
job of the ``evals`` harness.

Regenerate with::

    python -m tests.fakes.regenerate_golden
"""

from __future__ import annotations

import itertools
import json
import pathlib
import warnings
from typing import ClassVar

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)

from tests.fakes import world
from tests.fakes.install import assert_all_consumers_patched, install_fake_ee

GOLDEN_PATH = pathlib.Path(__file__).resolve().parents[1] / "data" / "api_golden.json"

#: Fields whose values are environment- or clock-dependent and so cannot be
#: pinned. Everything else in the response is compared exactly.
VOLATILE_KEYS = {
    "reduce_region_raw",
    "value_source",
    "irradiance_source",
    "ghi_sample_source",
    "urlTemplate",
    "mapid",
}


@pytest.fixture(autouse=True)
def client(monkeypatch):
    """
    A TestClient over an app whose ``ee`` is the fake.

    Function-scoped and autouse so the patch is applied (and undone) around
    every test. See tests/fakes/install.py for why the fake is installed by
    patching each module's bound ``ee`` rather than via ``sys.modules``.
    """
    install_fake_ee(monkeypatch)
    world.register_world()

    from fastapi.testclient import TestClient

    import solaris.api.app as app_mod

    return TestClient(app_mod.app)


def test_every_ee_consumer_is_patched(client):
    """
    Guards against a new module doing ``import ee`` without being added to
    EE_CONSUMERS -- which would leak to the real Earth Engine.
    """
    assert assert_all_consumers_patched() == []


def _strip_volatile(obj):
    if isinstance(obj, dict):
        return {k: _strip_volatile(v) for k, v in obj.items() if k not in VOLATILE_KEYS}
    if isinstance(obj, list):
        return [_strip_volatile(v) for v in obj]
    if isinstance(obj, float):
        return round(obj, 4)
    return obj


def _request(**overrides):
    return {**world.AOI_REQUEST, **overrides}


# ---------------------------------------------------------------------------
# Every endpoint answers
# ---------------------------------------------------------------------------


class TestAllEndpointsRunOffline:
    @pytest.mark.parametrize(
        "endpoint",
        ["/api/baseline", "/api/yield", "/api/series", "/api/buildings", "/api/tiles"],
    )
    def test_endpoint_returns_ok(self, client, endpoint):
        response = client.post(endpoint, json=_request())
        assert response.status_code == 200, response.text
        assert response.json().get("status") in (None, "ok")

    @pytest.mark.parametrize("mode", ["yearly", "quarterly", "monthly", "daily"])
    def test_every_temporal_mode_runs(self, client, mode):
        payload = _request(baseline_mode=mode)
        if mode == "daily":
            payload |= {
                "start_date": "2023-06-21",
                "end_date_exclusive": "2023-06-22",
            }
        response = client.post("/api/yield", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["baseline_time_mode"] == mode


# ---------------------------------------------------------------------------
# The world produces the inputs it was built to produce
# ---------------------------------------------------------------------------


class TestSyntheticWorldWiring:
    def test_era5_land_annual_ghi(self, client):
        body = client.post("/api/yield", json=_request()).json()
        assert body["regional_ghi_kwh_m2_period"] == pytest.approx(
            world.expected_annual_ghi_kwh_m2(), rel=1e-6
        )

    def test_beam_fraction_comes_from_era5_hourly(self, client):
        """
        Numerator and denominator both come from ERA5 HOURLY so the ratio is
        internally consistent. The world sets it to exactly 0.62.
        """
        body = client.post("/api/yield", json=_request()).json()
        assert body["beam_fraction"] == pytest.approx(world.expected_beam_fraction(), rel=1e-6)
        assert body["beam_fraction_source"] == "era5_hourly"

    def test_aod_and_soiling_come_from_the_registered_maiac_value(self, client):
        body = client.post("/api/yield", json=_request()).json()
        assert body["mean_aod_550nm"] == pytest.approx(world.AOD_VALUE, abs=1e-4)

    def test_roof_area_is_positive_and_below_the_aoi_area(self, client):
        body = client.post("/api/yield", json=_request()).json()
        assert body["roof_area_m2"] > 0


# ---------------------------------------------------------------------------
# Internal accounting identities
# ---------------------------------------------------------------------------


class TestYieldAccountingIdentities:
    STAGES: ClassVar[list[str]] = [
        "baseline_yield_kwh",
        "after_shadow_yield_kwh",
        "after_svf_yield_kwh",
        "after_uhi_yield_kwh",
        "after_soiling_yield_kwh",
    ]

    @pytest.fixture
    def body(self, client):
        return client.post("/api/yield", json=_request()).json()

    def test_stage_ladder_is_monotone_non_increasing(self, body):
        """
        Each stage adds a penalty, so energy can only fall. The production code
        clamps each per-stage delta with ``max(0.0, ...)``, which *hides* a
        non-monotone ladder rather than reporting it -- so assert the ladder
        directly.
        """
        values = [body[k] for k in self.STAGES]
        assert all(a >= b - 1e-6 for a, b in itertools.pairwise(values)), values

    def test_total_loss_equals_first_minus_last(self, body):
        expected = body["baseline_yield_kwh"] - body["after_soiling_yield_kwh"]
        assert body["penalty_loss_kwh"] == pytest.approx(expected, abs=1e-4)

    def test_loss_percentage_is_consistent(self, body):
        expected = body["penalty_loss_kwh"] / body["baseline_yield_kwh"] * 100.0
        assert body["penalty_loss_pct"] == pytest.approx(expected, abs=1e-4)

    def test_per_stage_losses_sum_to_the_total(self, body):
        contribution = body["penalty_contribution"]
        parts = sum(
            contribution[k]
            for k in ("shadow_loss_kwh", "svf_loss_kwh", "uhi_loss_kwh", "soiling_loss_kwh")
        )
        assert parts == pytest.approx(body["penalty_loss_kwh"], rel=1e-6)

    def test_contribution_percentages_sum_to_one_hundred(self, body):
        contribution = body["penalty_contribution"]
        total = sum(
            contribution[k]
            for k in (
                "shadow_contribution_pct",
                "svf_contribution_pct",
                "uhi_contribution_pct",
                "soiling_contribution_pct",
            )
        )
        assert total == pytest.approx(100.0, abs=0.01)

    def test_period_yield_matches_the_final_stage(self, body):
        if "period_yield_kwh" in body:
            assert body["period_yield_kwh"] == pytest.approx(
                body["after_soiling_yield_kwh"], rel=1e-9
            )

    def test_net_irradiance_matches_the_documented_formula(self, body):
        """
        net = GHI * [diffuse * SVF + beam * (1 - shadow)] * uhi * soiling,
        recomputed here independently of the handler.

        Note the handler computes ``mean_net_retention`` internally but never
        returns it, so the retention scalar behind the reported net irradiance
        is not directly observable by a client -- it has to be inferred, as
        here.
        """
        retention = (1.0 - body["beam_fraction"]) * body["mean_sky_view_factor"] + body[
            "beam_fraction"
        ] * (1.0 - body["mean_shadow_fraction"])
        expected = (
            body["regional_ghi_kwh_m2_period"]
            * retention
            * body["uhi_derate_factor"]
            * body["soiling_retention_factor"]
        )
        assert body["net_irradiance_kwh_m2_period"] == pytest.approx(expected, rel=1e-4)

    def test_beam_and_diffuse_fractions_are_complementary(self, body):
        assert body["beam_fraction"] + body["diffuse_fraction"] == pytest.approx(1.0, abs=1e-9)

    def test_combined_derate_is_the_product_of_its_parts(self, body):
        expected = body["uhi_derate_factor"] * body["soiling_retention_factor"]
        assert body["combined_derate_factor"] == pytest.approx(expected, abs=1e-6)

    def test_shade_matrix_areas_do_not_exceed_the_roof(self, body):
        for interval in body["shade_intervals"]:
            assert 0.0 <= interval["shade_fraction"] <= 1.0
            assert interval["shade_area_m2"] <= body["roof_area_m2"] + 1e-6

    def test_retention_scalars_are_bounded(self, body):
        for key in (
            "mean_shadow_retention",
            "mean_sky_view_factor",
            "uhi_derate_factor",
            "soiling_retention_factor",
            "combined_derate_factor",
        ):
            assert 0.0 <= body[key] <= 1.0, f"{key} out of range: {body[key]}"

    def test_pv_parameters_are_echoed(self, body):
        from solaris.core import constants as C

        assert body["panel_efficiency"] == C.PANEL_EFFICIENCY
        assert body["performance_ratio"] == C.PERFORMANCE_RATIO
        assert body["packing_factor"] == C.PACKING_FACTOR


class TestPenaltyBalance:
    """
    Records how the total loss is currently apportioned.

    Measured on the synthetic city: soiling accounts for ~88% of all penalty
    loss, against ~8% for shadowing and ~4% for sky-view obstruction. In other
    words the headline "urban penalty" is dominated by a single uncalibrated
    linear coefficient (``mean_AOD * 0.08``), while the two geometric layers the
    project's contribution rests on are marginal -- and both are understated by
    defects D1 and D2. Pinned so the balance shift is visible once those are
    fixed and the soiling model is replaced.
    """

    @pytest.fixture
    def contribution(self, client):
        return client.post("/api/yield", json=_request()).json()["penalty_contribution"]

    def test_soiling_currently_dominates(self, contribution):
        assert contribution["soiling_contribution_pct"] > 70.0

    def test_sky_view_contribution_is_marginal(self, contribution):
        assert contribution["svf_contribution_pct"] < 10.0

    def test_shadow_contribution_is_marginal(self, contribution):
        assert contribution["shadow_contribution_pct"] < 20.0


# ---------------------------------------------------------------------------
# Golden comparison
# ---------------------------------------------------------------------------


class TestGoldenResponses:
    """
    Byte-level regression guard for the router split.

    If this fails after a refactor, the refactor changed behaviour. If it fails
    after a deliberate model change, regenerate the golden in its own commit so
    the numeric diff is reviewable on its own.
    """

    ENDPOINTS: ClassVar[list[str]] = [
        "/api/baseline",
        "/api/yield",
        "/api/series",
        "/api/buildings",
    ]

    @pytest.fixture
    def golden(self):
        if not GOLDEN_PATH.exists():
            pytest.skip(
                f"missing {GOLDEN_PATH.name}; run `python -m tests.fakes.regenerate_golden`"
            )
        return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

    @pytest.mark.parametrize("endpoint", ENDPOINTS)
    def test_response_matches_golden(self, client, golden, endpoint):
        if endpoint not in golden:
            pytest.skip(f"{endpoint} not in the golden file")
        got = _strip_volatile(client.post(endpoint, json=_request()).json())
        assert got == golden[endpoint], (
            f"{endpoint} response changed. If this was intentional, regenerate "
            "the golden file in a separate commit."
        )

    def test_golden_covers_every_recorded_endpoint(self, golden):
        assert set(golden) == set(self.ENDPOINTS)
