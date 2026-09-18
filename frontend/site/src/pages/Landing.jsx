import { Link } from 'react-router-dom';
import Hero from '../components/Hero.jsx';
import {
  specificYieldSummary,
  referenceSpreadPct,
  beamFractionSuite,
  soiling,
  manifest,
  fmt,
} from '../data/reports.js';

const yieldSummary = specificYieldSummary();
const spread = referenceSpreadPct();
const beam = beamFractionSuite();

export default function Landing() {
  return (
    <>
      <Hero>
        <p
          style={{
            textTransform: 'uppercase',
            letterSpacing: '0.14em',
            fontSize: '0.8rem',
            color: 'var(--accent)',
            fontWeight: 600,
            marginBottom: '0.8rem',
          }}
        >
          Open satellite data · urban India
        </p>
        <h1 style={{ maxWidth: '22ch' }}>Rooftop solar potential, computed per building</h1>
        <p className="lede" style={{ maxWidth: '52ch' }}>
          Pick a point in urban India and a date range. SOLARIS estimates rooftop
          generation from open satellite data, modelling shadow, sky obstruction, urban
          heat and dust separately so you can see which one costs most.
        </p>
        <div style={{ display: 'flex', gap: '0.75rem', flexWrap: 'wrap', marginTop: '1.6rem' }}>
          <Link className="btn btn--primary btn--lg" to="/explore">
            Open the map
          </Link>
          <Link className="btn btn--lg btn--ghost" to="/validation">
            Validation results
          </Link>
        </div>
        <p style={{ fontSize: '0.86rem', color: 'var(--text-faint)', marginTop: '1.2rem' }}>
          No sign-in required. Every page except the map is static.
        </p>
      </Hero>

      <div className="page" style={{ paddingTop: '1rem' }}>
        <div className="grid grid--4">
          <Link to="/validation" className="card" style={{ color: 'inherit' }}>
            <div className="stat">
              <div className="stat__value">
                {yieldSummary ? `${yieldSummary.nInBand}/${yieldSummary.n}` : '--'}
              </div>
              <div className="stat__label">City-years in the published band</div>
              <p className="stat__note">
                Specific yield against measured Indian plants, {yieldSummary?.band?.[0]}–
                {yieldSummary?.band?.[1]} kWh/kWp/yr.
              </p>
            </div>
          </Link>

          <Link to="/validation" className="card" style={{ color: 'inherit' }}>
            <div className="stat">
              <div className="stat__value">±{spread ? spread.toFixed(1) : '--'}%</div>
              <div className="stat__label">Reference disagreement</div>
              <p className="stat__note">
                The two free references for India differ by this much. It bounds any
                accuracy claim here.
              </p>
            </div>
          </Link>

          <Link to="/models" className="card" style={{ color: 'inherit' }}>
            <div className="stat">
              <div className="stat__value">{fmt.signed(manifest?.skill_vs_erbs, 2)}</div>
              <div className="stat__label">Skill over the published baseline</div>
              <p className="stat__note">
                Learned beam/diffuse split against Erbs (1982), spatial and temporal
                holdout.
              </p>
            </div>
          </Link>

          <Link to="/method" className="card" style={{ color: 'inherit' }}>
            <div className="stat">
              <div className="stat__value">
                {soiling?.window_spread_x ? `${soiling.window_spread_x}×` : '--'}
              </div>
              <div className="stat__label">Soiling range across Delhi&apos;s months</div>
              <p className="stat__note">
                Driven by rainfall. A single annual coefficient cannot express it.
              </p>
            </div>
          </Link>
        </div>

        <h2>How it works</h2>
        <div className="grid grid--3">
          <div className="card">
            <h3>Rooftops</h3>
            <p style={{ marginBottom: 0 }}>
              Google Open Buildings 2.5D supplies footprint and height at about 4 m. It is
              the only open building-height product covering India, and height is what
              makes shadow modelling possible.
            </p>
          </div>
          <div className="card">
            <h3>Shadow and sky</h3>
            <p style={{ marginBottom: 0 }}>
              A directional trace across the height raster, at sun positions weighted by
              the energy each carries. Sky-view factor handles the diffuse half of the
              resource, which shadow modelling alone misses.
            </p>
          </div>
          <div className="card">
            <h3>Heat and dust</h3>
            <p style={{ marginBottom: 0 }}>
              MODIS land surface temperature gives the urban heat anomaly; aerosol depth
              and ERA5 rainfall drive a Kimber soiling model. Both reported separately
              rather than folded into one derate.
            </p>
          </div>
        </div>

        <h2>Traceability</h2>
        <div className="grid grid--2" style={{ marginTop: '1.4rem' }}>
          <div className="card">
            <h3>Figures come from committed artifacts</h3>
            <p>
              The Validation and Models pages import the evaluation reports at build time.
              Regenerate a report and these pages change; there is no second copy of the
              numbers.
            </p>
            <Link to="/validation">Validation →</Link>
          </div>
          <div className="card">
            <h3>Known limitations are listed</h3>
            <p>
              Ten fixed defects, the reference dataset that turned out not to cover India,
              an ML track dropped after profiling, and the approximations still in place.
            </p>
            <Link to="/limitations">Limitations →</Link>
          </div>
        </div>

        <div className="callout callout--caution" style={{ marginTop: '2rem' }}>
          <div className="callout__title">Scope</div>
          <p>
            A screening estimate, not an engineering one. ERA5 irradiance is ~9 km, so
            within one cell every roof receives the same sunlight and all spatial variation
            comes from geometry. Beam fraction across the reference set averages{' '}
            <strong>{fmt.fixed(beam?.reference_beam_fraction_mean, 3)}</strong> against the{' '}
            {beam?.era5_fallback_used_by_model} previously assumed. A real installation
            still needs a site survey.
          </p>
        </div>
      </div>
    </>
  );
}
