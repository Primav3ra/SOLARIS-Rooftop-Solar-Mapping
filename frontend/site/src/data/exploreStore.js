/**
 * Session state for the Explore page, held outside React.
 *
 * Component state is discarded on unmount, so navigating to another page and
 * back lost the query, the result, and every overlay tile template -- each
 * return trip re-ran a computation that takes tens of seconds and spends Earth
 * Engine quota to reproduce an answer the browser had already received. A
 * module-level store lives for the life of the page and survives route changes
 * without a context provider.
 *
 * Nothing is written to localStorage. Earth Engine map tokens expire within
 * hours, so restoring one across a browser restart would yield overlays that
 * fail silently.
 */

/** The last query, so returning to Explore restores the form. */
let query = null;

/** The last successful result and its series. */
let result = null;
let series = null;
let meta = {};

/**
 * Completed computations, most recent first.
 *
 * Comparison is the primary task: a single rooftop figure carries little
 * information without a second one to place it against. Retaining completed
 * results lets a visitor revisit a site without recomputing it, which is both
 * faster for them and cheaper in Earth Engine calls.
 */
let history = [];

/** Retained sites. Above roughly this many the list stops being scannable. */
const HISTORY_LIMIT = 8;

const overlays = new Map();

/** Earth Engine map tokens expire; six hours is comfortably inside that. */
const OVERLAY_TTL_MS = 6 * 60 * 60 * 1000;

/** Fields a tile rendering depends on. Anything else must not invalidate it. */
function requestSignature(request) {
  return JSON.stringify({
    lat: Number(request.lat).toFixed(4),
    lon: Number(request.lon).toFixed(4),
    half: Number(request.half_size_deg).toFixed(4),
    mode: request.baseline_mode,
    year: request.year ?? null,
    quarter: request.quarter ?? null,
    month: request.month ?? null,
    start: request.start_date ?? null,
  });
}

function overlayKey(layer, request) {
  return `${layer}||${requestSignature(request)}`;
}

/** A short label for a stored site. */
function describe(state, resultBody) {
  const lat = Number(state.lat).toFixed(4);
  const lon = Number(state.lon).toFixed(4);
  const window =
    state.mode === 'yearly'
      ? String(state.year)
      : state.mode === 'quarterly'
        ? `Q${state.quarter} ${state.year}`
        : state.mode === 'monthly'
          ? `${new Date(2000, Number(state.month) - 1, 1).toLocaleString('en', { month: 'short' })} ${state.year}`
          : `${state.year}-${String(state.month).padStart(2, '0')}-15`;
  return {
    coords: `${lat}, ${lon}`,
    window,
    area: resultBody?.roof_area_m2 ?? null,
    energy: resultBody?.period_yield_kwh ?? null,
  };
}

export const exploreStore = {
  getQuery: () => query,
  setQuery(next) {
    query = next;
  },

  getResult: () => ({ result, series, meta }),
  setResult(nextResult, nextSeries, nextMeta) {
    result = nextResult;
    series = nextSeries;
    meta = nextMeta ?? {};
  },
  clearResult() {
    result = null;
    series = null;
    meta = {};
  },

  // -- history ----------------------------------------------------------

  getHistory: () => history,

  /**
   * Record a completed computation.
   *
   * Keyed on the full request signature, so re-running the same site at a
   * different window adds an entry rather than overwriting one. An identical
   * re-run replaces its predecessor and moves to the front.
   */
  remember(state, resultBody, seriesBody, metaBody, request) {
    const id = requestSignature(request);
    const entry = {
      id,
      at: Date.now(),
      state: { ...state },
      result: resultBody,
      series: seriesBody,
      meta: metaBody ?? {},
      label: describe(state, resultBody),
    };
    history = [entry, ...history.filter((h) => h.id !== id)].slice(0, HISTORY_LIMIT);
    return entry;
  },

  /** Restore a stored entry as the live result. Returns its query state. */
  restore(id) {
    const entry = history.find((h) => h.id === id);
    if (!entry) return null;
    result = entry.result;
    series = entry.series;
    meta = { ...entry.meta, restored: true };
    query = { ...entry.state };
    return entry.state;
  },

  forget(id) {
    history = history.filter((h) => h.id !== id);
  },

  clearHistory() {
    history = [];
  },

  // -- overlay tile templates -------------------------------------------

  getOverlay(layer, request) {
    const key = overlayKey(layer, request);
    const entry = overlays.get(key);
    if (!entry) return null;
    if (Date.now() - entry.at > OVERLAY_TTL_MS) {
      overlays.delete(key);
      return null;
    }
    return entry.data;
  },
  setOverlay(layer, request, data) {
    overlays.set(overlayKey(layer, request), { data, at: Date.now() });
  },
  /** Which layers are already available for this query, for the UI to mark. */
  cachedLayers(request) {
    const signature = requestSignature(request);
    const now = Date.now();
    const out = new Set();
    for (const [key, entry] of overlays.entries()) {
      if (now - entry.at > OVERLAY_TTL_MS) continue;
      const [layer, rest] = key.split('||');
      if (rest === signature) out.add(layer);
    }
    return out;
  },
};
