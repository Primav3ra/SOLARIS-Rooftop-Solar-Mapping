import { Link } from 'react-router-dom';
import { generatedAt, validation } from '../data/reports.js';

const DATASETS = [
  {
    name: 'Open Buildings 2.5D Temporal v1 · Open Buildings v3',
    holder: 'Google Research',
    licence: 'CC BY 4.0',
    url: 'https://sites.research.google/open-buildings/',
    note: 'Attribution required. Building footprints and heights.',
  },
  {
    name: 'ERA5 and ERA5-Land reanalysis',
    holder: 'ECMWF / Copernicus Climate Change Service',
    licence: 'Copernicus Licence (free, attribution required)',
    url: 'https://cds.climate.copernicus.eu/',
    note: 'Neither the European Commission nor ECMWF is responsible for any use of this data. Irradiance, temperature and precipitation.',
  },
  {
    name: 'MODIS MOD11A2 and MCD19A2 (MAIAC)',
    holder: 'NASA LP DAAC',
    licence: 'Public domain',
    url: 'https://lpdaac.usgs.gov/',
    note: 'Land surface temperature and aerosol optical depth.',
  },
  {
    name: 'SRTM GL1',
    holder: 'NASA / USGS',
    licence: 'Public domain',
    url: 'https://lpdaac.usgs.gov/products/srtmgl1v003/',
    note: 'Terrain elevation.',
  },
  {
    name: 'NASA POWER',
    holder: 'NASA Langley Research Center',
    licence: 'Public domain',
    url: 'https://power.larc.nasa.gov/',
    note: 'Validation reference only, not a model input. Requested via the POWER Project funded through the NASA Earth Science Directorate Applied Science Program.',
  },
  {
    name: 'Global Solar Atlas',
    holder: 'World Bank Group / Solargis',
    licence: 'CC BY 4.0',
    url: 'https://globalsolaratlas.info/',
    note: 'Long-term-average figures used as a held-out third reference.',
  },
  {
    name: 'OpenStreetMap basemap tiles',
    holder: 'OpenStreetMap contributors, rendered by CARTO',
    licence: 'ODbL 1.0 (data), CC BY 3.0 (tiles)',
    url: 'https://www.openstreetmap.org/copyright',
    note: 'Basemap only; no OSM data enters any computation.',
  },
];

const SOFTWARE = [
  { name: 'pvlib-python', licence: 'BSD-3-Clause', use: 'Transposition, cell temperature, and the Kimber soiling model used as an independent cross-check' },
  { name: 'Earth Engine Python API', licence: 'Apache-2.0', use: 'Server-side raster computation' },
  { name: 'FastAPI, Pydantic, Uvicorn', licence: 'MIT / BSD', use: 'The HTTP layer and request validation' },
  { name: 'scikit-learn', licence: 'BSD-3-Clause', use: 'The decomposition model ladder' },
  { name: 'MapLibre GL JS', licence: 'BSD-3-Clause', use: 'The map' },
  { name: 'Chart.js', licence: 'MIT', use: 'Every chart on this site' },
  { name: 'React, Vite, React Router', licence: 'MIT', use: 'This site' },
  { name: 'three.js, @react-three/fiber, drei', licence: 'MIT', use: 'The landing animation' },
];

export default function About() {
  const generated = generatedAt();

  return (
    <div className="page">
      <h1>About &amp; licences</h1>
      <p className="lede">
        A final-year project estimating rooftop solar potential for urban India from open
        satellite data. All inputs are openly licensed; all reported figures trace to a
        committed artifact.
      </p>

      <h2>Attribution</h2>
      <p>
        CC BY 4.0 and the Copernicus licence require attribution as a condition of use.
      </p>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Dataset</th>
              <th>Rights holder</th>
              <th>Licence</th>
            </tr>
          </thead>
          <tbody>
            {DATASETS.map((dataset) => (
              <tr key={dataset.name}>
                <td>
                  <a href={dataset.url} target="_blank" rel="noreferrer">
                    {dataset.name}
                  </a>
                  <div style={{ fontSize: '0.84rem', color: 'var(--text-faint)', marginTop: '0.2rem' }}>
                    {dataset.note}
                  </div>
                </td>
                <td>{dataset.holder}</td>
                <td style={{ whiteSpace: 'nowrap' }}>{dataset.licence}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2>Software</h2>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Library</th>
              <th>Licence</th>
              <th>Used for</th>
            </tr>
          </thead>
          <tbody>
            {SOFTWARE.map((item) => (
              <tr key={item.name}>
                <td>{item.name}</td>
                <td style={{ whiteSpace: 'nowrap' }}>{item.licence}</td>
                <td>{item.use}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <h2>Imagery on the landing page</h2>
      <p>
        The Earth on the landing page is generated procedurally in a fragment shader; it
        contains no satellite imagery. This avoids a multi-megabyte texture download and
        any imagery licence.
      </p>
      <p>
        The continents are recognisable as landmass but are not accurate coastlines. It is
        an illustration, not a map.
      </p>

      <h2>Code</h2>
      <p>
        Licensed under the MIT Licence. The repository includes the evaluation harness, the
        cached reference data, the committed model artifacts, and the documentation — so
        every figure on this site can be regenerated from a clone.
      </p>
      <p>
        A fresh clone with no Google Cloud account gets a fully green test run. That is
        deliberate, and it is why the project contains a numpy-backed fake Earth Engine
        rather than mocks.
      </p>

      <h2>Sessions and data collected</h2>
      <p>
        There is no sign-in and no account. Each browser session receives an opaque token
        used to key a per-session computation allowance; it is not a credential and
        identifies no one. Nothing is stored server-side beyond cached computation results,
        which are keyed on coordinates quantised to approximately 11 m and carry no session
        identifier.
      </p>
      <p>
        Google sign-in was implemented during development and removed. It verified an ID
        token and keyed rate limiting on the account subject, but moved no Earth Engine
        quota: the OAuth client belongs to this project, so calls are billed here
        regardless of who is authenticated.
      </p>

      <h2>Provenance of the numbers on this site</h2>
      <p>
        Each page reads a committed JSON artifact at build time. If a model is retrained
        and its report regenerated, these pages change; there is no second copy to go
        stale.
      </p>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Artifact</th>
              <th>Generated</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>
                <code>evals/reports/latest.json</code> — validation
              </td>
              <td>{generated.validation}</td>
            </tr>
            <tr>
              <td>
                <code>evals/reports/soiling.json</code> — soiling
              </td>
              <td>{generated.soiling}</td>
            </tr>
            <tr>
              <td>
                <code>ml/reports/decomposition.json</code> — the model ladder
              </td>
              <td>{generated.decomposition}</td>
            </tr>
            <tr>
              <td>
                <code>evals/reports/track_b_profile.json</code> — the dropped track
              </td>
              <td>{generated.trackB}</td>
            </tr>
          </tbody>
        </table>
      </div>
      <p className="source-note">
        Algorithm version {validation.algo_version}, datasets version{' '}
        {validation.dataset_version}. Both prefix every cache key, so a change to the
        physics invalidates stale results automatically instead of needing a manual flush.
      </p>

      <div className="callout">
        <div className="callout__title">Start here if you are evaluating this project</div>
        <p style={{ marginBottom: 0 }}>
          <Link to="/validation">Validation</Link> for whether the numbers are right,{' '}
          <Link to="/limitations">Limitations</Link> for where they are not, and{' '}
          <Link to="/models">Models</Link> for the one planned component that was measured
          and then dropped.
        </p>
      </div>
    </div>
  );
}
