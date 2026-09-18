"""
Tests for the beam/diffuse decomposition model.

The emphasis is on the things that make an ML result trustworthy rather than
merely good-looking:

* the holdout is spatial **and** temporal, so a score cannot come from testing
  on hours whose neighbours were trained on,
* the published Erbs correlation is the baseline, not the constant — beating a
  constant proves nothing,
* the selection gate is declared before training and cannot be relaxed to suit
  the outcome.

Also asserted: the physics the features encode. Diffuse fraction must fall as
the clearness index rises, because that is what the atmosphere does; a model
that got that backwards would still score reasonably on RMSE.
"""

from __future__ import annotations

import itertools
import math

import pytest

from solaris.ml import decomposition, features, splits

pytest.importorskip("sklearn", reason="the ML tracks need the ml extra")
pytest.importorskip("numpy")


def _sample(
    *,
    kt: float = 0.6,
    city: str = "delhi",
    year: int = 2021,
    stamp: str = "2021061512",
    diffuse_fraction: float = 0.4,
    abs_latitude: float = 28.6,
    ghi: float = 500.0,
) -> features.Sample:
    elevation = 50.0
    return features.Sample(
        city=city,
        year=year,
        stamp=stamp,
        kt=kt,
        sin_elevation=math.sin(math.radians(elevation)),
        air_mass=features.kasten_young_air_mass(elevation),
        sin_doy=0.0,
        cos_doy=1.0,
        abs_latitude=abs_latitude,
        kt_persistence=1.0,
        diffuse_fraction=diffuse_fraction,
        ghi=ghi,
    )


class TestFeatureMaths:
    def test_air_mass_is_one_at_zenith(self):
        assert features.kasten_young_air_mass(90.0) == pytest.approx(1.0, abs=0.01)

    def test_air_mass_grows_towards_the_horizon(self):
        values = [features.kasten_young_air_mass(e) for e in (90, 60, 30, 10, 5)]
        assert all(a < b for a, b in itertools.pairwise(values)), values

    def test_air_mass_stays_finite_at_the_horizon(self):
        """
        Kasten-Young is used rather than 1/sin(elevation) precisely because the
        latter diverges here.
        """
        assert features.kasten_young_air_mass(0.0) <= 40.0
        assert math.isfinite(features.kasten_young_air_mass(0.1))

    def test_extraterrestrial_tracks_the_eccentricity_cycle(self):
        """
        Earth-Sun distance varies ~3.3% over a year, which is not negligible
        when it sits in the denominator of the clearness index.
        """
        january = features.extraterrestrial_horizontal(5, 1.0)
        july = features.extraterrestrial_horizontal(186, 1.0)
        assert january > july
        assert (january - july) / july == pytest.approx(0.066, abs=0.02)

    def test_extraterrestrial_is_zero_below_the_horizon(self):
        assert features.extraterrestrial_horizontal(100, -0.5) == 0.0

    def test_feature_vector_matches_the_declared_names(self):
        sample = _sample()
        assert len(sample.as_features()) == len(features.FEATURE_NAMES)

    def test_day_of_year_is_cyclical(self):
        """
        Encoded as sin/cos rather than an integer: an integer lets a tree carve
        arbitrary date groupings, and the cyclical pair makes December adjacent
        to January, which it is.
        """
        assert "sin_doy" in features.FEATURE_NAMES
        assert "cos_doy" in features.FEATURE_NAMES
        assert "day_of_year" not in features.FEATURE_NAMES


class TestErbsBaseline:
    """
    Erbs et al. (1982). Checked against the published piecewise form, because
    it is the baseline the learned model has to beat.
    """

    def test_overcast_is_almost_entirely_diffuse(self):
        assert decomposition.ErbsModel.diffuse_fraction(0.1) > 0.95

    def test_very_clear_is_mostly_beam(self):
        assert decomposition.ErbsModel.diffuse_fraction(0.85) == pytest.approx(0.165)

    def test_monotone_decreasing_over_the_middle_branch(self):
        """
        The physics: as the atmosphere removes less, a smaller share of what
        arrives is scattered. A model that got this backwards would still score
        plausibly on RMSE, so it is worth asserting directly.
        """
        values = [
            decomposition.ErbsModel.diffuse_fraction(kt)
            for kt in (0.25, 0.35, 0.45, 0.55, 0.65, 0.75)
        ]
        assert all(a > b for a, b in itertools.pairwise(values)), values

    def test_bounded(self):
        for kt in [i / 100 for i in range(0, 101)]:
            value = decomposition.ErbsModel.diffuse_fraction(kt)
            assert 0.0 <= value <= 1.0, kt

    def test_is_continuous_at_the_branch_points(self):
        for boundary in (0.22, 0.80):
            below = decomposition.ErbsModel.diffuse_fraction(boundary - 1e-6)
            above = decomposition.ErbsModel.diffuse_fraction(boundary + 1e-6)
            assert abs(below - above) < 0.05, boundary


class TestConstantAndClimatology:
    def test_the_constant_is_the_production_fallback(self):
        """Rung one is exactly what production does today, so it is the floor."""
        model = decomposition.ConstantModel()
        assert model.value == pytest.approx(1.0 - decomposition.CURRENT_FALLBACK)

    def test_climatology_learns_a_monthly_table(self):
        train = [
            _sample(stamp="2021011512", diffuse_fraction=0.7),
            _sample(stamp="2021011513", diffuse_fraction=0.7),
            _sample(stamp="2021061512", diffuse_fraction=0.3),
            _sample(stamp="2021061513", diffuse_fraction=0.3),
        ]
        model = decomposition.ClimatologyModel().fit(train)
        january = model.predict([_sample(stamp="2021011514")])[0]
        june = model.predict([_sample(stamp="2021061514")])[0]
        assert january == pytest.approx(0.7)
        assert june == pytest.approx(0.3)

    def test_climatology_falls_back_to_the_global_mean(self):
        model = decomposition.ClimatologyModel().fit(
            [_sample(stamp="2021061512", diffuse_fraction=0.4)]
        )
        unseen = model.predict([_sample(stamp="2021121512", abs_latitude=5.0)])[0]
        assert unseen == pytest.approx(0.4)


class TestScoring:
    def test_perfect_prediction_scores_zero(self):
        samples = [_sample(diffuse_fraction=0.4)]
        result = decomposition.score("x", [0.4], samples)
        assert result.rmse == 0.0
        assert result.mbe == 0.0

    def test_bias_is_signed(self):
        samples = [_sample(diffuse_fraction=0.4)]
        assert decomposition.score("x", [0.5], samples).mbe == pytest.approx(0.1)
        assert decomposition.score("x", [0.3], samples).mbe == pytest.approx(-0.1)

    def test_ghi_weighting_emphasises_bright_hours(self):
        """
        An error at midday costs more energy than the same error at dusk, so the
        weighted metric is the one that matters for yield.
        """
        samples = [
            _sample(diffuse_fraction=0.4, ghi=900.0),
            _sample(diffuse_fraction=0.4, ghi=50.0),
        ]
        accurate_at_noon = decomposition.score("a", [0.40, 0.60], samples)
        accurate_at_dusk = decomposition.score("b", [0.60, 0.40], samples)
        assert accurate_at_noon.rmse == pytest.approx(accurate_at_dusk.rmse)
        assert accurate_at_noon.rmse_ghi_weighted < accurate_at_dusk.rmse_ghi_weighted

    def test_empty_sample_set_does_not_divide_by_zero(self):
        result = decomposition.score("x", [], [])
        assert result.n == 0


class TestSelectionGate:
    """
    The gate is the discipline, so it gets its own tests. With a few thousand
    samples from ten sites it would be easy to ship a model with a flattering
    score and no real advantage over a forty-year-old published correlation.
    """

    def test_a_marginal_learned_model_does_not_ship(self):
        scores = [
            decomposition.Score("erbs", 100, 0.10, 0.08, 0.0, skill_vs_erbs=0.0),
            decomposition.Score("gradient_boosting", 100, 0.095, 0.07, 0.0, skill_vs_erbs=0.05),
        ]
        winner, reason = decomposition.select_winner(scores)
        assert winner == "erbs"
        assert "under the" in reason

    def test_a_clearly_better_learned_model_ships(self):
        scores = [
            decomposition.Score("erbs", 100, 0.10, 0.08, 0.0, skill_vs_erbs=0.0),
            decomposition.Score("gradient_boosting", 100, 0.07, 0.05, 0.0, skill_vs_erbs=0.30),
        ]
        winner, reason = decomposition.select_winner(scores)
        assert winner == "gradient_boosting"
        assert "clearing the" in reason

    def test_the_gate_threshold_is_a_module_constant(self):
        """
        Declared in the module rather than computed at selection time, so it
        cannot be adjusted to suit a result.
        """
        assert decomposition.MIN_SKILL_OVER_ERBS == 0.10

    def test_erbs_is_the_baseline_not_the_constant(self):
        """
        Skill is measured against Erbs. Measuring against the constant would
        make almost anything look good.
        """
        scores = [
            decomposition.Score("constant", 10, 0.25, 0.2, 0.0),
            decomposition.Score("erbs", 10, 0.10, 0.08, 0.0),
            decomposition.Score("gradient_boosting", 10, 0.20, 0.15, 0.0),
        ]
        _winner, _reason = decomposition.select_winner(scores)
        gbm = next(s for s in scores if s.name == "gradient_boosting")
        # Worse than Erbs despite beating the constant handily.
        assert gbm.skill_vs_erbs is None or gbm.skill_vs_erbs < 0


class TestSplits:
    def test_holdout_is_spatial_and_temporal(self):
        samples = [
            _sample(city="delhi", year=2021),  # train
            _sample(city="delhi", year=2022),  # withheld: held-out year only
            _sample(city="mumbai", year=2021),  # withheld: held-out city only
            _sample(city="mumbai", year=2022),  # test: both
        ]
        split = splits.split_samples(samples)
        assert len(split.train) == 1
        assert len(split.test) == 1
        assert len(split.withheld) == 2
        assert split.test[0].city == "mumbai"
        assert split.test[0].year == 2022

    def test_no_city_appears_in_both_sides(self):
        samples = [
            _sample(city=city, year=year)
            for city in ("delhi", "mumbai", "jaipur", "bengaluru")
            for year in (2020, 2021, 2022)
        ]
        split = splits.split_samples(samples)
        train_cities = {s.city for s in split.train}
        test_cities = {s.city for s in split.test}
        assert not (train_cities & test_cities)

    def test_no_year_appears_in_both_sides(self):
        samples = [
            _sample(city=city, year=year)
            for city in ("delhi", "mumbai")
            for year in (2020, 2021, 2022)
        ]
        split = splits.split_samples(samples)
        assert not ({s.year for s in split.train} & {s.year for s in split.test})

    def test_no_spatial_leakage_between_sites(self):
        """
        Adding a city to the evaluation set without checking its separation
        would quietly weaken the holdout, so it is asserted.
        """
        assert splits.spatial_leakage() == []

    def test_haversine_is_sane(self):
        # Delhi to Mumbai is roughly 1160 km.
        distance = splits.haversine_km(28.6139, 77.2090, 19.0760, 72.8777)
        assert 1100 < distance < 1250

    def test_test_sites_span_distinct_regimes(self):
        """Clustered test sites would make the holdout easier than it looks."""
        assert {"mumbai", "bengaluru", "guwahati"} == splits.TEST_CITIES


class TestTrainedLadderAgainstCachedData:
    """Runs on the committed hourly climatology, so it needs no network."""

    @pytest.fixture(scope="class")
    def report(self):
        from solaris.ml.train import run

        # persist=False, deliberately. This fixture used to promote its winner
        # to ml/artifacts/, so every test run rewrote a committed file with a
        # fresh version and timestamp -- leaving `git status` permanently dirty
        # and making the shipped model whatever the last test run produced.
        result = run(persist=False)
        if "skipped" in result:
            pytest.skip(result["skipped"])
        return result

    def test_evaluating_does_not_promote_a_model(self, report):
        """
        The guard for the above. An evaluation must leave the artifact alone,
        and the report must say so rather than leaving it ambiguous.
        """
        assert report["persisted"] is False
        assert report["saved"] is None

    def test_dataset_is_non_trivial(self, report):
        assert report["n_samples"] > 500

    def test_both_split_sides_are_populated(self, report):
        assert report["split"]["n_train"] > 100
        assert report["split"]["n_test"] > 50

    def test_no_leakage_recorded(self, report):
        assert report["spatial_leakage"] == []

    def test_every_rung_was_evaluated(self, report):
        names = {s["name"] for s in report["scores"]}
        assert {"constant", "climatology", "erbs", "ridge", "gradient_boosting"} <= names

    def test_erbs_beats_the_constant(self, report):
        """
        A sanity check on the whole setup. If a published correlation driven by
        the clearness index did not beat a fixed number, something would be
        wrong with the features or the target.
        """
        by_name = {s["name"]: s for s in report["scores"]}
        assert by_name["erbs"]["rmse"] < by_name["constant"]["rmse"]

    def test_a_winner_was_chosen_with_a_stated_reason(self, report):
        assert report["winner"] in {
            "erbs",
            "ridge",
            "gradient_boosting",
            "climatology",
        }
        assert len(report["reason"]) > 40

    def test_the_reference_beam_fraction_is_below_the_production_constant(self, report):
        """
        The measured motivation for this work: ERA5 uses a monthly aerosol
        climatology and overestimates direct radiation, with the error growing
        in aerosol load -- exactly India's regime.
        """
        observed = report["observed"]
        assert (
            observed["implied_mean_beam_fraction"] < observed["production_fallback_beam_fraction"]
        )

    def test_report_serialises(self, report):
        import json

        json.dumps(report)

    def test_markdown_renders(self, report):
        from solaris.ml.train import format_markdown

        text = format_markdown(report)
        assert "The ladder" in text
        assert "Erbs" in text
        assert "Winner" in text
