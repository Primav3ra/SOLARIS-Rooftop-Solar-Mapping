import { useEffect, useRef } from "react";
import {
  Chart,
  baseOptions,
  palette,
  lossColour,
  directLabels,
  cssVar,
} from "./charts.js";

/**
 * Two charts, because "how do the factors affect generation?" is two questions.
 *
 * 1. **A budget bar** — one stacked bar of the baseline, split into what is
 *    delivered and what each factor takes. A part-to-whole question, so a
 *    stacked bar is the right form.
 * 2. **A waterfall of the losses alone** — rescaled so the losses fill the
 *    axis. "Which factor costs most, and by how much?" is a length comparison
 *    between steps, and needs the steps to be the data.
 *
 * One chart cannot show a 3% loss against 90% survival and still let you
 * compare that 3% with the 1% beside it; splitting them makes both legible.
 *
 * Colour comes from `lossColour`, the same source the Explore charts use.
 * These three components used to carry their own palette -- positional, the
 * set that failed the colour-vision validator, plus a hand-picked purple for
 * heat island -- so the same entity was a different colour on each page.
 * Shadow and sky view were even inverted: green and blue on Explore, blue and
 * green here. A reader who learned the key on one page misread the other.
 */

const FACTORS = [
  {
    key: "shadow",
    term: "shadow",
    label: "Shadow",
    note: "Neighbouring buildings blocking the direct beam",
  },
  {
    key: "skyView",
    term: "sky_view",
    label: "Sky view",
    note: "Obstructed sky reducing diffuse light",
  },
  {
    key: "heat",
    term: "heat",
    label: "Heat island",
    note: "Urban air temperature above the rural background",
  },
  {
    key: "soiling",
    term: "soiling",
    label: "Soiling",
    note: "Dust, net of rain washing",
  },
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

/** The factors, with their losses, largest first. */
export function rankedLosses(chain, ghi) {
  const loss = lossBreakdown(chain, ghi);
  return FACTORS.map((f) => ({ ...f, value: Math.max(0, loss[f.key]) })).sort(
    (a, b) => b.value - a.value,
  );
}

/**
 * What these inputs mean, in a sentence. Computed, so it moves with the
 * sliders: the sandbox exists to show what the result is sensitive to, and a
 * static caption cannot say that.
 */
export function budgetFinding(chain, ghi) {
  if (!ghi) return null;
  const loss = lossBreakdown(chain, ghi);
  const ranked = rankedLosses(chain, ghi);
  const deliveredPct = (loss.delivered / ghi) * 100;
  const top = ranked[0];
  const second = ranked[1];
  if (!top) return null;
  const ratio = second?.value ? top.value / second.value : null;
  return (
    <>
      <strong>
        {deliveredPct.toFixed(1)}% of the baseline is delivered at these inputs
      </strong>{" "}
      — {top.label.toLowerCase()} is the largest loss at{" "}
      {((top.value / ghi) * 100).toFixed(2)}%
      {ratio && ratio > 1.15
        ? `, ${ratio.toFixed(1)}× the next, ${second.label.toLowerCase()}.`
        : `, with ${second.label.toLowerCase()} close behind.`}
    </>
  );
}

export function BudgetBar({ chain, ghi }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current) return undefined;
    const p = palette();
    const loss = lossBreakdown(chain, ghi);
    const segments = [
      // Delivered is context, not the subject -- 90-odd percent of the bar,
      // in a quiet neutral so the eye goes to the losses, which are the point.
      {
        label: "Delivered",
        value: loss.delivered,
        colour: cssVar("--ink-500", "#3b4757"),
      },
      ...FACTORS.map((f) => ({
        label: f.label,
        value: loss[f.key],
        colour: lossColour(f.term),
      })),
    ];
    const opts = baseOptions({ showLegend: true });
    const chart = new Chart(ref.current, {
      type: "bar",
      data: {
        labels: [""],
        datasets: segments.map((s) => ({
          label: s.label,
          data: [Math.max(0, s.value)],
          backgroundColor: s.colour,
          // A surface gap between segments, not a border around them.
          borderColor: cssVar("--bg-raised", "#11161f"),
          borderWidth: { left: 1, right: 1, top: 0, bottom: 0 },
          borderSkipped: false,
        })),
      },
      options: {
        ...opts,
        indexAxis: "y",
        plugins: {
          ...opts.plugins,
          directLabels: {
            // Only the delivered share is wide enough to label on the bar; the
            // losses are read from the legend, the finding and the table.
            format: (value, dataset) =>
              dataset.label === "Delivered"
                ? `${((value / ghi) * 100).toFixed(1)}% delivered`
                : "",
            minWidth: 60,
            outsideWhenTight: false,
          },
          tooltip: {
            ...opts.plugins.tooltip,
            callbacks: {
              label: (item) =>
                `${item.dataset.label}: ${item.raw.toFixed(1)} kWh/m² ` +
                `(${((item.raw / ghi) * 100).toFixed(2)}%)`,
            },
          },
        },
        scales: {
          x: {
            stacked: true,
            max: ghi,
            ticks: { color: p.dim, font: { size: 11 } },
            grid: { color: p.grid, drawTicks: false },
            border: { color: p.grid },
            title: {
              display: true,
              text: "kWh/m² of the baseline",
              color: p.faint,
            },
          },
          y: {
            stacked: true,
            ticks: { display: false },
            grid: { display: false },
          },
        },
      },
      plugins: [directLabels],
    });
    return () => chart.destroy();
  }, [chain, ghi]);
  return <canvas ref={ref} />;
}

export function LossWaterfall({ chain, ghi }) {
  const ref = useRef(null);
  useEffect(() => {
    if (!ref.current) return undefined;
    const loss = lossBreakdown(chain, ghi);
    const steps = FACTORS.map((f) => ({
      ...f,
      value: Math.max(0, loss[f.key]),
    }));
    const total = steps.reduce((sum, s) => sum + s.value, 0);

    // Floating bars: each loss spans from the running total down to the next,
    // so the *step* is the mark and a 3% loss and a 0.5% loss can be compared
    // with each other instead of both vanishing against the baseline.
    let running = total;
    const bars = steps.map((s) => {
      const top = running;
      running -= s.value;
      return [running, top];
    });

    const opts = baseOptions({ yTitle: "kWh/m² lost" });
    const chart = new Chart(ref.current, {
      type: "bar",
      data: {
        labels: [...steps.map((s) => s.label), "Total lost"],
        datasets: [
          {
            data: [...bars, [0, total]],
            backgroundColor: [
              ...steps.map((s) => lossColour(s.term)),
              cssVar("--ink-500", "#3b4757"),
            ],
            borderWidth: 0,
            borderRadius: 4,
          },
        ],
      },
      options: {
        ...opts,
        plugins: {
          ...opts.plugins,
          directLabels: {
            format: (_value, _dataset, i) => {
              const value = i < steps.length ? steps[i].value : total;
              return `${((value / ghi) * 100).toFixed(2)}%`;
            },
            minWidth: 9999,
          },
          tooltip: {
            ...opts.plugins.tooltip,
            callbacks: {
              label: (item) => {
                const index = item.dataIndex;
                const value = index < steps.length ? steps[index].value : total;
                const note =
                  index < steps.length
                    ? steps[index].note
                    : "All factors combined";
                return [
                  `${value.toFixed(1)} kWh/m²  (${((value / ghi) * 100).toFixed(2)}% of baseline)`,
                  note,
                ];
              },
            },
          },
        },
      },
      plugins: [directLabels],
    });
    return () => chart.destroy();
  }, [chain, ghi]);
  return <canvas ref={ref} />;
}

/**
 * The same numbers as a ranked list, which is often the clearer read.
 *
 * Sorted by size, so "what costs most here" needs no chart at all. Each bar
 * wears its factor's colour, so the ranking and the charts above it read as
 * one key. Sliding the beam fraction reorders it, which is the point the prose
 * makes about diffuse light being roughly half the resource.
 */
export function LossRanking({ chain, ghi }) {
  const rows = rankedLosses(chain, ghi);
  const largest = rows[0]?.value || 1;

  return (
    <div className="ranking">
      {rows.map((row) => (
        <div className="ranking__row" key={row.key}>
          <div className="ranking__label">
            <span
              className="ranking__swatch"
              style={{ background: lossColour(row.term) }}
              aria-hidden="true"
            />
            {row.label}
          </div>
          <div className="ranking__track">
            <div
              className="ranking__fill"
              style={{
                width: `${(row.value / largest) * 100}%`,
                background: lossColour(row.term),
              }}
            />
          </div>
          <div className="ranking__value">
            {row.value.toFixed(1)}
            <span className="ranking__unit"> kWh/m²</span>
          </div>
          <div className="ranking__pct">
            {((row.value / ghi) * 100).toFixed(2)}%
          </div>
        </div>
      ))}
    </div>
  );
}
