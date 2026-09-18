# SOLARIS validation report

Generated 2026-09-18T12:07:49Z  
engine `reference`, algo v2, 
datasets v1

## Specific yield against published plant performance

Specific yield is the comparison that matters: it is what plant studies report, and the packing factor and module efficiency cancel out of it, so it isolates the irradiance and loss chain from the capacity assumptions.

- Published plausibility band: **1000-1750 kWh/kWp/yr**
- Model: mean **1302**, range 1103-1428
- Inside the band: **30/30** city-years (100%)

| City | Zone | Year | Ref GHI | Beam | Retention | Derate | Specific yield | In band |
|---|---|---|---:|---:|---:|---:|---:|:-:|
| Delhi | composite, very high aerosol | 2020 | 1758 | 0.532 | 0.949 | 0.941 | 1256 | yes |
| Delhi | composite, very high aerosol | 2021 | 1710 | 0.513 | 0.950 | 0.941 | 1223 | yes |
| Delhi | composite, very high aerosol | 2022 | 1739 | 0.529 | 0.949 | 0.941 | 1243 | yes |
| Jaipur | semi-arid, high aerosol | 2020 | 1920 | 0.602 | 0.948 | 0.941 | 1370 | yes |
| Jaipur | semi-arid, high aerosol | 2021 | 1875 | 0.589 | 0.948 | 0.941 | 1339 | yes |
| Jaipur | semi-arid, high aerosol | 2022 | 1903 | 0.606 | 0.948 | 0.941 | 1358 | yes |
| Jodhpur | arid, dusty | 2020 | 2002 | 0.633 | 0.947 | 0.941 | 1428 | yes |
| Jodhpur | arid, dusty | 2021 | 1987 | 0.622 | 0.948 | 0.941 | 1417 | yes |
| Jodhpur | arid, dusty | 2022 | 1982 | 0.634 | 0.947 | 0.941 | 1414 | yes |
| Ahmedabad | semi-arid | 2020 | 1986 | 0.619 | 0.948 | 0.941 | 1417 | yes |
| Ahmedabad | semi-arid | 2021 | 1907 | 0.583 | 0.948 | 0.941 | 1362 | yes |
| Ahmedabad | semi-arid | 2022 | 1986 | 0.619 | 0.948 | 0.941 | 1417 | yes |
| Mumbai | coastal, humid | 2020 | 1887 | 0.571 | 0.949 | 0.941 | 1348 | yes |
| Mumbai | coastal, humid | 2021 | 1817 | 0.534 | 0.949 | 0.941 | 1299 | yes |
| Mumbai | coastal, humid | 2022 | 1868 | 0.546 | 0.949 | 0.941 | 1335 | yes |
| Bengaluru | plateau, moderate | 2020 | 1950 | 0.534 | 0.949 | 0.941 | 1394 | yes |
| Bengaluru | plateau, moderate | 2021 | 1885 | 0.519 | 0.950 | 0.941 | 1348 | yes |
| Bengaluru | plateau, moderate | 2022 | 1862 | 0.513 | 0.950 | 0.941 | 1332 | yes |
| Chennai | coastal tropical | 2020 | 1840 | 0.530 | 0.949 | 0.941 | 1315 | yes |
| Chennai | coastal tropical | 2021 | 1843 | 0.519 | 0.950 | 0.941 | 1318 | yes |
| Chennai | coastal tropical | 2022 | 1835 | 0.519 | 0.950 | 0.941 | 1312 | yes |
| Kolkata | humid subtropical | 2020 | 1605 | 0.460 | 0.951 | 0.941 | 1149 | yes |
| Kolkata | humid subtropical | 2021 | 1567 | 0.441 | 0.951 | 0.941 | 1122 | yes |
| Kolkata | humid subtropical | 2022 | 1654 | 0.457 | 0.951 | 0.941 | 1184 | yes |
| Guwahati | high cloud, north-east | 2020 | 1543 | 0.499 | 0.950 | 0.941 | 1104 | yes |
| Guwahati | high cloud, north-east | 2021 | 1592 | 0.487 | 0.950 | 0.941 | 1139 | yes |
| Guwahati | high cloud, north-east | 2022 | 1543 | 0.512 | 0.950 | 0.941 | 1103 | yes |
| Leh | high altitude, low aerosol | 2020 | 1924 | 0.520 | 0.950 | 0.941 | 1376 | yes |
| Leh | high altitude, low aerosol | 2021 | 1827 | 0.487 | 0.950 | 0.941 | 1307 | yes |
| Leh | high altitude, low aerosol | 2022 | 1877 | 0.485 | 0.950 | 0.941 | 1343 | yes |

## Reference disagreement

The two credible free references for India do not agree. This bounds how tight any accuracy claim can honestly be.

| City | NASA POWER | Global Solar Atlas | Spread | Spread % |
|---|---:|---:|---:|---:|
| Delhi | 1736 | 1930 | 194 | 10.6% |

## Beam fraction

The most ERA5-sensitive input in the model. ERA5 uses a monthly aerosol climatology and is documented to overestimate direct while underestimating diffuse, with the error growing in aerosol load.

- Reference mean over 30 city-years: **0.540** (range 0.441-0.634)
- Model fallback when ERA5 sampling fails: 0.60

## Published references used

- **Delhi 12 kWp rooftop, measured**: 1147 kWh/kWp/yr — Performance evaluation of a rooftop PV plant in Northern India
  - A single well-maintained plant, so its PR sits above the typical range. Reported as a point value, not a band.
- **Indian rooftop, typical year one**: 1400-1700 kWh/kWp/yr — Industry-observed annual figures, high-irradiance states
  - Assumes a tilted array; this model treats every roof as horizontal.
- **Assam MW-scale rooftop, measured**: 1055 kWh/kWp/yr — Performance analysis of MW-scale rooftop plants in Assam
  - 2.89 kWh/kWp/day annualised; high-cloud north-east regime.

## What this does not establish

- Roofs are treated as horizontal, so no tilt gain (+8-12% annually in north India) is included. A tilted comparison would sit higher.
- The geometric loss factors in the `reference` engine are nominal stand-ins; only the `era5` engine computes them per site.
- Reference GHI is satellite-derived, not ground-measured. NASA POWER is only semi-independent of ERA5 (shared MERRA-2 meteorology lineage, distinct radiation retrieval).
