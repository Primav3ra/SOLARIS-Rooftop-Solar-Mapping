"""
Decomposition of the lumped performance ratio into named, separately-reported
loss terms.

The problem this solves
-----------------------
The model applied ``performance_ratio = 0.80`` **and** a separate urban
heat-island derate. But a PR of 0.80 for an Indian climate already contains a
full temperature loss -- typically 7-11 percentage points of the 20 point
deficit. So the urban *excess* temperature was charged twice.

Compounding it, that excess was itself inflated: the UHI layer used MODIS land
*surface* temperature as a stand-in for *air* temperature, and daytime surface
UHI in Indian cities runs 2-3x the canopy-layer air UHI. (Phase 3 introduced an
explicit ``SURFACE_TO_AIR_RATIO`` for that, but it does not fix the double
count.)

Two errors of opposite sign, neither measured: the headline number could look
reasonable while both components were wrong. That is the worst possible
starting point for a validation exercise, because it cannot be diagnosed from
the output.

The fix
-------
Name every loss, compute the ones that are computable, and assume only what
must be assumed. Temperature moves out of the scalar entirely and is computed
per-timestep from irradiance and air temperature by :mod:`solaris.physics.pv`;
soiling moves out and is computed from aerosol data. What remains is a
balance-of-system scalar covering the genuinely site-independent losses.

Where the numbers come from
---------------------------
The default percentages follow NREL's PVWatts v5 documentation, which is also
what ``pvlib.pvsystem.pvwatts_losses`` implements, so they are traceable rather
than invented. Shading and soiling are set to zero here because this model
computes them from data -- leaving PVWatts' defaults in would double-count
them, which is the same mistake in a new place.

What the decomposition actually produces
---------------------------------------
Balance of system works out to **0.868**. With a computed ~7% temperature loss
and ~5.6% soiling for Delhi that gives a performance ratio near **0.76**.

Observed Indian rooftop plants run 0.70-0.93, with 0.78-0.83 quoted as typical
for a well-built year-one system. So this stack sits a little below typical and
inside the measured range. That is reported rather than tuned away, and the
likely reasons are worth stating: published "typical" figures come from
commissioned, well-maintained plants, whereas this model estimates potential
over generic urban fabric. The two most uncertain terms are the 3% IAM and
spectral allowance and the soiling coefficient.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Loss terms that remain assumptions
# ---------------------------------------------------------------------------

#: Incidence-angle and spectral mismatch.
#:
#: pvlib's ``get_total_irradiance`` returns plane-of-array irradiance *without*
#: an incidence-angle modifier, unlike PVWatts, which folds one into its own
#: transposition. So this term has to be added explicitly here rather than
#: taken from the PVWatts list.
IAM_AND_SPECTRAL_LOSS = 0.03

# The remaining assumed losses mirror NREL's PVWatts v5 list exactly -- the
# same defaults `pvlib.pvsystem.pvwatts_losses` implements -- so every number
# is traceable rather than invented.
#
# Two are deliberately zeroed because this model computes them from data;
# leaving PVWatts' defaults in would double-count them, which is the same
# mistake this module exists to remove:
#   soiling  (PVWatts default 2%) -> computed from MAIAC aerosol data
#   shading  (PVWatts default 3%) -> computed from the 2.5D shadow model

MISMATCH_LOSS = 0.02
DC_WIRING_LOSS = 0.02
CONNECTIONS_LOSS = 0.005
LID_LOSS = 0.015
NAMEPLATE_RATING_LOSS = 0.01
SNOW_LOSS = 0.0  # not a factor at Indian urban latitudes
AGE_LOSS = 0.0  # year one

#: Plant downtime.
#:
#: **Defaults to zero, unlike PVWatts' 3%.** This model estimates *technical
#: potential* -- how much a rooftop could generate -- not the delivered energy
#: of one operated plant, and charging an operations loss to a resource
#: assessment would conflate the two.
#:
#: Set it to 0.03 when comparing against a metered plant's performance ratio,
#: since a measured PR necessarily includes that plant's downtime. The eval
#: harness records which value it used.
AVAILABILITY_LOSS = 0.0

#: Inverter Euro-weighted efficiency. pvlib's pvwatts inverter default.
INVERTER_EFFICIENCY = 0.96

#: Module power temperature coefficient, per degC. Crystalline silicon,
#: IEC 60891 / De Soto et al. (2006).
TEMPERATURE_COEFFICIENT = -0.004

#: Standard test condition cell temperature (degC).
STC_CELL_TEMPERATURE_C = 25.0


@dataclass(frozen=True)
class LossBreakdown:
    """
    Every loss in the chain, named and separated.

    ``temperature`` and ``soiling`` are **computed**, not assumed, so they are
    optional here: a caller that has not run the temperature model leaves them
    None and gets only the balance-of-system scalar. That distinction is the
    whole point -- it makes "what did we assume?" answerable from the object.
    """

    #: Losses that stay assumptions, as fractions in [0, 1).
    iam_and_spectral: float = IAM_AND_SPECTRAL_LOSS
    mismatch: float = MISMATCH_LOSS
    dc_wiring: float = DC_WIRING_LOSS
    connections: float = CONNECTIONS_LOSS
    lid: float = LID_LOSS
    nameplate_rating: float = NAMEPLATE_RATING_LOSS
    snow: float = SNOW_LOSS
    age: float = AGE_LOSS
    unavailability: float = AVAILABILITY_LOSS
    inverter: float = 1.0 - INVERTER_EFFICIENCY

    #: Computed per site and period. None until the models have run.
    temperature: float | None = None
    soiling: float | None = None
    shading_beam: float | None = None
    sky_view_diffuse: float | None = None

    #: Free-text provenance for anything unusual.
    notes: tuple[str, ...] = field(default_factory=tuple)

    # -- derived ----------------------------------------------------------

    @property
    def balance_of_system(self) -> float:
        """
        Retention from the assumed, site-independent losses only.

        Deliberately excludes temperature, soiling and the geometric layers,
        all of which this model computes. Multiplying them in here is exactly
        the double count being removed.
        """
        retention = 1.0
        for loss in (
            self.iam_and_spectral,
            self.mismatch,
            self.dc_wiring,
            self.connections,
            self.lid,
            self.nameplate_rating,
            self.snow,
            self.age,
            self.unavailability,
            self.inverter,
        ):
            retention *= 1.0 - loss
        return retention

    @property
    def computed_retention(self) -> float:
        """Retention from the computed losses, ignoring any still unset."""
        retention = 1.0
        for loss in (
            self.temperature,
            self.soiling,
            self.shading_beam,
            self.sky_view_diffuse,
        ):
            if loss is not None:
                retention *= 1.0 - loss
        return retention

    @property
    def performance_ratio(self) -> float:
        """
        Effective performance ratio: everything except the geometric layers.

        This is the quantity comparable to a measured plant PR, which is why
        the shading and sky-view terms are excluded -- a metered plant's PR is
        computed against the irradiance that actually reached its plane, so its
        shading losses are already in the numerator.
        """
        retention = self.balance_of_system
        for loss in (self.temperature, self.soiling):
            if loss is not None:
                retention *= 1.0 - loss
        return retention

    @property
    def total_retention(self) -> float:
        """Every loss combined, including the geometric layers."""
        return self.balance_of_system * self.computed_retention

    def as_dict(self) -> dict[str, float | None | list[str]]:
        """Flat, JSON-serialisable form for the API response and eval records."""
        return {
            "assumed": {
                "iam_and_spectral": self.iam_and_spectral,
                "mismatch": self.mismatch,
                "dc_wiring": self.dc_wiring,
                "connections": self.connections,
                "lid": self.lid,
                "nameplate_rating": self.nameplate_rating,
                "snow": self.snow,
                "age": self.age,
                "unavailability": self.unavailability,
                "inverter": round(self.inverter, 4),
            },
            "computed": {
                "temperature": self.temperature,
                "soiling": self.soiling,
                "shading_beam": self.shading_beam,
                "sky_view_diffuse": self.sky_view_diffuse,
            },
            "balance_of_system_retention": round(self.balance_of_system, 5),
            "performance_ratio": round(self.performance_ratio, 5),
            "total_retention": round(self.total_retention, 5),
            "notes": list(self.notes),
        }

    def with_computed(
        self,
        *,
        temperature: float | None = None,
        soiling: float | None = None,
        shading_beam: float | None = None,
        sky_view_diffuse: float | None = None,
        notes: tuple[str, ...] = (),
    ) -> LossBreakdown:
        """Return a copy with the computed terms filled in."""
        return LossBreakdown(
            iam_and_spectral=self.iam_and_spectral,
            mismatch=self.mismatch,
            dc_wiring=self.dc_wiring,
            connections=self.connections,
            lid=self.lid,
            nameplate_rating=self.nameplate_rating,
            snow=self.snow,
            age=self.age,
            unavailability=self.unavailability,
            inverter=self.inverter,
            temperature=self.temperature if temperature is None else temperature,
            soiling=self.soiling if soiling is None else soiling,
            shading_beam=self.shading_beam if shading_beam is None else shading_beam,
            sky_view_diffuse=(
                self.sky_view_diffuse if sky_view_diffuse is None else sky_view_diffuse
            ),
            notes=self.notes + notes,
        )


def temperature_loss(mean_cell_temperature_c: float) -> float:
    """
    Fractional power loss from cell temperature above STC.

    ``loss = -gamma * (T_cell - 25)``, with gamma negative, so the result is
    positive for a cell hotter than STC. Clamped at zero: a cell *below* STC
    gains power, which this chain does not model, and reporting a negative
    "loss" would flip the sign of the derate.
    """
    delta = mean_cell_temperature_c - STC_CELL_TEMPERATURE_C
    return max(0.0, -TEMPERATURE_COEFFICIENT * delta)


def legacy_lumped_scalar(
    panel_efficiency: float,
    performance_ratio: float,
    packing_factor: float,
) -> float:
    """
    The single scalar the pre-pvlib chain used: ``0.18 x 0.80 x 0.70 = 0.1008``.

    Retained verbatim as the control arm, so the two engines can be compared
    line by line rather than by recollection.
    """
    return panel_efficiency * performance_ratio * packing_factor
