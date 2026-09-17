"""
The beam/diffuse decomposition model, as a ladder of four candidates.

The gate, stated before any training
------------------------------------
Four rungs are fitted and all four are reported. **The simplest one that wins
ships.** Specifically: if the learned model's skill over the published Erbs
correlation is under 0.10, Erbs ships instead.

Committing to that up front is what keeps this honest. With roughly a few
thousand samples from ten sites it would be easy to produce a gradient-boosting
model with a flattering in-sample score and no real advantage over a published
correlation that has been validated worldwide for forty years. A model has to
earn its place against that, not against nothing.

The rungs
---------
1. ``constant``     -- the 0.60 the model currently falls back to. The thing
                       being replaced, so it is the floor to beat.
2. ``climatology``  -- per-month, per-latitude-band mean diffuse fraction. No
                       ML, 36 parameters, completely transparent.
3. ``erbs``         -- Erbs et al. (1982), via pvlib. A **published**
                       correlation, and the real baseline: beating the constant
                       proves nothing, beating Erbs would be a result.
4. ``gradient_boosting`` / ``ridge`` -- learned, on the features in
                       :mod:`solaris.ml.features`.

Splitting
---------
Spatial **and** temporal holdout, both at once. Hours within a city-day are
strongly correlated, so a random row split would report a fantasy score: the
model would be tested on hours whose neighbours it trained on. See
:mod:`solaris.ml.splits`.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Protocol

from solaris.ml.features import FEATURE_NAMES, Sample

#: The constant the production code falls back to when ERA5 sampling fails.
#: Rung one, and the thing this work exists to improve on.
CURRENT_FALLBACK = 0.60

#: Minimum skill over Erbs for a learned model to ship. Declared here, before
#: training, so it cannot be adjusted to suit the result.
MIN_SKILL_OVER_ERBS = 0.10

#: Latitude bands for the climatology rung, degrees.
LATITUDE_BANDS = ((0.0, 15.0), (15.0, 25.0), (25.0, 90.0))


class Predictor(Protocol):
    name: str

    def predict(self, samples: list[Sample]) -> list[float]: ...


# ---------------------------------------------------------------------------
# Rung 1: the constant
# ---------------------------------------------------------------------------


@dataclass
class ConstantModel:
    """The current production fallback, as a baseline."""

    value: float = 1.0 - CURRENT_FALLBACK  # diffuse fraction, not beam
    name: str = "constant"

    def fit(self, samples: list[Sample]) -> ConstantModel:
        return self

    def predict(self, samples: list[Sample]) -> list[float]:
        return [self.value] * len(samples)


# ---------------------------------------------------------------------------
# Rung 2: per-month, per-latitude-band climatology
# ---------------------------------------------------------------------------


def _latitude_band(abs_lat: float) -> int:
    for index, (low, high) in enumerate(LATITUDE_BANDS):
        if low <= abs_lat < high:
            return index
    return len(LATITUDE_BANDS) - 1


def _month_from_stamp(stamp: str) -> int:
    return int(stamp[4:6])


@dataclass
class ClimatologyModel:
    """
    Mean diffuse fraction per (month, latitude band).

    36 transparent parameters, no ML, and it cannot extrapolate badly. If the
    learned rung cannot clearly beat this, this is what should ship.
    """

    table: dict[tuple[int, int], float] = field(default_factory=dict)
    global_mean: float = 0.5
    name: str = "climatology"

    def fit(self, samples: list[Sample]) -> ClimatologyModel:
        buckets: dict[tuple[int, int], list[float]] = {}
        for sample in samples:
            key = (_month_from_stamp(sample.stamp), _latitude_band(sample.abs_latitude))
            buckets.setdefault(key, []).append(sample.diffuse_fraction)
        self.table = {k: sum(v) / len(v) for k, v in buckets.items()}
        self.global_mean = (
            sum(s.diffuse_fraction for s in samples) / len(samples) if samples else 0.5
        )
        return self

    def predict(self, samples: list[Sample]) -> list[float]:
        out = []
        for sample in samples:
            key = (_month_from_stamp(sample.stamp), _latitude_band(sample.abs_latitude))
            out.append(self.table.get(key, self.global_mean))
        return out


# ---------------------------------------------------------------------------
# Rung 3: Erbs -- the published baseline
# ---------------------------------------------------------------------------


@dataclass
class ErbsModel:
    """
    Erbs et al. (1982), the standard published correlation.

    Implemented from the piecewise form rather than called through pvlib's
    hourly API, because pvlib wants a full time-indexed series while the
    evaluation works on an arbitrary sample list. The coefficients are the
    published ones.

    This is the baseline that matters. It has been validated worldwide for
    decades, so a learned model that cannot beat it is not worth shipping or
    maintaining.
    """

    name: str = "erbs"

    def fit(self, samples: list[Sample]) -> ErbsModel:
        return self

    @staticmethod
    def diffuse_fraction(kt: float) -> float:
        if kt <= 0.22:
            return 1.0 - 0.09 * kt
        if kt <= 0.80:
            return 0.9511 - 0.1604 * kt + 4.388 * kt**2 - 16.638 * kt**3 + 12.336 * kt**4
        return 0.165

    def predict(self, samples: list[Sample]) -> list[float]:
        return [min(max(self.diffuse_fraction(sample.kt), 0.0), 1.0) for sample in samples]


# ---------------------------------------------------------------------------
# Rung 4: learned
# ---------------------------------------------------------------------------


@dataclass
class LearnedModel:
    """
    A learned decomposition model.

    Gradient boosting by default: a few thousand tabular rows with heterogeneous
    feature scales and a likely non-linear interaction between clearness index
    and air mass is exactly what trees handle well, and a neural net on this
    much data would be indefensible. Ridge is available as the linear rung.
    """

    kind: str = "gradient_boosting"
    model: object | None = None
    name: str = "gradient_boosting"

    def fit(self, samples: list[Sample]) -> LearnedModel:
        import numpy as np

        features = np.array([s.as_features() for s in samples], dtype=float)
        target = np.array([s.diffuse_fraction for s in samples], dtype=float)

        if self.kind == "ridge":
            from sklearn.linear_model import Ridge
            from sklearn.pipeline import make_pipeline
            from sklearn.preprocessing import StandardScaler

            self.model = make_pipeline(StandardScaler(), Ridge(alpha=1.0))
            self.name = "ridge"
        else:
            from sklearn.ensemble import HistGradientBoostingRegressor

            # Modest depth and a leaf floor: with a few thousand rows from ten
            # sites, a deeper model memorises sites rather than learning the
            # physics.
            self.model = HistGradientBoostingRegressor(
                max_depth=4,
                max_iter=300,
                learning_rate=0.06,
                min_samples_leaf=40,
                l2_regularization=1.0,
                random_state=0,
            )
            self.name = "gradient_boosting"

        self.model.fit(features, target)
        return self

    def predict(self, samples: list[Sample]) -> list[float]:
        import numpy as np

        if self.model is None:
            raise RuntimeError("model is not fitted")
        features = np.array([s.as_features() for s in samples], dtype=float)
        return [float(min(max(v, 0.0), 1.0)) for v in self.model.predict(features)]

    def feature_importance(self) -> dict[str, float] | None:
        """
        Permutation importance is not computed here; for the tree model the
        split-based importance is not exposed by HistGradientBoosting, so this
        returns None rather than inventing a number.
        """
        return None


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass
class Score:
    name: str
    n: int
    rmse: float
    mae: float
    mbe: float
    #: Skill against Erbs: ``1 - rmse/rmse_erbs``. Zero or negative means the
    #: rung adds nothing over the published correlation.
    skill_vs_erbs: float | None = None
    #: Weighted by GHI, because an error at midday matters more than at dusk.
    rmse_ghi_weighted: float | None = None


def score(name: str, predicted: list[float], samples: list[Sample]) -> Score:
    """Error metrics for one rung on one split."""
    n = len(samples)
    if n == 0:
        return Score(name=name, n=0, rmse=float("nan"), mae=float("nan"), mbe=float("nan"))
    errors = [p - s.diffuse_fraction for p, s in zip(predicted, samples, strict=True)]
    rmse = math.sqrt(sum(e * e for e in errors) / n)
    mae = sum(abs(e) for e in errors) / n
    mbe = sum(errors) / n

    total_ghi = sum(s.ghi for s in samples)
    weighted = (
        math.sqrt(sum(e * e * s.ghi for e, s in zip(errors, samples, strict=True)) / total_ghi)
        if total_ghi > 0
        else None
    )
    return Score(
        name=name,
        n=n,
        rmse=round(rmse, 5),
        mae=round(mae, 5),
        mbe=round(mbe, 5),
        rmse_ghi_weighted=round(weighted, 5) if weighted is not None else None,
    )


def build_ladder() -> list[object]:
    """Every candidate, cheapest first."""
    return [
        ConstantModel(),
        ClimatologyModel(),
        ErbsModel(),
        LearnedModel(kind="ridge"),
        LearnedModel(kind="gradient_boosting"),
    ]


def evaluate_ladder(
    train: list[Sample], test: list[Sample]
) -> tuple[list[Score], dict[str, object]]:
    """
    Fit every rung on ``train`` and score it on ``test``.

    Returns the scores plus the fitted models, so the winner can be serialised
    without refitting.
    """
    scores: list[Score] = []
    fitted: dict[str, object] = {}

    for model in build_ladder():
        model.fit(train)  # type: ignore[attr-defined]
        predicted = model.predict(test)  # type: ignore[attr-defined]
        result = score(model.name, predicted, test)  # type: ignore[attr-defined]
        scores.append(result)
        fitted[result.name] = model

    erbs = next((s for s in scores if s.name == "erbs"), None)
    if erbs and erbs.rmse > 0:
        for result in scores:
            result.skill_vs_erbs = round(1.0 - result.rmse / erbs.rmse, 4)

    return scores, fitted


def select_winner(scores: list[Score]) -> tuple[str, str]:
    """
    Apply the gate declared at the top of this module.

    Returns ``(chosen_name, reason)``. The reason is recorded in the report so
    the decision is auditable rather than implicit.
    """
    by_name = {s.name: s for s in scores}
    erbs = by_name.get("erbs")
    if erbs is None:
        return "erbs", "Erbs baseline missing; defaulting to the published correlation."

    learned = [
        s
        for s in scores
        if s.name in {"ridge", "gradient_boosting"} and s.skill_vs_erbs is not None
    ]
    if not learned:
        return "erbs", "No learned rung was evaluated."

    best = max(learned, key=lambda s: s.skill_vs_erbs or -1.0)
    if (best.skill_vs_erbs or 0.0) >= MIN_SKILL_OVER_ERBS:
        return best.name, (
            f"{best.name} beats Erbs by {best.skill_vs_erbs:.3f} skill, clearing the "
            f"{MIN_SKILL_OVER_ERBS} gate declared before training."
        )
    return "erbs", (
        f"Best learned rung ({best.name}) improves on Erbs by only "
        f"{best.skill_vs_erbs:.3f} skill, under the {MIN_SKILL_OVER_ERBS} gate. "
        "Shipping the published correlation: it is validated worldwide, has no "
        "training data to maintain, and cannot extrapolate badly."
    )


def scores_as_dicts(scores: list[Score]) -> list[dict]:
    return [asdict(s) for s in scores]


__all__ = [
    "CURRENT_FALLBACK",
    "FEATURE_NAMES",
    "MIN_SKILL_OVER_ERBS",
    "ClimatologyModel",
    "ConstantModel",
    "ErbsModel",
    "LearnedModel",
    "Score",
    "build_ladder",
    "evaluate_ladder",
    "score",
    "scores_as_dicts",
    "select_winner",
]
