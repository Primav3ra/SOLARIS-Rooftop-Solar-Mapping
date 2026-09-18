import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import Figure from '../components/Figure.jsx';
import { Chart, baseOptions, palette } from '../components/charts.js';
import { BudgetBar, LossWaterfall, LossRanking, lossBreakdown } from '../components/LossWaterfall.jsx';
import { fmt, pvlibSuite, soiling } from '../data/reports.js';

const pvlib = pvlibSuite();

/**
 * The physics chain, recomputed in the browser.
 *
 * Pure arithmetic, no API call — which is what makes the sliders worth having.
 * A reader can interrogate the sensitivity of the model directly instead of
 * taking a number on trust, and it costs no satellite quota to do so.
 *
 * The chain mirrors `solaris.gee.penalties.net_irradiance_image` and the PV
 * conversion in `solaris.physics`. It is a *teaching* version: the real one
 * works per pixel over a raster, so these figures are for one representative
 * roof rather than an area.
 */
function computeChain(inputs) {
  const {
    ghi,
    beamFraction,
    shadowFrequency,
    skyViewFactor,
    deltaTAir,
    soilingLoss,
    tempCoeff,
    packingFactor,
    panelEfficiency,
    performanceRatio,
    roofArea,
  } = inputs;

  const diffuseFraction = 1 - beamFraction;
  const beamKept = beamFraction * (1 - shadowFrequency);
  const diffuseKept = diffuseFraction * skyViewFactor;
  const geometricRetention = beamKept + diffuseKept;

  const afterGeometry = ghi * geometricRetention;
  const uhiDerate = 1 + tempCoeff * deltaTAir;
  const afterHeat = afterGeometry * uhiDerate;
  const soilingRetention = 1 - soilingLoss;
  const netIrradiance = afterHeat * soilingRetention;

  const capacityKwp = (roofArea * packingFactor * panelEfficiency * 1000) / 1000;
  const energyKwh = netIrradiance * roofArea * packingFactor * panelEfficiency * performanceRatio;
  const specificYield = capacityKwp > 0 ? energyKwh / capacityKwp : 0;

  return {
    diffuseFraction,
    beamKept,
    diffuseKept,
    geometricRetention,
    afterGeometry,
    uhiDerate,
    afterHeat,
    soilingRetention,
    netIrradiance,
    capacityKwp,
    energyKwh,
    specificYield,
    stages: [
      { name: 'ERA5 GHI on the horizontal', value: ghi },
      { name: 'after shadow (beam only)', value: ghi * (beamKept + diffuseFraction) },
      { name: 'after sky view (diffuse)', value: afterGeometry },
      { name: 'after heat island', value: afterHeat },
      { name: 'after soiling — net', value: netIrradiance },
    ],
  };
}

const DEFAULTS = {
  ghi: 1900,
  beamFraction: 0.54,
  shadowFrequency: 0.06,
  skyViewFactor: 0.95,
  deltaTAir: 1.2,
  soilingLoss: 0.039,
  tempCoeff: -0.004,
  packingFactor: 0.7,
  panelEfficiency: 0.18,
  performanceRatio: 0.8,
  roofArea: 100,
};

function Slider({ label, hint, value, min, max, step, format, onChange }) {
  return (
    <div className="field">
      <div className="field__label">
        <span>{label}</span>
        <span className="field__value">{format(value)}</span>
      </div>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
      {hint && <p className="field__hint">{hint}</p>}
    </div>
  );
}

export default function Method() {
  const [inputs, setInputs] = useState(DEFAULTS);
  const set = (patch) => setInputs((current) => ({ ...current, ...patch }));
  const chain = useMemo(() => computeChain(inputs), [inputs]);
  const baseline = useMemo(() => computeChain(DEFAULTS), []);

  return (
    <div className="page">
      <h1>Method</h1>
      <p className="lede">
        Rooftop yield is estimated as global horizontal irradiance reduced by four loss
        terms, then converted to electrical output. The terms act on different components
        of the resource: shadow attenuates the direct beam only, sky-view factor the
        diffuse only, and the temperature and soiling derates scale the total.
      </p>
      <p className="lede">
        Controls below recompute the chain client-side. They are provided to establish
        which inputs the result is sensitive to, since several are assumptions rather than
        measurements.
      </p>

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'minmax(280px, 360px) 1fr',
          gap: '2rem',
          alignItems: 'start',
          marginTop: '2rem',
        }}
      >
        <div className="card">
          <h3 style={{ marginTop: 0 }}>Inputs</h3>

          <Slider
            label="Annual GHI"
            hint="ERA5-Land, summed over the year. Indian cities run 1540–2000."
            value={inputs.ghi}
            min={1200}
            max={2200}
            step={10}
            format={(v) => `${v} kWh/m²`}
            onChange={(ghi) => set({ ghi })}
          />
          <Slider
            label="Beam fraction"
            hint="Our reference set averages 0.540. The old fallback of 0.60 was measurably too high."
            value={inputs.beamFraction}
            min={0.35}
            max={0.75}
            step={0.005}
            format={(v) => v.toFixed(3)}
            onChange={(beamFraction) => set({ beamFraction })}
          />
          <Slider
            label="Shadow frequency"
            hint="Fraction of insolation-weighted time the roof is shaded. Affects beam only."
            value={inputs.shadowFrequency}
            min={0}
            max={0.6}
            step={0.005}
            format={(v) => fmt.pct(v, 1)}
            onChange={(shadowFrequency) => set({ shadowFrequency })}
          />
          <Slider
            label="Sky view factor"
            hint="Fraction of sky visible. Affects diffuse only — which is roughly half the total, so this term is not a refinement."
            value={inputs.skyViewFactor}
            min={0.4}
            max={1}
            step={0.005}
            format={(v) => v.toFixed(3)}
            onChange={(skyViewFactor) => set({ skyViewFactor })}
          />
          <Slider
            label="Urban heat anomaly (air)"
            hint="Air temperature excess over the rural background. Not the surface anomaly, which runs 2–3× higher."
            value={inputs.deltaTAir}
            min={0}
            max={6}
            step={0.1}
            format={(v) => `+${v.toFixed(1)} °C`}
            onChange={(deltaTAir) => set({ deltaTAir })}
          />
          <Slider
            label="Soiling loss"
            hint="Mean over the window. Delhi rain-only is about 3.9%; monthly cleaning halves it."
            value={inputs.soilingLoss}
            min={0}
            max={0.2}
            step={0.001}
            format={(v) => fmt.pct(v, 2)}
            onChange={(soilingLoss) => set({ soilingLoss })}
          />
          <Slider
            label="Packing factor"
            hint="Panels never tile a whole roof: setbacks, water tanks, access."
            value={inputs.packingFactor}
            min={0.3}
            max={1}
            step={0.01}
            format={(v) => v.toFixed(2)}
            onChange={(packingFactor) => set({ packingFactor })}
          />
          <Slider
            label="Performance ratio"
            hint="Inverter, wiring, mismatch, absolute temperature. Indian year-one is typically 0.78–0.83."
            value={inputs.performanceRatio}
            min={0.6}
            max={0.95}
            step={0.005}
            format={(v) => v.toFixed(3)}
            onChange={(performanceRatio) => set({ performanceRatio })}
          />

          <button className="btn" style={{ width: '100%' }} onClick={() => setInputs(DEFAULTS)}>
            Reset to defaults
          </button>
        </div>

        <div>
          <div className="hero-figure" style={{ padding: '1rem' }}>
            <div className="hero-figure__value">{fmt.num(chain.specificYield)}</div>
            <div className="hero-figure__label">
              kWh/kWp/yr — specific yield
              {Math.abs(chain.specificYield - baseline.specificYield) > 1 && (
                <span style={{ color: 'var(--text-faint)' }}>
                  {' '}
                  ({chain.specificYield > baseline.specificYield ? '+' : ''}
                  {fmt.num(chain.specificYield - baseline.specificYield)} vs default)
                </span>
              )}
            </div>
          </div>

          <p style={{ textAlign: 'center', color: 'var(--text-dim)', fontSize: '0.9rem' }}>
            Specific yield is independent of packing factor and module efficiency: both
            appear in the numerator and the denominator. The metric therefore isolates the
            irradiance and loss chain from array-sizing assumptions, and is the quantity
            reported in plant performance literature.
          </p>

          <Figure
            title="Partition of the annual resource"
            caption="Baseline irradiance divided into delivered energy and the four modelled loss terms. For typical Indian urban roofs the loss terms sum to 5–12% of baseline, so the delivered fraction dominates this axis; the terms are replotted below on an independent scale."
            columns={[
              { key: 'name', label: 'Component' },
              { key: 'value', label: 'kWh/m²', numeric: true, render: (r) => fmt.num(r.value, 1) },
              { key: 'pct', label: 'Share', numeric: true, render: (r) => `${r.pct.toFixed(2)}%` },
            ]}
            rows={(() => {
              const loss = lossBreakdown(chain, inputs.ghi);
              return [
                ['Delivered', loss.delivered],
                ['Shadow', loss.shadow],
                ['Sky view', loss.skyView],
                ['Heat island', loss.heat],
                ['Soiling', loss.soiling],
              ].map(([name, value], i) => ({
                key: i,
                name,
                value,
                pct: (value / inputs.ghi) * 100,
              }));
            })()}
          >
            <BudgetBar chain={chain} ghi={inputs.ghi} />
          </Figure>

          <Figure
            title="Loss terms on an independent scale"
            caption="Each term expressed in kWh/m² removed from the baseline. Ranking is conditional on the beam fraction: shadow acts on the direct component and sky-view factor on the diffuse, so below a beam fraction of roughly 0.5 sky obstruction exceeds shadow at equivalent geometric obstruction."
            columns={[
              { key: 'label', label: 'Factor' },
              { key: 'value', label: 'kWh/m² lost', numeric: true, render: (r) => fmt.num(r.value, 1) },
              { key: 'pct', label: 'Of baseline', numeric: true, render: (r) => `${r.pct.toFixed(2)}%` },
            ]}
            rows={(() => {
              const loss = lossBreakdown(chain, inputs.ghi);
              return [
                ['Shadow', loss.shadow],
                ['Sky view', loss.skyView],
                ['Heat island', loss.heat],
                ['Soiling', loss.soiling],
              ].map(([label, value], i) => ({
                key: i,
                label,
                value,
                pct: (value / inputs.ghi) * 100,
              }));
            })()}
          >
            <LossWaterfall chain={chain} ghi={inputs.ghi} />
          </Figure>

          <h3 style={{ marginTop: '1.8rem' }}>Loss terms, ranked</h3>
          <LossRanking chain={chain} ghi={inputs.ghi} />

          <div className="grid grid--3">
            <div className="card">
              <div className="stat">
                <div className="stat__value">{fmt.pct(chain.geometricRetention, 1)}</div>
                <div className="stat__label">Geometric retention</div>
                <p className="stat__note">
                  {fmt.pct(chain.beamKept, 1)} from beam + {fmt.pct(chain.diffuseKept, 1)} from
                  diffuse.
                </p>
              </div>
            </div>
            <div className="card">
              <div className="stat">
                <div className="stat__value">{fmt.fixed(chain.uhiDerate, 4)}</div>
                <div className="stat__label">Heat derate</div>
                <p className="stat__note">
                  Urban excess only. The absolute temperature loss lives in the performance
                  ratio, kept separate so the two cannot double-count.
                </p>
              </div>
            </div>
            <div className="card">
              <div className="stat">
                <div className="stat__value">{fmt.fixed(chain.capacityKwp, 1)}</div>
                <div className="stat__label">kWp on {inputs.roofArea} m²</div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <h2>Sensitivity to the beam fraction</h2>
      <p>
        Across the 30 city-years in the evaluation set the diffuse component averages
        0.460 of global horizontal irradiance, with a range of 0.366 to 0.559. Sky-view
        factor therefore acts on a comparable share of the resource to the shadow term.
      </p>
      <p>
        Because the beam fraction determines how the two geometric terms divide the
        resource, error in it propagates to both. The production fallback of 0.60 carried a
        measured bias of −0.139 in diffuse fraction against the reference set, which is the
        basis for the learned decomposition model documented under{' '}
        <Link to="/models">Models</Link>.
      </p>

      <h2>Array orientation</h2>
      {pvlib && (
        <>
          <p>
            The served model treats every roof as horizontal. The pvlib layer computes
            tilted output for the evaluation, giving a difference of{' '}
            <strong>{fmt.fixed(pvlib.tilt_benefit_pct, 1)}%</strong> at Delhi with
            geometric losses held constant across mounts.
          </p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Mount</th>
                  <th className="num">Mean specific yield</th>
                  <th className="num">Mean cell temp</th>
                  <th className="num">Mean PR</th>
                  <th className="num">Transposition gain</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(pvlib.by_mount ?? {}).map(([mount, row]) => (
                  <tr key={mount}>
                    <td>{mount}</td>
                    <td className="num">{fmt.num(row.mean_specific_yield)}</td>
                    <td className="num">{fmt.fixed(row.mean_cell_temperature_c, 1)} °C</td>
                    <td className="num">{fmt.fixed(row.mean_performance_ratio, 3)}</td>
                    <td className="num">{fmt.fixed(row.mean_transposition_gain, 3)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="source-note">
            Transposition by {pvlib.transposition_model}, year {pvlib.year}. Hay-Davies
            rather than Perez: Perez needs a well-behaved sky-clearness pair and degrades
            when DNI quality is poor, which is exactly ERA5-derived DNI over urban India.
          </p>
          <div className="callout callout--caution">
            <div className="callout__title">Effect on comparison with published figures</div>
            <p>
              A flat-roof figure compared against a reference computed at optimal angles
              reads as model bias when it is a configuration mismatch. At{' '}
              {fmt.fixed(pvlib.tilt_benefit_pct, 1)}% it is the same size as the
              discrepancy against the best measured Indian plant.
            </p>
          </div>
        </>
      )}

      <h2>Soiling</h2>
      <p>
        The previous model was <code>loss = mean_annual_AOD × 0.08</code>: unbounded, no
        rainfall term, and identical for a single day and a full year. It is replaced by
        the published Kimber model, with deposition derived from aerosol optical depth and
        cleaning events from daily rainfall.
      </p>
      {soiling?.windows_delhi?.length > 0 && (
        <Figure
          title="Delhi soiling by month, new model against old"
          caption={`A ${soiling.window_spread_x}× spread between the cleanest and dirtiest month, where the old model returned one figure for all of them. November is worst because it is the long post-monsoon dry spell; July is best because it rains every other day.`}
          columns={[
            { key: 'window', label: 'Window' },
            { key: 'cleaning_rain_days', label: 'Rain days', numeric: true },
            {
              key: 'mean_dry_spell_days',
              label: 'Mean spell',
              numeric: true,
              render: (r) => `${fmt.fixed(r.mean_dry_spell_days, 1)}d`,
            },
            { key: 'loss', label: 'New', numeric: true, render: (r) => fmt.pct(r.loss, 2) },
            {
              key: 'loss_legacy_model',
              label: 'Old',
              numeric: true,
              render: (r) => fmt.pct(r.loss_legacy_model, 2),
            },
          ]}
          rows={soiling.windows_delhi.map((row, i) => ({ key: i, ...row }))}
        >
          <SoilingMonths rows={soiling.windows_delhi} />
        </Figure>
      )}

      <p>
        Derivations and citations for every coefficient:{' '}
        <a href="https://github.com" onClick={(e) => e.preventDefault()}>
          docs/methodology.md
        </a>{' '}
        in the repository.
      </p>
    </div>
  );
}

/* --- small inline charts ------------------------------------------------- */

function SoilingMonths({ rows }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current) return undefined;
    const p = palette();
    const chart = new Chart(ref.current, {
      type: 'bar',
      data: {
        labels: rows.map((r) => r.window.split(' ')[0]),
        datasets: [
          { label: 'Kimber, rain-aware', data: rows.map((r) => r.loss * 100), backgroundColor: p.accent, borderWidth: 0 },
          { label: 'Previous AOD-only model', data: rows.map((r) => r.loss_legacy_model * 100), backgroundColor: p.grid, borderWidth: 0 },
        ],
      },
      options: baseOptions({ showLegend: true, yTitle: 'Soiling loss (%)' }),
    });
    return () => chart.destroy();
  }, [rows]);
  return <canvas ref={ref} />;
}
