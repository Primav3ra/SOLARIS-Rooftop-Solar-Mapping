"""
Applying the hourly decomposition model to a window-aggregated request.

The integration problem, stated plainly
---------------------------------------
The model is fitted on **hourly** rows: given this hour's clearness index and
sun geometry, what fraction of the irradiance is diffuse? The API serves
**windows**: a month, a quarter, a year, summed to one GHI total. There is no
hour to hand the model.

Two bad ways to bridge that, both rejected:

*Feed the model the period aggregate as though it were an hour.* Diffuse
fraction is strongly non-linear in the clearness index, so evaluating it once
at the mean is not the mean of evaluating it -- Jensen again, the same trap the
soiling model documents.

*Fetch hourly ERA5 for the window and run the model per hour.* Correct, and
unaffordable: it is 8,760 values per request against a quota where one
development day consumed 91% of a month.

What this module does instead
-----------------------------
Evaluate the model at **the sun positions the request already computed**, and
weight the results by the same insolation weights the shadow model uses.

Those positions exist for every request -- 13 for a day, 39 for a month, 31 for
a year -- and they are already insolation-weighted, so the low-sun hours that
carry little energy contribute little here too. Using the same weighting as the
shadow trace also means the beam fraction and the shadow penalty are evaluated
over one consistent sampling of the window, rather than two that disagree.

The clearness index is the one quantity that cannot come from geometry, so it
is derived once for the whole window: measured GHI over extraterrestrial
horizontal insolation, the latter computed analytically per day and summed.

What this approximation costs
-----------------------------
Sub-daily variation in clearness is lost. Every sampled position within a
window sees the same kt, so a window that alternates clear mornings with cloudy
afternoons is modelled as uniformly average. The residual curvature over the
*geometry* axis is still captured, because each position carries its own
elevation and air mass -- it is only the kt axis that is flattened.

That is a real loss, and it is why ``source`` on the returned prediction says
``model:period`` rather than ``model``. It is still a strict improvement on the
alternative it replaces, which was a single constant 0.60 for every location,
season and sky state in India.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from solaris.ml.features import SOLAR_CONSTANT

#: Days beyond which a window is sampled rather than summed day by day.
#:
#: The extraterrestrial sum is closed-form per day and cheap, but a decade-long
#: window would still walk thousands of dates for a quantity that varies
#: smoothly. Sampling every nth day past this length costs nothing measurable
#: in accuracy.
MAX_EXACT_DAYS = 400


@dataclass(frozen=True)
class BeamFractionResult:
    """A beam fraction with its provenance, ready for ``data_quality``."""

    beam_fraction: float
    source: str
    detail: str
    clearness_index: float | None = None
    fallback_rate: float = 0.0


def _eccentricity(day_of_year: int) -> float:
    """First-order Earth-Sun distance correction."""
    return 1.0 + 0.033 * math.cos(2.0 * math.pi * day_of_year / 365.25)


def _solar_declination(day_of_year: int) -> float:
    """Declination in radians, Cooper's approximation."""
    return math.radians(23.45) * math.sin(2.0 * math.pi * (284 + day_of_year) / 365.25)


def daily_extraterrestrial_kwh_m2(latitude_deg: float, day_of_year: int) -> float:
    """
    Extraterrestrial insolation on a horizontal surface for one day, kWh/m^2.

    The standard closed form. Integrating the instantaneous value over the
    daylight arc has an analytic solution, so no hourly loop is needed:

        H0 = (24/pi) * Gsc * E * (cos(phi)cos(delta)sin(ws) + ws*sin(phi)sin(delta))

    ``ws`` is the sunset hour angle, clamped for polar day and night so that a
    high-latitude winter window returns zero rather than a domain error.
    """
    phi = math.radians(latitude_deg)
    delta = _solar_declination(day_of_year)

    cos_ws = -math.tan(phi) * math.tan(delta)
    if cos_ws >= 1.0:
        return 0.0  # polar night: the sun does not rise
    sunset_hour_angle = math.acos(max(cos_ws, -1.0))  # -1 clamps to polar day

    watt_hours = (
        (24.0 / math.pi)
        * SOLAR_CONSTANT
        * _eccentricity(day_of_year)
        * (
            math.cos(phi) * math.cos(delta) * math.sin(sunset_hour_angle)
            + sunset_hour_angle * math.sin(phi) * math.sin(delta)
        )
    )
    return max(watt_hours, 0.0) / 1000.0


def period_extraterrestrial_kwh_m2(
    latitude_deg: float, start_date: str, end_date_exclusive: str
) -> float:
    """Extraterrestrial horizontal insolation over ``[start, end)``, kWh/m^2."""
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date_exclusive)
    n_days = (end - start).days
    if n_days <= 0:
        return 0.0

    stride = 1 if n_days <= MAX_EXACT_DAYS else max(1, n_days // MAX_EXACT_DAYS)
    sampled = 0
    total = 0.0
    for offset in range(0, n_days, stride):
        day = start + timedelta(days=offset)
        total += daily_extraterrestrial_kwh_m2(latitude_deg, day.timetuple().tm_yday)
        sampled += 1
    # Scale a sampled sum back up to the full window.
    return total * (n_days / sampled) if sampled else 0.0


def period_clearness_index(
    *, ghi_kwh_m2: float, latitude_deg: float, start_date: str, end_date_exclusive: str
) -> float | None:
    """
    Window clearness index: measured GHI over extraterrestrial insolation.

    Returns ``None`` when the denominator vanishes, which happens for a polar
    winter window and would otherwise divide by zero. Clipped at 1.0 for the
    same reason the training set is: a clearness index above unity is a
    retrieval inconsistency, not a physical state.
    """
    extraterrestrial = period_extraterrestrial_kwh_m2(latitude_deg, start_date, end_date_exclusive)
    if extraterrestrial <= 0.0 or ghi_kwh_m2 <= 0.0:
        return None
    return min(ghi_kwh_m2 / extraterrestrial, 1.0)


def beam_fraction_for_window(
    *,
    era5_beam_fraction: float,
    era5_source: str,
    ghi_kwh_m2: float,
    latitude_deg: float,
    start_date: str,
    end_date_exclusive: str,
    solar_positions: Sequence[tuple[float, ...]],
) -> BeamFractionResult:
    """
    Beam fraction for a window, from the learned model where it applies.

    Falls back, in order, to Erbs and then to the ERA5 figure the caller
    already has. Every path reports which one answered, because a corrected
    value and an uncorrected one must not be indistinguishable in the response.
    """
    from solaris.ml import registry
    from solaris.ml.features import FEATURE_NAMES, Sample, kasten_young_air_mass

    kt = period_clearness_index(
        ghi_kwh_m2=ghi_kwh_m2,
        latitude_deg=latitude_deg,
        start_date=start_date,
        end_date_exclusive=end_date_exclusive,
    )
    if kt is None or not solar_positions:
        return BeamFractionResult(
            beam_fraction=era5_beam_fraction,
            source=era5_source,
            detail=(
                "No clearness index could be formed for this window, so the "
                "ERA5-derived beam fraction was used unchanged."
            ),
        )

    day_of_year = date.fromisoformat(start_date).timetuple().tm_yday
    sin_doy = math.sin(2.0 * math.pi * day_of_year / 365.25)
    cos_doy = math.cos(2.0 * math.pi * day_of_year / 365.25)

    samples: list[Sample] = []
    weights: list[float] = []
    for position in solar_positions:
        altitude_deg, _azimuth, weight = float(position[0]), float(position[1]), float(position[2])
        if altitude_deg <= 0.0 or weight <= 0.0:
            continue
        samples.append(
            Sample(
                city="request",
                year=int(start_date[:4]),
                stamp=start_date,
                kt=kt,
                sin_elevation=math.sin(math.radians(altitude_deg)),
                air_mass=kasten_young_air_mass(altitude_deg),
                sin_doy=sin_doy,
                cos_doy=cos_doy,
                abs_latitude=abs(latitude_deg),
                # No sub-daily series here, so every sampled position sits at
                # its own day's mean by construction. Stated in the module
                # docstring as the cost of this approximation.
                kt_persistence=1.0,
                diffuse_fraction=0.0,
                ghi=0.0,
            )
        )
        weights.append(weight)

    if not samples:
        return BeamFractionResult(
            beam_fraction=era5_beam_fraction,
            source=era5_source,
            detail=(
                "Every sampled sun position was below the horizon, so the "
                "ERA5-derived beam fraction was used unchanged."
            ),
            clearness_index=kt,
        )

    prediction = registry.predict_diffuse_fraction(samples)
    if not prediction.values:
        return BeamFractionResult(
            beam_fraction=era5_beam_fraction,
            source=era5_source,
            detail="The decomposition chain returned nothing; ERA5 used unchanged.",
            clearness_index=kt,
        )

    total_weight = sum(weights)
    diffuse_fraction = sum(v * w for v, w in zip(prediction.values, weights, strict=True)) / (
        total_weight or 1.0
    )
    beam = min(max(1.0 - diffuse_fraction, 0.0), 1.0)

    assert len(FEATURE_NAMES) == 7  # the Sample above must stay in step
    return BeamFractionResult(
        beam_fraction=round(beam, 4),
        source=f"{prediction.source}:period",
        detail=(
            f"{prediction.detail} Evaluated at {len(samples)} insolation-weighted "
            f"sun positions sharing one window clearness index of {kt:.3f}, "
            f"against an ERA5-derived {era5_beam_fraction:.3f}."
        ),
        clearness_index=round(kt, 4),
    )
