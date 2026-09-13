"""
Single source of truth for every physical, model and dataset constant.

Before this module the same numbers were declared in several places at once --
the grid emission factor in two JS files with a "keep in sync" comment, the PV
constants in both the backend and the frontend, the slope threshold in two
Python modules, and the ERA5 reduce scale four times over. Anything needing one
of these values imports it from here; the frontend fetches them from
``/api/presets`` rather than carrying its own copy.

Every value carries its provenance. Where a number is an assumption rather than
a measurement, the docstring says so -- see ``docs/methodology.md`` for the full
discussion and ``docs/limitations.md`` for what each one bounds.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Versioning -- bump these to invalidate caches and to make results traceable
# ---------------------------------------------------------------------------

#: Bump on any change to the penalty maths or the PV conversion chain.
ALGO_VERSION = "1"

#: Bump when a GEE collection id, band name or default vintage changes.
DATASET_VERSION = "1"


# ---------------------------------------------------------------------------
# PV system parameters
# ---------------------------------------------------------------------------

#: Module efficiency at STC. Typical mono-c-Si rooftop module, 2020s vintage.
PANEL_EFFICIENCY = 0.18

#: Lumped performance ratio. NOTE: this bundles temperature, inverter, wiring,
#: mismatch, soiling and availability losses into one scalar, which is why it
#: is ambiguous against the separate UHI and soiling penalty layers. Phase 3
#: decomposes it via pvlib; see docs/methodology.md. Observed year-one PR for
#: Indian rooftop plants is 0.78-0.83.
PERFORMANCE_RATIO = 0.80

#: Usable-roof coverage fraction: panels never tile 100% of a roof (setbacks,
#: obstructions, water tanks, access walkways). Typical 0.6-0.75.
PACKING_FACTOR = 0.70

#: Minimum Open Buildings v3 polygon confidence for a footprint to be used.
BUILDING_CONFIDENCE = 0.70

#: Standard test condition irradiance (W/m^2). Converts m^2 x efficiency to kWp.
STC_IRRADIANCE_W_M2 = 1000.0

#: Crystalline-silicon power temperature coefficient (per degC), IEC 60891 /
#: De Soto et al. (2006).
TEMP_COEFF_PER_C = -0.004


# ---------------------------------------------------------------------------
# Grid / emissions
# ---------------------------------------------------------------------------

#: India grid emission factor (kg CO2 per kWh). CEA CO2 Baseline Database
#: v20.0, FY2023-24 weighted average (0.727 tCO2/MWh). The provisional
#: FY2024-25 figure is 0.710.
GRID_EMISSION_FACTOR_KG_PER_KWH = 0.727


# ---------------------------------------------------------------------------
# Terrain / rooftop geometry
# ---------------------------------------------------------------------------

#: Terrain slope above which a pixel is excluded (degrees).
#: NOTE: derived from SRTM at 30 m, so this excludes buildings on steep
#: *terrain*, not steep *roofs* -- a scale mismatch against the 4 m roof
#: raster. Tracked as limitation P7.
MAX_SLOPE_DEG = 30.0

#: Open Buildings 2.5D effective resolution (m). Used as the analysis scale.
ROOF_SCALE_M = 4.0

#: Open Buildings presence band threshold. The model's confidence is not
#: calibrated, so this is a prior rather than a probability.
PRESENCE_THRESHOLD = 0.5

#: Open Buildings 2.5D Temporal v1 available vintages.
OPEN_BUILDINGS_MIN_YEAR = 2016
OPEN_BUILDINGS_MAX_YEAR = 2023
DEFAULT_ROOF_YEAR = 2022


# ---------------------------------------------------------------------------
# Reduce scales (m) -- match each dataset's native resolution
# ---------------------------------------------------------------------------

#: ERA5-Land: 0.1 deg at the equator (~9 km native).
ERA5_SCALE_M = 11_132.0

#: ERA5 (not -Land): 0.25 deg at the equator (~28 km native).
ERA5_HOURLY_SCALE_M = 27_830.0

#: MODIS MOD11A2 / MCD19A2 native resolution.
MODIS_SCALE_M = 1000.0


# ---------------------------------------------------------------------------
# Request limits -- the primary defence against Earth Engine quota exhaustion
# ---------------------------------------------------------------------------

MAX_AOI_KM2 = 30.0  # keeps AOIs inside the 4 m reduce-scale tier
MAX_HALF_SIZE_DEG = 0.025  # ~2.8 km half-side => ~27 km2 at Delhi latitude
MAX_AOI_VERTICES = 100
MAX_BUILDINGS = 2000
DEFAULT_HALF_SIZE_DEG = 0.01
