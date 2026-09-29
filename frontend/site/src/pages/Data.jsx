import { useState } from "react";
import { Link } from "react-router-dom";

const SOURCES = [
  {
    name: "Open Buildings 2.5D Temporal v1",
    role: "geometry",
    metres: 4,
    platform: "Google Research (Sentinel-2 derived)",
    resolution: "~4 m",
    revisit: "annual vintages",
    coverage: "2016–2023",
    licence: "CC BY 4.0",
    use: "Roof footprint and, crucially, building height. Height is what makes a shadow trace possible at all.",
    note: "The only open building-height product covering India. Its confidence band is a model score, not a calibrated probability, so the 0.5 presence threshold is a prior rather than a likelihood.",
  },
  {
    name: "Open Buildings v3",
    role: "geometry",
    metres: null,
    platform: "Google Research",
    resolution: "vector polygons",
    revisit: "static release",
    coverage: "current",
    licence: "CC BY 4.0",
    use: "Selecting which building you clicked, and its footprint area.",
  },
  {
    name: "ERA5-Land daily aggregates",
    role: "sunlight",
    metres: 9000,
    platform: "ECMWF / Copernicus",
    resolution: "~9 km",
    revisit: "daily, ~3 month lag",
    coverage: "1950–present",
    licence: "Copernicus (free, attribution)",
    use: "Global horizontal irradiance summed over the selected window, and daily rainfall, which sets when a roof gets washed.",
    note: "The resolution mismatch at the heart of this project: 9 km irradiance attributed to 4 m roofs, so every roof in one cell receives identical sunlight. Daily sums give the same window totals as the hourly product at about a twenty-third of the compute.",
  },
  {
    name: "ERA5 hourly",
    role: "sunlight",
    metres: 28000,
    platform: "ECMWF / Copernicus",
    resolution: "~28 km",
    revisit: "hourly",
    coverage: "1940–present",
    licence: "Copernicus (free, attribution)",
    use: "The direct component, giving the beam/diffuse split. ERA5-Land does not publish one.",
    note: "Uses a monthly aerosol climatology, and is documented to overestimate direct radiation with the error growing in aerosol load — exactly India’s regime. Measured: our reference set averages 0.540 beam fraction against the 0.60 this model used to assume.",
  },
  {
    name: "MODIS MOD11A2",
    role: "heat",
    metres: 1000,
    platform: "NASA Terra",
    resolution: "1 km",
    revisit: "8-day composite",
    coverage: "2000–present",
    licence: "Public domain (NASA)",
    use: "Land surface temperature, for the urban heat island anomaly.",
    note: "Surface temperature, not air temperature. Surface heat island runs 2–3× the air heat island in Indian cities, so an explicit transfer coefficient converts one to the other.",
  },
  {
    name: "MODIS MCD19A2 (MAIAC)",
    role: "dust",
    metres: 1000,
    platform: "NASA Terra + Aqua",
    resolution: "1 km",
    revisit: "daily",
    coverage: "2000–present",
    licence: "Public domain (NASA)",
    use: "Aerosol optical depth, which drives the dust deposition rate.",
    note: "MAIAC specifically, because dark-target retrieval fails over bright urban surfaces. The QA cloud mask is applied — the previous code claimed it was and was not.",
  },
  {
    name: "SRTM GL1",
    role: "terrain",
    metres: 30,
    platform: "NASA / USGS",
    resolution: "30 m",
    revisit: "single mission (2000)",
    coverage: "global to 60°",
    licence: "Public domain (USGS)",
    use: "Terrain slope exclusion.",
    note: "At 30 m this excludes buildings on steep terrain, not steep roofs — a scale mismatch against the 4 m roof raster, tracked as an open limitation.",
  },
  {
    name: "NASA POWER",
    role: "reference",
    metres: 50000,
    platform: "NASA (satellite-derived)",
    resolution: "~0.5°",
    revisit: "daily and hourly",
    coverage: "1981–present",
    licence: "Public domain (NASA)",
    use: "Validation reference, not a model input. Irradiance components and rainfall.",
    note: "Its hourly endpoint defaults to local solar time, not UTC — a 5.1-hour error at Delhi if read naively, which we found by correlating against solar altitude rather than by reading the docs.",
  },
];

/**
 * The scale comparison.
 *
 * This is the page's centrepiece and the reason it exists. The project's
 * central methodological tension is that it attributes 9 km irradiance to 4 m
 * rooftops, and *showing* that gap communicates it far better than a paragraph
 * can. Drawing it at a fixed on-screen scale means the ERA5 cell genuinely does
 * not fit on the page, which is the point.
 */
const SCALES = [
  {
    label: "Open Buildings 2.5D",
    metres: 4,
    colour: "var(--sun-300)",
    what: "one rooftop",
  },
  {
    label: "MODIS (LST, aerosol)",
    metres: 1000,
    colour: "var(--sun-500)",
    what: "a neighbourhood",
  },
  {
    label: "ERA5-Land (irradiance)",
    metres: 9000,
    colour: "var(--sky-400)",
    what: "a whole city district",
  },
  {
    label: "ERA5 (beam fraction)",
    metres: 28000,
    colour: "var(--sky-600)",
    what: "the metropolitan area",
  },
];

/**
 * What each dataset drives in the result, in the same colours the result
 * charts use -- so a card's colour answers "which part of my number does this
 * feed?" rather than decorating it.
 */
const ROLE = {
  geometry: { label: "Shadow and sky view", colour: "var(--cat-shadow)" },
  sunlight: { label: "Sunlight", colour: "var(--sun-400)" },
  heat: { label: "Heat island", colour: "var(--cat-heat)" },
  dust: { label: "Soiling", colour: "var(--cat-soiling)" },
  terrain: { label: "Slope exclusion", colour: "var(--sky-400)" },
  reference: { label: "Validation only", colour: "var(--ink-400)" },
};

// One logarithmic axis for every card, 4 m to 50 km, so resolutions compare
// at a glance. Linear would put everything but the two ERA5 products at zero.
const LOG_MIN = Math.log10(4);
const LOG_MAX = Math.log10(50000);

// Fine is full: the bar measures detail, and a 4 m product should read as
// the most detailed, not the smallest. Ticks use the same mapping, so a label
// sits exactly where that resolution would end the bar.
const detailOf = (metres) =>
  1 - (Math.log10(metres) - LOG_MIN) / (LOG_MAX - LOG_MIN);

function ResolutionMeter({ metres, colour }) {
  const detail = metres == null ? 1 : detailOf(metres);
  return (
    <div className="res-meter" aria-hidden="true">
      <div className="res-meter__track">
        <div
          className="res-meter__fill"
          style={{ width: `${Math.max(4, detail * 100)}%`, background: colour }}
        />
      </div>
      <div className="res-meter__scale">
        {[50000, 1000, 30, 4].map((m) => (
          <span key={m} style={{ left: `${detailOf(m) * 100}%` }}>
            {m >= 1000 ? `${m / 1000} km` : `${m} m`}
          </span>
        ))}
      </div>
    </div>
  );
}

/** Side of the active pixel, in screen px. */
const FRAME = 240;

const fmtLength = (m) => (m >= 1000 ? `${m / 1000} km` : `${m} m`);
const approx = (n) =>
  Math.abs(n - Math.round(n)) > 0.05 && n < 1000 ? "about " : "";
const fmtCount = (n) =>
  n >= 1e6
    ? `${(n / 1e6).toFixed(n >= 1e7 ? 0 : 1)} million`
    : Math.round(n).toLocaleString("en-IN");

/**
 * One pixel of each input, zooming out a step at a time.
 *
 * The first version drew every pixel on one linear scale, and four orders of
 * magnitude do not fit on one linear scale: a 4 m pixel came out at 0.16 px and
 * vanished, while 9 km and 28 km both overflowed the frame and were clipped to
 * the same height -- so the first step looked empty and the last two looked
 * identical, which is exactly where the comparison mattered.
 *
 * Here the frame always holds one pixel of the selected dataset, and every
 * finer dataset's pixel sits inside it at its true relative size. Stepping out
 * shrinks the previous pixel into its place, so the motion is the ratio. Where
 * a finer pixel is under a screen pixel it is marked, not drawn, and says so.
 */
function ScaleComparison() {
  // Opens on the coarsest input: the one view where every finer pixel is
  // visible nested inside it, so the whole ratio reads at a glance.
  const [index, setIndex] = useState(SCALES.length - 1);
  const active = SCALES[index];
  const finer = SCALES.slice(0, index).reverse(); // nearest first
  const previous = finer[0];
  const buildingPixels = (active.metres / 4) ** 2;
  // Finer pixels too small to draw are listed below the frame instead.
  const drawableCount = finer.filter(
    (f) => (FRAME * f.metres) / active.metres >= 3,
  ).length;

  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>Native resolution of each input</h3>
      <p>
        The frame holds one pixel of the selected dataset. Finer datasets sit
        inside it at their true relative size.
      </p>

      <div
        className="chip-row"
        style={{ marginBottom: "1.2rem" }}
        role="group"
        aria-label="Dataset"
      >
        {SCALES.map((scale, i) => (
          <button
            key={scale.label}
            className={`chip${i === index ? " is-active" : ""}`}
            aria-pressed={i === index}
            onClick={() => setIndex(i)}
          >
            {fmtLength(scale.metres)}
          </button>
        ))}
      </div>

      <div className="scale-stage">
        <div
          key={`frame-${index}`}
          className="scale-frame"
          style={{
            width: FRAME,
            height: FRAME,
            borderColor: active.colour,
            background: `color-mix(in srgb, ${active.colour} 12%, transparent)`,
          }}
          role="img"
          aria-label={`One ${fmtLength(active.metres)} ${active.label} pixel${
            previous
              ? `, containing ${fmtCount((active.metres / previous.metres) ** 2)} ${previous.label} pixels`
              : ""
          }.`}
        >
          <span className="scale-frame__label" style={{ color: active.colour }}>
            {fmtLength(active.metres)}
          </span>

          {finer.map((scale, depth) => {
            const side = (FRAME * scale.metres) / active.metres;
            const drawable = side >= 3;
            const isPrevious = depth === 0;
            return drawable ? (
              <div
                key={`${index}-${scale.label}`}
                className={`scale-nested${isPrevious ? " scale-nested--enter" : ""}`}
                style={{
                  width: side,
                  height: side,
                  borderColor: scale.colour,
                  background: `color-mix(in srgb, ${scale.colour} 22%, transparent)`,
                  // Starts at the full frame and shrinks into its slot.
                  "--from": active.metres / scale.metres,
                }}
              >
                {side >= 34 && (
                  <span
                    className="scale-nested__label"
                    style={{ color: scale.colour }}
                  >
                    {fmtLength(scale.metres)}
                  </span>
                )}
              </div>
            ) : (
              <span
                key={`${index}-${scale.label}`}
                className="scale-marker"
                style={{
                  "--c": scale.colour,
                  top: FRAME + 10 + (depth - drawableCount) * 18,
                }}
              >
                <span className="scale-marker__dot" />
                {fmtLength(scale.metres)} pixel:{" "}
                {side < 0.1 ? side.toFixed(3) : side.toFixed(1)} px at this
                scale
              </span>
            );
          })}
        </div>
      </div>

      <p style={{ marginTop: "1rem", marginBottom: "0.4rem" }}>
        <strong style={{ color: active.colour }}>{active.label}</strong> — one
        pixel is {fmtLength(active.metres)} across, about {active.what}.
        {previous && (
          <>
            {" "}
            It holds {approx((active.metres / previous.metres) ** 2)}
            {fmtCount((active.metres / previous.metres) ** 2)} {previous.label}{" "}
            pixels.
          </>
        )}
      </p>
      <p className="field__hint">
        {index === 0
          ? "The finest input, roughly one rooftop per pixel. Every other layer is coarser, so each of their pixels is shared by many rooftops -- step out to see how many."
          : index === 1
            ? `Every rooftop in this cell gets the same temperature and aerosol value, so differences between neighbouring roofs come from the 4 m geometry alone.`
            : `${fmtCount(buildingPixels)} rooftop pixels share this one ${active.label} value. Spatial variation in the estimate comes from the finer layers, never from within this cell.`}
      </p>
    </div>
  );
}

export default function Data() {
  return (
    <div className="page">
      <h1>Data &amp; satellites</h1>
      <p className="lede">
        Eight openly licensed datasets. Spatial resolution spans four orders of
        magnitude, from 4 m building geometry to 28 km irradiance components,
        and the estimate inherits the coarsest resolution of any input that
        varies spatially.
      </p>

      <ScaleComparison />

      <h2>The sources</h2>
      <div className="grid grid--2">
        {SOURCES.map((source) => (
          <div
            className="card source-card"
            key={source.name}
            style={{ "--role": ROLE[source.role].colour }}
          >
            <span className="source-card__role">{ROLE[source.role].label}</span>
            <h3 style={{ marginTop: 0 }}>{source.name}</h3>
            <p
              style={{
                color: "var(--text-dim)",
                fontSize: "0.9rem",
                marginBottom: "0.8rem",
              }}
            >
              {source.platform}
            </p>
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "auto 1fr",
                gap: "0.3rem 0.9rem",
                fontSize: "0.88rem",
                marginBottom: "0.9rem",
              }}
            >
              <span style={{ color: "var(--text-faint)" }}>Resolution</span>
              <span style={{ fontFamily: "var(--font-mono)" }}>
                {source.resolution}
              </span>
              <span style={{ color: "var(--text-faint)" }}>Revisit</span>
              <span>{source.revisit}</span>
              <span style={{ color: "var(--text-faint)" }}>Coverage</span>
              <span>{source.coverage}</span>
              <span style={{ color: "var(--text-faint)" }}>Licence</span>
              <span>{source.licence}</span>
            </div>
            <ResolutionMeter
              metres={source.metres}
              colour={ROLE[source.role].colour}
            />
            <p style={{ marginBottom: source.note ? "0.8rem" : 0 }}>
              {source.use}
            </p>
            {source.note && (
              <p
                style={{
                  fontSize: "0.87rem",
                  color: "var(--text-dim)",
                  borderLeft: "2px solid var(--border)",
                  paddingLeft: "0.8rem",
                  margin: 0,
                }}
              >
                {source.note}
              </p>
            )}
          </div>
        ))}
      </div>

      <h2>Inputs not used</h2>
      <p>
        No commercial irradiance product, lidar, aerial imagery or
        ground-station measurement. Lidar would resolve roof pitch and
        obstruction directly, removing two of the open limitations; for India it
        is neither free nor available at national coverage.
      </p>
      <p>
        One reference that should have been here is not:{" "}
        <strong>PVGIS-SARAH3 does not cover India.</strong> The documentation
        advertises Asia, and it was the intended primary validation reference.
        Probing the live API for seven Indian cities returned a spatial-coverage
        error for all seven; SARAH3 is the Meteosat prime-disk product. See{" "}
        <Link to="/validation">Validation</Link>.
      </p>

      <div className="callout">
        <div className="callout__title">Computation model</div>
        <p>
          No raster is downloaded. Reductions run server-side and only scalars
          return, so one query is a dozen small round-trips. The round-trip
          count is therefore the quantity worth optimising — see{" "}
          <Link to="/models">Models</Link>.
        </p>
      </div>
    </div>
  );
}
