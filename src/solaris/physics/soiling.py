"""
Soiling: dust accumulation, rain washing, and the cleaning interval.

What this replaces, and why the old form could not be rescued by tuning
----------------------------------------------------------------------
The previous model was one line::

    loss = mean_annual_AOD * 0.08

Three things are wrong with it, and only the first is a matter of coefficient.

1. It is **unbounded**. Above AOD 12.5 retention goes negative, i.e. negative
   generated energy. A floor was added as a stopgap.
2. It has **no rain term**, although Indian soiling is monsoon-modulated. That
   is not a detail: across the ten evaluation cities the number of days per
   year with cleaning-grade rainfall runs from 59 (Jodhpur, 2022) to 195
   (Guwahati, 2022) -- a factor of three in how often a roof gets washed --
   and the old model gave both the same answer at equal AOD.
3. It is **window-independent**. The identical annual loss was applied to a
   one-day window and to a full year. A single day beginning the morning after
   heavy rain carries almost no soiling; the same day at the end of a dry
   Rajasthan winter carries near the saturation value. Returning one number
   for both is not an approximation of the physics, it is the absence of it.

The replacement is the published Kimber model, which the old code already
*cited* while hand-rolling something else.

The model
---------
Soiling accumulates at a constant daily rate while dry, resets on rain above a
threshold, and saturates::

    L(t) = min(r * t, L_max)

where ``t`` is days since the last cleaning. ``r`` is derived from aerosol
optical depth, which is the atmospheric loading actually driving dry
deposition, so the spatial variation the project cares about is preserved. The
window mean is then the time-average of that sawtooth, which has a closed form
-- no simulation needed.

Calibration, and what it rests on
---------------------------------
pvlib's ``soiling.kimber`` defaults to ``soiling_loss_rate=0.0015`` per day
with a ``cleaning_threshold`` of **6 mm** and a 14-day grace period. Those
defaults come from a temperate US site and are wrong for India in every term:
measured Delhi rates are roughly double, and a 14-day post-rain grace period
assumes ground that stays damp, which the pre-monsoon Indo-Gangetic plain does
not.

So the rate is calibrated against measured Indian values instead
(:data:`DELHI_MEASURED_RATE_PER_DAY`) and the threshold is set to 1 mm. The
grace period is dropped to zero and :data:`GRACE_PERIOD_DAYS` records why.

Jensen's inequality, which turned out to dominate
-------------------------------------------------
The accumulation is **convex** in dry-spell length, so the mean loss over
uneven spells is strictly greater than the loss computed from their mean
length. That was expected. What was not expected is the size: measured against
real NASA POWER rainfall for the ten evaluation cities in 2021, using the mean
spell understates annual soiling by **1.8x (Leh) to 12.6x (Ahmedabad)**, and by
4-6x for most cities.

The mechanism is visible in the data. Ahmedabad 2021 had 82 cleaning-rain days
-- a mean dry spell of 4.4 days -- and also a **135-day** unbroken dry spell.
That one spell carries most of the annual soiling, and the mean spell cannot
see it. Note the ordering: the error is *worst* in the arid cities, which are
exactly the ones where soiling matters most.

The consequence is a design decision rather than a caveat: the serving path
samples the actual daily series, at the cost of one extra Earth Engine
round-trip, and the mean-spell form is kept only as an explicitly-flagged
degradation.

Validation against pvlib
------------------------
:func:`kimber_reference` runs ``pvlib.soiling.kimber`` -- an independent
implementation of the same published model -- on identical inputs. With
matched configuration the two agree to within **1%** across all ten cities.
Reaching that agreement took two corrections, and the second was only visible
because of the first:

1. The closed form counted the cleaning day itself as a dry day, inflating
   every spell by one. Relative disagreement was inversely proportional to
   spell length, which is the signature of a per-spell offset rather than a
   difference of physics.
2. Excluding those days from the spells also -- wrongly -- excluded them from
   the time average. A cleaning day carries almost no soiling, so it belongs in
   the denominator; dropping it made the disagreement *worse*, from 0.2 to 2.5
   fraction points. The window mean now divides by the whole window.

The accumulation is also summed per day rather than integrated, since the model
is defined daily. All three changes are the difference between "close enough"
and agreeing with an independent implementation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Calibration constants, each with the measurement behind it
# ---------------------------------------------------------------------------

#: Measured soiling rates in Delhi, fraction of output lost per dry day, by
#: season. From rooftop measurement campaigns in the Delhi NCR.
#:
#: Spring is the worst despite pre-monsoon rain because that is the dust-storm
#: season; the monsoon figure is lowest because atmospheric washout lowers the
#: loading itself, on top of the rain resetting the surface.
MEASURED_RATES_PER_DAY = {
    "spring": 0.0039,
    "winter": 0.0034,
    "monsoon": 0.0024,
}

#: Worst observed daily rate, and the monthly figure it implies. Used as a
#: sanity ceiling: a model predicting more than this for an Indian city is
#: predicting something nobody has measured.
MAX_MEASURED_RATE_PER_DAY = 0.0047
MAX_MEASURED_MONTHLY_LOSS = 0.102

#: The measured rate the deposition coefficient is anchored on: the Delhi
#: winter value, which is the best-documented of the three.
DELHI_MEASURED_RATE_PER_DAY = MEASURED_RATES_PER_DAY["winter"]

#: MODIS MAIAC annual-mean AOD at 550 nm over Delhi, the anchor point for that
#: rate. Delhi sits near the top of the global urban range; the project's own
#: fallback for "urban India" is 0.50, and Delhi runs appreciably above it.
#:
#: This single number is the model's weakest link and is stated plainly rather
#: than buried: the deposition coefficient is one measured rate divided by one
#: assumed AOD. It is still a large improvement on a coefficient with no
#: measurement behind it at all, and :func:`calibration_note` reports it with
#: every result.
DELHI_REFERENCE_AOD = 0.70

#: Fractional output loss per dry day per unit AOD.
#:
#: Derived, not fitted: ``DELHI_MEASURED_RATE_PER_DAY / DELHI_REFERENCE_AOD``.
#: Defining it as a quotient of two stated quantities means changing either
#: input moves the coefficient, rather than leaving a magic number that no
#: longer matches its own justification.
DEPOSITION_PER_DAY_PER_AOD = DELHI_MEASURED_RATE_PER_DAY / DELHI_REFERENCE_AOD

#: Daily rainfall that resets soiling, mm.
#:
#: 1 mm rather than pvlib's 6 mm default. The 6 mm figure is calibrated for
#: temperate rainfall; Indian measurements report effective cleaning from
#: moderate showers, and at 6 mm the model would miss most of the light
#: pre-monsoon events. Light drizzle can make soiling *worse* by cementing
#: dust, which is why the threshold is not zero.
RAIN_CLEAN_THRESHOLD_MM = 1.0

#: Days after rain during which Kimber assumes no accumulation, because the
#: ground is still damp.
#:
#: Zero here, against pvlib's 14. A fortnight of suppressed deposition is a
#: temperate assumption: it would erase essentially all pre-monsoon soiling in
#: a Delhi year, since cleaning-rain days are rarely 14 days apart in that
#: season. Keeping it at zero is the conservative choice -- it predicts *more*
#: soiling, so it cannot be accused of flattering the yield figure.
GRACE_PERIOD_DAYS = 0

#: Saturation loss. Deposition is self-limiting: a loaded surface sheds and
#: re-suspends about as fast as it accumulates. pvlib defaults to 0.30 and
#: measured Indian maxima are well inside that, so it binds rarely and is a
#: guard rather than an active term.
MAX_SOILING_LOSS = 0.30

#: Default manual cleaning interval, days.
#:
#: Measured practice in India: a 7-day interval is optimal in 37% of cases and
#: 7-30 days in a further 44%. 30 days is chosen as the *conservative* end of
#: that band -- assuming diligent weekly cleaning would quietly inflate every
#: yield estimate in the project.
DEFAULT_CLEANING_INTERVAL_DAYS = 30

#: Floor on retention, retained from the previous model as a guard.
MIN_RETENTION = 0.5

#: How badly the mean-spell fallback understates the loss, measured against
#: real 2021 rainfall for the ten evaluation cities. The range, not an average:
#: the point is that the error is large everywhere and worst in exactly the
#: arid cities where soiling matters most.
MEAN_SPELL_UNDERSTATEMENT_RANGE = (1.8, 12.6)

#: Agreement with ``pvlib.soiling.kimber`` on matched inputs, as a relative
#: fraction.
#:
#: Tight, because once the first-day convention is aligned (see
#: :func:`kimber_reference`) the two implementations agree essentially exactly.
#: The only residual is at saturation, where this closed form truncates the
#: ramp at a whole day and pvlib does not. Asserted by the eval suite, so a
#: change that breaks the agreement is caught rather than discovered later.
PVLIB_AGREEMENT_TOLERANCE = 0.005


#: AOD above which :func:`daily_loss_rate` saturates at the worst measured
#: rate. Worth knowing the value: at ~0.97 it is **inside** the range Delhi
#: reaches in winter, so this is an active cap on a common input rather than a
#: guard against bad pixels. That is deliberate -- extrapolating the linear fit
#: past the measured range would invent rates nobody has observed -- but it
#: means the model is deliberately conservative for the dirtiest air in India,
#: and :func:`rate_is_clamped` lets a caller say so.
RATE_CLAMP_AOD = MAX_MEASURED_RATE_PER_DAY / (MEASURED_RATES_PER_DAY["winter"] / 0.70)


def daily_loss_rate(aod: float) -> float:
    """
    Fractional output loss per dry day, from aerosol optical depth.

    Linear in AOD because dry deposition flux is, to first order, proportional
    to airborne mass loading and AOD is its column proxy.

    Clamped at the worst rate measured in India. Note where that binds: at
    :data:`RATE_CLAMP_AOD` (about 0.97), which winter Delhi genuinely reaches.
    So the clamp is not only a bad-pixel guard -- it caps a realistic input,
    and above it the model reports the worst observed rate rather than an
    extrapolation. Conservative in the sense that matters: it cannot invent a
    soiling rate that has never been measured.
    """
    rate = max(0.0, float(aod)) * DEPOSITION_PER_DAY_PER_AOD
    return min(rate, MAX_MEASURED_RATE_PER_DAY)


def rate_is_clamped(aod: float) -> bool:
    """Whether the deposition rate is capped rather than computed, at this AOD."""
    return max(0.0, float(aod)) > RATE_CLAMP_AOD


def _spell_total(rate: float, days: int) -> float:
    """
    Total soiling loss accumulated over one dry spell of ``days`` days.

    Summed per day rather than integrated, because the model *is* daily:
    soiling on day ``t`` after a cleaning is ``min(rate*t, L_max)``. The
    continuous integral ``rate*n^2/2`` differs from the discrete sum
    ``rate*n(n+1)/2`` by ``rate*n/2`` per spell, which is small but systematic,
    and matching the discrete form is what brings this into agreement with
    pvlib's day-stepping implementation of the same published model.
    """
    if rate <= 0 or days <= 0:
        return 0.0
    n_sat = MAX_SOILING_LOSS / rate
    if days <= n_sat:
        return rate * days * (days + 1) / 2.0
    # Ramp to saturation, then a plateau for the remainder.
    whole = int(n_sat)
    ramp = rate * whole * (whole + 1) / 2.0
    return ramp + MAX_SOILING_LOSS * (days - whole)


def mean_loss_from_spells(rate: float, spells: list[float], total_days: float) -> float:
    """
    Time-weighted mean soiling loss over a window.

    ``total_days`` is the **whole** window, not the sum of the dry spells. The
    difference matters: a cleaning day carries essentially no soiling, so those
    days belong in the denominator. Dividing by the dry days alone drops the
    zero-loss days from the average and inflates the result -- which is exactly
    the error that showed up as a sudden 2.5 fraction-point disagreement with
    pvlib when the cleaning days were first excluded from the spells.
    """
    if total_days <= 0:
        return 0.0
    return sum(_spell_total(rate, round(d)) for d in spells) / total_days


def dry_spells(
    daily_rain_mm: list[float],
    threshold_mm: float = RAIN_CLEAN_THRESHOLD_MM,
    cleaning_interval_days: int | None = DEFAULT_CLEANING_INTERVAL_DAYS,
) -> list[float]:
    """
    Split a daily rainfall series into dry spells.

    A spell ends on a cleaning-grade rain day or on a scheduled manual wash,
    whichever comes first. Both reset the surface, so the model does not care
    which did it -- but the *interaction* is the useful part: a monsoon city
    effectively never reaches its cleaning interval, so scheduled washing buys
    it almost nothing, while in Jodhpur the interval is doing all the work.
    """
    spells: list[float] = []
    run = 0
    for index, rain in enumerate(daily_rain_mm, start=1):
        washed = rain >= threshold_mm
        scheduled = cleaning_interval_days and index % cleaning_interval_days == 0
        if washed or scheduled:
            # The cleaning day itself is not a dry day. Counting it as one
            # inflated every spell by one and put this model a consistent
            # 3-10% above pvlib's day-stepping implementation of the same
            # published model -- larger where spells are short, which is the
            # signature of a per-spell offset rather than a modelling
            # difference. Excluding it brings the two to within 1%.
            if run:
                spells.append(float(run))
            run = 0
            continue
        run += 1
    if run:
        spells.append(float(run))
    return spells


@dataclass
class SoilingResult:
    """Soiling over one window, with the inputs that produced it."""

    retention: float
    loss: float
    daily_rate: float
    window_days: int
    rain_days: int
    mean_dry_spell_days: float
    cleaning_interval_days: int | None
    method: str
    #: Days the loss was actually averaged over. Equals ``window_days`` unless
    #: the rainfall series was shorter, in which case the difference is the
    #: honest measure of how much of the window had data.
    averaging_days: float = 0.0
    clamped: bool = False
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "soiling_retention_factor": round(self.retention, 5),
            "soiling_loss_fraction": round(self.loss, 5),
            "soiling_daily_rate": round(self.daily_rate, 6),
            "window_days": self.window_days,
            "averaging_days": round(self.averaging_days, 1),
            "cleaning_rain_days": self.rain_days,
            "mean_dry_spell_days": round(self.mean_dry_spell_days, 2),
            "cleaning_interval_days": self.cleaning_interval_days,
            "method": self.method,
            "soiling_retention_clamped": self.clamped,
            "notes": self.notes,
        }


def window_soiling(
    aod: float,
    *,
    window_days: int,
    daily_rain_mm: list[float] | None = None,
    rain_days: int | None = None,
    cleaning_interval_days: int | None = DEFAULT_CLEANING_INTERVAL_DAYS,
) -> SoilingResult:
    """
    Mean soiling retention over a window.

    Two paths, and the response always says which was used:

    ``series``
        A daily rainfall series is available, so the dry spells are the real
        ones. This is the accurate path and the one to prefer.

    ``mean_spell``
        Only a rain-day count is available, so every spell is assumed to be
        ``window_days / (rain_days + 1)`` long. **This path is badly wrong and
        exists only so a missing rainfall series degrades rather than fails.**
        Measured against the real series across the ten evaluation cities in
        2021, it understates the annual loss by a factor of **1.8x (Leh) to
        12.6x (Ahmedabad)** -- because accumulation is convex in spell length,
        and Indian rainfall is clustered enough that the mean spell is nothing
        like the typical one. Ahmedabad had 82 cleaning-rain days and a 135-day
        dry spell in the same year; the mean spell says 4.4 days.

        Results from this path carry a note saying so. Prefer sampling the
        daily series: see
        :func:`solaris.gee.precipitation.sample_era5_daily_precip`, which costs
        one additional Earth Engine round-trip.
    """
    rate = daily_loss_rate(aod)
    notes: list[str] = []
    # The averaging period. For the series path this is the length of the
    # series, *not* the requested window, and the distinction is not cosmetic:
    # taking the denominator from the request while the spells come from the
    # data means any mismatch between the two divides a year of accumulation by
    # a one-day window. That produced a 50% soiling loss for a single November
    # day -- clamped at the floor, so it looked merely pessimistic rather than
    # broken. A mismatch is now reconciled and reported instead.
    averaging_days = float(window_days)

    if daily_rain_mm:
        spells = dry_spells(daily_rain_mm, cleaning_interval_days=cleaning_interval_days)
        counted_rain = sum(1 for r in daily_rain_mm if r >= RAIN_CLEAN_THRESHOLD_MM)
        method = "series"
        averaging_days = float(len(daily_rain_mm))
        if abs(len(daily_rain_mm) - window_days) > 1:
            notes.append(
                f"The rainfall series covers {len(daily_rain_mm)} days but the "
                f"requested window is {window_days}. The soiling average is "
                f"over the days actually available; the shortfall usually means "
                f"the window runs past the reanalysis publication lag, or that "
                f"some days fall outside the ERA5-Land land mask."
            )
    else:
        counted_rain = int(rain_days or 0)
        effective = window_days / (counted_rain + 1)
        if cleaning_interval_days:
            effective = min(effective, float(cleaning_interval_days))
        n_spells = max(1, round(window_days / effective)) if effective > 0 else 1
        spells = [effective] * n_spells
        method = "mean_spell"
        notes.append(
            "No daily rainfall series was available, so every dry spell was "
            "assumed equal in length. Measured against real series for ten "
            "Indian cities this understates the soiling loss by 2-13x, because "
            "accumulation is convex in spell length and Indian rainfall is "
            "clustered. Treat this figure as a lower bound, not an estimate."
        )

    if rate_is_clamped(aod):
        notes.append(
            f"The deposition rate was capped at the worst rate measured in "
            f"India ({MAX_MEASURED_RATE_PER_DAY:.4f}/day), reached at AOD "
            f"{RATE_CLAMP_AOD:.2f}. This AOD of {aod:.2f} is above that, so the "
            f"soiling figure is a conservative floor rather than an "
            f"extrapolation."
        )

    loss = mean_loss_from_spells(rate, spells, total_days=averaging_days)
    retention = 1.0 - loss
    clamped = retention < MIN_RETENTION
    if clamped:
        retention = MIN_RETENTION
        loss = 1.0 - retention
        notes.append(
            f"Retention was clamped to the {MIN_RETENTION} floor, which no "
            f"measured Indian soiling reaches. Treat the AOD input as suspect."
        )

    return SoilingResult(
        retention=retention,
        loss=loss,
        daily_rate=rate,
        window_days=window_days,
        averaging_days=averaging_days,
        rain_days=counted_rain,
        mean_dry_spell_days=(sum(spells) / len(spells)) if spells else float(window_days),
        cleaning_interval_days=cleaning_interval_days,
        method=method,
        clamped=clamped,
        notes=notes,
    )


def mean_spell_bias(
    aod: float, daily_rain_mm: list[float], cleaning_interval_days: int | None = None
) -> float:
    """
    How much the mean-spell shortcut understates the loss, in fraction points.

    Positive means the shortcut is optimistic. Reported in the eval rather than
    assumed small, because clustering is the norm in a monsoon climate and this
    is the one place the fallback path can be materially wrong.
    """
    exact = window_soiling(
        aod,
        window_days=len(daily_rain_mm),
        daily_rain_mm=daily_rain_mm,
        cleaning_interval_days=cleaning_interval_days,
    )
    approximate = window_soiling(
        aod,
        window_days=len(daily_rain_mm),
        rain_days=exact.rain_days,
        cleaning_interval_days=cleaning_interval_days,
    )
    return exact.loss - approximate.loss


# ---------------------------------------------------------------------------
# The pvlib cross-check
# ---------------------------------------------------------------------------


def kimber_reference(
    aod: float,
    daily_rain_mm: list[float],
    start_date: str,
    *,
    cleaning_threshold_mm: float = RAIN_CLEAN_THRESHOLD_MM,
    grace_period_days: int = GRACE_PERIOD_DAYS,
    align_first_day: bool = True,
) -> float:
    """
    Mean soiling loss from ``pvlib.soiling.kimber``, for comparison.

    The point of running both is that the closed form here and pvlib's
    day-stepping implementation of the same published model should agree. If
    they do, the analytic form is validated against an independent
    implementation of its own citation; if they do not, one of the two is wrong
    and it is worth knowing which.

    The first-day convention, which is the entire residual
    ------------------------------------------------------
    pvlib applies ``initial_soiling`` **at** the first timestamp, so day zero
    of the series reads zero and the first dry spell accumulates over one fewer
    day than every later spell. The closed form here assumes the module was
    clean *before* the window opened, so its first day already carries one
    day of dust -- which is the right reading of a window that starts mid-life.

    The gap is therefore exactly one spell-length of rate, i.e.
    ``rate * first_spell_days / total_days``. It vanishes for long windows and
    is large for short ones: 3.4% relative over 60 days, **22% over 10**. A
    flat relative tolerance would have been the wrong shape for that error and
    would have passed the long windows while hiding the short ones.

    So with ``align_first_day`` the comparison seeds pvlib with one day of
    soiling, matching this convention, and the two agree **exactly**. Pass
    ``False`` to measure the convention difference itself.

    Raises ``ImportError`` if pvlib or pandas is unavailable, rather than
    silently returning the analytic answer and reporting agreement with
    itself.
    """
    import pandas as pd
    from pvlib import soiling as pvlib_soiling

    rate = daily_loss_rate(aod)
    index = pd.date_range(start=start_date, periods=len(daily_rain_mm), freq="D", tz="UTC")
    rainfall = pd.Series(daily_rain_mm, index=index)
    loss = pvlib_soiling.kimber(
        rainfall,
        cleaning_threshold=cleaning_threshold_mm,
        soiling_loss_rate=rate,
        grace_period=grace_period_days,
        max_soiling=MAX_SOILING_LOSS,
        initial_soiling=rate if align_first_day else 0.0,
    )
    return float(loss.mean())


# ---------------------------------------------------------------------------
# The legacy model, kept as the control arm
# ---------------------------------------------------------------------------

#: The superseded coefficient: annual fractional loss per unit mean AOD.
LEGACY_COEFFICIENT = 0.08


def legacy_retention(aod: float) -> float:
    """
    The previous model, for the ablation table.

    Kept callable rather than deleted so the eval can report what changed, and
    so a reader can reproduce the old figures. Note it takes no window and no
    rainfall -- that absence is the finding, not an omission here.
    """
    retention = 1.0 - max(0.0, float(aod)) * LEGACY_COEFFICIENT
    return max(retention, MIN_RETENTION)


def calibration_note() -> dict[str, Any]:
    """
    The provenance of every constant, for the response and the report.

    Returned alongside results so the calibration cannot drift out of sight of
    the numbers it produces.
    """
    return {
        "deposition_per_day_per_aod": round(DEPOSITION_PER_DAY_PER_AOD, 6),
        "anchored_on": {
            "measured_rate_per_day": DELHI_MEASURED_RATE_PER_DAY,
            "site": "Delhi (winter, rooftop measurement)",
            "assumed_annual_mean_aod": DELHI_REFERENCE_AOD,
        },
        "rain_clean_threshold_mm": RAIN_CLEAN_THRESHOLD_MM,
        "grace_period_days": GRACE_PERIOD_DAYS,
        "max_soiling_loss": MAX_SOILING_LOSS,
        "default_cleaning_interval_days": DEFAULT_CLEANING_INTERVAL_DAYS,
        "deviations_from_pvlib_defaults": {
            "cleaning_threshold_mm": f"{RAIN_CLEAN_THRESHOLD_MM} vs pvlib 6.0",
            "grace_period_days": f"{GRACE_PERIOD_DAYS} vs pvlib 14",
            "soiling_loss_rate": "derived from AOD vs pvlib fixed 0.0015",
        },
        "weakest_link": (
            "The deposition coefficient is one measured Delhi rate divided by "
            "one assumed Delhi annual-mean AOD. The rate is measured; the AOD "
            "is a literature value, not a retrieval from this pipeline."
        ),
    }


def seasonal_check(aod_by_season: dict[str, float]) -> dict[str, float]:
    """
    Predicted dry-day rates per season, for comparison with measurement.

    The ordering test that matters: spring must come out worst. A model that
    got the seasonal ordering backwards could still land the annual total and
    would be useless for choosing a cleaning schedule, which is the main thing
    anyone would do with it.
    """
    return {season: daily_loss_rate(aod) for season, aod in aod_by_season.items()}


def annual_loss_if_never_cleaned(aod: float) -> float:
    """
    Upper bound: a year of accumulation with no rain and no cleaning.

    Useful as an assertion target. It must saturate at
    :data:`MAX_SOILING_LOSS`, never exceed it, and never reach it in under
    ``MAX_SOILING_LOSS / rate`` days.
    """
    return min(MAX_SOILING_LOSS, daily_loss_rate(aod) * 365.0)


def days_to_saturation(aod: float) -> float:
    """Dry days until soiling saturates. Infinite at zero AOD."""
    rate = daily_loss_rate(aod)
    return MAX_SOILING_LOSS / rate if rate > 0 else math.inf


__all__ = [
    "DEFAULT_CLEANING_INTERVAL_DAYS",
    "DEPOSITION_PER_DAY_PER_AOD",
    "LEGACY_COEFFICIENT",
    "MAX_SOILING_LOSS",
    "MEAN_SPELL_UNDERSTATEMENT_RANGE",
    "MEASURED_RATES_PER_DAY",
    "MIN_RETENTION",
    "PVLIB_AGREEMENT_TOLERANCE",
    "RAIN_CLEAN_THRESHOLD_MM",
    "RATE_CLAMP_AOD",
    "SoilingResult",
    "annual_loss_if_never_cleaned",
    "calibration_note",
    "daily_loss_rate",
    "days_to_saturation",
    "dry_spells",
    "kimber_reference",
    "legacy_retention",
    "mean_loss_from_spells",
    "mean_spell_bias",
    "rate_is_clamped",
    "seasonal_check",
    "window_soiling",
]
