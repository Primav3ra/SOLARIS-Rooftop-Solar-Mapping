# Beam/diffuse decomposition model

Generated 2026-09-29T10:36:45Z

## What this replaces

The model falls back to a beam fraction of **0.60** when ERA5 sampling fails. The reference data puts the mean at **0.457** across 40,004 daytime hours -- so the constant over-attributes energy to the direct beam, which in turn over-applies the shadow penalty and under-applies the sky-view one.

## Holdout

- Train: 18,624 samples, 7 cities, years [2020, 2021]
- Test: 4,022 samples, cities ['bengaluru', 'guwahati', 'mumbai'], years [2022]
- Withheld from both: 17,358
- Closest test/train site pair under 250 km: none

A test sample comes from a held-out city **and** a held-out year. Hours within a city-day are strongly correlated, so a random row split would test on hours whose neighbours were trained on and report a score that says nothing about generalisation.

## The ladder

| Rung | n | RMSE | GHI-weighted RMSE | MAE | MBE | Skill vs Erbs |
|---|---:|---:|---:|---:|---:|---:|
| `constant` | 4,022 | 0.2678 | 0.1985 | 0.2202 | -0.1668 | -0.9360 |
| `climatology` | 4,022 | 0.1983 | 0.1830 | 0.1652 | -0.0219 | -0.4337 |
| `erbs` | 4,022 | 0.1383 | 0.1242 | 0.1111 | +0.0701 | +0.0000 |
| `ridge` | 4,022 | 0.0974 | 0.0914 | 0.0777 | -0.0004 | +0.2962 |
| `gradient_boosting` | 4,022 | 0.0790 | 0.0794 | 0.0624 | +0.0001 | +0.4291 |

Erbs et al. (1982) is the baseline that matters, not the constant. It is a published correlation validated worldwide for four decades, so a learned model that cannot beat it is not worth shipping or maintaining.

## Decision

**Winner: `gradient_boosting`**

gradient_boosting beats Erbs by 0.429 skill, clearing the 0.1 gate declared before training.

The gate -- a learned rung must beat Erbs by at least 0.1 skill -- was declared before training, in solaris/ml/decomposition.py, so it could not be adjusted to suit the outcome.

