"""
Error metrics for the validation harness.

Deliberately plain arithmetic, no numpy: the harness must run in the offline
CI job, which installs only the runtime dependencies.

Signed bias (MBE) is reported alongside the magnitude metrics because the two
answer different questions. A model can have near-zero MBE annually while being
+15% in monsoon and -15% in winter -- exactly the error structure ERA5's aerosol
climatology produces -- so the aggregate alone would hide it. Any per-period
comparison should report MBE per period, not just overall.
"""

from __future__ import annotations

import math


def describe(values: list[float]) -> dict[str, float]:
    """Summary statistics for a sample."""
    if not values:
        return {"n": 0}
    n = len(values)
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / n
    return {
        "n": n,
        "mean": round(mean, 4),
        "std": round(math.sqrt(variance), 4),
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "median": round(sorted(values)[n // 2], 4),
    }


def compare(model: list[float], reference: list[float]) -> dict[str, float]:
    """
    Model-vs-reference error metrics.

    MBE is signed, so a systematic over- or under-estimate is visible rather
    than averaged away into MAE.
    """
    if len(model) != len(reference):
        raise ValueError(
            f"length mismatch: {len(model)} model values vs {len(reference)} reference"
        )
    if not model:
        return {"n": 0}

    n = len(model)
    errors = [m - r for m, r in zip(model, reference, strict=True)]
    ref_mean = sum(reference) / n

    mbe = sum(errors) / n
    mae = sum(abs(e) for e in errors) / n
    rmse = math.sqrt(sum(e * e for e in errors) / n)

    out = {
        "n": n,
        "MBE": round(mbe, 4),
        "MAE": round(mae, 4),
        "RMSE": round(rmse, 4),
        "reference_mean": round(ref_mean, 4),
    }
    if ref_mean:
        out["rMBE_pct"] = round(mbe / ref_mean * 100.0, 3)
        out["rMAE_pct"] = round(mae / ref_mean * 100.0, 3)
        out["rRMSE_pct"] = round(rmse / ref_mean * 100.0, 3)

    # Slope and intercept separate a scale error from an offset error; a model
    # can hit rMBE ~= 0 with the wrong slope by trading one against the other.
    model_mean = sum(model) / n
    denominator = sum((r - ref_mean) ** 2 for r in reference)
    if denominator > 0:
        slope = (
            sum((r - ref_mean) * (m - model_mean) for m, r in zip(model, reference, strict=True))
            / denominator
        )
        out["slope"] = round(slope, 4)
        out["intercept"] = round(model_mean - slope * ref_mean, 4)

    total_ss = sum((m - model_mean) ** 2 for m in model)
    residual_ss = sum(e * e for e in errors)
    if total_ss > 0:
        out["R2"] = round(1.0 - residual_ss / total_ss, 4)
    return out


def skill_score(model_rmse: float, baseline_rmse: float) -> float | None:
    """
    Skill against a baseline: ``1 - RMSE_model / RMSE_baseline``.

    Zero or negative means the model adds nothing over the baseline and should
    be dropped, which is the gate the ML tracks are held to.
    """
    if baseline_rmse <= 0:
        return None
    return round(1.0 - model_rmse / baseline_rmse, 4)
