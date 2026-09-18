import { useEffect, useRef } from 'react';
import Figure from './Figure.jsx';
import { Chart, baseOptions, palette, categorical } from './charts.js';
import { fmt } from '../data/reports.js';

function severityBadge(quality) {
  const severity = quality?.severity ?? 'unknown';
  const map = { ok: 'badge--ok', degraded: 'badge--warn', unreliable: 'badge--bad' };
  return <span className={`badge ${map[severity] ?? ''}`}>{severity}</span>;
}

/**
 * Part-to-whole across four stages with long labels: a horizontal stacked bar.
 *
 * Not a pie. Four wedges are readable but a pie makes comparing two of them
 * hard, and the question a reader has here is "which one dominates and by how
 * much", which is a length comparison.
 */
function ContributionChart({ contribution }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current || !contribution) return undefined;
    const colours = categorical();
    const entries = [
      ['Shadow', contribution.shadow_contribution_pct],
      ['Sky view', contribution.svf_contribution_pct],
      ['Heat island', contribution.uhi_contribution_pct],
      ['Soiling', contribution.soiling_contribution_pct],
    ];
    const chart = new Chart(ref.current, {
      type: 'bar',
      data: {
        labels: ['Share of modelled loss'],
        datasets: entries.map(([label, value], i) => ({
          label,
          data: [value ?? 0],
          backgroundColor: colours[i % colours.length],
          borderWidth: 0,
        })),
      },
      options: {
        ...baseOptions({ showLegend: true }),
        indexAxis: 'y',
        scales: {
          x: { stacked: true, max: 100, ticks: { color: palette().dim, callback: (v) => `${v}%` }, grid: { color: palette().grid } },
          y: { stacked: true, ticks: { display: false }, grid: { display: false } },
        },
      },
    });
    return () => chart.destroy();
  }, [contribution]);
  return <canvas ref={ref} />;
}

/**
 * The stage cascade: baseline to net, with each delta direct-labelled.
 *
 * Four stages, so direct labels rather than a legend -- at this count a legend
 * makes the reader look away from the data to decode it.
 */
function CascadeChart({ result }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current || !result) return undefined;
    const p = palette();
    const stages = [
      ['Baseline', result.baseline_yield_kwh],
      ['After shadow', result.after_shadow_yield_kwh],
      ['After sky view', result.after_svf_yield_kwh],
      ['After heat', result.after_uhi_yield_kwh],
      ['Net', result.after_soiling_yield_kwh],
    ];
    const chart = new Chart(ref.current, {
      type: 'bar',
      data: {
        labels: stages.map(([label]) => label),
        datasets: [
          {
            data: stages.map(([, value]) => value),
            backgroundColor: stages.map((_, i) =>
              i === 0 ? p.dim : i === stages.length - 1 ? p.accent : p.sky,
            ),
            borderWidth: 0,
          },
        ],
      },
      options: {
        ...baseOptions({ yTitle: 'kWh over the window' }),
        plugins: {
          ...baseOptions().plugins,
          tooltip: {
            ...baseOptions().plugins.tooltip,
            callbacks: {
              label: (item) => {
                const baseline = stages[0][1];
                const drop = ((baseline - item.raw) / baseline) * 100;
                return `${fmt.num(item.raw)} kWh  (−${drop.toFixed(2)}% from baseline)`;
              },
            },
          },
        },
      },
    });
    return () => chart.destroy();
  }, [result]);
  return <canvas ref={ref} />;
}

function ShadeChart({ intervals }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current || !intervals?.length) return undefined;
    const p = palette();
    const chart = new Chart(ref.current, {
      type: 'bar',
      data: {
        labels: intervals.map((i) => i.label_ist ?? i.label),
        datasets: [
          {
            data: intervals.map((i) => (i.shade_fraction ?? 0) * 100),
            backgroundColor: intervals.map((i) =>
              i.sun_above_horizon === false ? p.grid : p.accent,
            ),
            borderWidth: 0,
          },
        ],
      },
      options: baseOptions({ yTitle: 'Roof in shade (%)' }),
    });
    return () => chart.destroy();
  }, [intervals]);
  return <canvas ref={ref} />;
}

export default function ResultPanel({ result, series, error, busy, meta, onRetry }) {
  if (error) {
    const title = error.allowanceExhausted
      ? 'Session allowance reached'
      : error.isConfiguration
        ? 'Earth Engine is not configured on this server'
        : 'Request did not complete';

    return (
      <div className="page">
        <div className={`callout ${error.allowanceExhausted ? '' : 'callout--caution'}`}>
          <div className="callout__title">{title}</div>
          <p>{error.message}</p>

          {/* A configuration problem is not a fault to retry. Showing the
              server's own hint is the only useful thing here, and it is why
              the API distinguishes 503-with-a-hint from a generic 500. */}
          {error.hint && (
            <p style={{ fontSize: '0.9rem' }}>
              <strong>What to do:</strong> {error.hint}
            </p>
          )}

          {error.isConfiguration && (
            <p style={{ fontSize: '0.9rem', color: 'var(--text-dim)' }}>
              Every other page on this site works without Earth Engine — the
              validation and model results are read from committed artifacts, so
              they need no credentials at all.
            </p>
          )}

          {error.requestId && (
            <p style={{ fontSize: '0.85rem', color: 'var(--text-faint)' }}>
              Request id <code>{error.requestId}</code>
            </p>
          )}

          {error.isTransient && (
            <button className="btn" onClick={onRetry}>
              Try again
            </button>
          )}
        </div>
      </div>
    );
  }

  if (!result) {
    return (
      <div className="page">
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Nothing computed yet</h3>
          <p style={{ marginBottom: 0 }}>
            Pick a point and a window, then press <strong>Compute potential</strong>. Each
            run takes a few seconds — the work happens on Google&apos;s servers and comes
            back as a dozen small round-trips.
          </p>
        </div>
      </div>
    );
  }

  const quality = result.data_quality;
  const contribution = result.penalty_contribution;
  const soilingPenalty = result.soiling_penalty;

  return (
    <div className="page">
      <div
        style={{
          display: 'flex',
          alignItems: 'baseline',
          gap: '0.8rem',
          flexWrap: 'wrap',
          marginBottom: '0.5rem',
        }}
      >
        <h2 style={{ margin: 0 }}>Result</h2>
        {severityBadge(quality)}
        {meta.cache && <span className="badge">cache {meta.cache.toLowerCase()}</span>}
        <span className="badge">
          {result.start_date} → {result.end_date_exclusive}
        </span>
      </div>

      <div className="hero-figure">
        <div className="hero-figure__value">{fmt.num(result.period_yield_kwh)}</div>
        <div className="hero-figure__label">
          kWh over the window, across {fmt.num(result.roof_area_m2)} m² of candidate roof
        </div>
      </div>

      <div className="grid grid--4">
        <div className="card">
          <div className="stat">
            <div className="stat__value">{fmt.pct(1 - result.combined_derate_factor, 2)}</div>
            <div className="stat__label">Heat + soiling loss</div>
          </div>
        </div>
        <div className="card">
          <div className="stat">
            <div className="stat__value">{fmt.pct(result.mean_shadow_fraction, 1)}</div>
            <div className="stat__label">Mean roof in shadow</div>
          </div>
        </div>
        <div className="card">
          <div className="stat">
            <div className="stat__value">{fmt.fixed(result.mean_sky_view_factor, 3)}</div>
            <div className="stat__label">Sky view factor</div>
            <p className="stat__note">1.0 is an unobstructed horizon.</p>
          </div>
        </div>
        <div className="card">
          <div className="stat">
            <div className="stat__value">{fmt.fixed(result.beam_fraction, 3)}</div>
            <div className="stat__label">Beam fraction</div>
            <p className="stat__note">from {result.beam_fraction_source}</p>
          </div>
        </div>
      </div>

      {/* Data quality first, before the charts. A degraded result that looks
          like a confident one is the failure mode this whole project has been
          unpicking. */}
      {quality?.findings?.length > 0 && (
        <div className="callout callout--caution">
          <div className="callout__title">
            {quality.findings.length} input{quality.findings.length === 1 ? '' : 's'} came
            from a fallback
          </div>
          <ul className="prose-list">
            {quality.findings.map((finding, i) => (
              <li key={i}>
                <strong>{finding.field ?? finding.source ?? 'input'}</strong> —{' '}
                {finding.explanation ?? finding.message}
              </li>
            ))}
          </ul>
        </div>
      )}

      {quality?.coverage && quality.coverage.status !== 'complete' && (
        <div className="callout callout--caution">
          <div className="callout__title">Partial coverage</div>
          <p>
            {quality.coverage.days_with_data} of {quality.coverage.days_requested} days had
            data. The window probably runs past the reanalysis publication lag, so this
            figure covers less than you asked for — which is why it is shown rather than
            quietly returned as a smaller number.
          </p>
        </div>
      )}

      <div className="grid grid--2">
        <Figure
          title="Where the energy goes"
          caption="Each stage applied in turn, from the raw irradiance baseline to the net figure. Hover for the cumulative drop."
          columns={[
            { key: 'stage', label: 'Stage' },
            { key: 'kwh', label: 'kWh', numeric: true, render: (r) => fmt.num(r.kwh) },
          ]}
          rows={[
            { key: 'b', stage: 'Baseline', kwh: result.baseline_yield_kwh },
            { key: 's', stage: 'After shadow', kwh: result.after_shadow_yield_kwh },
            { key: 'v', stage: 'After sky view', kwh: result.after_svf_yield_kwh },
            { key: 'u', stage: 'After heat island', kwh: result.after_uhi_yield_kwh },
            { key: 'n', stage: 'Net', kwh: result.after_soiling_yield_kwh },
          ]}
        >
          <CascadeChart result={result} />
        </Figure>

        <Figure
          title="Which penalty dominates here"
          caption="Share of total modelled loss. This split varies a lot by city: geometry dominates in a dense core, soiling in an arid one."
          columns={[
            { key: 'name', label: 'Penalty' },
            { key: 'pct', label: 'Share', numeric: true, render: (r) => `${r.pct?.toFixed(1)}%` },
            { key: 'kwh', label: 'kWh lost', numeric: true, render: (r) => fmt.num(r.kwh) },
          ]}
          rows={[
            { key: 'sh', name: 'Shadow', pct: contribution?.shadow_contribution_pct, kwh: contribution?.shadow_loss_kwh },
            { key: 'sv', name: 'Sky view', pct: contribution?.svf_contribution_pct, kwh: contribution?.svf_loss_kwh },
            { key: 'uh', name: 'Heat island', pct: contribution?.uhi_contribution_pct, kwh: contribution?.uhi_loss_kwh },
            { key: 'so', name: 'Soiling', pct: contribution?.soiling_contribution_pct, kwh: contribution?.soiling_loss_kwh },
          ]}
        >
          <ContributionChart contribution={contribution} />
        </Figure>
      </div>

      {result.shade_intervals?.length > 0 && (
        <Figure
          title="Shade through the day"
          caption="Local time, not UTC. Greyed bars are intervals with no sun above the horizon — which is why they show no shade, rather than because the roof was sunlit."
          columns={[
            { key: 'label_ist', label: 'Interval (IST)' },
            { key: 'label_utc', label: 'UTC' },
            {
              key: 'shade_fraction',
              label: 'In shade',
              numeric: true,
              render: (r) => fmt.pct(r.shade_fraction, 1),
            },
          ]}
          rows={result.shade_intervals.map((interval, i) => ({ key: i, ...interval }))}
        >
          <ShadeChart intervals={result.shade_intervals} />
        </Figure>
      )}

      {soilingPenalty && (
        <div className="card" style={{ marginTop: '1.5rem' }}>
          <h3 style={{ marginTop: 0 }}>Soiling, in detail</h3>
          <div className="grid grid--4">
            <div className="stat">
              <div className="stat__value">{fmt.pct(soilingPenalty.soiling_loss_fraction, 2)}</div>
              <div className="stat__label">Soiling loss</div>
            </div>
            <div className="stat">
              <div className="stat__value">{soilingPenalty.cleaning_rain_days}</div>
              <div className="stat__label">Cleaning-rain days</div>
            </div>
            <div className="stat">
              <div className="stat__value">{fmt.fixed(soilingPenalty.mean_dry_spell_days, 1)}d</div>
              <div className="stat__label">Mean dry spell</div>
            </div>
            <div className="stat">
              <div className="stat__value">
                {fmt.pct(1 - (soilingPenalty.legacy_retention_for_comparison ?? 1), 2)}
              </div>
              <div className="stat__label">Previous model said</div>
            </div>
          </div>
          {soilingPenalty.notes?.length > 0 && (
            <ul className="prose-list" style={{ marginTop: '1rem', fontSize: '0.9rem', color: 'var(--text-dim)' }}>
              {soilingPenalty.notes.map((note, i) => (
                <li key={i}>{note}</li>
              ))}
            </ul>
          )}
          <p className="source-note">
            Rainfall from {soilingPenalty.rainfall_source}, dry spells computed by the{' '}
            <code>{soilingPenalty.method}</code> path. Model:{' '}
            <code>{soilingPenalty.model}</code>.
          </p>
        </div>
      )}

      {series?.points?.length > 0 && (
        <SeriesFigure series={series} />
      )}

      <p className="source-note">
        This result is reproducible by link — the URL carries the query. Request id and
        cache status are in the response headers.
      </p>
    </div>
  );
}

function SeriesFigure({ series }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current || !series?.points?.length) return undefined;
    const p = palette();
    const chart = new Chart(ref.current, {
      type: 'line',
      data: {
        labels: series.points.map((pt) => pt.label ?? pt.period),
        datasets: [
          {
            label: 'Net yield',
            data: series.points.map((pt) => pt.yield_kwh ?? pt.period_yield_kwh),
            borderColor: p.accent,
            backgroundColor: `color-mix(in srgb, ${p.accent} 18%, transparent)`,
            fill: true,
            tension: 0.28,
            pointRadius: 3,
          },
        ],
      },
      options: baseOptions({ yTitle: 'kWh' }),
    });
    return () => chart.destroy();
  }, [series]);

  return (
    <Figure
      title="Generation across the window"
      caption="One request rather than one per point — the sub-period reductions are batched server-side."
      columns={[
        { key: 'label', label: 'Period' },
        {
          key: 'yield_kwh',
          label: 'kWh',
          numeric: true,
          render: (r) => fmt.num(r.yield_kwh ?? r.period_yield_kwh),
        },
      ]}
      rows={series.points.map((pt, i) => ({ key: i, ...pt }))}
    >
      <canvas ref={ref} />
    </Figure>
  );
}
