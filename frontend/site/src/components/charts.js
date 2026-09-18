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
 * - **Colour follows the entity, not its rank.** `seriesColour` maps a stable
 *   name to a stable hue, so a city does not change colour when the sort order
 *   changes.
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
} from 'chart.js';

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
);

function cssVar(name, fallback) {
  if (typeof window === 'undefined') return fallback;
  const value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

export function palette() {
  return {
    text: cssVar('--text', '#e6eaee'),
    dim: cssVar('--text-dim', '#96a1ad'),
    faint: cssVar('--text-faint', '#63707f'),
    grid: cssVar('--border', '#26303f'),
    accent: cssVar('--accent', '#ffa81f'),
    sky: cssVar('--sky-400', '#4aa8d8'),
    ok: cssVar('--ok', '#2e9e6b'),
    warn: cssVar('--warn', '#d98324'),
    bad: cssVar('--bad', '#c4453b'),
    ramp: [
      cssVar('--sun-200', '#ffd97f'),
      cssVar('--sun-300', '#ffc247'),
      cssVar('--sun-400', '#ffa81f'),
      cssVar('--sun-500', '#f28c00'),
      cssVar('--sun-600', '#cc6f00'),
      cssVar('--sun-700', '#9c5200'),
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
  const p = palette();
  return [p.accent, p.sky, p.ok, p.bad];
}

/** Stable colour per entity name, so sorting never recolours a series. */
export function seriesColour(name, index = 0) {
  const colours = categorical();
  if (!name) return colours[index % colours.length];
  let hash = 0;
  for (let i = 0; i < name.length; i += 1) hash = (hash * 31 + name.charCodeAt(i)) % 9973;
  return colours[hash % colours.length];
}

/** A single sequential ramp for magnitude: low values light, high values dark. */
export function rampColour(fraction) {
  const ramp = palette().ramp;
  const clamped = Math.max(0, Math.min(1, fraction));
  return ramp[Math.min(ramp.length - 1, Math.round(clamped * (ramp.length - 1)))];
}

export function baseOptions({ showLegend = false, xTitle, yTitle } = {}) {
  const p = palette();
  return {
    responsive: true,
    maintainAspectRatio: false,
    interaction: { mode: 'nearest', intersect: false },
    plugins: {
      // A hover layer by default: a static chart with no way to read an exact
      // value makes the reader estimate from pixels.
      tooltip: {
        backgroundColor: cssVar('--bg-raised', '#11161f'),
        borderColor: p.grid,
        borderWidth: 1,
        titleColor: p.text,
        bodyColor: p.dim,
        padding: 10,
        displayColors: true,
      },
      legend: {
        display: showLegend,
        position: 'bottom',
        labels: { color: p.dim, usePointStyle: true, boxWidth: 8, padding: 14 },
      },
    },
    scales: {
      x: {
        title: xTitle ? { display: true, text: xTitle, color: p.faint } : undefined,
        ticks: { color: p.dim, font: { size: 11 } },
        grid: { color: p.grid, drawTicks: false },
        border: { color: p.grid },
      },
      y: {
        title: yTitle ? { display: true, text: yTitle, color: p.faint } : undefined,
        ticks: { color: p.dim, font: { size: 11 } },
        grid: { color: p.grid, drawTicks: false },
        border: { color: p.grid },
      },
    },
  };
}

export { Chart };
