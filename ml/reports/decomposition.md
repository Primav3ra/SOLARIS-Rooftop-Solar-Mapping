# Beam/diffuse decomposition model

Generated 2026-09-14T15:04:13Z

## What this replaces

The model falls back to a beam fraction of **0.60** when ERA5 sampling fails. The reference data puts the mean at **0.456** across 3,935 daytime hours -- so the constant over-attributes energy to the direct beam, which in turn over-applies the shadow penalty and under-applies the sky-view one.

## Holdout

- Train: 1,830 samples, 7 cities, years [2020, 2021]
- Test: 396 samples, cities ['bengaluru', 'guwahati', 'mumbai'], years [2022]
- Withheld from both: 1,709
- Closest test/train site pair under 250 km: none

A test sample comes from a held-out city **and** a held-out year. Hours within a city-day are strongly correlated, so a random row split would test on hours whose neighbours were trained on and report a score that says nothing about generalisation.

## The ladder

| Rung | n | RMSE | GHI-weighted RMSE | MAE | MBE | Skill vs Erbs |
|---|---:|---:|---:|---:|---:|---:|
| `constant` | 396 | 0.2540 | 0.1890 | 0.2056 | -0.1391 | -0.7809 |
| `climatology` | 396 | 0.1958 | 0.1935 | 0.1604 | +0.0149 | -0.3730 |
| `erbs` | 396 | 0.1426 | 0.1238 | 0.1102 | +0.0770 | +0.0000 |
| `ridge` | 396 | 0.1097 | 0.1006 | 0.0862 | +0.0142 | +0.2308 |
| `gradient_boosting` | 396 | 0.0893 | 0.0885 | 0.0710 | +0.0153 | +0.3740 |

Erbs et al. (1982) is the baseline that matters, not the constant. It is a published correlation validated worldwide for four decades, so a learned model that cannot beat it is not worth shipping or maintaining.

## Decision

**Winner: `gradient_boosting`**

gradient_boosting beats Erbs by 0.374 skill, clearing the 0.1 gate declared before training.

The gate -- a learned rung must beat Erbs by at least 0.1 skill -- was declared before training, in solaris/ml/decomposition.py, so it could not be adjusted to suit the outcome.

