import { useState } from 'react';
import { Link } from 'react-router-dom';

/**
 * Both lists on one page, filterable.
 *
 * Resolved defects are shown alongside open limitations rather than hidden,
 * because a system's failure history is part of judging it -- and because
 * several of these produced plausible wrong numbers rather than errors, which
 * is the harder kind to catch and the more useful kind to publish.
 */
const OPEN = [
  {
    id: 'scale',
    title: '9 km irradiance attributed to 4 m rooftops',
    severity: 'fundamental',
    body: "ERA5-Land's native cell is about 9 km, so within one cell every roof receives identical irradiance. All spatial variation in the output comes from the geometry layers, not the meteorology. This is not a bug to fix — it is the resolution of the best open dataset for India — but it bounds what a per-building number means.",
  },
  {
    id: 'P1',
    title: 'Every roof is treated as horizontal',
    severity: 'material',
    body: 'No tilt, azimuth or incidence-angle modifier in the served path. This understates a well-installed array by 8–12% at north-Indian latitudes. The pvlib layer can compute tilted output and the evaluation reports both, but the map assumes flat.',
  },
  {
    id: 'P7',
    title: 'Roof slope comes from 30 m terrain, not the roof',
    severity: 'material',
    body: 'The 30° exclusion derives from SRTM at 30 m, so it excludes buildings on steep terrain rather than steep roofs — a scale mismatch against the 4 m roof raster. Deliberately not fixed: a correct version needs roof-plane fitting from the height raster, which is a project in itself.',
  },
  {
    id: 'vintage',
    title: 'Roof geometry is capped at 2023',
    severity: 'material',
    body: 'Open Buildings 2.5D Temporal v1 runs 2016–2023. Buildings completed after 2023 are invisible regardless of the irradiance window selected, so a 2026 query uses 2023 geometry.',
  },
  {
    id: 'P6',
    title: 'Annual shadow geometry rests on 31 sun positions',
    severity: 'minor',
    body: 'Not 8760 hours. Fine for an annual mean; it cannot resolve a specific morning.',
  },
  {
    id: 'P9',
    title: 'No inter-row shading or rooftop obstructions',
    severity: 'minor',
    body: 'Water tanks, stairwell housings and parapets are not modelled. The packing factor of 0.70 absorbs them as a lumped assumption.',
  },
  {
    id: 'truth',
    title: 'No ground truth anywhere in the loop',
    severity: 'fundamental',
    body: 'No pyranometer, no metered plant. Every reference is satellite-derived or a published aggregate, and accuracy is bounded at about 10% by the disagreement between the available free references.',
  },
  {
    id: 'aod',
    title: 'The soiling calibration rests on one assumed aerosol value',
    severity: 'material',
    body: 'The deposition rate is a measured Delhi figure divided by an assumed Delhi annual-mean AOD. The rate is measured; the AOD is a literature value, not a retrieval from this pipeline. Per-city soiling ranking inherits that uncertainty — the rainfall-driven results do not.',
  },
  {
    id: 'confidence',
    title: 'Building confidence is not a probability',
    severity: 'minor',
    body: 'The Open Buildings presence threshold of 0.5 is a prior, not a calibrated likelihood, so roof area carries an unquantified inclusion error.',
  },
  {
    id: 'quantise',
    title: 'Coordinates are quantised to about 11 metres',
    severity: 'minor',
    body: 'Cache keys round to 4 decimal places, so two clicks 10 m apart return the identical result. A deliberate trade: without it the cache hit rate would be near zero, because areas of interest arrive as raw map-click floats.',
  },
];

const RESOLVED = [
  {
    id: 'D1',
    title: 'translate() defaults to metres; the code passed pixel counts',
    body: 'The shadow search was offset 100 m where 400 m was intended, and sky-view horizon angles were computed over 4× the distance actually sampled — biasing the factor toward 1, i.e. toward no diffuse penalty at all. All distances are now in metres, which removes the class of bug by construction.',
  },
  {
    id: 'D2',
    title: 'The shadow mask was not a directional trace',
    body: 'A circular focal maximum plus a height test at a different location: two conditions referring to different places, with azimuth entering only through one rigid translate. The search was effectively isotropic. Rewritten as a directional trace across the height raster.',
  },
  {
    id: 'D9',
    title: 'Focal operations resolved against the request projection',
    body: 'A 100-pixel kernel spanned about 400 m when reducing at 4 m and kilometres when rendering a low-zoom tile. The shadow layer a user saw on the map was therefore not the shadow layer behind their number.',
  },
  {
    id: 'D3',
    title: 'Sun-position thinning dropped weight without renormalising',
    body: 'Would have halved the shadow frequency. The branch never fired — the maximum position count is 39 — so it was dead code concealing a live bug.',
  },
  {
    id: 'D4',
    title: 'Aerosol data had no QA masking, though the docstring claimed it did',
    body: 'Cloudy and cloud-shadowed retrievals were being averaged into the annual mean.',
  },
  {
    id: 'D5',
    title: 'Soiling loss was unbounded',
    body: 'Retention went negative above AOD 12.5 — negative generated energy.',
  },
  {
    id: 'D6',
    title: 'Naive annualisation',
    body: 'A single December day was scaled by 365.25/days and returned as an annual rate. Now omitted below 28 days rather than reported.',
  },
  {
    id: 'D7',
    title: 'A dataset filter used the host machine local timezone',
    body: 'Results were not reproducible across machines.',
  },
  {
    id: 'D8',
    title: 'Silent fallbacks returning plausible numbers',
    body: 'Distinguishable from real results only by an obscure source string. The data_quality block was the structural fix, and writing it immediately found two shade buckets with no sun reporting 0.0 shade — which reads as "fully sunlit".',
  },
  {
    id: 'D10',
    title: 'An empty sun-position list raised IndexError',
    body: 'Instead of a clear message. Reachable for a high-latitude winter window.',
  },
  {
    id: 'R5',
    title: 'The temporal bound was calendar-derived',
    body: 'It used today.year − 1, so Q2 2026 was refused two and a half months after it ended. The ceiling is now probed from ERA5-Land’s actual last published image.',
  },
  {
    id: 'P3/P4',
    title: 'Temperature loss was double-counted and mis-scaled at once',
    body: 'Two errors of opposite sign, neither measured. Surface temperature was used as air temperature, though surface heat island runs 2–3× the air heat island; while the absolute 35 °C-vs-25 °C loss was absent, folded ambiguously into a lumped PR of 0.80. The headline number could look reasonable with both components wrong — the worst possible starting point for a validation.',
  },
  {
    id: 'P5',
    title: 'Soiling had no rain-washing or cleaning term',
    body: 'One annual coefficient applied unchanged to windows from one day to one year. Replaced by the published Kimber model with real rainfall.',
  },
  {
    id: 'P8',
    title: 'Shade-matrix buckets were labelled UTC',
    body: 'The "08-12" bucket is really 13:30–17:30 IST. Both labels are now carried.',
  },
  {
    id: 'leak',
    title: 'Project ids leaked in 500-level error detail',
    body: 'The deployed instance answered a bad request with "Caller does not have required permission to use project pv-mapping-india". Note that HTTPException is handled by FastAPI before any middleware, so this needed its own exception handler rather than middleware alone.',
  },
  {
    id: 'cors',
    title: 'Wildcard CORS paired with credentials',
    body: 'Invalid per the CORS spec — browsers reject a wildcard origin when credentials are sent — so it never granted the access it appeared to. A wildcard is now rejected at startup.',
  },
  {
    id: 'project',
    title: 'The GCP project arrived in every request body',
    body: 'Which let any caller choose which project the server initialised Earth Engine against, and therefore whose quota and billing to spend. It is server configuration now.',
  },
];

const SEVERITY_BADGE = {
  fundamental: 'badge--bad',
  material: 'badge--warn',
  minor: 'badge',
};

export default function Limitations() {
  const [tab, setTab] = useState('open');

  return (
    <div className="page">
      <h1>Limitations</h1>
      <p className="lede">
        Ten defects were identified and corrected during development. The items below are
        unresolved constraints on the estimate, each attributed to its mechanism, followed
        by the corrected defects and their observed symptoms.
      </p>

      <div className="chip-row" style={{ margin: '1.5rem 0' }}>
        <button
          className={`chip${tab === 'open' ? ' is-active' : ''}`}
          onClick={() => setTab('open')}
        >
          Open ({OPEN.length})
        </button>
        <button
          className={`chip${tab === 'resolved' ? ' is-active' : ''}`}
          onClick={() => setTab('resolved')}
        >
          Resolved ({RESOLVED.length})
        </button>
      </div>

      {tab === 'open' ? (
        <>
          <div className="grid grid--2">
            {OPEN.map((item) => (
              <div className="card" key={item.id}>
                <div
                  style={{
                    display: 'flex',
                    gap: '0.6rem',
                    alignItems: 'center',
                    marginBottom: '0.6rem',
                  }}
                >
                  <span className={`badge ${SEVERITY_BADGE[item.severity]}`}>
                    {item.severity}
                  </span>
                  <code style={{ fontSize: '0.78rem' }}>{item.id}</code>
                </div>
                <h3 style={{ marginTop: 0, fontSize: '1.02rem' }}>{item.title}</h3>
                <p style={{ marginBottom: 0, fontSize: '0.92rem' }}>{item.body}</p>
              </div>
            ))}
          </div>

          <h2>Not built, and why</h2>
          <div className="grid grid--3">
            <div className="card">
              <h3 style={{ marginTop: 0 }}>The shadow surrogate model</h3>
              <p style={{ fontSize: '0.92rem', marginBottom: 0 }}>
                Profiled, then dropped. A perfect surrogate would remove zero Earth Engine
                round-trips, capping the speedup at 1.33×.{' '}
                <Link to="/models">The measurement →</Link>
              </p>
            </div>
            <div className="card">
              <h3 style={{ marginTop: 0 }}>The ERA5 bias correction</h3>
              <p style={{ fontSize: '0.92rem', marginBottom: 0 }}>
                Needs Earth Engine credentials to build the training set, which were
                unavailable. The decomposition model targets the same defect from the
                reference side instead.
              </p>
            </div>
            <div className="card">
              <h3 style={{ marginTop: 0 }}>Bring-your-own Earth Engine project</h3>
              <p style={{ fontSize: '0.92rem', marginBottom: 0 }}>
                The earthengine OAuth scope is sensitive, needing Google verification
                review and capping an unverified app at about 100 users. Earth Engine access
                is also not conferred by owning a Google account.
              </p>
            </div>
          </div>
        </>
      ) : (
        <>
          <p>
            Several of these produced plausible incorrect values rather than raising errors.
            Symptoms are recorded because the failure mode determines what a reader should
            check in any comparable system.
          </p>
          <div className="grid grid--2">
            {RESOLVED.map((item) => (
              <div className="card" key={item.id}>
                <div
                  style={{
                    display: 'flex',
                    gap: '0.6rem',
                    alignItems: 'center',
                    marginBottom: '0.6rem',
                  }}
                >
                  <span className="badge badge--ok">fixed</span>
                  <code style={{ fontSize: '0.78rem' }}>{item.id}</code>
                </div>
                <h3 style={{ marginTop: 0, fontSize: '1.02rem' }}>{item.title}</h3>
                <p style={{ marginBottom: 0, fontSize: '0.92rem' }}>{item.body}</p>
              </div>
            ))}
          </div>

          <h2>Errors found in the reference data itself</h2>
          <div className="grid grid--3">
            <div className="card">
              <h3 style={{ marginTop: 0 }}>NASA POWER hourly defaults to local solar time</h3>
              <p style={{ fontSize: '0.92rem', marginBottom: 0 }}>
                Reading it as UTC shifts every timestamp by longitude/15 hours — 5.1 h at
                Delhi. Found empirically: correlating GHI against sin(solar altitude) in UTC
                gives +0.994 with the flag passed explicitly, against +0.141 under the
                default.
              </p>
            </div>
            <div className="card">
              <h3 style={{ marginTop: 0 }}>Its irradiance components do not close</h3>
              <p style={{ fontSize: '0.92rem', marginBottom: 0 }}>
                They sum 5.26% below the reported GHI for Delhi, putting a uniform negative
                bias on every plane-of-array figure — and it had already produced a wrong
                conclusion, a test asserting that low-latitude tilt loses energy.
              </p>
            </div>
            <div className="card">
              <h3 style={{ marginTop: 0 }}>It is unreliable over high terrain</h3>
              <p style={{ fontSize: '0.92rem', marginBottom: 0 }}>
                458 mm/yr reported for Leh against an actual figure near 100 mm. Leh is
                retained as a low-aerosol contrast case, but its soiling figures should not
                be read as meaningful.
              </p>
            </div>
          </div>
        </>
      )}

      <div className="callout callout--caution" style={{ marginTop: '2.5rem' }}>
        <div className="callout__title">Scope of the estimate</div>
        <p>
          <strong>It is</strong> a screening estimate of technical rooftop solar
          potential — useful for comparing neighbourhoods, sizing a city-level programme,
          or seeing how much shadow and aerosol cost a given area.
        </p>
        <p style={{ marginBottom: 0 }}>
          <strong>It is not</strong> an engineering estimate for a specific installation.
          For that you need a site survey, roof-plane geometry, structural assessment,
          local obstruction mapping, and preferably a year of on-site measurement. Nothing
          here substitutes for any of those.
        </p>
      </div>
    </div>
  );
}
