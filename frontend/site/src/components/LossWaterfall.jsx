import { useEffect, useRef } from 'react';
import { Chart, baseOptions, palette } from './charts.js';

/**
 * Two charts, because "how do the factors affect generation?" is two questions.
 *
 * The previous single chart plotted *irradiance remaining* after each stage:
 * 1900, 1795, 1750, 1745, 1710. Five bars within 10% of each other, which
 * conveys almost nothing — the eye cannot compare the steps, and the steps are
 * the whole point. Worse, it made the losses look uniformly trivial when their
 * relative sizes differ by an order of magnitude.
 *
 * So:
 *
 * 1. **A budget bar** — one stacked bar of the baseline, split into what is
 *    delivered and what each factor takes. Answers "how much survives?" at a
 *    glance, and it is a part-to-whole question, so a stacked bar is the right
 *    form.
 * 2. **A waterfall of the losses alone** — rescaled so the losses fill the
 *    axis. Answers "which factor costs most, and by how much?", which is a
 *    length comparison between steps and needs the steps to be the data.
 *
 * Splitting them is what makes both legible. One chart cannot show a 3% loss
 * against a 90% survival and let you compare the 3% to the 1% beside it.
 */

const FACTORS = [
  { key: 'shadow', label: 'Shadow', note: 'Neighbouring buildings blocking the direct beam' },
  { key: 'skyView', label: 'Sky view', note: 'Obstructed sky reducing diffuse light' },
  { key: 'heat', label: 'Heat island', note: 'Urban air temperature above the rural background' },
  { key: 'soiling', label: 'Soiling', note: 'Dust, net of rain washing' },
];

/** Per-factor losses in kWh/m², derived from the chain. */
export function lossBreakdown(chain, ghi) {
  const stages = chain.stages.map((s) => s.value);
  return {
    baseline: ghi,
    delivered: stages[stages.length - 1],
    shadow: stages[0] - stages[1],
    skyView: stages[1] - stages[2],
    heat: stages[2] - stages[3],
    soiling: stages[3] - stages[4],
  };
}

export function BudgetBar({ chain, ghi }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current) return undefined;
    const p = palette();
    const loss = lossBreakdown(chain, ghi);
    const segments = [
      { label: 'Delivered', value: loss.delivered, colour: p.accent },
      { label: 'Shadow', value: loss.shadow, colour: p.sky },
      { label: 'Sky view', value: loss.skyView, colour: p.ok },
      { label: 'Heat island', value: loss.heat, colour: '#a855f7' },
      { label: 'Soiling', value: loss.soiling, colour: p.bad },
    ];
    const chart = new Chart(ref.current, {
      type: 'bar',
      data: {
        labels: [''],
        datasets: segments.map((s) => ({
          label: s.label,
          data: [Math.max(0, s.value)],
          backgroundColor: s.colour,
          borderWidth: 0,
        })),
      },
      options: {
        ...baseOptions({ showLegend: true }),
        indexAxis: 'y',
        plugins: {
          ...baseOptions({ showLegend: true }).plugins,
          tooltip: {
            ...baseOptions().plugins.tooltip,
            callbacks: {
              label: (item) =>
                `${item.dataset.label}: ${item.raw.toFixed(1)} kWh/m² ` +
                `(${((item.raw / ghi) * 100).toFixed(1)}%)`,
            },
          },
        },
        scales: {
          x: {
            stacked: true,
            max: ghi,
            ticks: { color: p.dim, font: { size: 11 } },
            grid: { color: p.grid },
            title: { display: true, text: 'kWh/m² of the baseline', color: p.faint },
          },
          y: { stacked: true, ticks: { display: false }, grid: { display: false } },
        },
      },
    });
    return () => chart.destroy();
  }, [chain, ghi]);
  return <canvas ref={ref} />;
}

export function LossWaterfall({ chain, ghi }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current) return undefined;
    const p = palette();
    const loss = lossBreakdown(chain, ghi);
    const steps = FACTORS.map((f) => ({ ...f, value: Math.max(0, loss[f.key]) }));
    const total = steps.reduce((sum, s) => sum + s.value, 0);

    // Floating bars: each loss is drawn from the running total down to the next,
    // so the *step* is the mark. This is what makes a 3% loss and a 0.5% loss
    // comparable to each other instead of both vanishing against the baseline.
    let running = total;
    const bars = steps.map((s) => {
      const top = running;
      running -= s.value;
      return [running, top];
    });

    const chart = new Chart(ref.current, {
      type: 'bar',
      data: {
        labels: [...steps.map((s) => s.label), 'Total lost'],
        datasets: [
          {
            data: [...bars, [0, total]],
            backgroundColor: [p.sky, p.ok, '#a855f7', p.bad, p.dim],
            borderWidth: 0,
          },
        ],
      },
      options: {
        ...baseOptions({ yTitle: 'kWh/m² lost' }),
        plugins: {
          ...baseOptions().plugins,
          tooltip: {
            ...baseOptions().plugins.tooltip,
            callbacks: {
              label: (item) => {
                const index = item.dataIndex;
                const value = index < steps.length ? steps[index].value : total;
                const note = index < steps.length ? steps[index].note : 'All factors combined';
                return [
                  `${value.toFixed(1)} kWh/m²  (${((value / ghi) * 100).toFixed(2)}% of baseline)`,
                  note,
                ];
              },
            },
          },
        },
      },
    });
    return () => chart.destroy();
  }, [chain, ghi]);
  return <canvas ref={ref} />;
}

/**
 * The same numbers as a ranked list, which is often the clearer read.
 *
 * Sorted by size, so "what costs most here" needs no chart at all. Sliding the
 * beam fraction reorders it, which is the point the prose makes about diffuse
 * light being roughly half the resource.
 */
export function LossRanking({ chain, ghi }) {
  const loss = lossBreakdown(chain, ghi);
  const rows = FACTORS.map((f) => ({ ...f, value: Math.max(0, loss[f.key]) })).sort(
    (a, b) => b.value - a.value,
  );
  const largest = rows[0]?.value || 1;

  return (
    <div className="ranking">
      {rows.map((row) => (
        <div className="ranking__row" key={row.key}>
          <div className="ranking__label">{row.label}</div>
          <div className="ranking__track">
            <div
              className="ranking__fill"
              style={{ width: `${(row.value / largest) * 100}%` }}
            />
          </div>
          <div className="ranking__value">
            {row.value.toFixed(1)}
            <span className="ranking__unit"> kWh/m²</span>
          </div>
          <div className="ranking__pct">{((row.value / ghi) * 100).toFixed(2)}%</div>
        </div>
      ))}
    </div>
  );
}
