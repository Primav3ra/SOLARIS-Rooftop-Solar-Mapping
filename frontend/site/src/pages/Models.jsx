import { useEffect, useRef } from 'react';
import { Link } from 'react-router-dom';
import Figure from '../components/Figure.jsx';
import { Chart, baseOptions, palette } from '../components/charts.js';
import { decomposition, manifest, soiling, trackB, fmt } from '../data/reports.js';

const ladder = decomposition.scores ?? [];
const gate = decomposition.gate ?? {};
const split = decomposition.split ?? {};

/**
 * The ladder, as skill over the published baseline.
 *
 * Plotted as skill rather than RMSE because skill is the quantity the decision
 * was made on, and because it puts the baseline at zero -- so "does this beat
 * the published correlation" is a question about which side of the axis a bar
 * is on, rather than a subtraction the reader has to do.
 */
function LadderChart() {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current || !ladder.length) return undefined;
    const p = palette();
    const chart = new Chart(ref.current, {
      type: 'bar',
      data: {
        labels: ladder.map((s) => s.name),
        datasets: [
          {
            data: ladder.map((s) => s.skill_vs_erbs ?? 0),
            backgroundColor: ladder.map((s) => {
              if (s.name === decomposition.winner) return p.accent;
              if (s.name === 'erbs') return p.sky;
              return (s.skill_vs_erbs ?? 0) < 0 ? p.grid : p.dim;
            }),
            borderWidth: 0,
          },
        ],
      },
      options: {
        ...baseOptions({ yTitle: 'Skill over Erbs (1982)' }),
        plugins: {
          ...baseOptions().plugins,
          tooltip: {
            ...baseOptions().plugins.tooltip,
            callbacks: {
              label: (item) => {
                const row = ladder[item.dataIndex];
                return [
                  `skill ${fmt.signed(row.skill_vs_erbs, 3)}`,
                  `RMSE ${fmt.fixed(row.rmse, 4)}`,
                  `bias ${fmt.signed(row.mbe, 3)}`,
                ];
              },
            },
          },
        },
      },
    });
    return () => chart.destroy();
  }, []);
  return <canvas ref={ref} />;
}

/**
 * Bias, in its own panel.
 *
 * Deliberately not on the same chart as skill. Value and error never share an
 * axis here -- a second y-axis lets a reader infer a relationship from a
 * scaling choice that the data does not support.
 */
function BiasChart() {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current || !ladder.length) return undefined;
    const p = palette();
    const chart = new Chart(ref.current, {
      type: 'bar',
      data: {
        labels: ladder.map((s) => s.name),
        datasets: [
          {
            data: ladder.map((s) => s.mbe ?? 0),
            backgroundColor: ladder.map((s) =>
              Math.abs(s.mbe ?? 0) > 0.05 ? p.bad : p.ok,
            ),
            borderWidth: 0,
          },
        ],
      },
      options: baseOptions({ yTitle: 'Mean bias error' }),
    });
    return () => chart.destroy();
  }, []);
  return <canvas ref={ref} />;
}

export default function Models() {
  return (
    <div className="page">
      <h1>Machine learning</h1>
      <p className="lede">
        Three model tracks were specified. Two were implemented; one was discontinued
        after profiling established an upper bound on its benefit. All three are
        documented, including the measurements behind the discontinuation.
      </p>

      <div className="callout">
        <div className="callout__title">Selection criteria</div>
        <p>
          A model must beat the published method, not the naive one — beating a constant
          proves nothing. The skill gate is declared in the module above the training code,
          before training, so it cannot be relaxed to suit the result.
        </p>
      </div>

      {/* ------------------------------------------------------------------ */}
      <h2>Track A — beam/diffuse decomposition</h2>

      <div className="grid grid--2">
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Measured baseline error</h3>
          <p>
            The beam fraction is the model&apos;s most ERA5-sensitive input. The validation
            harness put the reference mean at <strong>0.540</strong> across 30 city-years
            while production fell back to a constant <strong>0.60</strong>. ERA5 uses a
            monthly aerosol climatology and over-predicts the direct component, with the
            error growing in aerosol load — India&apos;s regime exactly.
          </p>
          <p style={{ marginBottom: 0 }}>
            <Link to="/validation">See the measurement →</Link>
          </p>
        </div>
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Deviation from the specification</h3>
          <p>
            The plan was an ERA5 → reference bias correction, which needs Earth Engine
            credentials that were unavailable. The reference side targets the same defect
            from the other end: learn diffuse fraction from cheap, always-available
            features.
          </p>
          <p style={{ marginBottom: 0 }}>
            It also produces the target series an ERA5 correction would train against.
          </p>
        </div>
      </div>

      {manifest && (
        <div className="grid grid--4" style={{ marginTop: '1.2rem' }}>
          <div className="card">
            <div className="stat">
              <div className="stat__value">{fmt.signed(manifest.skill_vs_erbs, 3)}</div>
              <div className="stat__label">Skill over Erbs</div>
              <p className="stat__note">Gate was {gate.min_skill_over_erbs}</p>
            </div>
          </div>
          <div className="card">
            <div className="stat">
              <div className="stat__value">{fmt.fixed(manifest.test_rmse, 4)}</div>
              <div className="stat__label">Test RMSE</div>
            </div>
          </div>
          <div className="card">
            <div className="stat">
              <div className="stat__value">{fmt.num(manifest.n_train)}</div>
              <div className="stat__label">Training samples</div>
              <p className="stat__note">{fmt.num(manifest.n_test)} held out</p>
            </div>
          </div>
          <div className="card">
            <div className="stat">
              <div className="stat__value" style={{ fontSize: '1.1rem' }}>
                {manifest.kind}
              </div>
              <div className="stat__label">Shipped model</div>
              <p className="stat__note">v{manifest.version}</p>
            </div>
          </div>
        </div>
      )}

      <Figure
        title="Candidate models, ranked by skill against Erbs (1982)"
        caption="Skill is 1 − RMSE/RMSE_erbs on the holdout. Erbs is the baseline rather than the production constant: the constant scores −0.78, so improvement over it does not establish that a learned model adds information. Negative skill indicates a rung that performs worse than the published correlation."
        columns={[
          { key: 'name', label: 'Rung' },
          { key: 'n', label: 'n', numeric: true, render: (r) => fmt.num(r.n) },
          { key: 'rmse', label: 'RMSE', numeric: true, render: (r) => fmt.fixed(r.rmse, 4) },
          {
            key: 'rmse_ghi_weighted',
            label: 'GHI-weighted',
            numeric: true,
            render: (r) => (r.rmse_ghi_weighted == null ? '--' : fmt.fixed(r.rmse_ghi_weighted, 4)),
          },
          { key: 'mae', label: 'MAE', numeric: true, render: (r) => fmt.fixed(r.mae, 4) },
          { key: 'mbe', label: 'Bias', numeric: true, render: (r) => fmt.signed(r.mbe, 4) },
          {
            key: 'skill_vs_erbs',
            label: 'Skill',
            numeric: true,
            render: (r) => (r.skill_vs_erbs == null ? '--' : fmt.signed(r.skill_vs_erbs, 4)),
          },
        ]}
        rows={ladder.map((r, i) => ({ key: i, ...r }))}
      >
        <LadderChart />
      </Figure>

      <Figure
        title="Mean bias error by candidate"
        caption="Bias is plotted separately from skill; the two are not combined on one axis. The production constant carries −0.139 in diffuse fraction. Erbs carries +0.077 over this reference set, over-predicting diffuse fraction — consistent with a correlation fitted on United States aerosol conditions applied to Indian loading. The selected model reduces this to +0.015."
        columns={[
          { key: 'name', label: 'Rung' },
          { key: 'mbe', label: 'Mean bias error', numeric: true, render: (r) => fmt.signed(r.mbe, 4) },
        ]}
        rows={ladder.map((r, i) => ({ key: i, ...r }))}
      >
        <BiasChart />
      </Figure>

      <div className="grid grid--2">
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Holdout construction</h3>
          <p>
            {split.n_test != null && (
              <>
                {fmt.num(split.n_train)} training samples against {fmt.num(split.n_test)}{' '}
                held out, from cities <code>{(split.test_cities ?? []).join(', ')}</code> in
                year {(split.test_years ?? []).join(', ')}.
              </>
            )}
          </p>
          <p style={{ marginBottom: 0 }}>
            Hours within a city-day are strongly correlated, so a random row split would
            test on hours whose neighbours were trained on. Closest test/train site pair
            under 250 km: <strong>{decomposition.spatial_leakage || 'none'}</strong>.
          </p>
        </div>
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Fallback chain</h3>
          <p>
            The chain is <code>model → Erbs → constant</code>, and the response{' '}
            <strong>always reports which rung answered</strong>. Erbs sits in the middle
            deliberately: it needs no artifact and no training data, so it is always
            available — which makes the constant a genuine last resort rather than the
            first fallback.
          </p>
          <p style={{ marginBottom: 0 }}>
            A domain check guards the learned rung: a boosted tree does not extrapolate, it
            returns the nearest leaf. Out-of-domain inputs fall through to Erbs, verified at
            latitude 75°N.
          </p>
        </div>
      </div>

      <div className="callout callout--resolved">
        <div className="callout__title">Selection outcome</div>
        <p>
          {decomposition.reason} The threshold was declared {gate.declared}.
        </p>
      </div>

      {/* ------------------------------------------------------------------ */}
      <h2>Track C — soiling</h2>
      <p>
        <strong>Reframed from ML to parametric.</strong> There is no open Indian
        PV-soiling label set, so training end to end would be a curve fit rather than a
        model. This uses the published Kimber model — which the previous code already cited
        while hand-rolling something else — calibrated against measured Indian rates.
      </p>
      <p>
        Detail on <Link to="/method">Method</Link>, validation on{' '}
        <Link to="/validation">Validation</Link>.
      </p>

      {soiling?.per_city?.length > 0 && (
        <Figure
          title="Annual soiling by city: new model against old"
          caption="Annual soiling loss per city under rain-only cleaning, with the superseded aerosol-only model for comparison. Note the longest-dry-spell column: Jodhpur and Ahmedabad record 125 and 135 consecutive days without cleaning-grade rainfall against mean spells of 12.0 and 13.5 days. Because accumulation is convex in spell length, the revised model returns higher loss than the aerosol-only model at these two sites despite their lower mean aerosol loading."
          tall
          columns={[
            { key: 'city', label: 'City' },
            { key: 'aod', label: 'AOD', numeric: true },
            { key: 'cleaning_rain_days', label: 'Rain days', numeric: true },
            { key: 'longest_dry_spell_days', label: 'Longest spell', numeric: true, render: (r) => `${r.longest_dry_spell_days}d` },
            { key: 'loss_rain_only', label: 'New', numeric: true, render: (r) => fmt.pct(r.loss_rain_only, 2) },
            { key: 'loss_with_30d_cleaning', label: 'New, 30d wash', numeric: true, render: (r) => fmt.pct(r.loss_with_30d_cleaning, 2) },
            { key: 'loss_legacy_model', label: 'Old', numeric: true, render: (r) => fmt.pct(r.loss_legacy_model, 2) },
            { key: 'pvlib_kimber_loss', label: 'pvlib', numeric: true, render: (r) => fmt.pct(r.pvlib_kimber_loss, 2) },
          ]}
          rows={soiling.per_city.map((r, i) => ({ key: i, ...r }))}
        >
          <SoilingCityChart rows={soiling.per_city} />
        </Figure>
      )}

      {soiling?.mean_spell_understatement && (
        <div className="callout callout--caution">
          <div className="callout__title">Degraded-input behaviour</div>
          <p>
            Without a daily rainfall series the model must assume every dry spell is the
            mean length. Measured against real series, that understates annual soiling by{' '}
            <strong>
              {soiling.mean_spell_understatement.min_x}× to{' '}
              {soiling.mean_spell_understatement.max_x}×
            </strong>{' '}
            — and worst in the arid cities where soiling matters most, because accumulation
            is convex in spell length.
          </p>
          <p>
            That is why the serving path spends one extra Earth Engine round-trip on the
            actual series, and why results from the fallback path carry an explicit note
            calling themselves a lower bound.
          </p>
        </div>
      )}

      {/* ------------------------------------------------------------------ */}
      <h2>Track B — measured, then dropped</h2>
      {trackB && (
        <>
          <p>
            A shadow/sky-view surrogate, specified as a speed optimisation rather than an
            accuracy one, and gated on profiling <code>/api/yield</code> first.
          </p>

          <div className="grid grid--4">
            <div className="card">
              <div className="stat">
                <div className="stat__value">
                  {trackB.surrogate_ceiling?.total_round_trips}
                </div>
                <div className="stat__label">Round-trips per query</div>
              </div>
            </div>
            <div className="card">
              <div className="stat">
                <div className="stat__value">
                  {trackB.surrogate_ceiling?.shadow_attributable_round_trips}
                </div>
                <div className="stat__label">Involving shadow compute</div>
              </div>
            </div>
            <div className="card">
              <div className="stat">
                <div className="stat__value">
                  {trackB.surrogate_ceiling?.round_trip_reduction}
                </div>
                <div className="stat__label">Round-trips a surrogate would save</div>
              </div>
            </div>
            <div className="card">
              <div className="stat">
                <div className="stat__value">
                  {trackB.surrogate_ceiling?.best_possible_speedup_if_compute_bound}×
                </div>
                <div className="stat__label">Ceiling on the speedup</div>
              </div>
            </div>
          </div>

          <h3>Basis for discontinuation</h3>
          <ul className="prose-list">
            {(trackB.decision?.reasons ?? []).map((reason, i) => (
              <li key={i}>{reason}</li>
            ))}
          </ul>

          <div className="callout">
            <div className="callout__title">Conditions that would warrant revisiting</div>
            <p>{trackB.decision?.what_would_change_this}</p>
          </div>
        </>
      )}

      <h2>Reproduction</h2>
      <pre>
        <code>{`python -m solaris.evals.fetch --hourly --years 2020 2021 2022
python -m solaris.ml.train
python -m solaris.evals.soiling_suite
python -m solaris.evals.profile_yield`}</code>
      </pre>
      <p className="source-note">
        Reports land in <code>ml/reports/</code> and <code>evals/reports/</code>. This page
        imports them at build time, so it cannot drift from the committed results.
      </p>
    </div>
  );
}

function SoilingCityChart({ rows }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current || !rows.length) return undefined;
    const p = palette();
    const sorted = [...rows].sort((a, b) => b.loss_rain_only - a.loss_rain_only);
    const chart = new Chart(ref.current, {
      type: 'bar',
      data: {
        labels: sorted.map((r) => r.city),
        datasets: [
          {
            label: 'Kimber, rain-aware',
            data: sorted.map((r) => r.loss_rain_only * 100),
            backgroundColor: p.accent,
            borderWidth: 0,
          },
          {
            label: 'Previous AOD-only model',
            data: sorted.map((r) => r.loss_legacy_model * 100),
            backgroundColor: p.grid,
            borderWidth: 0,
          },
        ],
      },
      options: baseOptions({ showLegend: true, yTitle: 'Annual soiling loss (%)' }),
    });
    return () => chart.destroy();
  }, [rows]);
  return <canvas ref={ref} />;
}
