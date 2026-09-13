"""
Independent reference data for validation, and the constraint it imposes.

The central design rule, and the reason this module exists separately from the
metrics: **there is no single ground truth to validate against.** The two
obvious free references for India disagree materially. For Delhi in 2020:

* NASA POWER (CERES/SRB satellite retrievals):  1753 kWh/m2/yr
* Global Solar Atlas (Solargis, World Bank):   ~1930 kWh/m2/yr

That is a ~10% spread between two credible published sources. **No accuracy
claim this project makes can honestly be tighter than that spread.** So every
comparison here reports each reference separately plus the inter-reference
range, and the report states the spread alongside the error. Quoting a single
error figure against a single reference would imply a precision the reference
data does not support.

What was rejected, and why
--------------------------
**PVGIS-SARAH3** is the obvious first choice and is what the plan originally
assumed. It does not cover India. Probing the live v5_3 API for Mumbai,
Ahmedabad, Jaipur, Delhi, Bengaluru, Kolkata and Jodhpur returns HTTP 400
"Location out of the spatial coverage of the radiation database selected" for
all seven -- SARAH3 is the Meteosat prime-disk product, roughly +/-65 deg
longitude, so the documentation's "Europe, Africa and Asia" means western Asia.

**PVGIS-ERA5** is reachable for India but is ERA5 itself, which is exactly what
this project already uses. Validating ERA5 against ERA5 would be circular.

So NASA POWER is the primary programmatic reference. It is only *semi*
independent -- its radiation comes from CERES/SRB retrievals while its
meteorology comes from MERRA-2, which shares a reanalysis lineage with ERA5
though not the radiation retrieval. Global Solar Atlas (Meteosat plus Solargis'
own clear-sky model) is the most independent gridded source but has no
documented point API, so its long-term averages are committed as a small table.

Measured plant yields are the only genuinely independent reference, but they
are confounded by system design, tilt and O&M, so they are used as a
plausibility band rather than a point comparison.
"""

from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field

CACHE_DIR = pathlib.Path(__file__).resolve().parents[3] / "evals" / "references"

NASA_POWER_URL = "https://power.larc.nasa.gov/api/temporal/daily/point"

#: NASA POWER parameters: all-sky GHI, diffuse, and DNI. The diffuse band is
#: what makes the beam fraction -- the project's most ERA5-sensitive input --
#: directly checkable rather than merely plausible.
NASA_POWER_PARAMS = "ALLSKY_SFC_SW_DWN,ALLSKY_SFC_SW_DIFF,ALLSKY_SFC_SW_DNI"

#: NASA POWER's fill value for a missing day.
FILL_VALUE = -999.0


@dataclass(frozen=True)
class City:
    """An evaluation site."""

    key: str
    name: str
    lat: float
    lon: float
    #: Climate note, for the per-city discussion in the report.
    zone: str
    #: Global Solar Atlas long-term-average GHI (kWh/m2/yr), where recorded.
    gsa_ghi_kwh_m2_yr: float | None = None


#: Sites spanning the four gradients that matter for this model: latitude,
#: aerosol loading, monsoon regime and elevation.
CITIES: tuple[City, ...] = (
    City("delhi", "Delhi", 28.6139, 77.2090, "composite, very high aerosol", 1930.0),
    City("jaipur", "Jaipur", 26.9124, 75.7873, "semi-arid, high aerosol"),
    City("jodhpur", "Jodhpur", 26.2389, 73.0243, "arid, dusty"),
    City("ahmedabad", "Ahmedabad", 23.0225, 72.5714, "semi-arid"),
    City("mumbai", "Mumbai", 19.0760, 72.8777, "coastal, humid"),
    City("bengaluru", "Bengaluru", 12.9716, 77.5946, "plateau, moderate"),
    City("chennai", "Chennai", 13.0827, 80.2707, "coastal tropical"),
    City("kolkata", "Kolkata", 22.5726, 88.3639, "humid subtropical"),
    City("guwahati", "Guwahati", 26.1445, 91.7362, "high cloud, north-east"),
    City("leh", "Leh", 34.1526, 77.5771, "high altitude, low aerosol"),
)

CITY_BY_KEY = {c.key: c for c in CITIES}


# ---------------------------------------------------------------------------
# Published plant performance -- the only genuinely independent reference
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PublishedYield:
    """A measured or industry-reported specific yield, with its provenance."""

    label: str
    specific_yield_kwh_per_kwp_yr: tuple[float, float]
    performance_ratio: tuple[float, float] | None
    source: str
    caveat: str = ""


PUBLISHED_YIELDS: tuple[PublishedYield, ...] = (
    PublishedYield(
        label="Delhi 12 kWp rooftop, measured",
        specific_yield_kwh_per_kwp_yr=(1147.0, 1147.0),
        performance_ratio=(0.85, 0.93),
        source="Performance evaluation of a rooftop PV plant in Northern India",
        caveat=(
            "A single well-maintained plant, so its PR sits above the typical "
            "range. Reported as a point value, not a band."
        ),
    ),
    PublishedYield(
        label="Indian rooftop, typical year one",
        specific_yield_kwh_per_kwp_yr=(1400.0, 1700.0),
        performance_ratio=(0.78, 0.83),
        source="Industry-observed annual figures, high-irradiance states",
        caveat="Assumes a tilted array; this model treats every roof as horizontal.",
    ),
    PublishedYield(
        label="Assam MW-scale rooftop, measured",
        specific_yield_kwh_per_kwp_yr=(1055.0, 1055.0),
        performance_ratio=(0.70, 0.70),
        source="Performance analysis of MW-scale rooftop plants in Assam",
        caveat="2.89 kWh/kWp/day annualised; high-cloud north-east regime.",
    ),
)

#: The plausibility band the model's specific yield should fall inside: the
#: union of the published figures above, widened slightly. Deliberately broad --
#: these plants differ in tilt, module technology and cleaning regime.
SPECIFIC_YIELD_BAND = (1000.0, 1750.0)

#: Observed year-one performance ratio for Indian rooftop plants.
PERFORMANCE_RATIO_BAND = (0.70, 0.93)


# ---------------------------------------------------------------------------
# Cached NASA POWER access
# ---------------------------------------------------------------------------


@dataclass
class DailySeries:
    """Daily NASA POWER values for one city-year, in kWh/m2/day."""

    city: str
    year: int
    ghi: dict[str, float] = field(default_factory=dict)
    diffuse: dict[str, float] = field(default_factory=dict)
    dni: dict[str, float] = field(default_factory=dict)

    @property
    def n_valid(self) -> int:
        return len(self.ghi)

    def annual_ghi_kwh_m2(self) -> float:
        """
        Annual total, scaled up if days are missing.

        NASA POWER daily data runs to within about a week of real time, so a
        completed year is normally complete. The scaling guards a partial year
        rather than silently under-reporting it.
        """
        if not self.ghi:
            return 0.0
        total = sum(self.ghi.values())
        days_in_year = 366 if _is_leap(self.year) else 365
        return total * days_in_year / self.n_valid

    def beam_fraction(self) -> float | None:
        """
        Annual beam fraction, as 1 - diffuse/GHI.

        This is the check that matters most: ERA5 uses a monthly aerosol
        climatology and is documented to overestimate direct and underestimate
        diffuse, with the error growing in aerosol load -- precisely India's
        regime. Delhi 2020 from POWER is 0.531, against the 0.55-0.72 the
        project's ERA5-derived figure spans.
        """
        common = set(self.ghi) & set(self.diffuse)
        if not common:
            return None
        ghi_total = sum(self.ghi[d] for d in common)
        if ghi_total <= 0:
            return None
        diffuse_total = sum(self.diffuse[d] for d in common)
        return 1.0 - diffuse_total / ghi_total

    def monthly_ghi_kwh_m2(self) -> dict[int, float]:
        """Monthly GHI totals, keyed 1-12."""
        out: dict[int, float] = {}
        for day, value in self.ghi.items():
            month = int(day[4:6])
            out[month] = out.get(month, 0.0) + value
        return out


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def cache_path(city: str, year: int) -> pathlib.Path:
    return CACHE_DIR / "nasa_power" / f"{city}_{year}.json"


def load_cached(city: str, year: int) -> DailySeries | None:
    """Load a cached city-year, or None if it has not been fetched."""
    path = cache_path(city, year)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return DailySeries(
        city=city,
        year=year,
        ghi=payload["ghi"],
        diffuse=payload.get("diffuse", {}),
        dni=payload.get("dni", {}),
    )


def save_cached(series: DailySeries, meta: dict) -> pathlib.Path:
    """Persist a fetched city-year alongside the request that produced it."""
    path = cache_path(series.city, series.year)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "city": series.city,
                "year": series.year,
                "ghi": series.ghi,
                "diffuse": series.diffuse,
                "dni": series.dni,
                "_meta": meta,
            },
            indent=1,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def missing_city_years(years: tuple[int, ...]) -> list[tuple[str, int]]:
    """Which city-years are not yet cached."""
    return [
        (city.key, year) for city in CITIES for year in years if load_cached(city.key, year) is None
    ]
