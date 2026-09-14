"""
Reference PV physics via pvlib: plane-of-array transposition and cell
temperature.

What this replaces
------------------
The original chain used global horizontal irradiance directly as if it were
plane-of-array irradiance, so every roof was implicitly horizontal, and
converted it to energy with one lumped scalar
(``0.18 x 0.80 x 0.70 = 0.1008``). No tilt, no albedo, no incidence-angle
modifier, and no temperature-dependent efficiency -- the NOCT cell-temperature
model was described in a docstring but never implemented.

pvlib is used rather than hand-rolling this because it is the industry-standard
validated implementation, and because two of the models the project already
*cites* in its docstrings ship in it: ``soiling.kimber`` is literally the
"Kimber et al. 2006" behind the hand-rolled ``0.08 x AOD`` coefficient.

Two deliberate choices
----------------------
**Hay-Davies, not Perez.** Perez needs a well-behaved sky-clearness and
brightness pair, and degrades when DNI/DHI quality is poor -- which is exactly
ERA5-derived DNI over aerosol-heavy India. Hay-Davies is robust and the
difference at the ~25 deg tilts relevant here is small. Perez is available as a
sensitivity, not the default.

**A monthly-diurnal climatology (12 x 24 = 288 steps), not 8760 hourly steps.**
Transposition and cell temperature are meaningless applied to an annual GHI
total -- both are non-linear in instantaneous irradiance -- but a year of
hourly data per request is not servable. 288 steps captures the annual integral
well because both models are smooth in (zenith, irradiance, temperature). The
discretisation error is *measured* against full hourly in the eval harness
rather than assumed.

``mount`` matters more than it looks
------------------------------------
Flat versus latitude-optimal tilt is worth +8-12% annually in north India. The
published plant figures this project validates against (1400-1700 kWh/kWp/yr)
assume a tilted array, so comparing a flat-roof model result against them would
read as a large model bias when it is purely a configuration mismatch. Every
eval record therefore carries ``mount`` explicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from solaris.physics import losses

MountType = Literal["flat", "optimal_fixed"]
RackingType = Literal[
    "open_rack_glass_glass",
    "close_mount_glass_glass",
    "open_rack_glass_polymer",
    "insulated_back_glass_polymer",
]
TranspositionModel = Literal["haydavies", "isotropic", "perez", "klucher", "reindl"]


class PvlibUnavailableError(RuntimeError):
    """Raised when the pvlib engine is requested but pvlib is not installed."""


def _require_pvlib():
    """
    Import pvlib lazily.

    Kept optional so the default serving path stays light: pvlib pulls numpy,
    pandas and scipy, which the legacy engine does not need. Install with
    ``pip install -e ".[physics]"``.
    """
    try:
        import pvlib
    except ImportError as exc:  # pragma: no cover - exercised by a skipif test
        raise PvlibUnavailableError(
            "The pvlib engine requires pvlib. Install it with:\n"
            '    pip install -e ".[physics]"\n'
            "or use engine='legacy_lumped', which needs no extra dependencies."
        ) from exc
    return pvlib


def optimal_tilt_deg(latitude: float) -> float:
    """
    Latitude-optimal fixed tilt for a south-facing array.

    ``0.87 * |lat| + 3.1`` is the widely used regression for the annual-energy
    optimum, and lands near 28 deg for Delhi -- consistent with what PVGIS
    reports for ``optimalangles=1`` there. Clamped to [0, 40]: beyond that the
    annual optimum never goes for inhabited latitudes, and a bad latitude
    should not produce an absurd geometry.
    """
    return max(0.0, min(40.0, 0.87 * abs(latitude) + 3.1))


def surface_azimuth_deg(latitude: float) -> float:
    """Equator-facing: 180 (south) in the northern hemisphere, 0 in the southern."""
    return 180.0 if latitude >= 0 else 0.0


@dataclass(frozen=True)
class ArrayConfig:
    """Array geometry and thermal mounting."""

    mount: MountType = "flat"
    #: Explicit tilt override (deg). None means derive it from ``mount``.
    tilt_deg: float | None = None
    azimuth_deg: float | None = None
    #: Ground reflectance. 0.2 is the usual urban default.
    albedo: float = 0.2
    #: Rooftop arrays in India are typically on elevated open racking, which
    #: runs 5-10 degC cooler than close-mounted.
    racking: RackingType = "open_rack_glass_glass"

    def resolve(self, latitude: float) -> tuple[float, float]:
        """Return ``(tilt_deg, azimuth_deg)`` for this config at a latitude."""
        if self.tilt_deg is not None:
            tilt = self.tilt_deg
        elif self.mount == "flat":
            tilt = 0.0
        else:
            tilt = optimal_tilt_deg(latitude)
        azimuth = (
            self.azimuth_deg if self.azimuth_deg is not None else surface_azimuth_deg(latitude)
        )
        return tilt, azimuth


@dataclass
class ComponentClosure:
    """How far an irradiance component triple was from closing, before scaling."""

    closure_error_pct: float
    n_daytime_steps: int
    method: str


def close_components(ghi, dni, dhi, cos_zenith, *, tolerance: float = 1e-9):
    """
    Scale DNI and DHI so that ``DNI * cos(z) + DHI == GHI`` at every step.

    Why this is necessary
    ---------------------
    Satellite irradiance products retrieve the three components more or less
    independently, so the triple does not close. Measured on NASA POWER for
    Delhi 2022: ``DNI * cos(z) + DHI`` came out **5.26% below GHI**.

    That matters because pvlib builds plane-of-array irradiance from DNI and
    DHI, never from GHI. So an unclosed triple makes POA at *zero* tilt come
    out 5.26% below GHI -- when a horizontal surface must equal GHI by
    definition. Left alone, every POA figure carries that negative bias, and it
    reads as model error rather than reference-data error.

    Method: preserve the beam/diffuse *ratio* and scale both to match GHI.
    GHI is the best-retrieved of the three, so it is the one to trust; the
    alternative of trusting DNI and back-solving DHI leans on the least
    reliable component. This is the standard GHI-preserving normalisation used
    in irradiance quality control.

    Returns ``(dni_closed, dhi_closed, ComponentClosure)``.
    """
    import numpy as np

    ghi_a = np.asarray(ghi, dtype=float)
    dni_a = np.asarray(dni, dtype=float)
    dhi_a = np.asarray(dhi, dtype=float)
    cosz = np.clip(np.asarray(cos_zenith, dtype=float), 0.0, None)

    implied = dni_a * cosz + dhi_a
    daytime = ghi_a > 5.0
    error_pct = 0.0
    if daytime.any() and ghi_a[daytime].sum() > 0:
        error_pct = (implied[daytime].sum() / ghi_a[daytime].sum() - 1.0) * 100.0

    scale = np.where(implied > tolerance, ghi_a / np.where(implied > tolerance, implied, 1.0), 1.0)
    return (
        dni_a * scale,
        dhi_a * scale,
        ComponentClosure(
            closure_error_pct=round(error_pct, 3),
            n_daytime_steps=int(daytime.sum()),
            method="ghi_preserving",
        ),
    )


@dataclass
class PoaResult:
    """Transposition output, aggregated over the evaluation period."""

    poa_global_kwh_m2: float
    poa_direct_kwh_m2: float
    poa_sky_diffuse_kwh_m2: float
    poa_ground_diffuse_kwh_m2: float
    ghi_kwh_m2: float
    tilt_deg: float
    azimuth_deg: float
    model: str
    n_steps: int
    closure: ComponentClosure | None = None

    @property
    def transposition_gain(self) -> float:
        """POA / GHI. The tilt gain, which was previously implicitly 1.0."""
        return self.poa_global_kwh_m2 / self.ghi_kwh_m2 if self.ghi_kwh_m2 else 1.0


def monthly_diurnal_index(year: int = 2021, timezone: str = "UTC"):
    """
    A 288-step index: the 15th of each month, hourly.

    Mid-month days stand in for their month's mean solar geometry, which is
    where the declination sits closest to the monthly average.
    """
    pd = _require_pvlib() and __import__("pandas")
    stamps = []
    for month in range(1, 13):
        stamps.extend(
            pd.date_range(
                f"{year}-{month:02d}-15 00:30",
                periods=24,
                freq="1h",
                tz=timezone,
            )
        )
    return pd.DatetimeIndex(stamps)


def transpose(
    *,
    latitude: float,
    longitude: float,
    ghi_per_step: list[float],
    times,
    config: ArrayConfig | None = None,
    dni_per_step: list[float] | None = None,
    dhi_per_step: list[float] | None = None,
    model: TranspositionModel = "haydavies",
) -> PoaResult:
    """
    Transpose horizontal irradiance onto the array plane.

    ``ghi_per_step`` is in W/m2, aligned with ``times``. Supply ``dni_per_step``
    and ``dhi_per_step`` when a measured beam/diffuse split is available --
    that is strongly preferred, because deriving it from GHI alone via Erbs
    adds its own error on top of ERA5's documented direct/diffuse bias.
    """
    pvlib = _require_pvlib()
    import numpy as np
    import pandas as pd

    config = config or ArrayConfig()
    tilt, azimuth = config.resolve(latitude)

    ghi = pd.Series(np.asarray(ghi_per_step, dtype=float), index=times)
    solar = pvlib.solarposition.get_solarposition(times, latitude, longitude)
    zenith = solar["apparent_zenith"]

    if dni_per_step is not None and dhi_per_step is not None:
        # Close the triple before transposing -- satellite components do not
        # close on their own. See close_components().
        cosz = np.cos(np.radians(zenith.values))
        dni_closed, dhi_closed, closure = close_components(
            ghi.values, dni_per_step, dhi_per_step, cosz
        )
        dni = pd.Series(dni_closed, index=times)
        dhi = pd.Series(dhi_closed, index=times)
    else:
        # Erbs is the fallback, not the default: it infers the split from the
        # clearness index alone, so its error compounds with ERA5's. It closes
        # by construction, so no scaling is needed.
        split = pvlib.irradiance.erbs(ghi, zenith, times)
        dni, dhi = split["dni"], split["dhi"]
        closure = ComponentClosure(0.0, int((ghi.values > 5).sum()), "erbs_derived")

    poa = pvlib.irradiance.get_total_irradiance(
        surface_tilt=tilt,
        surface_azimuth=azimuth,
        solar_zenith=zenith,
        solar_azimuth=solar["azimuth"],
        dni=dni,
        ghi=ghi,
        dhi=dhi,
        dni_extra=pvlib.irradiance.get_extra_radiation(times),
        albedo=config.albedo,
        model=model,
    ).fillna(0.0)

    # W/m2 averaged over N hourly steps -> kWh/m2 for a full year.
    hours_per_year = 8760.0
    scale = hours_per_year / (len(times) * 1000.0)

    return PoaResult(
        poa_global_kwh_m2=float(poa["poa_global"].sum()) * scale,
        poa_direct_kwh_m2=float(poa["poa_direct"].sum()) * scale,
        poa_sky_diffuse_kwh_m2=float(poa["poa_sky_diffuse"].sum()) * scale,
        poa_ground_diffuse_kwh_m2=float(poa["poa_ground_diffuse"].sum()) * scale,
        ghi_kwh_m2=float(ghi.sum()) * scale,
        tilt_deg=tilt,
        azimuth_deg=azimuth,
        model=model,
        n_steps=len(times),
        closure=closure,
    )


def cell_temperature(
    *,
    poa_global_w_m2: list[float],
    air_temperature_c: list[float] | float,
    wind_speed_m_s: list[float] | float = 1.0,
    racking: RackingType = "open_rack_glass_glass",
) -> list[float]:
    """
    Cell temperature via the SAPM model.

    This is what the original docstring described as ``T_cell = T_air +
    (NOCT-20)/800 * G`` and never implemented. SAPM is the better-validated
    form and is what pvlib ships.

    ``racking`` matters: close-mounted modules run 5-10 degC hotter than
    elevated open racking, which is a 2-4% difference in annual energy.
    """
    pvlib = _require_pvlib()
    import numpy as np

    params = pvlib.temperature.TEMPERATURE_MODEL_PARAMETERS["sapm"][racking]
    poa = np.asarray(poa_global_w_m2, dtype=float)
    air = np.broadcast_to(np.asarray(air_temperature_c, dtype=float), poa.shape)
    wind = np.broadcast_to(np.asarray(wind_speed_m_s, dtype=float), poa.shape)

    return list(
        pvlib.temperature.sapm_cell(
            poa_global=poa,
            temp_air=air,
            wind_speed=wind,
            a=params["a"],
            b=params["b"],
            deltaT=params["deltaT"],
        )
    )


def irradiance_weighted_cell_temperature(
    poa_global_w_m2: list[float],
    cell_temperature_c: list[float],
) -> float:
    """
    Mean cell temperature weighted by irradiance.

    A plain mean would be wrong here: the temperature loss only matters when
    the array is generating, and including night-time and low-sun hours drags
    the average towards the ambient minimum, understating the loss. Weighting
    by POA irradiance gives the temperature the energy actually experiences.
    """
    import numpy as np

    poa = np.asarray(poa_global_w_m2, dtype=float)
    temp = np.asarray(cell_temperature_c, dtype=float)
    total = poa.sum()
    if total <= 0:
        return float(temp.mean()) if temp.size else 0.0
    return float((poa * temp).sum() / total)


@dataclass
class YieldResult:
    """Specific yield and the full loss accounting behind it."""

    specific_yield_kwh_per_kwp_yr: float
    poa: PoaResult
    mean_cell_temperature_c: float
    breakdown: losses.LossBreakdown
    mount: MountType
    engine: str = "pvlib"

    def as_dict(self) -> dict:
        return {
            "engine": self.engine,
            "mount": self.mount,
            "specific_yield_kwh_per_kwp_yr": round(self.specific_yield_kwh_per_kwp_yr, 1),
            "poa_global_kwh_m2_yr": round(self.poa.poa_global_kwh_m2, 1),
            "ghi_kwh_m2_yr": round(self.poa.ghi_kwh_m2, 1),
            "transposition_gain": round(self.poa.transposition_gain, 4),
            "tilt_deg": round(self.poa.tilt_deg, 1),
            "azimuth_deg": round(self.poa.azimuth_deg, 1),
            "transposition_model": self.poa.model,
            "component_closure_error_pct": (
                self.poa.closure.closure_error_pct if self.poa.closure else None
            ),
            "mean_cell_temperature_c": round(self.mean_cell_temperature_c, 2),
            "losses": self.breakdown.as_dict(),
        }


def annual_specific_yield(
    *,
    latitude: float,
    longitude: float,
    ghi_per_step_w_m2: list[float],
    times,
    air_temperature_c: list[float] | float = 30.0,
    wind_speed_m_s: list[float] | float = 1.0,
    config: ArrayConfig | None = None,
    soiling_loss: float | None = None,
    shading_beam_loss: float | None = None,
    sky_view_diffuse_loss: float | None = None,
    dni_per_step: list[float] | None = None,
    dhi_per_step: list[float] | None = None,
    model: TranspositionModel = "haydavies",
) -> YieldResult:
    """
    Full chain: transposition, cell temperature, then the loss decomposition.

    Returns specific yield in kWh/kWp/yr, which is the quantity published plant
    studies report and therefore the only place this model can be compared
    against an independent measurement. Module efficiency and packing factor
    cancel out of it, so it tests the irradiance and loss chain independently
    of the capacity assumptions.
    """
    config = config or ArrayConfig()
    poa = transpose(
        latitude=latitude,
        longitude=longitude,
        ghi_per_step=ghi_per_step_w_m2,
        times=times,
        config=config,
        dni_per_step=dni_per_step,
        dhi_per_step=dhi_per_step,
        model=model,
    )

    # Re-run transposition per step to feed the thermal model, which needs
    # instantaneous irradiance rather than the period total.
    poa_steps = _poa_global_per_step(
        latitude=latitude,
        longitude=longitude,
        ghi_per_step=ghi_per_step_w_m2,
        times=times,
        config=config,
        dni_per_step=dni_per_step,
        dhi_per_step=dhi_per_step,
        model=model,
    )
    cell_temps = cell_temperature(
        poa_global_w_m2=poa_steps,
        air_temperature_c=air_temperature_c,
        wind_speed_m_s=wind_speed_m_s,
        racking=config.racking,
    )
    mean_cell = irradiance_weighted_cell_temperature(poa_steps, cell_temps)

    breakdown = losses.LossBreakdown().with_computed(
        temperature=losses.temperature_loss(mean_cell),
        soiling=soiling_loss,
        shading_beam=shading_beam_loss,
        sky_view_diffuse=sky_view_diffuse_loss,
        notes=(f"cell temperature from SAPM/{config.racking}",),
    )

    # Specific yield = POA x every retention term. Efficiency and packing
    # cancel between the energy and the nameplate, so neither appears.
    specific_yield = poa.poa_global_kwh_m2 * breakdown.total_retention

    return YieldResult(
        specific_yield_kwh_per_kwp_yr=specific_yield,
        poa=poa,
        mean_cell_temperature_c=mean_cell,
        breakdown=breakdown,
        mount=config.mount,
    )


def _poa_global_per_step(
    *,
    latitude: float,
    longitude: float,
    ghi_per_step: list[float],
    times,
    config: ArrayConfig,
    dni_per_step=None,
    dhi_per_step=None,
    model: TranspositionModel = "haydavies",
) -> list[float]:
    """Per-step POA global (W/m2), for the thermal model."""
    pvlib = _require_pvlib()
    import numpy as np
    import pandas as pd

    tilt, azimuth = config.resolve(latitude)
    ghi = pd.Series(np.asarray(ghi_per_step, dtype=float), index=times)
    solar = pvlib.solarposition.get_solarposition(times, latitude, longitude)
    zenith = solar["apparent_zenith"]

    if dni_per_step is not None and dhi_per_step is not None:
        cosz = np.cos(np.radians(zenith.values))
        dni_closed, dhi_closed, _ = close_components(ghi.values, dni_per_step, dhi_per_step, cosz)
        dni = pd.Series(dni_closed, index=times)
        dhi = pd.Series(dhi_closed, index=times)
    else:
        split = pvlib.irradiance.erbs(ghi, zenith, times)
        dni, dhi = split["dni"], split["dhi"]

    poa = pvlib.irradiance.get_total_irradiance(
        surface_tilt=tilt,
        surface_azimuth=azimuth,
        solar_zenith=zenith,
        solar_azimuth=solar["azimuth"],
        dni=dni,
        ghi=ghi,
        dhi=dhi,
        dni_extra=pvlib.irradiance.get_extra_radiation(times),
        albedo=config.albedo,
        model=model,
    ).fillna(0.0)
    return [float(v) for v in poa["poa_global"]]
