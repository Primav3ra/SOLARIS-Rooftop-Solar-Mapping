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
ALGO_VERSION = "2"  # Phase 3: shadow/sky-view geometry, QA masking, clamps

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


#: Shortest window for which an annualised figure is reported.
#:
#: Annualising by ``x 365.25/days`` assumes the window represents the year,
#: which a short one does not: a clear December day in Delhi annualises to
#: roughly double the true figure and a monsoon day to roughly half. A month is
#: the shortest window where within-window averaging makes the extrapolation
#: defensible, and even then it carries seasonal bias. Shorter windows get the
#: period total only -- which is always correct.
MIN_DAYS_FOR_ANNUALISATION = 28


# ---------------------------------------------------------------------------
# Request limits -- the primary defence against Earth Engine quota exhaustion
# ---------------------------------------------------------------------------

MAX_AOI_KM2 = 30.0  # keeps AOIs inside the 4 m reduce-scale tier
MAX_HALF_SIZE_DEG = 0.025  # ~2.8 km half-side => ~27 km2 at Delhi latitude
MAX_AOI_VERTICES = 100
MAX_BUILDINGS = 2000
DEFAULT_HALF_SIZE_DEG = 0.01


# ---------------------------------------------------------------------------
# Earth Engine compute cost
# ---------------------------------------------------------------------------

#: Monthly EECU-second ceiling on the Earth Engine noncommercial tier.
#:
#: A **system limit**, not a quota: the console reports it as non-adjustable,
#: and the daily figure alongside it reads "Unlimited" with no setting. So
#: there is no lever on Google's side, and the only possible guard is in this
#: application.
NONCOMMERCIAL_EECU_SECONDS_PER_MONTH = 540_000

#: Measured burn rate, recorded because it is the basis for the default budget.
#:
#: One day of development -- a few dozen queries, mostly yearly windows, plus
#: the live integration tests and the round-trip profiler -- consumed 39,301
#: EECU-seconds, which was 91% of that month's usage to date.
MEASURED_DEV_DAY_EECU_SECONDS = 39_301

#: EECU-seconds per cost unit, where one unit is one monthly-window query.
#:
#: Derived from the figure above rather than guessed, and the derivation is
#: written out because it is an estimate with real uncertainty:
#:
#:   39,301 EECU-seconds over roughly 30 substantive queries, predominantly
#:   yearly windows at 12 units each, gives on the order of 360 cost units, so
#:   about 110 EECU-seconds per unit. Rounded up to 120 for margin.
#:
#: Note the trap this replaced: the first attempt used ~1,000 EECU-seconds per
#: *unit*, which was really the cost of a yearly *query* -- twelve units. That
#: put the default budget at more than twice the monthly ceiling, and a test
#: asserting the budget fits inside the ceiling is what caught it.
#:
#: Refine this by watching the console meter against the logged cost, not by
#: re-deriving it from first principles.
EECU_SECONDS_PER_COST_UNIT = 120

#: Relative compute cost of a query, by temporal mode.
#:
#: Cost tracks the number of daily images the window reduces over, which is
#: what the irradiance, precipitation and shadow reductions all scale with. A
#: yearly query sums 365 daily images against 31 for a month, and its sun-position
#: set is larger too.
#:
#: Normalised so a monthly query costs 1. The budget is charged in these units
#: rather than in round-trips: round-trip count is nearly constant across modes
#: (12 for every /api/yield), so counting calls treated a yearly query and a
#: single-day query as identical when they differ by more than an order of
#: magnitude in compute.
EE_COST_UNITS = {
    "daily": 0.1,
    "monthly": 1.0,
    "quarterly": 3.0,
    "yearly": 12.0,
}

#: Cost of a tile rendering, in the same units. Tiles reduce at the map's own
#: scale rather than 4 m, so they are cheaper than a yield reduction, but the
#: roof preview fires on selection and so runs more often.
EE_COST_UNITS_TILE = 0.5


def ee_cost_units(mode: str) -> float:
    """Relative compute cost for a temporal mode, defaulting to the dearest."""
    return EE_COST_UNITS.get(mode, max(EE_COST_UNITS.values()))
