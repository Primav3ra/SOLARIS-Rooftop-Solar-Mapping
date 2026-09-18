import { useState } from 'react';
import { Link } from 'react-router-dom';

const SOURCES = [
  {
    name: 'Open Buildings 2.5D Temporal v1',
    platform: 'Google Research (Sentinel-2 derived)',
    resolution: '~4 m',
    revisit: 'annual vintages',
    coverage: '2016–2023',
    licence: 'CC BY 4.0',
    use: 'Roof footprint and, crucially, building height. Height is what makes a shadow trace possible at all.',
    note: 'The only open building-height product covering India. Its confidence band is a model score, not a calibrated probability, so the 0.5 presence threshold is a prior rather than a likelihood.',
  },
  {
    name: 'Open Buildings v3',
    platform: 'Google Research',
    resolution: 'vector polygons',
    revisit: 'static release',
    coverage: 'current',
    licence: 'CC BY 4.0',
    use: 'Selecting which building you clicked, and its footprint area.',
  },
  {
    name: 'ERA5-Land hourly',
    platform: 'ECMWF / Copernicus',
    resolution: '~9 km',
    revisit: 'hourly, ~3 month lag',
    coverage: '1950–present',
    licence: 'Copernicus (free, attribution)',
    use: 'Global horizontal irradiance, summed over the selected window. The energy input to everything else.',
    note: 'The resolution mismatch at the heart of this project: 9 km irradiance attributed to 4 m roofs. Within one cell every roof gets identical sunlight.',
  },
  {
    name: 'ERA5 hourly',
    platform: 'ECMWF / Copernicus',
    resolution: '~28 km',
    revisit: 'hourly',
    coverage: '1940–present',
    licence: 'Copernicus (free, attribution)',
    use: 'The direct component, giving the beam/diffuse split. ERA5-Land does not publish one.',
    note: 'Uses a monthly aerosol climatology, and is documented to overestimate direct radiation with the error growing in aerosol load — exactly India’s regime. Measured: our reference set averages 0.540 beam fraction against the 0.60 this model used to assume.',
  },
  {
    name: 'ERA5-Land daily aggregates',
    platform: 'ECMWF / Copernicus',
    resolution: '~9 km',
    revisit: 'daily',
    coverage: '1950–present',
    licence: 'Copernicus (free, attribution)',
    use: 'Daily rainfall, which sets when a roof gets washed. Published in metres — a factor of 1000 that is asserted in the tests.',
  },
  {
    name: 'MODIS MOD11A2',
    platform: 'NASA Terra',
    resolution: '1 km',
    revisit: '8-day composite',
    coverage: '2000–present',
    licence: 'Public domain (NASA)',
    use: 'Land surface temperature, for the urban heat island anomaly.',
    note: 'Surface temperature, not air temperature. Surface heat island runs 2–3× the air heat island in Indian cities, so an explicit transfer coefficient converts one to the other.',
  },
  {
    name: 'MODIS MCD19A2 (MAIAC)',
    platform: 'NASA Terra + Aqua',
    resolution: '1 km',
    revisit: 'daily',
    coverage: '2000–present',
    licence: 'Public domain (NASA)',
    use: 'Aerosol optical depth, which drives the dust deposition rate.',
    note: 'MAIAC specifically, because dark-target retrieval fails over bright urban surfaces. The QA cloud mask is applied — the previous code claimed it was and was not.',
  },
  {
    name: 'SRTM GL1',
    platform: 'NASA / USGS',
    resolution: '30 m',
    revisit: 'single mission (2000)',
    coverage: 'global to 60°',
    licence: 'Public domain (USGS)',
    use: 'Terrain slope exclusion.',
    note: 'At 30 m this excludes buildings on steep terrain, not steep roofs — a scale mismatch against the 4 m roof raster, tracked as an open limitation.',
  },
  {
    name: 'NASA POWER',
    platform: 'NASA (satellite-derived)',
    resolution: '~0.5°',
    revisit: 'daily and hourly',
    coverage: '1981–present',
    licence: 'Public domain (NASA)',
    use: 'Validation reference, not a model input. Irradiance components and rainfall.',
    note: 'Its hourly endpoint defaults to local solar time, not UTC — a 5.1-hour error at Delhi if read naively, which we found by correlating against solar altitude rather than by reading the docs.',
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
  { label: 'Open Buildings 2.5D', metres: 4, colour: 'var(--sun-300)', what: 'one rooftop' },
  { label: 'MODIS (LST, aerosol)', metres: 1000, colour: 'var(--sun-500)', what: 'a neighbourhood' },
  { label: 'ERA5-Land (irradiance)', metres: 9000, colour: 'var(--sky-400)', what: 'a whole city district' },
  { label: 'ERA5 (beam fraction)', metres: 28000, colour: 'var(--sky-600)', what: 'the metropolitan area' },
];

function ScaleComparison() {
  const [index, setIndex] = useState(0);
  const active = SCALES[index];
  // 4 m maps to 4 px, capped so the largest cell stays on screen; the cap is
  // itself informative and is labelled.
  const pixelsPerMetre = 0.04;
  const size = Math.min(active.metres * pixelsPerMetre, 560);
  const clipped = active.metres * pixelsPerMetre > 560;

  return (
    <div className="card">
      <h3 style={{ marginTop: 0 }}>Native resolution of each input</h3>
      <p>
        Each square is one pixel of the named dataset, drawn to the same scale.
      </p>

      <div className="chip-row" style={{ marginBottom: '1.2rem' }}>
        {SCALES.map((scale, i) => (
          <button
            key={scale.label}
            className={`chip${i === index ? ' is-active' : ''}`}
            onClick={() => setIndex(i)}
          >
            {scale.metres >= 1000 ? `${scale.metres / 1000} km` : `${scale.metres} m`}
          </button>
        ))}
      </div>

      <div
        style={{
          position: 'relative',
          height: '320px',
          background: 'var(--bg-inset)',
          borderRadius: 'var(--radius-sm)',
          border: '1px solid var(--border)',
          overflow: 'hidden',
          display: 'grid',
          placeItems: 'center',
        }}
      >
        {/* The 4 m reference square is always drawn, so the growth is legible
            rather than abstract. */}
        <div
          style={{
            position: 'absolute',
            width: `${4 * pixelsPerMetre * 40}px`,
            height: `${4 * pixelsPerMetre * 40}px`,
            background: 'var(--sun-300)',
            borderRadius: '2px',
            zIndex: 2,
            boxShadow: '0 0 0 1px rgb(0 0 0 / 0.5)',
          }}
          title="One 4 m Open Buildings pixel, scaled up 40x so it is visible"
        />
        <div
          style={{
            width: `${size}px`,
            height: `${size}px`,
            maxWidth: '96%',
            maxHeight: '96%',
            border: `2px solid ${active.colour}`,
            background: `color-mix(in srgb, ${active.colour} 12%, transparent)`,
            borderRadius: '3px',
            transition: 'width 0.45s ease, height 0.45s ease',
          }}
        />
      </div>

      <p style={{ marginTop: '1rem', marginBottom: 0 }}>
        <strong style={{ color: active.colour }}>{active.label}</strong> — one pixel is{' '}
        {active.metres >= 1000 ? `${active.metres / 1000} km` : `${active.metres} m`} across,
        about {active.what}.{' '}
        {clipped && <em>Clipped: it does not fit on this page at this scale.</em>}
      </p>
      <p className="field__hint">
        The amber square is one 4 m building pixel at 40× magnification. One ERA5-Land
        cell contains approximately 5.1 million such pixels, all assigned identical
        irradiance. Spatial variation in the output therefore originates entirely in the
        geometry and aerosol layers.
      </p>
    </div>
  );
}

export default function Data() {
  return (
    <div className="page">
      <h1>Data &amp; satellites</h1>
      <p className="lede">
        Nine openly licensed datasets. Spatial resolution spans four orders of
        magnitude, from 4 m building geometry to 28 km irradiance components, and the
        estimate inherits the coarsest resolution of any input that varies spatially.
      </p>

      <ScaleComparison />

      <h2>The sources</h2>
      <div className="grid grid--2">
        {SOURCES.map((source) => (
          <div className="card" key={source.name}>
            <h3 style={{ marginTop: 0 }}>{source.name}</h3>
            <p style={{ color: 'var(--text-dim)', fontSize: '0.9rem', marginBottom: '0.8rem' }}>
              {source.platform}
            </p>
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'auto 1fr',
                gap: '0.3rem 0.9rem',
                fontSize: '0.88rem',
                marginBottom: '0.9rem',
              }}
            >
              <span style={{ color: 'var(--text-faint)' }}>Resolution</span>
              <span style={{ fontFamily: 'var(--font-mono)' }}>{source.resolution}</span>
              <span style={{ color: 'var(--text-faint)' }}>Revisit</span>
              <span>{source.revisit}</span>
              <span style={{ color: 'var(--text-faint)' }}>Coverage</span>
              <span>{source.coverage}</span>
              <span style={{ color: 'var(--text-faint)' }}>Licence</span>
              <span>{source.licence}</span>
            </div>
            <p style={{ marginBottom: source.note ? '0.8rem' : 0 }}>{source.use}</p>
            {source.note && (
              <p
                style={{
                  fontSize: '0.87rem',
                  color: 'var(--text-dim)',
                  borderLeft: '2px solid var(--border)',
                  paddingLeft: '0.8rem',
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
        No commercial irradiance product, lidar, aerial imagery or ground-station
        measurement. Lidar would resolve roof pitch and obstruction directly, removing two
        of the open limitations; for India it is neither free nor available at national
        coverage.
      </p>
      <p>
        One reference that should have been here is not:{' '}
        <strong>PVGIS-SARAH3 does not cover India.</strong> The documentation advertises
        Asia, and it was the intended primary validation reference. Probing the live API
        for seven Indian cities returned a spatial-coverage error for all seven; SARAH3 is
        the Meteosat prime-disk product. See <Link to="/validation">Validation</Link>.
      </p>

      <div className="callout">
        <div className="callout__title">Computation model</div>
        <p>
          No raster is downloaded. Reductions run server-side and only scalars return, so
          one query is a dozen small round-trips. The round-trip count is therefore the
          quantity worth optimising — see <Link to="/models">Models</Link>.
        </p>
      </div>
    </div>
  );
}
