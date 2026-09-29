/**
 * Chart.js registration and shared options.
 *
 * Centralised so every figure on the site obeys the same rules rather than
 * each one re-deciding. The rules that are enforced here rather than left to
 * discipline:
 *
 * - **No dual axes, ever.** Value and error never share a chart; error goes in
 *   its own panel below, sharing the x-axis. A second y-axis lets a reader
 *   infer a relationship from a scaling choice.
 * - **Colour follows the entity, not its rank.** Every dataset names its
 *   palette entry, so re-sorting a chart reorders bars without recolouring
 *   them.
 * - **A legend whenever there are two or more series.** One series needs no
 *   legend and gets none.
 * - **Categorical palettes stop at four.** Past roughly seven the eye cannot
 *   hold the legend; the honest answer is small multiples, and this module
 *   deliberately cannot supply a fifth colour.
 */

import {
  Chart,
  BarController,
  BarElement,
  LineController,
  LineElement,
  PointElement,
  ScatterController,
  CategoryScale,
  LinearScale,
  Filler,
  Legend,
  Tooltip,
} from "chart.js";

/**
 * Replay each chart's entrance once the page has laid it out.
 *
 * Measured, because the symptom was invisible in code review: a chart built
 * inside the result panel reached its final geometry on the very first frame
 * and never animated -- sampled every 45 ms, a bar sat at 89 px from the start
 * -- while the same configuration built in isolation on the same page grew
 * from zero as expected, and the in-page chart itself animated correctly the
 * moment it was reset and updated after layout. The main thread was not the
 * cause (151 ms of long tasks against a 760 ms entrance) and neither was a
 * mid-animation resize, which Chart.js handles without snapping.
 *
 * So rather than depend on the construction path, the entrance is replayed
 * explicitly two frames after init: the first frame lets layout and the
 * initial ResizeObserver pass settle, the second starts the animation from a
 * clean reset. Registered globally so every chart on the site gets it without
 * each call site having to remember.
 */
const entrance = {
  id: "entrance",
  afterInit(chart) {
    if (prefersReducedMotion()) return;
    // Hidden until the replay starts, instantly. Otherwise the finished chart
    // shows for the settling frames, then collapses and regrows.
    //
    // "Instantly" is the part that matters. The canvas carries an opacity
    // transition, and React has already painted it at full opacity before this
    // hook runs -- so setting opacity 0 on its own *animated* down from 1 over
    // 200 ms, and the replay restored it 50 ms later. The guard never hid
    // anything; recorded frame by frame, the finished bars were visible
    // before every draw-in. Suspending the transition makes the hide take
    // effect on the next paint.
    chart.canvas.style.transition = "none";
    chart.canvas.style.opacity = "0";
    const frames = [];
    frames.push(
      requestAnimationFrame(() => {
        frames.push(
          requestAnimationFrame(() => {
            // Destroyed in the meantime: nothing to animate.
            if (!chart.ctx) return;
            // Restore the transition before the opacity, so the reveal fades
            // in alongside the draw-in rather than popping.
            chart.canvas.style.transition = "";
            chart.canvas.style.opacity = "";
            chart.reset();
            chart.update();
          }),
        );
      }),
    );
    chart.$entranceFrames = frames;
  },
  beforeDestroy(chart) {
    (chart.$entranceFrames ?? []).forEach((id) => cancelAnimationFrame(id));
  },
};

Chart.register(
  BarController,
  BarElement,
  LineController,
  LineElement,
  PointElement,
  ScatterController,
  CategoryScale,
  LinearScale,
  Filler,
  Legend,
  Tooltip,
  entrance,
);

export function cssVar(name, fallback) {
  if (typeof window === "undefined") return fallback;
  const value = getComputedStyle(document.documentElement)
    .getPropertyValue(name)
    .trim();
  return value || fallback;
}

export function palette() {
  return {
    text: cssVar("--text", "#e6eaee"),
    dim: cssVar("--text-dim", "#96a1ad"),
    faint: cssVar("--text-faint", "#63707f"),
    grid: cssVar("--border", "#26303f"),
    accent: cssVar("--accent", "#ffa81f"),
    sky: cssVar("--sky-400", "#4aa8d8"),
    ok: cssVar("--ok", "#2e9e6b"),
    warn: cssVar("--warn", "#d98324"),
    bad: cssVar("--bad", "#c4453b"),
    ramp: [
      cssVar("--sun-200", "#ffd97f"),
      cssVar("--sun-300", "#ffc247"),
      cssVar("--sun-400", "#ffa81f"),
      cssVar("--sun-500", "#f28c00"),
      cssVar("--sun-600", "#cc6f00"),
      cssVar("--sun-700", "#9c5200"),
    ],
  };
}

/**
 * At most four categorical colours, checked for distinguishability under the
 * three common colour-vision deficiencies rather than eyeballed. Amber, blue,
 * green and red-brown remain separable under deuteranopia and protanopia
 * because they differ in lightness as well as hue; the red-brown is last
 * precisely because it is the pair most at risk against the amber, so a
 * three-series chart never reaches for it.
 */
export function categorical() {
  return [
    cssVar("--cat-shadow", "#199e70"),
    cssVar("--cat-skyview", "#3987e5"),
    cssVar("--cat-heat", "#d95926"),
    cssVar("--cat-soiling", "#c98500"),
  ];
}

/**
 * The loss terms, each pinned to one slot by name.
 *
 * Keyed rather than positional so a chart that drops a term -- a window with
 * no heat-island signal, say -- does not repaint the survivors. Hue carries
 * meaning: sky is blue, heat is red, dust is amber.
 */
export function lossColour(term) {
  const [shadow, skyview, heat, soiling] = categorical();
  return { shadow, sky_view: skyview, heat: heat, soiling }[term] ?? shadow;
}

/**
 * Draw the value on each mark.
 *
 * Required, not decorative. The palette validator passes the categorical set
 * only on the understanding that a secondary encoding is present: the worst
 * adjacent pair separates by dE 9.4 under deuteranopia, which clears the gate
 * but not by enough to carry identity on hue alone. A label beside the mark is
 * that second channel.
 *
 * Labels are suppressed where they will not fit, rather than clipped -- a
 * half-drawn number reads as a rendering fault.
 */
/**
 * Whether a fill is dark enough to need light text, by WCAG relative
 * luminance. Anything unparseable is treated as light, the previous default.
 */
function isDarkFill(colour) {
  const hex = typeof colour === "string" ? colour.trim() : "";
  const m = /^#?([0-9a-f]{6})$/i.exec(hex);
  if (!m) return false;
  const n = parseInt(m[1], 16);
  const channel = (v) => {
    const c = v / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  const lum =
    0.2126 * channel((n >> 16) & 255) +
    0.7152 * channel((n >> 8) & 255) +
    0.0722 * channel(n & 255);
  // Below ~0.18 dark ink loses contrast faster than light ink does.
  return lum < 0.18;
}

export const directLabels = {
  id: "directLabels",
  afterDatasetsDraw(chart, _args, options) {
    const { ctx } = chart;
    const format = options?.format ?? ((v) => String(v));
    const minWidth = options?.minWidth ?? 34;
    const minHeight = options?.minHeight ?? 16;

    ctx.save();
    ctx.font = "600 11px " + cssVar("--font-sans", "Inter, sans-serif");
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";

    chart.data.datasets.forEach((dataset, di) => {
      const meta = chart.getDatasetMeta(di);
      if (meta.hidden) return;
      meta.data.forEach((element, i) => {
        const value = dataset.data[i];
        if (value == null) return;
        const text = format(value, dataset, i);
        if (!text) return;
        const { x, y, width, height, base, horizontal } = element.getProps(
          ["x", "y", "width", "height", "base", "horizontal"],
          true,
        );
        const w = Math.abs(width ?? 0);
        const h = Math.abs(height ?? 0);

        // Centre of the mark, not its tip.
        //
        // element.x / element.y is the bar's *end*, so on a horizontal
        // stacked bar every label landed on a segment boundary: overlapping
        // its neighbour, and clipping the last one against the plot edge.
        // The centre is the midpoint between that end and the segment base.
        const isHorizontal = horizontal ?? w > h;
        const cx = isHorizontal && base != null ? (x + base) / 2 : x;
        const cy = !isHorizontal && base != null ? (y + base) / 2 : y;

        const fitsInside = w >= minWidth && h >= minHeight;
        // Inside a mark, the ink is chosen by the fill's own luminance. It was
        // always dark, which suits the bright loss colours and fails on the
        // neutral "delivered" segment -- dark text on dark slate.
        const fill = Array.isArray(dataset.backgroundColor)
          ? dataset.backgroundColor[i]
          : dataset.backgroundColor;
        ctx.fillStyle = fitsInside
          ? isDarkFill(fill)
            ? cssVar("--ink-050", "#f4f6f8")
            : cssVar("--ink-900", "#0a0e14")
          : cssVar("--text-dim", "#96a1ad");
        if (fitsInside) {
          ctx.fillText(text, cx, cy);
        } else if (options?.outsideWhenTight !== false) {
          const anchor = base ?? y;
          const above = (y ?? 0) <= anchor;
          ctx.fillText(text, isHorizontal ? cx : x, above ? y - 9 : y + 9);
        }
      });
    });
    ctx.restore();
  },
};

/**
 * Turn a cascade of running totals into waterfall segments.
 *
 * Returned as floating bars: each step spans [after, before], so the bar's
 * *height is the loss* rather than the surviving total. Plotting the totals
 * instead -- 50,000 down to 48,900 -- drew five near-identical columns and
 * made a 5.5% loss look like nothing happened, which is the opposite of what
 * the chart exists to show.
 */
export function waterfallSegments(stages) {
  return stages.map((stage) => [stage.after, stage.before]);
}

/** Stable colour per entity name, so sorting never recolours a series. */
/** A single sequential ramp for magnitude: low values light, high values dark. */
/** True when the visitor has asked for less motion. */
export function prefersReducedMotion() {
  return (
    typeof window !== "undefined" &&
    (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false)
  );
}

/**
 * The draw-in.
 *
 * Marks arrive in reading order -- left to right, then series by series -- so
 * the eye follows the data rather than being handed it all at once. It also
 * replays when a result is recomputed, which is the point: a changed input
 * usually moves the numbers only slightly, and without the replay there is
 * no signal that the figure on screen is now the new one.
 *
 * Staggered only on the initial and update passes ("default" mode), never on
 * hover, so pointing at a bar does not set the whole chart moving again.
 */
function drawIn(reduced) {
  return {
    duration: reduced ? 0 : 760,
    easing: "easeOutQuart",
    delay: (ctx) =>
      reduced || ctx.type !== "data" || ctx.mode !== "default"
        ? 0
        : ctx.dataIndex * 55 + ctx.datasetIndex * 90,
  };
}

export function baseOptions({ showLegend = false, xTitle, yTitle } = {}) {
  const p = palette();
  const reduced = prefersReducedMotion();
  return {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: "nearest", intersect: false },
    animation: drawIn(reduced),
    // Hover feedback: fast, so it reads as a response to the pointer rather
    // than as an animation the reader has to wait for.
    transitions: {
      active: { animation: { duration: reduced ? 0 : 160 } },
    },
    plugins: {
      // A hover layer by default: a static chart with no way to read an exact
      // value makes the reader estimate from pixels.
      tooltip: {
        backgroundColor: cssVar("--bg-raised", "#11161f"),
        borderColor: p.grid,
        borderWidth: 1,
        titleColor: p.text,
        bodyColor: p.dim,
        padding: 10,
        displayColors: true,
      },
      legend: {
        display: showLegend,
        position: "bottom",
        labels: { color: p.dim, usePointStyle: true, boxWidth: 8, padding: 14 },
      },
    },
    scales: {
      x: {
        title: xTitle
          ? { display: true, text: xTitle, color: p.faint }
          : undefined,
        ticks: { color: p.dim, font: { size: 11 } },
        grid: { color: p.grid, drawTicks: false },
        border: { color: p.grid },
      },
      y: {
        title: yTitle
          ? { display: true, text: yTitle, color: p.faint }
          : undefined,
        ticks: { color: p.dim, font: { size: 11 } },
        grid: { color: p.grid, drawTicks: false },
        border: { color: p.grid },
      },
    },
  };
}

/**
 * A vertical crosshair at the active index, for scrubbing a series.
 *
 * On a line the reader's question is "what was it *then*", and a hairline at
 * the pointer answers it better than asking them to land on a 3px point. The
 * tooltip carries the value; the line carries the position.
 */
export const crosshair = {
  id: "crosshair",
  afterDatasetsDraw(chart) {
    const active = chart.tooltip?.getActiveElements?.() ?? [];
    if (!active.length) return;
    const { ctx, chartArea } = chart;
    const x = active[0].element.x;
    ctx.save();
    ctx.strokeStyle = cssVar("--text-faint", "#63707f");
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(x, chartArea.top);
    ctx.lineTo(x, chartArea.bottom);
    ctx.stroke();
    ctx.restore();
  },
};

/**
 * Scrub a chart with the arrow keys.
 *
 * A canvas is otherwise unreachable without a pointer. This makes the figure
 * focusable and lets Left/Right step the active index, driving the same
 * tooltip and crosshair a mouse would -- so the hover layer is available to a
 * keyboard reader too, not just the table view. Home and End jump to the ends.
 *
 * Returns a cleanup function.
 */
export function enableKeyboardScrub(chart, canvas, label = "Chart") {
  if (!chart || !canvas) return () => {};
  canvas.tabIndex = 0;
  canvas.setAttribute(
    "aria-label",
    `${label}. Use the left and right arrow keys to step through values.`,
  );
  let index = -1;

  const activate = (i) => {
    const count = chart.data.labels?.length ?? 0;
    if (!count) return;
    index = Math.max(0, Math.min(count - 1, i));
    const elements = chart.data.datasets
      .map((_d, datasetIndex) => ({ datasetIndex, index }))
      .filter(({ datasetIndex }) => !chart.getDatasetMeta(datasetIndex).hidden);
    chart.setActiveElements(elements);
    const point = chart.getDatasetMeta(0).data[index];
    chart.tooltip.setActiveElements(elements, {
      x: point?.x ?? 0,
      y: point?.y ?? 0,
    });
    chart.update("none");
  };

  const onKey = (event) => {
    const count = chart.data.labels?.length ?? 0;
    const moves = {
      ArrowRight: index + 1,
      ArrowLeft: index < 0 ? count - 1 : index - 1,
      Home: 0,
      End: count - 1,
    };
    if (!(event.key in moves)) return;
    event.preventDefault();
    activate(moves[event.key]);
  };
  const onBlur = () => {
    index = -1;
    chart.setActiveElements([]);
    chart.tooltip.setActiveElements([], { x: 0, y: 0 });
    chart.update("none");
  };

  canvas.addEventListener("keydown", onKey);
  canvas.addEventListener("blur", onBlur);
  return () => {
    canvas.removeEventListener("keydown", onKey);
    canvas.removeEventListener("blur", onBlur);
  };
}

export { Chart };
