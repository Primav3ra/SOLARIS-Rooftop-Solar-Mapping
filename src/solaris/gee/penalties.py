"""
Penalty layers applied to the ERA5 baseline irradiance at 4m resolution.

Penalty layers:
  ShadowPenalty  -- 2.5D building-height shadow model, blocks direct beam (Open Buildings raster, 4m)
  SkyViewFactor  -- 2.5D building-height sky occlusion, blocks diffuse    (Open Buildings raster, 4m)
  UHIPenalty     -- Temperature derate from UHI effect  (MODIS LST, 1km)
  SoilingPenalty -- Dust/soiling derate from MODIS MAIAC AOD (1km, 550nm)

Combined net energy formula (see net_irradiance_image):
  E_net = GHI_period
          * [ diffuse_fraction * SVF + beam_fraction * (1 - shadow_frequency) ]  [Shadow + SkyViewFactor]
          * uhi_derate_factor           [UHIPenalty,     scalar ~0.97-1.00]
          * soiling_retention_factor    [SoilingPenalty, scalar ~0.94-1.00]
          * panel_efficiency * PR * packing_factor * roof_area_m2

Research basis:
  Shadow  : 2.5D geometric shadow casting. Known approximations documented in
            ShadowPenalty class docstring.
            Shadow frequency image is data-driven from Open Buildings 2.5D at 4m.
            Beam fraction is sampled from ERA5 HOURLY direct radiation band and
            applied so only the beam component is attenuated by shadows:
              net = GHI * (1 - shadow_frequency * beam_fraction)
            Diffuse (~30-45 % of GHI in urban India) reaches rooftops from the
            open sky hemisphere and is NOT blocked by surrounding buildings
            (rooftop Sky View Factor ~0.85-0.95; SVF correction deferred).

  UHI     : De Soto et al. (2006) / IEC 60891 temperature-coefficient derating.
              P_loss = gamma * delta_T   [gamma ~ -0.004 /degC for c-Si]
            UHI intensity delta_T estimated from MODIS daytime LST minus a 20 km
            focal-mean background (removes the regional temperature gradient).
            Observed UHI in Indian cities: 2-6 degC
            (Mohan et al. 2011; Bhati & Mohan 2018).
            Expected derate range: ~0.99 (2 degC) to ~0.976 (6 degC).

  Soiling : MODIS MAIAC AOD at 550 nm (MCD19A2 v061; Lyapustin et al. 2011).
            Direct measure of atmospheric aerosol column loading -- the actual
            driver of dry-deposition soiling on PV cover glass.
              soiling_loss = mean_AOD_550nm * SOILING_COEFFICIENT
            SOILING_COEFFICIENT = 0.08 /AOD_unit/year (Kimber et al. 2006;
            Sayyah et al. 2014; Mani & Pillai 2010).
            No output cap: loss follows directly from the measured AOD.
            Urban India AOD (0.5-1.2) is 3-5x rural; a genuine urban penalty.
"""

from __future__ import annotations

import math
from typing import Any

import ee

# ---------------------------------------------------------------------------
# Helpers shared across penalty classes
# ---------------------------------------------------------------------------


def _reduce_mean(image: ee.Image, band: str, aoi: ee.Geometry, scale: float) -> float | None:
    """Mean of a single band over AOI. Returns None if no valid pixels."""
    raw = image.reduceRegion(
        reducer=ee.Reducer.mean(),
        geometry=aoi,
        scale=scale,
        maxPixels=1e9,
        bestEffort=True,
    ).getInfo()
    val = (raw or {}).get(band)
    return float(val) if val is not None else None


def _make_solar_positions() -> list[tuple[float, float, float]]:
    """
    18 representative (alt_deg, az_deg, weight) positions: solstices x2, equinox x1,
    6 times of day each. weight = sin(alt_rad); equinox doubled for spring+autumn.
    Weights normalised to sum = 1. Solar geometry for Delhi 28.6 N.
    """
    seasons = [
        (
            "summer",
            [
                (8.0, 69.0),
                (28.0, 84.0),
                (58.0, 100.0),
                (84.0, 180.0),
                (58.0, 260.0),
                (28.0, 276.0),
                (8.0, 291.0),
            ],
        ),
        (
            "winter",
            [
                (3.0, 120.0),
                (14.0, 136.0),
                (28.0, 150.0),
                (38.0, 180.0),
                (28.0, 210.0),
                (14.0, 224.0),
                (3.0, 240.0),
            ],
        ),
        (
            "equinox",
            [
                (5.0, 90.0),
                (21.0, 98.0),
                (44.0, 112.0),
                (62.0, 180.0),
                (44.0, 248.0),
                (21.0, 262.0),
                (5.0, 270.0),
            ],
        ),
    ]
    positions: list[tuple[float, float, float]] = []
    for label, entries in seasons:
        repeat = 2 if label == "equinox" else 1
        for alt, az in entries:
            if alt < 2.0:
                continue
            positions.append((alt, az, math.sin(math.radians(alt)) * repeat))
    total = sum(w for _, _, w in positions)
    return [(a, z, w / total) for a, z, w in positions]


# ---------------------------------------------------------------------------
# Class 1 - ShadowPenalty
# ---------------------------------------------------------------------------


def _shadow_sample_distances(
    pixel_size_m: float = 4.0,
    near_field_m: float = 64.0,
    max_m: float = 400.0,
    growth: float = 1.5,
) -> tuple[float, ...]:
    """
    Distances (m) at which the shadow trace tests for occlusion: one pixel
    apart through the near field, then geometrically spaced out to ``max_m``.
    """
    distances: list[float] = []
    d = pixel_size_m
    while d <= near_field_m:
        distances.append(round(d, 3))
        d += pixel_size_m
    while d <= max_m:
        distances.append(round(d, 3))
        d *= growth
    if distances and distances[-1] < max_m:
        distances.append(max_m)
    return tuple(distances)


class ShadowPenalty:
    """
    2.5D shadow model over the Open Buildings height raster, insolation-weighted.

    Method
    ------
    A pixel is shadowed at sun position ``(alt, az)`` when some point along the
    direction *towards* the sun is tall enough to occlude it::

        shadowed  <=>  exists d :  H(p + d * u_sun) - H(p)  >  d * tan(alt)

    where ``u_sun = (sin az, cos az)`` and ``d`` runs over SAMPLE_DISTANCES_M.
    This is the standard DSM shadow trace (Ratti & Richens 1999): subtracting
    the pixel's own height makes it self-consistent, so a roof is never
    shadowed by a neighbour of equal or lower height.

    Sun positions are weighted by sin(altitude), so low winter and morning sun
    -- long shadows, little energy -- does not dominate an annual figure.

    What this replaced, and why
    ---------------------------
    The previous implementation projected a shadow-length image by a *fixed*
    offset, took ``focal_max`` over a **circular** kernel, and then tested
    caster height at a single fixed offset. Three problems compounded:

    1. The offsets were computed in pixels but passed to ``translate()``, whose
       units default to **metres**, so the intended 400 m reach was 100 m.
    2. The circular kernel made the search isotropic -- azimuth entered only
       through one rigid translate -- while the fixed-offset height test meant
       only a single pixel could ever be flagged. Measured on a 40 m tower with
       the sun at 45 deg: exactly one shadowed pixel, 25 px away, instead of a
       10 px shadow adjacent to the building. Shadow area did not respond to
       sun altitude at all.
    3. A pixel-denominated ``focal_max`` kernel resolves against the projection
       of the *request*, so the same expression covered a different physical
       area in /api/yield (reduced at 4 m) than in /api/tiles (a coarse tile
       scale).

    Every distance here is denominated in **metres**, which removes (1) and (3)
    by construction rather than by remembering to reproject.

    Remaining approximations
    ------------------------
    * Occlusion is sampled at discrete distances, so a caster that only
      occludes between two samples is missed. Spacing is one pixel out to 16 m
      and widens with distance, where the subtended angle changes slowly.
    * Shadows attenuate the direct beam only. Diffuse is handled by
      :class:`SkyViewFactor` and the beam/diffuse split in
      :func:`net_irradiance_image`.
    * Vegetation and non-building structures are absent from the height raster.
    * **Edge convention.** The height raster is clipped to the AOI, so a
      neighbour sampled beyond the boundary is masked. Such a neighbour is
      treated as ground level (``unmask(0)``) -- i.e. no obstruction -- because
      the alternative, propagating the mask, silently drops those roof pixels
      from the reduction entirely. Measured on a 64x64 test grid before this
      was handled: 75% of sky-view pixels came back masked, and the reported
      energy fell 58% purely from the excluded pixels. The cost is that
      shadowing and sky-view obstruction are underestimated in a band one
      search-radius wide inside the AOI edge, so buffer the AOI when a boundary
      building matters.
    """

    #: Maximum occlusion search distance (m).
    MAX_SHADOW_M: float = 400.0

    #: Tallest building the search bothers to look for (m). Covers essentially
    #: all Indian urban fabric, and lets each sun position prune distances
    #: beyond ``MAX_BUILDING_HEIGHT_M / tan(alt)`` -- nothing shorter than this
    #: could cast that far. With the sun high the list collapses to a handful of
    #: steps; only low sun needs the full reach. Without this the trace would
    #: emit ~40 translate operations per sun position regardless of geometry.
    MAX_BUILDING_HEIGHT_M: float = 150.0

    #: Out to here, occlusion is tested at every pixel. Most urban shadowing is
    #: near-field, and coarser spacing leaves visible gaps: with 4/8/12/16/24/32 m
    #: steps a 40 m tower produced 6 shadowed pixels of the 10 it should cast,
    #: missing those at 20, 28 and 36 m.
    NEAR_FIELD_M: float = 128.0

    #: Geometric growth factor beyond the near field, where the angle a caster
    #: subtends changes slowly with distance.
    FAR_FIELD_GROWTH: float = 1.2

    #: Populated just after the class body, from _shadow_sample_distances().
    SAMPLE_DISTANCES_M: tuple[float, ...] = ()

    #: Altitude floor (deg). Below this the shadow length diverges and the
    #: insolation weight is negligible anyway.
    MIN_ALTITUDE_DEG: float = 2.0

    # Default positions (Delhi 28.6 N); overridden by the solar_geometry module.
    _DELHI_POSITIONS: list[tuple[float, float, float]] = _make_solar_positions()

    @staticmethod
    def _mask_for_position(
        building_height: ee.Image,
        alt_deg: float,
        az_deg: float,
        pixel_size_m: float = 4.0,
    ) -> ee.Image:
        """
        Binary shadow mask (1 = shadow, 0 = sunlit) for one sun position.

        ``pixel_size_m`` is accepted for call-site compatibility but no longer
        needed: the trace is denominated in metres.
        """
        alt_rad = math.radians(max(alt_deg, ShadowPenalty.MIN_ALTITUDE_DEG))
        tan_alt = math.tan(alt_rad)

        # Unit vector towards the sun, in (east, north).
        az_rad = math.radians(az_deg)
        ux, uy = math.sin(az_rad), math.cos(az_rad)

        # No building under MAX_BUILDING_HEIGHT_M can occlude from further than
        # this, so testing beyond it is wasted work.
        reach_m = min(
            ShadowPenalty.MAX_SHADOW_M,
            ShadowPenalty.MAX_BUILDING_HEIGHT_M / tan_alt,
        )

        occluded: ee.Image | None = None
        for distance_m in ShadowPenalty.SAMPLE_DISTANCES_M:
            if distance_m > reach_m:
                break
            # translate() shifts content, so translated[p] == original[p - shift].
            # Sampling H(p + d*u) therefore needs a shift of -d*u.
            neighbour = building_height.translate(
                -ux * distance_m, -uy * distance_m, units="meters"
            ).unmask(0.0)
            rise = neighbour.subtract(building_height)
            hit = rise.gt(distance_m * tan_alt)
            occluded = hit if occluded is None else occluded.Or(hit)

        if occluded is None:  # pragma: no cover - SAMPLE_DISTANCES_M is non-empty
            occluded = ee.Image(0)

        # Pin the mask to the height raster's own, and treat "no data up-sun" as
        # "not shadowed".
        #
        # This is not cosmetic. Each translate() shifts the image footprint, so
        # Or-ing forty sample distances intersects forty slightly different
        # footprints and erodes the valid region; summing thirty-nine sun
        # positions then intersects all of those. Over a small area of interest
        # the survivors reach **zero**, and a fully-masked shadow layer makes
        # net irradiance masked, which reduces to a sum of 0 -- so the endpoint
        # reported 0 kWh with shadow accounting for 100% of the loss, and a
        # blank mean shadow fraction. Measured on a 0.22 km half-size AOI over
        # Delhi: 12,432 valid height pixels, 1,425 surviving one low-sun
        # position, and 0 surviving the sum.
        #
        # The convention matches what ``neighbour.unmask(0.0)`` above already
        # encodes: absent height data means no building, so nothing occludes.
        # Applying it to the accumulated result as well makes every position
        # share the height raster's mask, which is what lets the weighted sum
        # keep it.
        #
        # Worth noting why the offline suite could not catch this: the numpy
        # fake has no notion of a footprint distinct from a mask, so the
        # erosion does not happen there. It needed a live integration test,
        # which now exists.
        return occluded.unmask(0).updateMask(building_height.mask()).rename("in_shadow").toUint8()

    @staticmethod
    def frequency(
        building_height: ee.Image,
        solar_positions: list[tuple] | None = None,
        pixel_size_m: float = 4.0,
    ) -> ee.Image:
        """
        Insolation-weighted shadow frequency in [0, 1]. Band: shadow_frequency.

        0 = never shadowed; 1 = shadowed at every weighted sun position.
        """
        if solar_positions is None:
            pwt = ShadowPenalty._DELHI_POSITIONS
        elif not solar_positions:
            # Reachable for a high-latitude winter window, where the altitude
            # floor filters every position. Previously an IndexError from
            # indexing [0] before the truthiness check.
            raise ValueError(
                "solar_positions is empty: no sun position clears the "
                f"{ShadowPenalty.MIN_ALTITUDE_DEG} deg altitude floor for this "
                "window and latitude"
            )
        elif len(solar_positions[0]) >= 3:
            # (alt_deg, az_deg_from_north, weight, ...metadata)
            pwt = [(a, z, w) for (a, z, w, *_rest) in solar_positions]
        else:
            # Entries without weights, e.g. (alt, az): weight them uniformly.
            n = len(solar_positions)
            pwt = [(a, z, 1.0 / n) for (a, z, *_rest) in solar_positions]

        imgs = [
            ShadowPenalty._mask_for_position(building_height, a, z, pixel_size_m)
            .toFloat()
            .multiply(w)
            for a, z, w in pwt
        ]
        result = imgs[0]
        for img in imgs[1:]:
            result = result.add(img)
        return result.rename("shadow_frequency")


ShadowPenalty.SAMPLE_DISTANCES_M = _shadow_sample_distances(
    near_field_m=ShadowPenalty.NEAR_FIELD_M,
    max_m=ShadowPenalty.MAX_SHADOW_M,
    growth=ShadowPenalty.FAR_FIELD_GROWTH,
)


# ---------------------------------------------------------------------------
# Class 1b - SkyViewFactor  (diffuse counterpart of ShadowPenalty)
# ---------------------------------------------------------------------------


class SkyViewFactor:
    """
    The diffuse-light sibling of ShadowPenalty. Shadows block the direct beam; tall
    neighbours also hide part of the sky dome, so a roof doesn't collect the full
    diffuse either. SVF = the fraction of sky the roof can still see.

        SVF = 1 - avg over 8 compass directions of sin^2(horizon angle)

    where the horizon angle in a direction is the steepest obstruction we hit stepping
    outward. The sin^2 weighting is the usual flat-surface diffuse form (Oke). Net
    retention then reads: diffuse_fraction * SVF + beam_fraction * (1 - shadow_freq).

    Since we subtract self-height, only genuinely TALLER neighbours count, so roof SVF
    usually lands ~0.85-1.0. Street canyons would be far lower, but roof pixels aren't
    down in the canyon.
    """

    N_AZIMUTH: int = 8

    #: Horizon sampling distances, in **metres**. Roughly log-spaced: near
    #: buildings set the horizon, distant ones barely subtend an angle.
    #:
    #: These were previously pixel counts passed to ``translate()``, whose units
    #: default to metres -- so the rise was sampled 4x nearer than the distance
    #: it was divided by, understating every horizon angle and biasing SVF
    #: towards 1, i.e. towards no diffuse penalty at all. Measured for a 10 m
    #: wall 4 m away: 0.964888 against an analytic 0.892241, exactly the value
    #: obtained by substituting 16 m for 4 m.
    DIST_M: tuple[float, ...] = (4.0, 8.0, 16.0, 32.0, 64.0)

    #: Retained for backwards compatibility with callers that report it.
    DIST_PX: tuple[int, ...] = (1, 2, 4, 8, 16)

    @staticmethod
    def image(
        building_height: ee.Image,
        pixel_size_m: float = 4.0,
        n_azimuth: int | None = None,
        dist_m: tuple[float, ...] | None = None,
    ) -> ee.Image:
        """
        Per-pixel Sky View Factor in [0, 1]. Band: sky_view_factor.

        ``pixel_size_m`` is accepted for call-site compatibility but unused --
        every distance is in metres, which also makes the result independent of
        the scale the image is later requested at.
        """
        n_az = int(n_azimuth or SkyViewFactor.N_AZIMUTH)
        dists = dist_m or SkyViewFactor.DIST_M

        sin2_terms: list[ee.Image] = []
        for k in range(n_az):
            az = 2.0 * math.pi * k / n_az
            ux, uy = math.sin(az), math.cos(az)

            # Walk outward along this azimuth, keeping the steepest obstruction.
            angle_imgs: list[ee.Image] = []
            for distance_m in dists:
                neighbour = building_height.translate(
                    -ux * distance_m, -uy * distance_m, units="meters"
                ).unmask(0.0)
                # Only taller neighbours occlude, so clamp the rise at zero.
                rise = neighbour.subtract(building_height).max(0.0)
                angle_imgs.append(rise.divide(distance_m).atan())
            horizon = angle_imgs[0]
            for a in angle_imgs[1:]:
                horizon = horizon.max(a)

            sin2_terms.append(horizon.sin().pow(2))

        acc = sin2_terms[0]
        for t in sin2_terms[1:]:
            acc = acc.add(t)
        mean_sin2 = acc.divide(n_az)

        return ee.Image(1.0).subtract(mean_sin2).clamp(0.0, 1.0).rename("sky_view_factor")


# ---------------------------------------------------------------------------
# Class 2 - UHIPenalty
# ---------------------------------------------------------------------------


class UHIPenalty:
    """
    Efficiency hit from the city running hotter than its surroundings (urban heat island).

    Panels lose output above 25 degC (STC): dP/P = gamma * (T_cell - 25), with
    gamma ~ -0.004 /degC for crystalline silicon (IEC 60891; De Soto 2006). Cell temp is
    the usual NOCT form, T_cell = T_ambient + (NOCT-20)/800 * G. We only charge for the
    *urban excess* here -- delta_T = urban - rural background -- so uhi_derate =
    1 + gamma*delta_T. For an Indian city that's small: 2-6 degC of UHI (Mohan et al. 2011)
    works out to roughly 0.8-2.4% off.

    Getting delta_T from MODIS: take the annual median daytime LST (MOD11A2, 1 km; raw*0.02
    = Kelvin, then -273.15), subtract a 30 km focal-mean "background". The 30 km is
    deliberate -- for a sprawling city like Delhi a tighter window sits entirely inside the
    heat island and the anomaly collapses to ~0. A full year of data keeps the composite
    stable across seasons.
    """

    MODIS_COLLECTION = "MODIS/061/MOD11A2"
    LST_DAY_BAND = "LST_Day_1km"
    LST_SCALE = 0.02  # raw integer * 0.02 = Kelvin (MODIS scale factor)
    K_TO_C_OFFSET = 273.15
    #: Rural-background window radius, in **metres**. Deliberately large: for a
    #: sprawling city a tighter window sits entirely inside the heat island and
    #: the anomaly collapses towards zero.
    #:
    #: This was previously 30 *pixels*, which only meant 30 km when the request
    #: happened to be at MODIS' 1 km scale. A pixel-denominated kernel resolves
    #: against the projection of the request, so the background window -- and
    #: therefore the reported heat-island intensity -- moved with the reduce
    #: scale. Measured on a synthetic hot spot: delta_T read 2.78 C at scale=4
    #: against 4.94 C at scale=100, a 78% swing from the request scale alone.
    BACKGROUND_KERNEL_M = 30_000.0

    #: Retained so callers that report the window size keep working.
    BACKGROUND_KERNEL_PX = 30

    DEFAULT_TEMP_COEFF = -0.004  # /degC, crystalline silicon (IEC 60891)

    #: Surface-to-air heat-island transfer coefficient (dimensionless).
    #:
    #: MODIS measures *land surface* temperature, but a PV module responds to
    #: *air* temperature. Daytime surface UHI in Indian cities typically runs
    #: 2-3x the canopy-layer air UHI, so charging the full LST anomaly to the
    #: cell-temperature derate overstates the penalty by roughly that factor.
    #:
    #: 0.3 is the mid-point of the commonly reported range. It is an explicit,
    #: uncertain parameter rather than an implicit assumption -- previously the
    #: LST anomaly was used directly, which silently conflated the two and,
    #: combined with the temperature loss already inside PERFORMANCE_RATIO,
    #: produced two errors of opposite sign that neither cancelled nor was
    #: measured. See docs/limitations.md (P4).
    SURFACE_TO_AIR_RATIO = 0.3

    @classmethod
    def _lst_celsius(cls, aoi: ee.Geometry, year: int) -> ee.Image:
        """Annual median daytime LST in degC for the given calendar year."""
        return (
            ee.ImageCollection(cls.MODIS_COLLECTION)
            .filterBounds(aoi)
            .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
            .select(cls.LST_DAY_BAND)
            .median()
            .multiply(cls.LST_SCALE)
            .subtract(cls.K_TO_C_OFFSET)
            .rename("LST_celsius")
        )

    @classmethod
    def stats(
        cls,
        aoi: ee.Geometry,
        start_date: str,
        temp_coeff: float = DEFAULT_TEMP_COEFF,
        scale_m: float = 1000.0,
        surface_to_air_ratio: float = SURFACE_TO_AIR_RATIO,
    ) -> dict[str, Any]:
        """
        UHI intensity + derate for the AOI. start_date just supplies the year for the
        annual LST composite; scale_m should sit near MODIS' native 1 km. Returns a dict
        with delta_t_uhi_celsius, the mean/background LSTs, the uhi_derate_factor
        (1 + gamma*delta_T), the gamma used, and a source tag.
        """
        year = int(start_date[:4])
        lst = cls._lst_celsius(aoi, year)

        # Rural/background reference. Denominated in metres so the window is
        # 30 km regardless of the scale the image is later requested at.
        background = lst.focal_mean(
            radius=cls.BACKGROUND_KERNEL_M,
            kernelType="circle",
            units="meters",
        )
        uhi_anomaly = lst.subtract(background).rename("uhi_anomaly")

        # Single reduceRegion call for both bands
        combined = lst.addBands(uhi_anomaly)
        raw = (
            combined.reduceRegion(
                reducer=ee.Reducer.mean(),
                geometry=aoi,
                scale=scale_m,
                maxPixels=1e9,
                bestEffort=True,
            ).getInfo()
            or {}
        )

        urban_lst = raw.get("LST_celsius")
        delta_t = raw.get("uhi_anomaly")
        source = "reduceRegion"

        if delta_t is None:
            delta_t = 0.0
            source = "fallback_zero"
        if urban_lst is None:
            urban_lst = 35.0  # representative Indian urban daytime T (degC)

        delta_t = float(delta_t)
        urban_lst = float(urban_lst)
        background_lst = urban_lst - delta_t

        # The derate responds to *air* temperature, so scale the surface
        # anomaly down by SURFACE_TO_AIR_RATIO before applying gamma.
        delta_t_air = delta_t * surface_to_air_ratio
        derate = 1.0 + temp_coeff * delta_t_air

        return {
            "delta_t_uhi_celsius": round(delta_t, 3),
            "delta_t_air_celsius": round(delta_t_air, 3),
            "surface_to_air_ratio": surface_to_air_ratio,
            "mean_lst_day_celsius": round(urban_lst, 2),
            "background_lst_celsius": round(background_lst, 2),
            "uhi_derate_factor": round(derate, 5),
            "temp_coeff_per_c": temp_coeff,
            "source": source,
            "modis_collection": cls.MODIS_COLLECTION,
            "background_kernel_km": cls.BACKGROUND_KERNEL_M / 1000.0,
            "accounting_year": year,
            "scale_m": scale_m,
        }


# ---------------------------------------------------------------------------
# Class 3 - SoilingPenalty
# ---------------------------------------------------------------------------


class SoilingPenalty:
    """
    Dust/soiling derate driven by MODIS MAIAC aerosol optical depth (AOD, 550 nm).

    The idea: airborne aerosols (dust, soot, sulphate/nitrate) settle on the cover glass
    and cut its transmittance, and how fast they deposit tracks the aerosol column above.
    So we take annual loss = mean AOD * 0.08 per AOD unit (Kimber et al. 2006; Sayyah
    et al. 2014; Mani & Pillai 2010 for South Asian dust). No clamp -- the loss just
    follows the AOD, e.g. ~4% at a typical urban 0.5, ~8% at a Delhi-winter 1.0.

    Why AOD and not a bare-soil index like DBSI: DBSI measures the dust *source*, two steps
    removed from what lands on a panel, and turning it into a loss % needs an arbitrary
    fudge range. AOD is the actual atmospheric loading that drives dry deposition, and the
    0.08 coefficient maps to a real physical process. It's also genuinely urban -- city AOD
    runs 3-5x its rural surroundings, which is the whole point of the project.

    Source: MODIS MAIAC MCD19A2 v061 (Lyapustin et al. 2011), 1 km daily -- MAIAC is the
    one that actually works over bright urban surfaces where dark-target fails. We take the
    annual mean of valid retrievals (GEE's QA masking drops the cloudy days).
    """

    MAIAC_COLLECTION = "MODIS/061/MCD19A2_GRANULES"
    QA_BAND = "AOD_QA"

    #: AOD_QA bits 0-2 are the cloud mask; 001 means "clear". MCD19A2_GRANULES
    #: ships **unmasked** retrievals, so without this the annual mean folds in
    #: cloudy and cloud-shadowed pixels. The class docstring previously claimed
    #: "GEE's QA masking drops the cloudy days", which was simply not true --
    #: nothing applied a mask.
    QA_CLOUD_MASK_BITS = 0b111
    QA_CLOUD_CLEAR = 0b001
    AOD_BAND = "Optical_Depth_055"  # 550 nm standard reference; scale factor 0.001
    AOD_SCALE = 0.001
    SOILING_COEFFICIENT = 0.08  # fractional loss per unit mean AOD per year
    # (Kimber et al. 2006; Sayyah et al. 2014)

    #: Floor on the retention factor.
    #:
    #: ``loss = mean_AOD * 0.08`` was applied uncapped, so any AOD above 12.5
    #: produced a *negative* retention factor and therefore negative generated
    #: energy. Measured soiling in Delhi is 0.24-0.47 %/day with 7-30 day
    #: cleaning, i.e. a few per cent to ~10% annually, so a retention below 0.5
    #: is outside anything the literature supports and indicates bad input
    #: rather than a real loss. Binding the floor is logged in the returned
    #: dict so it is never silent.
    MIN_RETENTION = 0.5

    @classmethod
    def aod_image(cls, aoi: ee.Geometry, year: int) -> ee.Image:
        """
        Annual mean AOD at 550 nm, QA-masked, for the given calendar year.
        Band: AOD_550nm.

        Only retrievals whose AOD_QA cloud mask reads "clear" contribute. Every
        other pixel is masked, so it is excluded from the mean rather than
        averaged in -- which is what ``.mean()`` on the raw band was doing.
        """

        def _keep_clear(image: ee.Image) -> ee.Image:
            qa = image.select(cls.QA_BAND)
            clear = qa.bitwiseAnd(cls.QA_CLOUD_MASK_BITS).eq(cls.QA_CLOUD_CLEAR)
            return image.select(cls.AOD_BAND).updateMask(clear)

        return (
            ee.ImageCollection(cls.MAIAC_COLLECTION)
            .filterBounds(aoi)
            .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
            .map(_keep_clear)
            .mean()
            .multiply(cls.AOD_SCALE)
            .rename("AOD_550nm")
        )

    @classmethod
    def stats_windowed(
        cls,
        aoi: ee.Geometry,
        start_date: str,
        end_date_exclusive: str,
        *,
        cleaning_interval_days: int | None = None,
        scale_m: float = 1000.0,
        sample_rain: bool = True,
        point: ee.Geometry | None = None,
    ) -> dict[str, Any]:
        """
        Soiling retention for the actual window, with rain washing.

        Supersedes :meth:`stats`, which returns the same annual number for a
        one-day window as for a year. This one derives a daily deposition rate
        from AOD, samples ERA5-Land daily rainfall over the window, and
        time-averages the resulting sawtooth -- so a monsoon week and a dry
        Rajasthan quarter come out differently, which they are.

        Costs one extra Earth Engine round-trip for the rainfall series. That
        is deliberate: the alternative is a rain-day count, and the mean-spell
        approximation it forces understates the loss by up to 12.6x. See
        :mod:`solaris.physics.soiling`.

        Falls back to the rain-day approximation, and then to the legacy
        coefficient, always reporting which path answered.
        """
        from datetime import date

        from solaris.gee.precipitation import sample_era5_daily_precip
        from solaris.physics import soiling as soiling_model

        year = int(start_date[:4])
        aod_img = cls.aod_image(aoi, year)
        mean_aod = _reduce_mean(aod_img, "AOD_550nm", aoi, scale_m)
        aod_source = "reduceRegion"
        if mean_aod is None:
            mean_aod = 0.50
            aod_source = "fallback_urban_midpoint"
        mean_aod = float(mean_aod)

        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date_exclusive)
        window_days = max(1, (end - start).days)

        daily_rain: list[float] | None = None
        rain_source = "not_sampled"
        if sample_rain:
            sampled = sample_era5_daily_precip(
                point if point is not None else aoi.centroid(1),
                start_date,
                end_date_exclusive,
            )
            rain_source = sampled["source"]
            if sampled["precip_mm"]:
                daily_rain = sampled["precip_mm"]

        result = soiling_model.window_soiling(
            mean_aod,
            window_days=window_days,
            daily_rain_mm=daily_rain,
            rain_days=None,
            cleaning_interval_days=cleaning_interval_days,
        )

        out = result.as_dict()
        out.update(
            {
                "mean_aod_550nm": round(mean_aod, 4),
                "source": aod_source,
                "rainfall_source": rain_source,
                "maiac_collection": cls.MAIAC_COLLECTION,
                "accounting_year": year,
                "scale_m": scale_m,
                "model": "kimber_aod_calibrated",
                "calibration": soiling_model.calibration_note(),
                "legacy_retention_for_comparison": round(
                    soiling_model.legacy_retention(mean_aod), 5
                ),
            }
        )
        return out

    @classmethod
    def stats(
        cls,
        aoi: ee.Geometry,
        start_date: str,
        soiling_coefficient: float = SOILING_COEFFICIENT,
        scale_m: float = 1000.0,
    ) -> dict[str, Any]:
        """
        Soiling retention from MAIAC AOD. Year comes from start_date; scale_m near MAIAC's
        1 km. Returns the mean AOD, the loss fraction (mean_AOD * coefficient, uncapped),
        the retention factor (1 - loss), the coefficient, and a source tag.

        **Superseded by :meth:`stats_windowed`.** Kept as the control arm for
        the ablation table and so the previously published figures remain
        reproducible. Its defining limitation is structural rather than a
        matter of coefficient: it has no rainfall term and no window
        dependence, so it returns the same annual loss for a monsoon day as for
        a dry pre-monsoon quarter.
        """
        year = int(start_date[:4])
        aod_img = cls.aod_image(aoi, year)

        mean_aod = _reduce_mean(aod_img, "AOD_550nm", aoi, scale_m)
        source = "reduceRegion"

        if mean_aod is None:
            # No valid MAIAC retrievals for this AOI/year (very unlikely for India)
            mean_aod = 0.50  # conservative urban India annual mean
            source = "fallback_urban_midpoint"

        mean_aod = float(mean_aod)
        loss = mean_aod * soiling_coefficient
        retention = 1.0 - loss

        clamped = retention < cls.MIN_RETENTION
        if clamped:
            retention = cls.MIN_RETENTION
            loss = 1.0 - retention

        return {
            "mean_aod_550nm": round(mean_aod, 4),
            "soiling_loss_fraction": round(loss, 4),
            "soiling_retention_factor": round(retention, 5),
            "soiling_retention_clamped": clamped,
            "soiling_coefficient": soiling_coefficient,
            "source": source,
            "maiac_collection": cls.MAIAC_COLLECTION,
            "accounting_year": year,
            "scale_m": scale_m,
        }


# ---------------------------------------------------------------------------
# Combined net-irradiance builder (used by /api/yield and /api/tiles)
# ---------------------------------------------------------------------------


def net_irradiance_image(
    baseline_kwh_m2_period: float,
    shadow_frequency: ee.Image,
    beam_fraction: float = 1.0,
    uhi_derate: float = 1.0,
    soiling_retention: float = 1.0,
    sky_view_factor: Any | None = None,
) -> ee.Image:
    """
    Stitches the penalty layers into one per-pixel net-irradiance image:

      net = GHI * [ diffuse_frac * SVF + beam_frac * (1 - shadow_freq) ] * uhi * soiling

    The bracket is the whole point: split GHI into diffuse + beam, then knock the beam
    down by shadows and the diffuse down by the sky-view factor. Pass sky_view_factor
    as an image or a scalar; leave it None for an open sky (SVF=1), which reduces to the
    old beam-only form GHI * (1 - shadow_freq * beam_frac) -- handy for the shadow-only
    stage. uhi/soiling are plain scalars from their stats() calls. Band out:
    'net_irradiance_kwh_m2_period'.
    """
    diffuse_fraction = 1.0 - beam_fraction

    if sky_view_factor is None:
        svf = ee.Image(1.0)
    elif isinstance(sky_view_factor, int | float):
        svf = ee.Image(float(sky_view_factor))
    else:
        svf = sky_view_factor

    beam_retention = ee.Image(1.0).subtract(shadow_frequency).multiply(beam_fraction)
    diffuse_retention = svf.multiply(diffuse_fraction)
    corrected_retention = diffuse_retention.add(beam_retention)

    effective_baseline = baseline_kwh_m2_period * uhi_derate * soiling_retention
    # Floor at zero: negative net irradiance -- and therefore negative generated
    # energy -- is physically impossible whatever the derates say. SoilingPenalty
    # already clamps its own retention, but this function takes plain scalars
    # from any caller, so the invariant is enforced here too.
    return (
        corrected_retention.multiply(effective_baseline)
        .max(0.0)
        .rename("net_irradiance_kwh_m2_period")
    )
