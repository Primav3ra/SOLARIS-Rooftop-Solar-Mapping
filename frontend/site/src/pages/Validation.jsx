import { useEffect, useRef } from 'react';
import { Link } from 'react-router-dom';
import Figure from '../components/Figure.jsx';
import { Chart, baseOptions, palette } from '../components/charts.js';
import {
  validation,
  specificYieldRows,
  specificYieldSummary,
  referenceSpreadRows,
  referenceSpreadPct,
  beamFractionSuite,
  pvlibSuite,
  soiling,
  fmt,
} from '../data/reports.js';

const summary = specificYieldSummary();
const rows = specificYieldRows();
const spreadRows = referenceSpreadRows();
const spread = referenceSpreadPct();
const beam = beamFractionSuite();
const pvlib = pvlibSuite();

/**
 * Specific yield per city-year against the published band.
 *
 * A band-and-points figure rather than a bar chart: the question is "does this
 * fall inside the band", which is a position question, and a bar chart would
 * invite reading the *height* differences as meaningful when the band is what
 * matters.
 */
function YieldBandChart() {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current || !rows.length) return undefined;
    const p = palette();
    const band = summary?.band ?? [1000, 1750];
    const labels = rows.map((r) => `${r.city} ${r.year}`);

    const chart = new Chart(ref.current, {
      type: 'line',
      data: {
        labels,
        datasets: [
          {
            label: 'Published band (upper)',
            data: labels.map(() => band[1]),
            borderColor: 'transparent',
            backgroundColor: `color-mix(in srgb, ${p.ok} 14%, transparent)`,
            fill: '+1',
            pointRadius: 0,
          },
          {
            label: 'Published band (lower)',
            data: labels.map(() => band[0]),
            borderColor: 'transparent',
            pointRadius: 0,
            fill: false,
          },
          {
            label: 'Model',
            data: rows.map((r) => r.specific_yield_kwh_per_kwp_yr),
            borderColor: p.accent,
            backgroundColor: p.accent,
            showLine: false,
            pointRadius: 4,
          },
        ],
      },
      options: {
        ...baseOptions({ showLegend: true, yTitle: 'kWh/kWp/yr' }),
        plugins: {
          ...baseOptions({ showLegend: true }).plugins,
          legend: {
            ...baseOptions({ showLegend: true }).plugins.legend,
            labels: {
              ...baseOptions({ showLegend: true }).plugins.legend.labels,
              filter: (item) => item.text === 'Model' || item.text.includes('upper'),
            },
          },
        },
        scales: {
          ...baseOptions().scales,
          x: { ...baseOptions().scales.x, ticks: { color: p.dim, font: { size: 9 }, maxRotation: 70, minRotation: 70 } },
        },
      },
    });
    return () => chart.destroy();
  }, []);
  return <canvas ref={ref} />;
}

/**
 * The reference spread, as the hero visual.
 *
 * This chart *is* the honesty argument made visible: two independent
 * references for the same city, and the gap between them is the floor on any
 * accuracy claim. Faceted by reference rather than coloured on one axis,
 * because these are two measurements of one quantity and not two series.
 */
function SpreadChart() {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current || !spreadRows.length) return undefined;
    const p = palette();
    const chart = new Chart(ref.current, {
      type: 'bar',
      data: {
        labels: spreadRows.map((r) => r.city),
        datasets: [
          {
            label: 'NASA POWER',
            data: spreadRows.map((r) => r.nasa_power_kwh_m2_yr),
            backgroundColor: p.sky,
            borderWidth: 0,
          },
          {
            label: 'Global Solar Atlas',
            data: spreadRows.map((r) => r.global_solar_atlas_kwh_m2_yr),
            backgroundColor: p.accent,
            borderWidth: 0,
          },
        ],
      },
      options: baseOptions({ showLegend: true, yTitle: 'kWh/m²/yr' }),
    });
    return () => chart.destroy();
  }, []);
  return <canvas ref={ref} />;
}

/** Small multiples: past the categorical ceiling, one panel per city. */
function CityMultiples() {
  const byCity = rows.reduce((acc, row) => {
    (acc[row.city] ??= []).push(row);
    return acc;
  }, {});
  return (
    <div className="small-multiples">
      {Object.entries(byCity).map(([city, cityRows]) => (
        <CityPanel key={city} city={city} rows={cityRows} />
      ))}
    </div>
  );
}

function CityPanel({ city, rows: cityRows }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current) return undefined;
    const p = palette();
    const band = summary?.band ?? [1000, 1750];
    const chart = new Chart(ref.current, {
      type: 'bar',
      data: {
        labels: cityRows.map((r) => r.year),
        datasets: [
          {
            data: cityRows.map((r) => r.specific_yield_kwh_per_kwp_yr),
            backgroundColor: p.accent,
            borderWidth: 0,
          },
        ],
      },
      options: {
        ...baseOptions(),
        scales: {
          x: { ticks: { color: p.faint, font: { size: 9 } }, grid: { display: false } },
          y: {
            min: band[0] * 0.9,
            max: band[1] * 1.02,
            ticks: { color: p.faint, font: { size: 9 }, maxTicksLimit: 3 },
            grid: { color: p.grid },
          },
        },
      },
    });
    return () => chart.destroy();
  }, [cityRows]);

  return (
    <div>
      <div className="figure__title">{city}</div>
      <div className="figure__canvas">
        <canvas ref={ref} />
      </div>
    </div>
  );
}

export default function Validation() {
  return (
    <div className="page">
      <h1>Validation</h1>
      <p className="lede">
        Every number here is read from a committed evaluation artifact at build time, so
        this page cannot drift from the repository.
      </p>
      <p className="source-note" style={{ marginTop: 0 }}>
        Generated {validation.generated_utc} · algorithm v{validation.algo_version} ·
        datasets v{validation.dataset_version} · engine <code>{validation.engine}</code>
      </p>

      <div className="callout">
        <div className="callout__title">Bound on reported accuracy</div>
        <p>
          The two free references for India disagree by about{' '}
          <strong>{spread?.toFixed(1)}%</strong>. No accuracy claim here can be tighter
          than that, so results are reported against a reference ensemble with an explicit
          band rather than a single figure. The spread is printed on every evaluation run.
        </p>
      </div>

      <Figure
        title="Inter-reference disagreement"
        caption="Annual global horizontal irradiance for Delhi from two independent satellite-derived products. NASA POWER is a 0.5° reanalysis-assimilated product; Global Solar Atlas derives from Solargis at 250 m. The difference between them is irreducible without ground measurement."
        columns={[
          { key: 'city', label: 'City' },
          { key: 'nasa_power_kwh_m2_yr', label: 'NASA POWER', numeric: true },
          { key: 'global_solar_atlas_kwh_m2_yr', label: 'Global Solar Atlas', numeric: true },
          { key: 'spread_kwh_m2_yr', label: 'Spread', numeric: true },
          {
            key: 'spread_pct_of_mean',
            label: '% of mean',
            numeric: true,
            render: (r) => `${r.spread_pct_of_mean}%`,
          },
        ]}
        rows={spreadRows.map((r, i) => ({ key: i, ...r }))}
      >
        <SpreadChart />
      </Figure>

      <h2>Specific yield against measured plants</h2>
      <p>
        Specific yield is independent of packing factor and module efficiency, which
        appear in both the numerator and the denominator. The metric isolates the
        irradiance and loss chain from array-sizing assumptions, and is the quantity
        reported in plant performance studies, permitting direct comparison.
      </p>

      {summary && (
        <>
          <div className="hero-figure">
            <div className="hero-figure__value">
              {summary.nInBand}/{summary.n}
            </div>
            <div className="hero-figure__label">
              city-years inside the published band of {summary.band[0]}–{summary.band[1]}{' '}
              kWh/kWp/yr
            </div>
          </div>

          <div className="grid grid--4">
            {[
              ['Mean', summary.mean, 'kWh/kWp/yr'],
              ['Median', summary.median, 'kWh/kWp/yr'],
              ['Range', null, `${fmt.num(summary.min)}–${fmt.num(summary.max)}`],
              ['Std dev', summary.std, 'kWh/kWp/yr'],
            ].map(([label, value, unit]) => (
              <div className="card" key={label}>
                <div className="stat">
                  <div className="stat__value">
                    {value != null ? fmt.num(value) : unit}
                  </div>
                  <div className="stat__label">
                    {label} {value != null ? unit : ''}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      <Figure
        title="Every city-year against the band"
        caption="Model specific yield per city-year against the band reported in Indian rooftop plant studies (1000–1750 kWh/kWp/yr). The band spans measured installations of differing tilt, maintenance regime and vintage, so it is a plausibility interval rather than a tolerance."
        tall
        columns={[
          { key: 'city', label: 'City' },
          { key: 'year', label: 'Year', numeric: true },
          { key: 'reference_ghi_kwh_m2', label: 'Ref GHI', numeric: true },
          {
            key: 'reference_beam_fraction',
            label: 'Beam',
            numeric: true,
            render: (r) => fmt.fixed(r.reference_beam_fraction, 3),
          },
          {
            key: 'net_retention',
            label: 'Retention',
            numeric: true,
            render: (r) => fmt.fixed(r.net_retention, 3),
          },
          {
            key: 'specific_yield_kwh_per_kwp_yr',
            label: 'kWh/kWp/yr',
            numeric: true,
            render: (r) => fmt.num(r.specific_yield_kwh_per_kwp_yr),
          },
          {
            key: 'in_published_band',
            label: 'In band',
            render: (r) => (r.in_published_band ? 'yes' : 'no'),
          },
        ]}
        rows={rows.map((r, i) => ({ key: i, ...r }))}
      >
        <YieldBandChart />
      </Figure>

      <Figure
        title="By city"
        caption="One panel per city, shared vertical scale. Between-city variation is driven primarily by annual irradiance and cloud regime; Guwahati and Kolkata sit lowest at 1543–1654 kWh/m²/yr of resource, Jodhpur highest at 1982–2002."
        columns={[
          { key: 'city', label: 'City' },
          { key: 'year', label: 'Year', numeric: true },
          {
            key: 'specific_yield_kwh_per_kwp_yr',
            label: 'kWh/kWp/yr',
            numeric: true,
            render: (r) => fmt.num(r.specific_yield_kwh_per_kwp_yr),
          },
        ]}
        rows={rows.map((r, i) => ({ key: i, ...r }))}
      >
        <CityMultiples />
      </Figure>

      <div className="callout callout--caution">
        <div className="callout__title">Interpretation of the band result</div>
        <p>
          Against the best-documented single measurement — a 12 kWp Delhi rooftop plant at
          1147 kWh/kWp/yr — the model gives 1256 for Delhi 2020, about{' '}
          <strong>+8%</strong>. Two things cut against reading that as an accuracy claim.
          That plant reports a performance ratio of 0.85–0.93, above the typical Indian
          year-one range of 0.78–0.83; and it is a <em>tilted</em> array, while this model
          treats every roof as horizontal. Tilt is worth{' '}
          {pvlib ? `${fmt.fixed(pvlib.tilt_benefit_pct, 1)}%` : '8–12%'} at Delhi — the
          same size as the discrepancy, in the same direction.
        </p>
        <p>
          So +8% against a tilted, well-maintained plant is consistent with the model being
          approximately right. It is not evidence of 8% accuracy; the band is the
          defensible claim.
        </p>
      </div>

      <h2>Beam fraction</h2>
      {beam && (
        <>
          <p>
            The most ERA5-sensitive input in the model. ERA5 uses a monthly aerosol
            climatology and is documented to overestimate direct radiation with the error
            growing in aerosol load — which is exactly India&apos;s regime.
          </p>
          <div className="grid grid--3">
            <div className="card">
              <div className="stat">
                <div className="stat__value">
                  {fmt.fixed(beam.reference_beam_fraction_mean, 3)}
                </div>
                <div className="stat__label">Reference mean, {beam.n_city_years} city-years</div>
                <p className="stat__note">
                  Range {fmt.fixed(beam.reference_beam_fraction_min, 3)}–
                  {fmt.fixed(beam.reference_beam_fraction_max, 3)}
                </p>
              </div>
            </div>
            <div className="card">
              <div className="stat">
                <div className="stat__value">{beam.era5_fallback_used_by_model}</div>
                <div className="stat__label">What the model assumed</div>
                <p className="stat__note">
                  Measurably high, and the motivation for the learned model.
                </p>
              </div>
            </div>
            <div className="card">
              <div className="stat">
                <div className="stat__value">
                  {fmt.signed(
                    beam.era5_fallback_used_by_model - beam.reference_beam_fraction_mean,
                    3,
                  )}
                </div>
                <div className="stat__label">Bias in the old constant</div>
                <p className="stat__note">
                  <Link to="/models">How it was replaced →</Link>
                </p>
              </div>
            </div>
          </div>
          <p className="source-note">{beam.note}</p>
        </>
      )}

      <h2>Physics engine, against pvlib</h2>
      {pvlib && (
        <>
          <p>
            The transposition and cell-temperature models are pvlib&apos;s, not
            hand-rolled. The 288-record monthly-diurnal climatology is a real
            approximation, so its error against full hourly data is measured rather than
            assumed adequate.
          </p>
          <Figure
            title="Discretisation error: 288 records against a full year"
            caption="Annual irradiance from the 288-record monthly-diurnal climatology against a full daily series. Transposition and cell-temperature models are non-linear in instantaneous irradiance and cannot be applied to an annual total, so some temporal resolution is required; this quantifies the error introduced by using 288 records rather than 8760."
            columns={[
              { key: 'city', label: 'City' },
              {
                key: 'climatology_288_step_kwh_m2',
                label: '288-step',
                numeric: true,
                render: (r) => fmt.num(r.climatology_288_step_kwh_m2),
              },
              {
                key: 'full_year_daily_kwh_m2',
                label: 'Full year',
                numeric: true,
                render: (r) => fmt.num(r.full_year_daily_kwh_m2),
              },
              {
                key: 'error_pct',
                label: 'Error',
                numeric: true,
                render: (r) => `${r.error_pct > 0 ? '+' : ''}${r.error_pct}%`,
              },
            ]}
            rows={(pvlib.climatology_discretisation ?? []).map((r, i) => ({ key: i, ...r }))}
          >
            <DiscretisationChart rows={pvlib.climatology_discretisation ?? []} />
          </Figure>
        </>
      )}

      <h2>Soiling</h2>
      {soiling && (
        <>
          <p>
            Validated three ways: against published seasonal measurements, against an
            independent implementation of the same published model, and against the model
            it replaces.
          </p>
          <div className="grid grid--3">
            <div className="card">
              <div className="stat">
                <div className="stat__value">
                  {fmt.pct(soiling.pvlib?.worst_absolute_disagreement, 3)}
                </div>
                <div className="stat__label">Worst disagreement with pvlib</div>
                <p className="stat__note">
                  Fraction points of loss, across all ten cities.
                </p>
              </div>
            </div>
            <div className="card">
              <div className="stat">
                <div className="stat__value">
                  {soiling.seasonal_ordering_correct ? 'correct' : 'WRONG'}
                </div>
                <div className="stat__label">Seasonal ordering</div>
                <p className="stat__note">
                  Spring worst, monsoon best — which matters more than the magnitudes.
                </p>
              </div>
            </div>
            <div className="card">
              <div className="stat">
                <div className="stat__value">{soiling.window_spread_x}×</div>
                <div className="stat__label">Spread across Delhi&apos;s months</div>
              </div>
            </div>
          </div>

          <Figure
            title="Predicted dry-day rates against published measurements"
            caption="Predicted against measured dry-day soiling rates for Delhi. The winter value is the calibration anchor and is not an independent test; spring and monsoon are predictions from aerosol loading alone and agree within 3%."
            columns={[
              { key: 'season', label: 'Season' },
              { key: 'assumed_aod', label: 'Assumed AOD', numeric: true },
              {
                key: 'predicted_rate_per_day',
                label: 'Predicted %/day',
                numeric: true,
                render: (r) => fmt.fixed(r.predicted_rate_per_day * 100, 3),
              },
              {
                key: 'measured_rate_per_day',
                label: 'Measured %/day',
                numeric: true,
                render: (r) => fmt.fixed(r.measured_rate_per_day * 100, 3),
              },
              {
                key: 'relative_error',
                label: 'Error',
                numeric: true,
                render: (r) => `${(r.relative_error * 100).toFixed(1)}%`,
              },
            ]}
            rows={(soiling.seasonal ?? []).map((r, i) => ({ key: i, ...r }))}
          >
            <SeasonalChart rows={soiling.seasonal ?? []} />
          </Figure>

          <div className="callout callout--resolved">
            <div className="callout__title">Errors identified by the cross-check</div>
            <p>
              The cleaning day was counted as a dry day. Excluding it then also excluded
              those days from the time average, which made the disagreement worse — that is
              how it was caught. And accumulation was integrated continuously where the
              model is defined per day. None was visible in isolation.
            </p>
          </div>
        </>
      )}

      <h2>Limits of this validation</h2>
      <ul className="prose-list">
        <li>
          <strong>No ground truth.</strong> No pyranometer and no metered plant are in the
          loop. Every reference is satellite-derived or a published aggregate.
        </li>
        <li>
          <strong>No per-building verification.</strong> The 4 m geometry is never checked
          against a surveyed roof. Validation is at city scale.
        </li>
        <li>
          <strong>Accuracy is bounded at ~{spread?.toFixed(0)}%</strong> by the reference
          disagreement, regardless of how well the model matches any single source.
        </li>
        <li>
          <strong>The decomposition model is trained on NASA POWER</strong>, so it cannot
          be validated against NASA POWER. Independent validation would need Global Solar
          Atlas or ground stations as a held-out reference.
        </li>
        <li>
          <strong>The soiling calibration rests on one assumed aerosol value.</strong> The
          rate is measured; the AOD it is anchored on is a literature figure, not a
          retrieval from this pipeline.
        </li>
      </ul>
      <p>
        More on all of these, plus the ten defects this process found, on{' '}
        <Link to="/limitations">Limitations</Link>.
      </p>
    </div>
  );
}

function DiscretisationChart({ rows }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current || !rows.length) return undefined;
    const p = palette();
    const chart = new Chart(ref.current, {
      type: 'bar',
      data: {
        labels: rows.map((r) => r.city),
        datasets: [
          {
            data: rows.map((r) => r.error_pct),
            backgroundColor: rows.map((r) => (Math.abs(r.error_pct) > 5 ? p.warn : p.sky)),
            borderWidth: 0,
          },
        ],
      },
      options: baseOptions({ yTitle: 'Error vs full hourly (%)' }),
    });
    return () => chart.destroy();
  }, [rows]);
  return <canvas ref={ref} />;
}

function SeasonalChart({ rows }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current || !rows.length) return undefined;
    const p = palette();
    const chart = new Chart(ref.current, {
      type: 'bar',
      data: {
        labels: rows.map((r) => r.season),
        datasets: [
          {
            label: 'Predicted',
            data: rows.map((r) => r.predicted_rate_per_day * 100),
            backgroundColor: p.accent,
            borderWidth: 0,
          },
          {
            label: 'Measured',
            data: rows.map((r) => r.measured_rate_per_day * 100),
            backgroundColor: p.sky,
            borderWidth: 0,
          },
        ],
      },
      options: baseOptions({ showLegend: true, yTitle: 'Soiling rate (%/day)' }),
    });
    return () => chart.destroy();
  }, [rows]);
  return <canvas ref={ref} />;
}
