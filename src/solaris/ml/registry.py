"""
Model loading, with a fallback chain that is never silent.

The rule
--------
The API must never hard-depend on a model artifact. A missing, corrupt or
out-of-domain model degrades to the next rung down, and **the response always
reports which rung answered**. An invisible fallback is the failure mode this
whole project has been unpicking: a plausible number with no indication it came
from a default.

The chain, best first::

    learned model  ->  Erbs (published correlation)  ->  the 0.60 constant

Erbs sits in the middle deliberately. It needs no artifact, no training data and
no dependencies beyond arithmetic, so it is always available -- which makes the
constant a genuine last resort rather than the first fallback it is today.

Domain checking
---------------
A gradient-boosted tree does not extrapolate; it returns the nearest leaf. So
an input outside the training range gets a confident answer with no basis. The
guard is a per-feature range check against the training envelope recorded at fit
time: out-of-domain inputs fall through to Erbs rather than getting an
extrapolated prediction.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import asdict, dataclass, field
from typing import Any

MODEL_DIR = pathlib.Path(__file__).resolve().parents[3] / "ml" / "artifacts"
DECOMPOSITION_NAME = "decomposition"


@dataclass
class Envelope:
    """Per-feature training range, for the domain check."""

    feature_names: list[str]
    minimum: list[float]
    maximum: list[float]

    def contains(self, values: list[float], tolerance: float = 0.05) -> bool:
        """
        True if every feature is inside its training range, plus a small margin.

        The margin avoids rejecting a value that is only marginally outside --
        the boundary is a sample artifact, not a physical limit.
        """
        for value, low, high in zip(values, self.minimum, self.maximum, strict=True):
            span = high - low
            slack = span * tolerance if span > 0 else abs(high) * tolerance + 1e-9
            if value < low - slack or value > high + slack:
                return False
        return True

    @classmethod
    def from_samples(cls, samples, feature_names) -> Envelope:
        vectors = [s.as_features() for s in samples]
        columns = list(zip(*vectors, strict=True))
        return cls(
            feature_names=list(feature_names),
            minimum=[min(col) for col in columns],
            maximum=[max(col) for col in columns],
        )


@dataclass
class Manifest:
    """
    What shipped, and on what evidence.

    Committed alongside the artifact so "which model is live, and how good was
    it?" is answerable from the repository -- without loading a pickle.
    """

    name: str
    version: str
    kind: str
    trained_utc: str
    n_train: int
    n_test: int
    test_rmse: float
    skill_vs_erbs: float
    feature_names: list[str]
    envelope: dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Prediction:
    """A prediction and, always, its provenance."""

    values: list[float]
    source: str
    detail: str = ""

    @property
    def is_learned(self) -> bool:
        return self.source.startswith("model:")


def artifact_path(name: str = DECOMPOSITION_NAME) -> pathlib.Path:
    return MODEL_DIR / f"{name}.joblib"


def manifest_path(name: str = DECOMPOSITION_NAME) -> pathlib.Path:
    return MODEL_DIR / f"{name}.manifest.json"


def save(model, manifest: Manifest, name: str = DECOMPOSITION_NAME) -> pathlib.Path:
    """Persist a fitted model and its manifest."""
    import joblib

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, artifact_path(name))
    manifest_path(name).write_text(
        json.dumps(manifest.as_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return artifact_path(name)


def load_manifest(name: str = DECOMPOSITION_NAME) -> Manifest | None:
    path = manifest_path(name)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return Manifest(**payload)
    except (json.JSONDecodeError, TypeError):
        return None


_CACHE: dict[str, Any] = {}


def load(name: str = DECOMPOSITION_NAME):
    """
    Load a model, or None if it is unavailable for any reason.

    Deliberately swallows every failure: a corrupt artifact must degrade the
    prediction, not take an endpoint down.
    """
    if name in _CACHE:
        return _CACHE[name]
    path = artifact_path(name)
    if not path.exists():
        _CACHE[name] = None
        return None
    try:
        import joblib

        _CACHE[name] = joblib.load(path)
    except Exception:
        _CACHE[name] = None
    return _CACHE[name]


def reset() -> None:
    _CACHE.clear()


def predict_diffuse_fraction(samples) -> Prediction:
    """
    Diffuse fraction for each sample, with provenance.

    Walks the fallback chain and reports which rung answered, so a caller can
    surface it in ``data_quality`` rather than presenting a default as a
    computed value.
    """
    from solaris.ml.decomposition import CURRENT_FALLBACK, ErbsModel

    if not samples:
        return Prediction(values=[], source="none", detail="no samples")

    model = load()
    manifest = load_manifest()

    if model is not None and manifest is not None:
        envelope = Envelope(**manifest.envelope) if manifest.envelope else None
        in_domain = envelope is None or all(envelope.contains(s.as_features()) for s in samples)
        if in_domain:
            try:
                import numpy as np

                matrix = np.array([s.as_features() for s in samples], dtype=float)
                values = [float(min(max(v, 0.0), 1.0)) for v in model.predict(matrix)]
                return Prediction(
                    values=values,
                    source=f"model:{manifest.name}@{manifest.version}",
                    detail=(
                        f"{manifest.kind}, test RMSE {manifest.test_rmse:.4f}, "
                        f"skill over Erbs {manifest.skill_vs_erbs:+.3f}"
                    ),
                )
            except Exception:
                pass  # fall through to Erbs
        else:
            erbs = ErbsModel()
            return Prediction(
                values=erbs.predict(samples),
                source="erbs",
                detail=(
                    "Input is outside the model's training envelope, so the "
                    "published Erbs correlation was used instead. A tree model "
                    "does not extrapolate -- it returns the nearest leaf -- so "
                    "an out-of-domain prediction would be confident and "
                    "baseless."
                ),
            )

    erbs = ErbsModel()
    if model is None:
        return Prediction(
            values=erbs.predict(samples),
            source="erbs",
            detail=(
                "No learned model artifact available; used the published Erbs "
                f"(1982) correlation. Still preferable to the {CURRENT_FALLBACK} "
                "constant, which carries a measured bias of about -0.14 in "
                "diffuse fraction over India."
            ),
        )
    return Prediction(values=erbs.predict(samples), source="erbs", detail="Model load failed.")


def constant_fallback(n: int) -> Prediction:
    """
    The last resort: the constant production uses today.

    Kept reachable and named so the bottom of the chain is explicit rather than
    implied.
    """
    from solaris.ml.decomposition import CURRENT_FALLBACK

    return Prediction(
        values=[1.0 - CURRENT_FALLBACK] * n,
        source="constant",
        detail=(
            f"Fixed beam fraction of {CURRENT_FALLBACK}. Measured mean over "
            "India is nearer 0.544, so this over-attributes energy to the "
            "direct beam."
        ),
    )
