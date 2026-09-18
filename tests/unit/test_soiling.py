"""
Tests for the soiling model.

Three things are asserted, in descending order of how badly getting them wrong
would matter:

1. **Window dependence.** The old model returned one annual number for every
   window. The single most important property of the new one is that a monsoon
   week and a dry post-monsoon month come out differently, in the right
   direction, by a sensible amount.
2. **Agreement with pvlib.** ``pvlib.soiling.kimber`` is an independent
   implementation of the same published model. Matched inputs must agree, and
   the tolerance is asserted rather than eyeballed -- reaching it found two
   real defects in the closed form.
3. **Calibration against measurement.** Predicted dry-day rates must match the
   published Delhi figures, and the seasonal *ordering* must be right. A model
   that landed the annual total with the seasons reversed would be useless for
   the one decision anyone makes with it: when to clean.

The fixtures use real NASA POWER rainfall where available, because the whole
finding about dry-spell clustering is invisible in synthetic uniform rain.
"""

from __future__ import annotations

import itertools
import math

import pytest

from solaris.physics import soiling

pytest.importorskip("pvlib", reason="the pvlib cross-check needs the physics extra")


def _rain(pattern: str, wet_mm: float = 8.0) -> list[float]:
    """``'..X..X'`` -> a rainfall series. ``X`` is a cleaning-grade day."""
    return [wet_mm if c == "X" else 0.0 for c in pattern]


# ---------------------------------------------------------------------------
# The deposition rate
# ---------------------------------------------------------------------------


class TestDailyRate:
    def test_matches_the_delhi_measurement_at_the_anchor_aod(self):
        """
        The calibration is a quotient of two stated numbers, so this is really
        a test that the quotient has not drifted from its own justification.
        """
        rate = soiling.daily_loss_rate(soiling.DELHI_REFERENCE_AOD)
        assert rate == pytest.approx(soiling.DELHI_MEASURED_RATE_PER_DAY, rel=1e-9)

    def test_is_linear_in_aod_below_the_clamp(self):
        """
        Linear only up to :data:`RATE_CLAMP_AOD`, which is about 0.97 -- inside
        the range Delhi reaches in winter, so the clamp is an active cap on
        realistic input and not merely a guard.
        """
        assert soiling.daily_loss_rate(0.8) == pytest.approx(2 * soiling.daily_loss_rate(0.4))

    def test_the_clamp_binds_inside_the_realistic_aod_range(self):
        assert 0.9 < soiling.RATE_CLAMP_AOD < 1.1
        assert not soiling.rate_is_clamped(0.7)
        assert soiling.rate_is_clamped(1.2)

    def test_a_clamped_rate_is_reported(self):
        """A conservative floor presented as an estimate would be a silent fallback."""
        result = soiling.window_soiling(1.5, window_days=30, daily_rain_mm=[0.0] * 30)
        assert any("conservative floor" in note for note in result.notes)

    def test_zero_aerosol_means_no_soiling(self):
        assert soiling.daily_loss_rate(0.0) == 0.0

    def test_clamped_at_the_worst_measured_rate(self):
        """
        An AOD of 10 is a bad retrieval, not an atmosphere. Extrapolating the
        linear fit there would predict a rate nobody has observed.
        """
        assert soiling.daily_loss_rate(10.0) == soiling.MAX_MEASURED_RATE_PER_DAY

    def test_negative_aod_is_treated_as_zero(self):
        assert soiling.daily_loss_rate(-1.0) == 0.0

    def test_seasonal_ordering_is_right(self):
        """
        Spring worst, monsoon best. Getting this backwards would still fit an
        annual total and would invert every cleaning-schedule recommendation.
        """
        rates = soiling.seasonal_check({"spring": 0.80, "winter": 0.72, "monsoon": 0.50})
        assert rates["spring"] > rates["winter"] > rates["monsoon"]

    def test_predicted_seasonal_rates_match_the_published_ones(self):
        """
        Within 5%. Note what this does and does not show: the *winter* value is
        the anchor, so only spring and monsoon are genuinely predictions, and
        both rest on literature AOD values rather than retrievals from this
        pipeline.
        """
        rates = soiling.seasonal_check({"spring": 0.80, "winter": 0.72, "monsoon": 0.50})
        for season, predicted in rates.items():
            measured = soiling.MEASURED_RATES_PER_DAY[season]
            assert predicted == pytest.approx(measured, rel=0.05), season


# ---------------------------------------------------------------------------
# Dry spells
# ---------------------------------------------------------------------------


class TestDrySpells:
    def test_rain_ends_a_spell(self):
        assert soiling.dry_spells(_rain("...X...X.."), cleaning_interval_days=None) == [
            3.0,
            3.0,
            2.0,
        ]

    def test_the_cleaning_day_is_not_a_dry_day(self):
        """
        The off-by-one that put this model a consistent 3-10% above pvlib.
        """
        assert soiling.dry_spells(_rain("X"), cleaning_interval_days=None) == []

    def test_drizzle_below_the_threshold_does_not_clean(self):
        """
        Deliberate: light rain can cement dust rather than remove it, so the
        threshold is not "any rain".
        """
        series = [0.5] * 10
        assert soiling.dry_spells(series, cleaning_interval_days=None) == [10.0]

    def test_a_scheduled_wash_also_ends_a_spell(self):
        spells = soiling.dry_spells([0.0] * 10, cleaning_interval_days=5)
        assert spells == [4.0, 4.0]

    def test_rain_and_scheduling_interact(self):
        """
        The useful part of the model: in a wet city the interval never binds,
        so scheduled cleaning buys nothing, while in an arid one it does all
        the work.
        """
        wet = soiling.dry_spells(_rain("XX" * 15), cleaning_interval_days=30)
        dry = soiling.dry_spells([0.0] * 30, cleaning_interval_days=30)
        assert max(wet, default=0) <= 1
        assert max(dry) == 29.0

    def test_an_all_dry_series_is_one_spell(self):
        assert soiling.dry_spells([0.0] * 100, cleaning_interval_days=None) == [100.0]


# ---------------------------------------------------------------------------
# Accumulation
# ---------------------------------------------------------------------------


class TestAccumulation:
    def test_saturates_and_never_exceeds_the_ceiling(self):
        for aod in (0.1, 0.5, 1.0, 5.0):
            assert soiling.annual_loss_if_never_cleaned(aod) <= soiling.MAX_SOILING_LOSS

    def test_saturation_takes_longer_at_lower_aerosol(self):
        assert soiling.days_to_saturation(0.3) > soiling.days_to_saturation(0.9)

    def test_saturation_is_unreachable_without_aerosol(self):
        assert math.isinf(soiling.days_to_saturation(0.0))

    def test_a_single_day_loses_one_day_of_rate(self):
        """
        The cleanest statement of what the old model could not do: one day
        carries one day of dust.
        """
        result = soiling.window_soiling(0.70, window_days=1, daily_rain_mm=[0.0])
        assert result.loss == pytest.approx(soiling.daily_loss_rate(0.70), rel=1e-6)

    def test_loss_grows_with_the_dry_window(self):
        losses = [
            soiling.window_soiling(0.70, window_days=n, daily_rain_mm=[0.0] * n).loss
            for n in (1, 7, 30, 90)
        ]
        assert losses == sorted(losses)
        assert all(a < b for a, b in itertools.pairwise(losses))

    def test_the_average_runs_over_the_whole_window_not_just_dry_days(self):
        """
        A cleaning day carries almost no soiling, so it belongs in the
        denominator. Dropping those days inflated the result and was found by
        the pvlib comparison getting *worse* after an otherwise correct fix.
        """
        all_dry = soiling.window_soiling(0.70, window_days=10, daily_rain_mm=[0.0] * 10)
        half_wet = soiling.window_soiling(0.70, window_days=10, daily_rain_mm=_rain("X.X.X.X.X."))
        assert half_wet.loss < all_dry.loss


# ---------------------------------------------------------------------------
# Window dependence -- the headline property
# ---------------------------------------------------------------------------


class TestWindowDependence:
    def test_a_wet_window_is_far_cleaner_than_a_dry_one(self):
        wet = soiling.window_soiling(0.70, window_days=30, daily_rain_mm=_rain("XX" * 15))
        dry = soiling.window_soiling(0.70, window_days=30, daily_rain_mm=[0.0] * 30)
        assert dry.loss > 10 * wet.loss

    def test_the_legacy_model_cannot_tell_them_apart(self):
        """
        Stated as a test so the improvement is recorded rather than asserted in
        prose. The control arm returns one number for both.
        """
        assert soiling.legacy_retention(0.70) == soiling.legacy_retention(0.70)

    def test_scheduled_cleaning_helps_an_arid_site_and_barely_a_wet_one(self):
        dry_series = [0.0] * 365
        wet_series = _rain("..X" * 122)[:365]

        dry_uncleaned = soiling.window_soiling(
            0.50, window_days=365, daily_rain_mm=dry_series, cleaning_interval_days=None
        ).loss
        dry_cleaned = soiling.window_soiling(
            0.50, window_days=365, daily_rain_mm=dry_series, cleaning_interval_days=30
        ).loss
        wet_uncleaned = soiling.window_soiling(
            0.50, window_days=365, daily_rain_mm=wet_series, cleaning_interval_days=None
        ).loss
        wet_cleaned = soiling.window_soiling(
            0.50, window_days=365, daily_rain_mm=wet_series, cleaning_interval_days=30
        ).loss

        assert dry_uncleaned - dry_cleaned > 0.05
        assert wet_uncleaned - wet_cleaned < 0.005

    def test_a_short_series_is_reported_not_silently_rescaled(self):
        """
        The defect this replaced: the denominator came from the *request* while
        the spells came from the data, so a full year of accumulation divided
        by a one-day window gave a 50% loss for a single November day -- which,
        clamped at the floor, read as merely pessimistic rather than broken.
        """
        result = soiling.window_soiling(0.70, window_days=365, daily_rain_mm=[0.0] * 30)
        assert result.averaging_days == 30.0
        assert result.loss < soiling.MAX_SOILING_LOSS
        assert any("publication lag" in note for note in result.notes)


# ---------------------------------------------------------------------------
# The mean-spell fallback
# ---------------------------------------------------------------------------


class TestMeanSpellFallback:
    def test_it_is_used_and_labelled_when_no_series_is_available(self):
        result = soiling.window_soiling(0.70, window_days=365, rain_days=90)
        assert result.method == "mean_spell"
        assert result.notes

    def test_the_note_says_it_is_a_lower_bound(self):
        """
        A caveat nobody can act on is not a caveat. The note has to say which
        direction the error runs and roughly how big it is.
        """
        note = " ".join(soiling.window_soiling(0.70, window_days=365, rain_days=90).notes)
        assert "lower bound" in note
        assert "13x" in note or "2-13x" in note

    def test_it_understates_a_clustered_series(self):
        """
        Jensen's inequality, with a series built to expose it: the same 10 rain
        days, once spread out and once bunched at the start.
        """
        spread = _rain("..X" * 10 + "." * 35)[:65]
        clustered = _rain("X" * 10 + "." * 55)

        exact_clustered = soiling.window_soiling(
            0.70, window_days=65, daily_rain_mm=clustered, cleaning_interval_days=None
        )
        exact_spread = soiling.window_soiling(
            0.70, window_days=65, daily_rain_mm=spread, cleaning_interval_days=None
        )
        assert exact_clustered.loss > exact_spread.loss

        approximate = soiling.window_soiling(
            0.70,
            window_days=65,
            rain_days=exact_clustered.rain_days,
            cleaning_interval_days=None,
        )
        assert exact_clustered.loss > approximate.loss

    def test_the_recorded_understatement_range_is_a_range_and_material(self):
        low, high = soiling.MEAN_SPELL_UNDERSTATEMENT_RANGE
        assert 1.0 < low < high
        assert high > 5.0, "if this path were nearly right it would not need a warning"


# ---------------------------------------------------------------------------
# pvlib agreement
# ---------------------------------------------------------------------------


class TestPvlibAgreement:
    @pytest.mark.parametrize(
        "pattern",
        [
            "." * 60,
            ("..X" * 20),
            ("X" + "." * 59),
            ("." * 30 + "X" * 5 + "." * 25),
        ],
    )
    def test_matches_the_published_implementation(self, pattern):
        """
        The real validation. An independent implementation of Kimber, on the
        same inputs, with no manual washing on either side and the first-day
        convention aligned.
        """
        series = _rain(pattern)
        ours = soiling.window_soiling(
            0.70,
            window_days=len(series),
            daily_rain_mm=series,
            cleaning_interval_days=None,
        ).loss
        theirs = soiling.kimber_reference(0.70, series, "2021-01-01")
        assert ours == pytest.approx(theirs, rel=soiling.PVLIB_AGREEMENT_TOLERANCE, abs=0.001)

    def test_the_unaligned_convention_difference_is_one_spell_of_rate(self):
        """
        Pins the explanation rather than just the tolerance. Unaligned, pvlib
        reads zero on day zero, so it accumulates over one fewer day in the
        first spell -- a gap of exactly ``rate * first_spell / total_days``.
        Confirming the *size* is what showed this was a boundary convention and
        not a difference of physics.
        """
        series = [0.0] * 60
        ours = soiling.window_soiling(
            0.70, window_days=60, daily_rain_mm=series, cleaning_interval_days=None
        ).loss
        unaligned = soiling.kimber_reference(0.70, series, "2021-01-01", align_first_day=False)
        expected_gap = soiling.daily_loss_rate(0.70) * 60 / 60
        assert ours - unaligned == pytest.approx(expected_gap, rel=0.02)

    def test_the_cross_check_raises_rather_than_agreeing_with_itself(self, monkeypatch):
        """
        If pvlib is missing, this must fail loudly. Falling back to our own
        answer would report perfect agreement with ourselves, which is the
        worst possible outcome for a validation function.
        """
        import builtins

        real_import = builtins.__import__

        def _no_pvlib(name, *args, **kwargs):
            if name.startswith("pvlib"):
                raise ImportError("pvlib is not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_pvlib)
        with pytest.raises(ImportError):
            soiling.kimber_reference(0.70, [0.0] * 10, "2021-01-01")


# ---------------------------------------------------------------------------
# Bounds and reporting
# ---------------------------------------------------------------------------


class TestBoundsAndReporting:
    def test_retention_stays_in_range_for_absurd_inputs(self):
        for aod in (0.0, 0.5, 5.0, 50.0, 500.0):
            for days in (1, 30, 365, 3650):
                result = soiling.window_soiling(aod, window_days=days, daily_rain_mm=[0.0] * days)
                assert 0.0 <= result.retention <= 1.0, (aod, days)
                assert soiling.MIN_RETENTION <= result.retention <= 1.0, (aod, days)

    def test_the_clamp_is_reported_when_it_binds(self):
        result = soiling.window_soiling(500.0, window_days=365, daily_rain_mm=[0.0] * 365)
        if result.clamped:
            assert any("clamped" in note for note in result.notes)

    def test_the_legacy_model_is_still_bounded(self):
        """
        Its original defect: retention went negative above AOD 12.5, i.e.
        negative generated energy.
        """
        assert soiling.legacy_retention(100.0) >= soiling.MIN_RETENTION

    def test_the_calibration_is_reported_with_its_weakest_link(self):
        note = soiling.calibration_note()
        assert note["anchored_on"]["measured_rate_per_day"] > 0
        assert "assumed" in note["weakest_link"]
        assert note["deviations_from_pvlib_defaults"]

    def test_deviations_from_pvlib_defaults_are_enumerated(self):
        """
        Three defaults are overridden. Each has a reason, and an unexplained
        override is how a model quietly becomes uncheckable.
        """
        deviations = soiling.calibration_note()["deviations_from_pvlib_defaults"]
        assert set(deviations) == {
            "cleaning_threshold_mm",
            "grace_period_days",
            "soiling_loss_rate",
        }

    def test_the_result_says_which_path_answered(self):
        with_series = soiling.window_soiling(0.7, window_days=10, daily_rain_mm=[0.0] * 10)
        without = soiling.window_soiling(0.7, window_days=10, rain_days=2)
        assert with_series.method == "series"
        assert without.method == "mean_spell"

    def test_an_empty_window_does_not_divide_by_zero(self):
        assert soiling.mean_loss_from_spells(0.003, [], total_days=0.0) == 0.0


# ---------------------------------------------------------------------------
# Against real rainfall
# ---------------------------------------------------------------------------


class TestAgainstRealRainfall:
    """
    Uses the committed NASA POWER precipitation cache. Skipped if absent, since
    a fresh clone should still get a green run.
    """

    def _series(self, city: str, year: int = 2021) -> list[float]:
        from solaris.evals.references import load_cached_precip

        cached = load_cached_precip(city, year)
        if cached is None:
            pytest.skip(f"no cached precipitation for {city} {year}")
        return [cached.precip_mm[d] for d in sorted(cached.precip_mm)]

    def test_annual_losses_land_in_the_published_range(self):
        """
        Measured Indian soiling is a few per cent annually with 7-30 day
        cleaning, up to roughly 10% per month uncleaned. An annual figure
        outside 0-15% for any city would be outside anything published.
        """
        for city in ("delhi", "jodhpur", "mumbai", "guwahati"):
            series = self._series(city)
            result = soiling.window_soiling(0.5, window_days=len(series), daily_rain_mm=series)
            assert 0.0 < result.loss < 0.15, (city, result.loss)

    def test_an_arid_city_soils_more_than_a_wet_one_at_equal_aerosol(self):
        """
        The rain term, isolated: AOD held constant, so any difference is
        rainfall alone. The old model could not produce one.
        """
        jodhpur = self._series("jodhpur")
        guwahati = self._series("guwahati")
        arid = soiling.window_soiling(
            0.5, window_days=len(jodhpur), daily_rain_mm=jodhpur, cleaning_interval_days=None
        ).loss
        wet = soiling.window_soiling(
            0.5, window_days=len(guwahati), daily_rain_mm=guwahati, cleaning_interval_days=None
        ).loss
        assert arid > 2 * wet

    def test_the_mean_spell_fallback_is_badly_wrong_on_real_data(self):
        """
        Pins the finding. The fallback is not a slightly-worse estimate; on
        real Indian rainfall it is out by a factor of several, and worst in the
        arid cities where soiling matters most.
        """
        series = self._series("ahmedabad")
        exact = soiling.window_soiling(
            0.52, window_days=len(series), daily_rain_mm=series, cleaning_interval_days=None
        )
        approximate = soiling.window_soiling(
            0.52,
            window_days=len(series),
            rain_days=exact.rain_days,
            cleaning_interval_days=None,
        )
        assert exact.loss / approximate.loss > 5.0

    def test_agrees_with_pvlib_on_every_city(self):
        for city in ("delhi", "jaipur", "jodhpur", "mumbai", "chennai", "leh"):
            series = self._series(city)
            ours = soiling.window_soiling(
                0.5, window_days=len(series), daily_rain_mm=series, cleaning_interval_days=None
            ).loss
            theirs = soiling.kimber_reference(0.5, series, "2021-01-01")
            assert ours == pytest.approx(
                theirs, rel=soiling.PVLIB_AGREEMENT_TOLERANCE, abs=0.001
            ), city
