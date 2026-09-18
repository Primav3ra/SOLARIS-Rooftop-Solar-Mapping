"""
Train and evaluate the beam/diffuse decomposition ladder.

    python -m solaris.ml.train

Writes ``ml/reports/decomposition.json`` and prints a markdown summary. The
report records which rung won **and why**, so the decision is auditable rather
than implicit in whichever file happens to be in the models directory.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from datetime import UTC, datetime

from solaris.ml import decomposition, features, registry, splits

REPORT_DIR = pathlib.Path(__file__).resolve().parents[3] / "ml" / "reports"
DEFAULT_YEARS = (2020, 2021, 2022)


def run(years: tuple[int, ...] = DEFAULT_YEARS, *, persist: bool = True) -> dict:
    """
    Fit and score the ladder, and optionally promote the winner.

    ``persist=False`` evaluates without writing anything. That separation
    matters more than it looks: the test suite calls this to assert the ladder
    still behaves, and while it persisted, **every ``pytest`` run overwrote the
    committed model artifact** with a new version and timestamp. Two
    consequences, both bad. ``git status`` was never clean, so a real artifact
    change was indistinguishable from test noise; and the shipped model became
    whatever the last test run happened to produce rather than a reviewed,
    deliberately promoted one -- which defeats the entire point of committing it
    alongside a manifest.
    """
    samples = features.build_dataset(years)
    if not samples:
        return {
            "skipped": (
                "No cached hourly reference data. Run:\n"
                "  python -m solaris.evals.fetch --hourly --years 2020 2021 2022"
            )
        }

    split = splits.split_samples(samples)
    if not split.test or not split.train:
        return {"skipped": f"Split produced an empty side: {split.summary()}"}

    scores, fitted = decomposition.evaluate_ladder(split.train, split.test)
    winner, reason = decomposition.select_winner(scores)

    # Persist the winner only if it is a learned rung. Erbs and the constant
    # need no artifact -- they are arithmetic -- so writing one would imply a
    # dependency that does not exist, and the registry already falls back to
    # them when no artifact is present.
    saved: dict | None = None
    if persist and winner in {"ridge", "gradient_boosting"}:
        winning_score = next(s for s in scores if s.name == winner)
        manifest = registry.Manifest(
            name="decomposition",
            version=datetime.now(UTC).strftime("%Y%m%d"),
            kind=winner,
            trained_utc=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            n_train=len(split.train),
            n_test=len(split.test),
            test_rmse=winning_score.rmse,
            skill_vs_erbs=winning_score.skill_vs_erbs or 0.0,
            feature_names=list(features.FEATURE_NAMES),
            envelope=registry.Envelope.from_samples(split.train, features.FEATURE_NAMES).__dict__,
            notes=reason,
        )
        path = registry.save(fitted[winner].model, manifest)
        saved = {"artifact": str(path.name), "version": manifest.version}

    # Reference distribution, for context on what the constant gets wrong.
    observed = [s.diffuse_fraction for s in samples]
    mean_observed = sum(observed) / len(observed)

    return {
        "schema_version": 1,
        "generated_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "task": "beam_diffuse_decomposition",
        "target": "diffuse_fraction",
        "years": list(years),
        "features": list(features.FEATURE_NAMES),
        "n_samples": len(samples),
        "split": split.summary(),
        "spatial_leakage": splits.spatial_leakage(),
        "scores": decomposition.scores_as_dicts(scores),
        "gate": {
            "min_skill_over_erbs": decomposition.MIN_SKILL_OVER_ERBS,
            "declared": "before training, in solaris/ml/decomposition.py",
        },
        "winner": winner,
        "reason": reason,
        "saved": saved,
        "persisted": bool(saved),
        "observed": {
            "mean_diffuse_fraction": round(mean_observed, 4),
            "implied_mean_beam_fraction": round(1.0 - mean_observed, 4),
            "production_fallback_beam_fraction": decomposition.CURRENT_FALLBACK,
        },
    }


def format_markdown(report: dict) -> str:
    if "skipped" in report:
        return f"# Decomposition model\n\nSkipped: {report['skipped']}\n"

    out: list[str] = []
    add = out.append
    add("# Beam/diffuse decomposition model\n")
    add(f"Generated {report['generated_utc']}\n")

    add("## What this replaces\n")
    observed = report["observed"]
    add(
        f"The model falls back to a beam fraction of "
        f"**{observed['production_fallback_beam_fraction']:.2f}** when ERA5 sampling "
        f"fails. The reference data puts the mean at "
        f"**{observed['implied_mean_beam_fraction']:.3f}** across "
        f"{report['n_samples']:,} daytime hours -- so the constant "
        f"over-attributes energy to the direct beam, which in turn over-applies "
        f"the shadow penalty and under-applies the sky-view one.\n"
    )

    add("## Holdout\n")
    split = report["split"]
    add(
        f"- Train: {split['n_train']:,} samples, {len(split['train_cities'])} cities, "
        f"years {split['train_years']}"
    )
    add(
        f"- Test: {split['n_test']:,} samples, cities {split['test_cities']}, "
        f"years {split['test_years']}"
    )
    add(f"- Withheld from both: {split['n_withheld']:,}")
    add(f"- Closest test/train site pair under 250 km: {report['spatial_leakage'] or 'none'}\n")
    add(
        "A test sample comes from a held-out city **and** a held-out year. Hours "
        "within a city-day are strongly correlated, so a random row split would "
        "test on hours whose neighbours were trained on and report a score that "
        "says nothing about generalisation.\n"
    )

    add("## The ladder\n")
    add("| Rung | n | RMSE | GHI-weighted RMSE | MAE | MBE | Skill vs Erbs |")
    add("|---|---:|---:|---:|---:|---:|---:|")
    for row in report["scores"]:
        skill = row.get("skill_vs_erbs")
        weighted = row.get("rmse_ghi_weighted")
        add(
            f"| `{row['name']}` | {row['n']:,} | {row['rmse']:.4f} "
            f"| {weighted if weighted is None else f'{weighted:.4f}'} "
            f"| {row['mae']:.4f} | {row['mbe']:+.4f} "
            f"| {'--' if skill is None else f'{skill:+.4f}'} |"
        )

    add(
        "\nErbs et al. (1982) is the baseline that matters, not the constant. It "
        "is a published correlation validated worldwide for four decades, so a "
        "learned model that cannot beat it is not worth shipping or maintaining.\n"
    )

    add("## Decision\n")
    gate = report["gate"]
    add(f"**Winner: `{report['winner']}`**\n")
    add(f"{report['reason']}\n")
    add(
        f"The gate -- a learned rung must beat Erbs by at least "
        f"{gate['min_skill_over_erbs']} skill -- was declared "
        f"{gate['declared']}, so it could not be adjusted to suit the outcome.\n"
    )
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--years", type=int, nargs="+", default=list(DEFAULT_YEARS))
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument(
        "--no-save",
        action="store_true",
        help="evaluate the ladder without promoting the winner to ml/artifacts/",
    )
    args = parser.parse_args(argv)

    report = run(tuple(args.years), persist=not args.no_save)
    markdown = format_markdown(report)

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "decomposition.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (REPORT_DIR / "decomposition.md").write_text(markdown, encoding="utf-8")

    if not args.quiet:
        print(markdown)
    if "skipped" in report:
        print(report["skipped"], file=sys.stderr)
        return 1
    print(f"wrote {REPORT_DIR / 'decomposition.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
