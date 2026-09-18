/**
 * The single place the site reads committed results from.
 *
 * Everything here is a build-time JSON import of an artifact produced by the
 * Python evaluation suites. That is deliberate and it is the site's main
 * credibility property: **there are no hand-copied numbers anywhere in the
 * frontend**, so a page cannot silently disagree with the repository it
 * describes. Regenerating a report and rebuilding updates the page; there is
 * no second copy to forget.
 *
 * The consequence to respect when editing: if a suite changes its schema, the
 * page breaks loudly at build time rather than rendering a stale figure.
 */

import validation from '@reports/latest.json';
import soiling from '@reports/soiling.json';
import trackB from '@reports/track_b_profile.json';
import decomposition from '@mlreports/decomposition.json';
import manifest from '@artifacts/decomposition.manifest.json';

export { validation, soiling, trackB, decomposition, manifest };

/** One suite from the validation report, by name. */
export function suite(name) {
  return validation.suites?.[name] ?? null;
}

/**
 * The reference spread, as a percentage.
 *
 * Surfaced prominently rather than buried, because it is the constraint that
 * bounds every accuracy claim the project makes: the two credible free
 * references for India disagree by about this much, so nothing here can
 * honestly claim to be more accurate than that.
 */
export function referenceSpreadPct() {
  const rows = suite('irradiance_reference_spread')?.rows ?? [];
  const values = rows.map((r) => r.spread_pct_of_mean).filter((v) => v != null);
  return values.length ? Math.max(...values) : null;
}

/** The per-city reference comparison, for the spread chart. */
export function referenceSpreadRows() {
  return suite('irradiance_reference_spread')?.rows ?? [];
}

/** Specific-yield summary statistics and the published band. */
export function specificYieldSummary() {
  const s = suite('specific_yield_vs_published');
  if (!s) return null;
  return {
    band: s.band,
    n: s.n,
    nInBand: s.n_in_band,
    fracInBand: s.frac_in_band,
    ...s.summary,
  };
}

/** The pvlib physics-engine suite: mounts, tilt benefit, discretisation. */
export function pvlibSuite() {
  return suite('pvlib_engine');
}

/** Specific-yield rows, for the validation table and scatter. */
export function specificYieldRows() {
  return suite('specific_yield_vs_published')?.by_city_year ?? [];
}

export function beamFractionSuite() {
  return suite('beam_fraction');
}

/** The decomposition ladder, worst rung first, as the report records it. */
export function decompositionLadder() {
  return decomposition.scores ?? [];
}

export function generatedAt() {
  return {
    validation: validation.generated_utc,
    soiling: soiling.generated_utc,
    decomposition: decomposition.generated_utc,
    trackB: trackB.generated_utc,
  };
}

/** Formatting helpers, kept here so every page renders numbers the same way. */
export const fmt = {
  pct: (x, digits = 1) => (x == null ? '--' : `${(x * 100).toFixed(digits)}%`),
  num: (x, digits = 0) =>
    x == null ? '--' : x.toLocaleString('en-IN', { maximumFractionDigits: digits }),
  signed: (x, digits = 3) => (x == null ? '--' : `${x >= 0 ? '+' : ''}${x.toFixed(digits)}`),
  fixed: (x, digits = 2) => (x == null ? '--' : x.toFixed(digits)),
};
