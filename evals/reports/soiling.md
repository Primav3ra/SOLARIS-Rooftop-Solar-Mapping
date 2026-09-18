# Soiling model evaluation

Generated 2026-09-17T18:47:33Z against 2021 rainfall.

## What changed

The previous model was `loss = mean_annual_AOD * 0.08`: unbounded, with no rainfall term, and returning the same annual figure for a one-day window as for a year. It is replaced by the published Kimber model -- which the old code already cited -- with the deposition rate derived from AOD and cleaning events taken from ERA5-Land rainfall.

## Per-city annual soiling

| City | AOD | %/day | Rain days | Mean spell | Longest spell | New (rain only) | New (30d wash) | Old model | pvlib |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| delhi | 0.70 | 0.340 | 91 | 8.8d | 62d | 3.89% | 2.06% | 5.60% | 3.89% |
| jaipur | 0.55 | 0.267 | 96 | 8.7d | 65d | 3.02% | 1.58% | 4.40% | 3.02% |
| jodhpur | 0.48 | 0.233 | 66 | 12.0d | 125d | 6.15% | 1.91% | 3.84% | 6.15% |
| ahmedabad | 0.52 | 0.253 | 82 | 13.5d | 135d | 7.26% | 1.96% | 4.16% | 7.27% |
| mumbai | 0.48 | 0.233 | 157 | 11.6d | 39d | 1.63% | 0.95% | 3.84% | 1.63% |
| bengaluru | 0.38 | 0.185 | 151 | 5.8d | 49d | 1.31% | 0.68% | 3.04% | 1.31% |
| chennai | 0.42 | 0.204 | 179 | 5.8d | 51d | 1.33% | 0.66% | 3.36% | 1.33% |
| kolkata | 0.62 | 0.301 | 175 | 8.6d | 69d | 2.79% | 1.30% | 4.96% | 2.79% |
| guwahati | 0.55 | 0.267 | 181 | 6.3d | 46d | 1.72% | 1.08% | 4.40% | 1.72% |
| leh | 0.12 | 0.058 | 101 | 5.7d | 35d | 0.29% | 0.23% | 0.96% | 0.29% |

Note the *longest* dry spell column against the mean. Ahmedabad and Jodhpur have unremarkable mean spells and four-month unbroken ones; that gap is what drives everything below.

The AOD column is a literature annual mean, not a retrieval from this pipeline, so the per-city **ranking** inherits that uncertainty. The rainfall-driven results do not.

**The change is not uniformly downward, and where it goes up is the more interesting result.** For jodhpur, ahmedabad the new model predicts *more* soiling than the old one, despite below-average AOD. The reason is the longest-spell column: an AOD-only model has no way to know a roof went four months without rain. So the previous model was not merely imprecise in arid India -- it was **optimistic** there, in the one climate where soiling does the most damage, while being pessimistic in the wet cities where rain cleans for free.

## Seasonal rates against measurement

| Season | Assumed AOD | Predicted %/day | Measured %/day | Error |
|---|---:|---:|---:|---:|
| spring | 0.80 | 0.389 | 0.390 | -0.4% |
| winter *(anchor)* | 0.72 | 0.350 | 0.340 | +2.9% |
| monsoon | 0.50 | 0.243 | 0.240 | +1.2% |

Ordering spring > winter > monsoon: **True**. That ordering matters more than the magnitudes: a model that fitted the annual total with the seasons reversed would invert every cleaning-schedule recommendation, which is the main thing anyone would use this for.

Winter is the calibration anchor, so only spring and monsoon are genuine predictions.

## Window dependence, Delhi

| Window | Days | Rain days | Mean spell | New model | Old model |
|---|---:|---:|---:|---:|---:|
| January (dry winter) | 31 | 4 | 9.0d | 3.59% | 5.60% |
| April (pre-monsoon dust) | 30 | 1 | 14.5d | 2.89% | 5.60% |
| July (monsoon) | 31 | 21 | 2.5d | 0.24% | 5.60% |
| November (post-monsoon, dry) | 30 | 0 | 30.0d | 5.27% | 5.60% |

A **21.9x** spread between the cleanest and dirtiest month, where the old model returned one figure for all of them. This is the single largest behavioural change in the soiling term.

## Validation against pvlib

Worst absolute disagreement with `pvlib.soiling.kimber` across all cities: **0.003 fraction points** of loss, against a tolerance of 0.5%. Compared with the first-day convention aligned; see solaris.physics.soiling.kimber_reference for why that matters and how large the unaligned difference is.

Reaching that agreement found three real errors in the closed form: the cleaning day was being counted as a dry day, those days were then wrongly dropped from the time average as well, and the accumulation was integrated continuously where the model is defined per day. Each was invisible in isolation.

## The model's own worst case

Without a daily rainfall series the model must assume every dry spell is the mean length. Measured against the real series, that understates annual soiling by **1.77x to 12.64x** -- and worst in the arid cities where soiling matters most, because accumulation is convex in spell length.

This is why the serving path spends an extra Earth Engine round-trip sampling the actual series, and why results from the fallback path carry an explicit note calling themselves a lower bound.

