"""
Tests for the validation harness.

The harness is the project's evidence base, so its arithmetic needs checking as
much as the model's. In particular the algebraic property that makes specific
yield the right comparison -- packing factor and module efficiency cancel out
of it -- is asserted here rather than only claimed in prose.
"""

from __future__ import annotations

import pytest

from solaris.evals import metrics
from solaris.evals.harness import net_retention, specific_yield
from solaris.evals.references import (
    DailySeries,
)


class TestSpecificYieldAlgebra:
    def test_packing_and_efficiency_cancel(self):
        """
        The property the harness rests on: specific yield does not depend on
        packing factor or module efficiency, so it isolates the irradiance and
        loss chain from the least defensible constants in the model.
        """
        ghi, retention, derate, pr = 1800.0, 0.95, 0.94, 0.80
        area = 500.0
        reference = specific_yield(ghi, retention, derate, pr)

        for packing in (0.4, 0.7, 1.0):
            for efficiency in (0.15, 0.18, 0.22):
                energy = ghi * area * retention * derate * efficiency * pr * packing
                kwp = area * packing * efficiency  # x 1 kW/m2 at STC
                assert energy / kwp == pytest.approx(reference, rel=1e-12)

    def test_linear_in_irradiance(self):
        low = specific_yield(1000.0, 0.95, 0.94)
        high = specific_yield(2000.0, 0.95, 0.94)
        assert high == pytest.approx(2 * low, rel=1e-12)


class TestNetRetention:
    def test_no_obstruction_retains_everything(self):
        assert net_retention(0.6, 0.0, 1.0) == pytest.approx(1.0)

    @pytest.mark.parametrize("shadow", [0.0, 0.5, 1.0])
    def test_zero_beam_makes_shadow_irrelevant(self, shadow):
        assert net_retention(0.0, shadow, 1.0) == pytest.approx(1.0)

    @pytest.mark.parametrize("svf", [0.5, 0.8, 1.0])
    def test_full_beam_makes_sky_view_irrelevant(self, svf):
        assert net_retention(1.0, 0.25, svf) == pytest.approx(0.75)

    def test_matches_the_documented_decomposition(self):
        beam, shadow, svf = 0.54, 0.06, 0.96
        expected = (1 - beam) * svf + beam * (1 - shadow)
        assert net_retention(beam, shadow, svf) == pytest.approx(expected)

    def test_bounded(self):
        for beam in (0.0, 0.3, 0.6, 1.0):
            for shadow in (0.0, 0.5, 1.0):
                for svf in (0.0, 0.5, 1.0):
                    assert 0.0 <= net_retention(beam, shadow, svf) <= 1.0


class TestDailySeries:
    @staticmethod
    def _day_keys(year: int, n: int) -> list[str]:
        """NASA POWER keys days as YYYYMMDD, not year plus day-of-year."""
        from datetime import date, timedelta

        start = date(year, 1, 1)
        return [(start + timedelta(days=i)).strftime("%Y%m%d") for i in range(n)]

    @classmethod
    def _series(cls, per_day: float, year: int = 2021, n: int = 365):
        days = dict.fromkeys(cls._day_keys(year, n), per_day)
        return DailySeries(city="test", year=year, ghi=days)

    def test_annual_total(self):
        assert self._series(5.0).annual_ghi_kwh_m2() == pytest.approx(5.0 * 365)

    def test_partial_year_is_scaled_not_undercounted(self):
        """
        A half-fetched year must not report half the irradiance -- that would
        look like a genuinely low-irradiance site.
        """
        assert self._series(5.0, n=180).annual_ghi_kwh_m2() == pytest.approx(5.0 * 365, rel=1e-9)

    def test_leap_year_uses_366_days(self):
        assert self._series(5.0, year=2020, n=366).annual_ghi_kwh_m2() == pytest.approx(5.0 * 366)

    def test_empty_series_is_zero_not_an_error(self):
        assert DailySeries(city="x", year=2021).annual_ghi_kwh_m2() == 0.0

    def test_beam_fraction_from_diffuse(self):
        days = dict.fromkeys(self._day_keys(2021, 365), 5.0)
        diffuse = dict.fromkeys(days, 2.0)
        series = DailySeries(city="x", year=2021, ghi=days, diffuse=diffuse)
        assert series.beam_fraction() == pytest.approx(1.0 - 2.0 / 5.0)

    def test_beam_fraction_is_none_without_diffuse(self):
        assert self._series(5.0).beam_fraction() is None

    def test_monthly_totals_sum_to_the_annual_total(self):
        monthly = self._series(5.0).monthly_ghi_kwh_m2()
        assert set(monthly) == set(range(1, 13))
        assert sum(monthly.values()) == pytest.approx(5.0 * 365)


class TestMetrics:
    def test_perfect_agreement(self):
        got = metrics.compare([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
        assert got["MBE"] == 0.0
        assert got["RMSE"] == 0.0
        assert got["slope"] == pytest.approx(1.0)

    def test_signed_bias_is_preserved(self):
        """MBE must stay signed, or a systematic offset averages away."""
        assert metrics.compare([2.0, 3.0], [1.0, 2.0])["MBE"] == pytest.approx(1.0)
        assert metrics.compare([1.0, 2.0], [2.0, 3.0])["MBE"] == pytest.approx(-1.0)

    def test_cancelling_errors_show_in_mae_not_mbe(self):
        """
        The failure mode the harness must not hide: +15% in one season and -15%
        in the next looks perfect on MBE alone. This is exactly the error
        structure ERA5's aerosol climatology produces.
        """
        got = metrics.compare([2.0, 0.0], [1.0, 1.0])
        assert got["MBE"] == pytest.approx(0.0)
        assert got["MAE"] == pytest.approx(1.0)
        assert got["RMSE"] == pytest.approx(1.0)

    def test_relative_metrics(self):
        assert metrics.compare([110.0, 110.0], [100.0, 100.0])["rMBE_pct"] == pytest.approx(10.0)

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError, match="length mismatch"):
            metrics.compare([1.0], [1.0, 2.0])

    def test_empty_input(self):
        assert metrics.compare([], []) == {"n": 0}

    def test_describe(self):
        got = metrics.describe([1.0, 2.0, 3.0])
        assert got["n"] == 3
        assert got["mean"] == pytest.approx(2.0)
        assert got["min"] == 1.0
        assert got["max"] == 3.0

    def test_skill_score(self):
        assert metrics.skill_score(5.0, 10.0) == pytest.approx(0.5)
        assert metrics.skill_score(10.0, 10.0) == pytest.approx(0.0)
        assert metrics.skill_score(15.0, 10.0) < 0  # worse than the baseline
        assert metrics.skill_score(1.0, 0.0) is None
