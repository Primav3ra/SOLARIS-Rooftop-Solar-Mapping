"""
Feature assembly for the beam/diffuse decomposition model.

Why this model, and how it differs from the plan
-----------------------------------------------
The plan's Track A was an ERA5-to-reference bias correction. That needs ERA5
data, which needs Earth Engine credentials -- so the ERA5 side is deferred. But
the *reference* side is buildable now, and it targets the same defect from the
other end.

The defect, measured: the model's beam fraction is its most ERA5-sensitive
input, and the validation harness put the reference mean at **0.540** across 30
city-years while the model falls back to a constant **0.60** and its ERA5 path
documents 0.55-0.72. ERA5 uses a monthly aerosol climatology and is documented
to overestimate direct radiation with the error growing in aerosol load, which
is exactly India's regime.

So: learn the diffuse fraction from cheap, always-available features. That
replaces the 0.60 constant with something reference-grounded, and it produces
the target series an ERA5 correction would later be trained against.

Why the features are these features
-----------------------------------
This is a well-established problem -- Erbs (1982), Reindl (1990),
Boland-Ridley-Lauret -- and every published correlation is driven by the
**clearness index** ``Kt = GHI / extraterrestrial horizontal``. That is the
physically right primary predictor: it measures how much the atmosphere removed,
and diffuse fraction is near 1 under thick cloud and near 0.15 under clear sky.

Added beyond Kt, each for a stated reason:

``solar_elevation``  air mass changes the diffuse fraction at a given Kt; low
                     sun scatters more.
``day_of_year``      encoded as sin/cos, not an integer. An integer lets a tree
                     carve arbitrary date groupings and overfit; the cyclical
                     pair also makes December adjacent to January, which it is.
``latitude``         a proxy for the climate regime.
``kt_persistence``   Kt relative to the day's mean. Broken cloud gives a high
                     diffuse fraction at moderate Kt, which instantaneous Kt
                     alone cannot distinguish from thin uniform haze.

Deliberately **not** included: aerosol optical depth. It would very likely help
-- aerosol is the mechanism behind India's high diffuse fraction -- but it comes
from MODIS via Earth Engine, so requiring it would make the model unusable in
exactly the fallback case it exists to serve.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

#: Solar constant (W/m^2), used for the extraterrestrial normalisation.
SOLAR_CONSTANT = 1366.1

#: Elevation floor (degrees). Below this, GHI is tiny and the clearness index
#: becomes numerically unstable, so samples are dropped rather than fitted.
MIN_ELEVATION_DEG = 7.0

#: Minimum GHI (W/m^2) for a usable sample.
MIN_GHI_W_M2 = 20.0

FEATURE_NAMES = (
    "kt",
    "sin_elevation",
    "air_mass",
    "sin_doy",
    "cos_doy",
    "abs_latitude",
    "kt_persistence",
)


@dataclass(frozen=True)
class Sample:
    """One training row: features plus the diffuse fraction to predict."""

    city: str
    year: int
    stamp: str
    kt: float
    sin_elevation: float
    air_mass: float
    sin_doy: float
    cos_doy: float
    abs_latitude: float
    kt_persistence: float
    diffuse_fraction: float
    ghi: float

    def as_features(self) -> list[float]:
        return [getattr(self, name) for name in FEATURE_NAMES]


def extraterrestrial_horizontal(day_of_year: int, sin_elevation: float) -> float:
    """
    Extraterrestrial irradiance on a horizontal surface (W/m^2).

    The eccentricity correction is the standard Spencer-style first-order term;
    the Earth-Sun distance varies about 3.3% over a year, which is not
    negligible when it sits in the denominator of the clearness index.
    """
    eccentricity = 1.0 + 0.033 * math.cos(2.0 * math.pi * day_of_year / 365.0)
    return SOLAR_CONSTANT * eccentricity * max(sin_elevation, 0.0)


def kasten_young_air_mass(elevation_deg: float) -> float:
    """
    Relative air mass (Kasten & Young 1989).

    Preferred over ``1/sin(elevation)``, which diverges at the horizon; this
    form stays finite and is the standard in PV work.
    """
    if elevation_deg <= 0:
        return 40.0
    zenith = 90.0 - elevation_deg
    denominator = math.cos(math.radians(zenith)) + 0.50572 * (96.07995 - zenith) ** -1.6364
    return min(40.0, 1.0 / denominator) if denominator > 0 else 40.0


def _solar_elevation(lat: float, lon: float, stamp: str) -> float:
    """Solar elevation for a ``YYYYMMDDHH`` UTC stamp, mid-hour."""
    from solaris.gee.solar_geometry import sun_altitude_azimuth_north

    when = datetime(
        int(stamp[0:4]),
        int(stamp[4:6]),
        int(stamp[6:8]),
        int(stamp[8:10]),
        30,
        tzinfo=UTC,
    )
    elevation, _azimuth = sun_altitude_azimuth_north(lat, lon, when)
    return elevation


def build_samples(series, latitude: float, longitude: float) -> list[Sample]:
    """
    Turn one :class:`~solaris.evals.references.HourlySeries` into training rows.

    Samples below the elevation and irradiance floors are dropped: the
    clearness index is numerically unstable there, and those hours carry almost
    no energy so fitting them would trade accuracy where it matters for
    accuracy where it does not.
    """
    samples: list[Sample] = []

    # Daily mean Kt, for the persistence feature.
    per_day: dict[str, list[float]] = {}
    kt_by_stamp: dict[str, float] = {}

    for stamp in series.stamps:
        ghi = series.ghi.get(stamp, 0.0)
        if ghi < MIN_GHI_W_M2:
            continue
        elevation = _solar_elevation(latitude, longitude, stamp)
        if elevation < MIN_ELEVATION_DEG:
            continue
        sin_elevation = math.sin(math.radians(elevation))
        day_of_year = (
            datetime(int(stamp[0:4]), int(stamp[4:6]), int(stamp[6:8]), tzinfo=UTC)
            .timetuple()
            .tm_yday
        )
        extraterrestrial = extraterrestrial_horizontal(day_of_year, sin_elevation)
        if extraterrestrial <= 0:
            continue
        # Clip at 1.0: a Kt above unity means a retrieval inconsistency, not a
        # physical state, and letting it through would teach the model to
        # extrapolate off the end of its own predictor.
        kt = min(ghi / extraterrestrial, 1.0)
        kt_by_stamp[stamp] = kt
        per_day.setdefault(stamp[:8], []).append(kt)

    for stamp, kt in kt_by_stamp.items():
        ghi = series.ghi[stamp]
        diffuse = series.diffuse.get(stamp)
        if diffuse is None or ghi <= 0:
            continue
        diffuse_fraction = min(max(diffuse / ghi, 0.0), 1.0)

        elevation = _solar_elevation(latitude, longitude, stamp)
        day_of_year = (
            datetime(int(stamp[0:4]), int(stamp[4:6]), int(stamp[6:8]), tzinfo=UTC)
            .timetuple()
            .tm_yday
        )
        day_mean = sum(per_day[stamp[:8]]) / len(per_day[stamp[:8]])

        samples.append(
            Sample(
                city=series.city,
                year=series.year,
                stamp=stamp,
                kt=kt,
                sin_elevation=math.sin(math.radians(elevation)),
                air_mass=kasten_young_air_mass(elevation),
                sin_doy=math.sin(2.0 * math.pi * day_of_year / 365.25),
                cos_doy=math.cos(2.0 * math.pi * day_of_year / 365.25),
                abs_latitude=abs(latitude),
                kt_persistence=kt / day_mean if day_mean > 0 else 1.0,
                diffuse_fraction=diffuse_fraction,
                ghi=ghi,
            )
        )
    return samples


def build_dataset(years: tuple[int, ...]) -> list[Sample]:
    """Assemble every cached city-year into one sample list."""
    from solaris.evals.references import CITIES, load_cached_hourly

    samples: list[Sample] = []
    for city in CITIES:
        for year in years:
            series = load_cached_hourly(city.key, year)
            if series is None or series.n_steps == 0:
                continue
            samples.extend(build_samples(series, city.lat, city.lon))
    return samples
