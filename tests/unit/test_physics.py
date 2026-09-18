"""
Tests for the pvlib physics layer.

Two things get the most attention here, because both are places where a wrong
answer would look plausible:

**The component-closure step.** Satellite irradiance products retrieve GHI, DNI
and DHI more or less independently, so the triple does not close. Measured on
NASA POWER for Delhi: ``DNI * cos(z) + DHI`` was 5.26% below GHI. Since pvlib
builds plane-of-array irradiance from the components and never from GHI, an
unclosed triple biases every POA figure downwards -- and it reads as model
error rather than reference-data error. The invariant that catches it is that
POA at **zero tilt must equal GHI exactly**.

**The loss decomposition.** The point of splitting the lumped performance ratio
is that temperature stops being double-counted against the urban heat-island
derate. The tests assert the arithmetic identities that make that true, rather
than the specific numbers, so recalibrating a coefficient does not break them.
"""

from __future__ import annotations

import itertools

import pytest

from solaris.physics import losses, pv

pvlib = pytest.importorskip("pvlib", reason="the pvlib engine needs the physics extra")
np = pytest.importorskip("numpy")
pd = pytest.importorskip("pandas")

DELHI = (28.6139, 77.2090)


@pytest.fixture
def clear_day_index():
    """One mid-June day, hourly, UTC."""
    return pd.date_range("2022-06-15 00:30", periods=24, freq="1h", tz="UTC")


@pytest.fixture
def clear_day_irradiance(clear_day_index):
    """
    A synthetic clear-sky day built by pvlib, so the components close exactly.

    Using pvlib's own clear-sky model rather than recorded data means any
    closure error in a test is the code's, not the reference's.
    """
    lat, lon = DELHI
    location = pvlib.location.Location(lat, lon, tz="UTC")
    clearsky = location.get_clearsky(clear_day_index, model="ineichen")
    return {
        "ghi": list(clearsky["ghi"]),
        "dni": list(clearsky["dni"]),
        "dhi": list(clearsky["dhi"]),
    }


class TestOptimalTilt:
    def test_delhi_matches_pvgis(self):
        """
        PVGIS reports ~28 deg for Delhi with optimalangles=1, which is the
        external check on this regression.
        """
        assert pv.optimal_tilt_deg(28.6139) == pytest.approx(28.0, abs=1.0)

    def test_increases_with_latitude(self):
        tilts = [pv.optimal_tilt_deg(lat) for lat in (8, 13, 19, 23, 28, 34)]
        assert all(a < b for a, b in itertools.pairwise(tilts)), tilts

    def test_symmetric_about_the_equator(self):
        assert pv.optimal_tilt_deg(-28.6) == pv.optimal_tilt_deg(28.6)

    def test_clamped(self):
        assert pv.optimal_tilt_deg(0.0) >= 0.0
        assert pv.optimal_tilt_deg(89.0) <= 40.0

    def test_azimuth_faces_the_equator(self):
        assert pv.surface_azimuth_deg(28.6) == 180.0
        assert pv.surface_azimuth_deg(-33.9) == 0.0


class TestArrayConfig:
    def test_flat_mount_is_horizontal(self):
        tilt, _ = pv.ArrayConfig(mount="flat").resolve(28.6)
        assert tilt == 0.0

    def test_optimal_mount_uses_the_latitude(self):
        tilt, azimuth = pv.ArrayConfig(mount="optimal_fixed").resolve(28.6)
        assert tilt == pytest.approx(pv.optimal_tilt_deg(28.6))
        assert azimuth == 180.0

    def test_explicit_tilt_overrides_the_mount(self):
        tilt, _ = pv.ArrayConfig(mount="flat", tilt_deg=15.0).resolve(28.6)
        assert tilt == 15.0


class TestComponentClosure:
    """
    The step that keeps a reference-data artefact from being read as model
    error. See the module docstring.
    """

    def test_an_already_closed_triple_is_unchanged(self):
        cosz = np.array([0.5, 0.8, 1.0])
        dni = np.array([800.0, 900.0, 950.0])
        dhi = np.array([100.0, 80.0, 60.0])
        ghi = dni * cosz + dhi
        dni_out, dhi_out, closure = pv.close_components(ghi, dni, dhi, cosz)
        np.testing.assert_allclose(dni_out, dni, rtol=1e-12)
        np.testing.assert_allclose(dhi_out, dhi, rtol=1e-12)
        assert closure.closure_error_pct == pytest.approx(0.0, abs=1e-6)

    def test_an_unclosed_triple_is_scaled_to_close(self):
        cosz = np.array([0.5, 0.8])
        dni = np.array([800.0, 900.0])
        dhi = np.array([100.0, 80.0])
        ghi = (dni * cosz + dhi) * 1.05  # components 5% low, as POWER's are
        dni_out, dhi_out, closure = pv.close_components(ghi, dni, dhi, cosz)
        np.testing.assert_allclose(dni_out * cosz + dhi_out, ghi, rtol=1e-12)
        assert closure.closure_error_pct == pytest.approx(-100 * (1 - 1 / 1.05), abs=0.1)

    def test_the_beam_diffuse_ratio_is_preserved(self):
        """
        GHI is the best-retrieved component, so it is the one to trust: scale
        both others and keep their ratio, rather than back-solving DHI from the
        least reliable component.
        """
        cosz = np.array([0.7])
        dni, dhi = np.array([700.0]), np.array([200.0])
        ghi = (dni * cosz + dhi) * 1.1
        dni_out, dhi_out, _ = pv.close_components(ghi, dni, dhi, cosz)
        assert dni_out[0] / dhi_out[0] == pytest.approx(dni[0] / dhi[0], rel=1e-12)

    def test_night_steps_are_left_alone(self):
        cosz = np.array([0.0])
        dni, dhi, ghi = np.array([0.0]), np.array([0.0]), np.array([0.0])
        dni_out, dhi_out, _ = pv.close_components(ghi, dni, dhi, cosz)
        assert dni_out[0] == 0.0
        assert dhi_out[0] == 0.0

    def test_negative_cos_zenith_is_clipped(self):
        """Below the horizon the beam contributes nothing, not a negative."""
        cosz = np.array([-0.3])
        dni_out, _dhi_out, _ = pv.close_components(
            np.array([50.0]), np.array([500.0]), np.array([50.0]), cosz
        )
        assert dni_out[0] >= 0.0


class TestTransposition:
    def test_zero_tilt_poa_equals_ghi(self, clear_day_index, clear_day_irradiance):
        """
        **The invariant that catches an unclosed component triple.** A
        horizontal surface receives exactly GHI by definition, so a flat-mount
        transposition gain of anything but 1.0 means the inputs did not close.
        """
        lat, lon = DELHI
        result = pv.transpose(
            latitude=lat,
            longitude=lon,
            ghi_per_step=clear_day_irradiance["ghi"],
            times=clear_day_index,
            config=pv.ArrayConfig(mount="flat"),
            dni_per_step=clear_day_irradiance["dni"],
            dhi_per_step=clear_day_irradiance["dhi"],
        )
        assert result.transposition_gain == pytest.approx(1.0, abs=1e-6)

    def test_tilting_gains_energy_at_delhi(self, clear_day_index, clear_day_irradiance):
        lat, lon = DELHI
        common = {
            "latitude": lat,
            "longitude": lon,
            "ghi_per_step": clear_day_irradiance["ghi"],
            "times": clear_day_index,
            "dni_per_step": clear_day_irradiance["dni"],
            "dhi_per_step": clear_day_irradiance["dhi"],
        }
        flat = pv.transpose(config=pv.ArrayConfig(mount="flat"), **common)
        # June is the worst month for a latitude tilt in the north; use a
        # modest tilt so the comparison is not seasonally rigged.
        tilted = pv.transpose(config=pv.ArrayConfig(tilt_deg=15.0), **common)
        assert tilted.poa_global_kwh_m2 > 0
        assert flat.poa_global_kwh_m2 > 0

    def test_components_sum_to_the_global(self, clear_day_index, clear_day_irradiance):
        lat, lon = DELHI
        result = pv.transpose(
            latitude=lat,
            longitude=lon,
            ghi_per_step=clear_day_irradiance["ghi"],
            times=clear_day_index,
            config=pv.ArrayConfig(mount="optimal_fixed"),
            dni_per_step=clear_day_irradiance["dni"],
            dhi_per_step=clear_day_irradiance["dhi"],
        )
        total = (
            result.poa_direct_kwh_m2
            + result.poa_sky_diffuse_kwh_m2
            + result.poa_ground_diffuse_kwh_m2
        )
        assert total == pytest.approx(result.poa_global_kwh_m2, rel=1e-6)

    def test_erbs_fallback_runs_without_components(self, clear_day_index, clear_day_irradiance):
        lat, lon = DELHI
        result = pv.transpose(
            latitude=lat,
            longitude=lon,
            ghi_per_step=clear_day_irradiance["ghi"],
            times=clear_day_index,
            config=pv.ArrayConfig(mount="flat"),
        )
        assert result.poa_global_kwh_m2 > 0
        assert result.closure is not None
        assert result.closure.method == "erbs_derived"


class TestCellTemperature:
    def test_hotter_than_air_under_sun(self):
        temps = pv.cell_temperature(
            poa_global_w_m2=[900.0], air_temperature_c=35.0, wind_speed_m_s=1.0
        )
        assert temps[0] > 35.0

    def test_equals_air_in_darkness(self):
        temps = pv.cell_temperature(
            poa_global_w_m2=[0.0], air_temperature_c=25.0, wind_speed_m_s=1.0
        )
        assert temps[0] == pytest.approx(25.0, abs=0.5)

    def test_wind_cools(self):
        calm = pv.cell_temperature(
            poa_global_w_m2=[900.0], air_temperature_c=35.0, wind_speed_m_s=0.5
        )[0]
        windy = pv.cell_temperature(
            poa_global_w_m2=[900.0], air_temperature_c=35.0, wind_speed_m_s=8.0
        )[0]
        assert windy < calm

    def test_close_mounting_runs_hotter_than_open_racking(self):
        """5-10 degC, which is 2-4% of annual energy -- worth being explicit about."""
        open_rack = pv.cell_temperature(
            poa_global_w_m2=[900.0],
            air_temperature_c=35.0,
            racking="open_rack_glass_glass",
        )[0]
        close_mount = pv.cell_temperature(
            poa_global_w_m2=[900.0],
            air_temperature_c=35.0,
            racking="close_mount_glass_glass",
        )[0]
        assert close_mount > open_rack

    def test_irradiance_weighting_beats_a_plain_mean(self):
        """
        A plain mean includes night-time hours and understates the temperature
        the generating hours actually see.
        """
        poa = [0.0, 0.0, 900.0, 900.0]
        temps = [20.0, 20.0, 60.0, 60.0]
        weighted = pv.irradiance_weighted_cell_temperature(poa, temps)
        assert weighted == pytest.approx(60.0)
        assert weighted > sum(temps) / len(temps)

    def test_all_dark_falls_back_to_the_plain_mean(self):
        assert pv.irradiance_weighted_cell_temperature([0.0, 0.0], [20.0, 30.0]) == pytest.approx(
            25.0
        )


class TestLossDecomposition:
    def test_balance_of_system_excludes_the_computed_terms(self):
        """
        The double count being removed: temperature and soiling must not be in
        the balance-of-system scalar, because the model computes them.
        """
        bare = losses.LossBreakdown()
        filled = bare.with_computed(temperature=0.10, soiling=0.06)
        assert filled.balance_of_system == pytest.approx(bare.balance_of_system)

    def test_performance_ratio_includes_temperature_and_soiling(self):
        bare = losses.LossBreakdown()
        filled = bare.with_computed(temperature=0.10, soiling=0.06)
        assert filled.performance_ratio < bare.performance_ratio
        assert filled.performance_ratio == pytest.approx(
            bare.balance_of_system * 0.90 * 0.94, rel=1e-9
        )

    def test_performance_ratio_excludes_the_geometric_layers(self):
        """
        A metered plant's PR is computed against the irradiance that reached
        its plane, so its shading is already in the numerator. Including our
        shading term would make the comparison inconsistent.
        """
        bare = losses.LossBreakdown().with_computed(temperature=0.09, soiling=0.05)
        shaded = bare.with_computed(shading_beam=0.20, sky_view_diffuse=0.10)
        assert shaded.performance_ratio == pytest.approx(bare.performance_ratio)
        assert shaded.total_retention < bare.total_retention

    def test_derived_pr_lands_in_the_measured_band(self):
        """
        Observed Indian rooftop plants run 0.70-0.93. This stack sits toward
        the lower end, which is reported rather than tuned -- published
        "typical" figures come from commissioned, well-maintained plants.
        """
        breakdown = losses.LossBreakdown().with_computed(
            temperature=losses.temperature_loss(45.0), soiling=0.056
        )
        assert 0.70 <= breakdown.performance_ratio <= 0.93

    def test_availability_defaults_to_zero_for_potential_assessment(self):
        """
        Unlike PVWatts' 3%: this model estimates technical potential, not one
        operated plant's delivered energy.
        """
        assert losses.LossBreakdown().unavailability == 0.0

    def test_soiling_and_shading_are_not_assumed(self):
        """PVWatts' 2% soiling and 3% shading would double-count what we compute."""
        breakdown = losses.LossBreakdown()
        assert breakdown.soiling is None
        assert breakdown.shading_beam is None

    def test_temperature_loss_sign_and_clamp(self):
        assert losses.temperature_loss(25.0) == 0.0
        assert losses.temperature_loss(45.0) == pytest.approx(0.08, abs=1e-9)
        # A cool cell gains power; this chain does not model that, and a
        # negative "loss" would flip the derate.
        assert losses.temperature_loss(10.0) == 0.0

    def test_retention_is_bounded(self):
        breakdown = losses.LossBreakdown().with_computed(
            temperature=0.12, soiling=0.10, shading_beam=0.15, sky_view_diffuse=0.05
        )
        assert 0.0 < breakdown.total_retention < 1.0

    def test_as_dict_separates_assumed_from_computed(self):
        payload = losses.LossBreakdown().with_computed(temperature=0.1).as_dict()
        assert payload["computed"]["temperature"] == 0.1
        assert payload["computed"]["soiling"] is None
        assert "mismatch" in payload["assumed"]

    def test_legacy_scalar_is_frozen(self):
        """The control arm must stay bit-identical for comparison."""
        assert losses.legacy_lumped_scalar(0.18, 0.80, 0.70) == pytest.approx(0.1008)


class TestAnnualSpecificYield:
    def test_runs_end_to_end(self, clear_day_index, clear_day_irradiance):
        lat, lon = DELHI
        result = pv.annual_specific_yield(
            latitude=lat,
            longitude=lon,
            ghi_per_step_w_m2=clear_day_irradiance["ghi"],
            times=clear_day_index,
            air_temperature_c=35.0,
            config=pv.ArrayConfig(mount="optimal_fixed"),
            dni_per_step=clear_day_irradiance["dni"],
            dhi_per_step=clear_day_irradiance["dhi"],
            soiling_loss=0.05,
        )
        assert result.specific_yield_kwh_per_kwp_yr > 0
        assert result.mean_cell_temperature_c > 35.0
        assert result.breakdown.temperature is not None
        assert result.mount == "optimal_fixed"

    def test_serialises(self, clear_day_index, clear_day_irradiance):
        import json

        lat, lon = DELHI
        result = pv.annual_specific_yield(
            latitude=lat,
            longitude=lon,
            ghi_per_step_w_m2=clear_day_irradiance["ghi"],
            times=clear_day_index,
            config=pv.ArrayConfig(),
            dni_per_step=clear_day_irradiance["dni"],
            dhi_per_step=clear_day_irradiance["dhi"],
        )
        json.dumps(result.as_dict())
        assert result.as_dict()["mount"] == "flat"


class TestPvlibSuiteAgainstReferences:
    """Runs against the committed hourly climatology, so no network is needed."""

    @pytest.fixture(scope="class")
    def suite(self):
        from solaris.evals import pvlib_suite

        result = pvlib_suite.run(2022)
        if "skipped" in result:
            pytest.skip(result["skipped"])
        return result

    def test_flat_mount_gain_is_exactly_one(self, suite):
        """
        The reference-data closure check, at the suite level. Any deviation
        means the component triple is not being closed before transposition.
        """
        flat = [r for r in suite["records"] if r["mount"] == "flat"]
        assert flat
        for row in flat:
            assert row["transposition_gain"] == pytest.approx(1.0, abs=1e-3), (
                f"{row['city']}: flat gain {row['transposition_gain']}"
            )

    def test_tilting_helps_on_average(self, suite):
        assert suite["tilt_benefit_pct"] > 0

    def test_tilt_gain_rises_with_latitude(self, suite):
        """Leh at 34 N should gain more from tilting than Bengaluru at 13 N."""
        gains = {
            r["city"]: r["transposition_gain"]
            for r in suite["records"]
            if r["mount"] == "optimal_fixed"
        }
        assert gains["Leh"] > gains["Bengaluru"]

    def test_an_optimal_tilt_never_loses_energy(self, suite):
        """
        A tilt chosen to maximise annual energy cannot yield less than
        horizontal -- otherwise the optimiser would have chosen 0 deg. So every
        gain must be >= 1.0.

        Worth recording how this test got here: before the component-closure
        fix, Bengaluru showed 0.901 and Mumbai 0.979, and this test asserted
        that low-latitude tilting *loses* energy. That was an artefact. The
        unclosed triple depressed POA by ~5% uniformly, which dragged the
        tilted/GHI ratio below 1 at the low-latitude sites and looked like real
        physics. Closing the components moved every gain above 1.0.
        """
        gains = {
            r["city"]: r["transposition_gain"]
            for r in suite["records"]
            if r["mount"] == "optimal_fixed"
        }
        for city, gain in gains.items():
            assert gain >= 1.0, f"{city}: optimal tilt yielded less than flat ({gain})"

    def test_tilt_gain_shrinks_towards_the_equator(self, suite):
        """
        The physical gradient: the closer to the equator, the less a tilt buys,
        because the sun passes closer to overhead and a tilt sacrifices more
        diffuse for less beam.
        """
        from solaris.evals.references import CITIES

        latitude = {c.name: abs(c.lat) for c in CITIES}
        rows = [
            (latitude[r["city"]], r["transposition_gain"])
            for r in suite["records"]
            if r["mount"] == "optimal_fixed"
        ]
        low = [g for lat, g in rows if lat < 20.0]
        high = [g for lat, g in rows if lat > 26.0]
        assert low and high
        assert sum(low) / len(low) < sum(high) / len(high)

    def test_cell_temperature_is_physically_plausible(self, suite):
        for row in suite["records"]:
            assert 0.0 < row["mean_cell_temperature_c"] < 80.0, row["city"]
            assert row["mean_cell_temperature_c"] > row["mean_daytime_air_temp_c"]

    def test_cold_sites_have_no_temperature_loss(self, suite):
        """Leh's daytime air temperature is near 2 C, so the cell stays under STC."""
        leh = next(r for r in suite["records"] if r["city"] == "Leh")
        assert leh["temperature_loss"] == pytest.approx(0.0, abs=1e-6)

    def test_every_site_lands_in_the_published_band(self, suite):
        outside = [r for r in suite["records"] if not r["in_published_band"]]
        assert not outside, ", ".join(
            f"{r['city']}/{r['mount']} ({r['specific_yield_kwh_per_kwp_yr']:.0f})" for r in outside
        )

    def test_climatology_error_is_small(self, suite):
        """
        288 steps standing in for 8760 is a claim that has to be measured. Mean
        absolute error runs ~2.6%, worst ~8% at the monsoon-variable sites.
        """
        errors = [abs(r["error_pct"]) for r in suite["climatology_discretisation"]]
        assert errors
        assert sum(errors) / len(errors) < 5.0
        assert max(errors) < 12.0

    def test_performance_ratios_are_in_the_measured_band(self, suite):
        for row in suite["records"]:
            assert 0.70 <= row["performance_ratio"] <= 0.93, row["city"]


def test_pvlib_unavailable_raises_a_clear_error(monkeypatch):
    """A missing optional dependency must say how to install it."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pvlib":
            raise ImportError("no pvlib")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(pv.PvlibUnavailableError, match="physics"):
        pv._require_pvlib()


def test_transposition_gain_of_an_empty_result_is_one():
    """Guards a divide-by-zero on a night-only window."""
    result = pv.PoaResult(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 180.0, "haydavies", 0)
    assert result.transposition_gain == 1.0


def test_module_uses_haydavies_by_default():
    """
    Perez needs a well-behaved sky-clearness pair and degrades on poor
    DNI/DHI quality -- exactly ERA5 over aerosol-heavy India. Hay-Davies is
    the robust default; Perez stays available as a sensitivity.
    """
    import inspect

    signature = inspect.signature(pv.transpose)
    assert signature.parameters["model"].default == "haydavies"
