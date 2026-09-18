import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { api, ApiError, encodeQuery, decodeQuery } from '../data/api.js';
import { exploreStore } from '../data/exploreStore.js';
import MapPanel from '../components/MapPanel.jsx';
import ResultPanel from '../components/ResultPanel.jsx';
import SiteHistory from '../components/SiteHistory.jsx';
import { CITIES } from '../data/cities.js';

const DEFAULTS = {
  lat: 28.6139,
  lon: 77.209,
  half: 0.01,
  mode: 'yearly',
  year: 2023,
  quarter: 1,
  month: 1,
  clean: '',
};

/** The request body, built from UI state. Kept pure so it is easy to reason about. */
function buildRequest(state) {
  const body = {
    lat: Number(state.lat),
    lon: Number(state.lon),
    half_size_deg: Number(state.half),
    baseline_mode: state.mode,
  };
  if (state.mode !== 'daily') body.year = Number(state.year);
  if (state.mode === 'quarterly') body.quarter = Number(state.quarter);
  if (state.mode === 'monthly') body.month = Number(state.month);
  if (state.mode === 'daily') {
    // Both dates, and end must be start + 1 day exclusive. Sending only
    // start_date returns a 400: the server will not guess the span for you,
    // which is right -- an implied end date is how a one-day query silently
    // becomes a one-year one.
    const month = String(state.month).padStart(2, '0');
    const start = new Date(Date.UTC(Number(state.year), Number(state.month) - 1, 15));
    const end = new Date(start.getTime() + 86400000);
    body.start_date = `${state.year}-${month}-15`;
    body.end_date_exclusive = end.toISOString().slice(0, 10);
  }
  if (state.clean !== '' && state.clean != null) {
    body.cleaning_interval_days = Number(state.clean);
  }
  return body;
}

/**
 * The current theme, read from the attribute Shell sets on <html>.
 *
 * A context would be tidier, but this is the only consumer outside Shell and a
 * MutationObserver on one attribute is less machinery than a provider.
 */
function useTheme() {
  const [theme, setTheme] = useState(
    () => document.documentElement.dataset.theme ?? 'dark',
  );
  useEffect(() => {
    const observer = new MutationObserver(() =>
      setTheme(document.documentElement.dataset.theme ?? 'dark'),
    );
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme'],
    });
    return () => observer.disconnect();
  }, []);
  return theme;
}

export default function Explore() {
  const theme = useTheme();
  const [searchParams, setSearchParams] = useSearchParams();

  // Order matters: an explicit URL wins (a shared permalink must reproduce its
  // query), then the session store, then defaults.
  const [state, setState] = useState(() => {
    if (searchParams.toString()) return decodeQuery(searchParams.toString(), DEFAULTS);
    return exploreStore.getQuery() ?? DEFAULTS;
  });

  const restored = exploreStore.getResult();
  const [result, setResult] = useState(restored.result);
  const [series, setSeries] = useState(restored.series);
  const [meta, setMeta] = useState(restored.meta);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [presets, setPresets] = useState(null);
  const [history, setHistory] = useState(() => exploreStore.getHistory());
  const [activeId, setActiveId] = useState(null);
  const abortRef = useRef(null);

  const set = useCallback((patch) => setState((s) => ({ ...s, ...patch })), []);

  // Keep the store in step, so a route change loses nothing.
  useEffect(() => {
    exploreStore.setQuery(state);
  }, [state]);

  useEffect(() => {
    api
      .presets()
      .then(({ data }) => setPresets(data))
      .catch(() => setPresets(null));
  }, []);

  const run = useCallback(async () => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setBusy(true);
    setError(null);
    const body = buildRequest(state);

    // The permalink is written before the request rather than after, so a
    // shared URL reproduces the query even if the request itself fails.
    setSearchParams(encodeQuery(state), { replace: true });

    let yieldResponse;
    try {
      yieldResponse = await api.yield(body, controller.signal);
      setResult(yieldResponse.data);
      setMeta(yieldResponse.meta);
      exploreStore.setResult(yieldResponse.data, null, yieldResponse.meta);

      // The curve is a second request and is allowed to fail on its own: the
      // headline number is the thing the page is for, and losing the chart
      // should not lose the number.
      try {
        const seriesResponse = await api.series(body, controller.signal);
        setSeries(seriesResponse.data);
        exploreStore.setResult(yieldResponse.data, seriesResponse.data, yieldResponse.meta);
        const entry = exploreStore.remember(
          state,
          yieldResponse.data,
          seriesResponse.data,
          yieldResponse.meta,
          body,
        );
        setHistory(exploreStore.getHistory());
        setActiveId(entry.id);
      } catch {
        setSeries(null);
        // The curve is optional; the site is still worth retaining without it.
        const entry = exploreStore.remember(
          state,
          yieldResponse.data,
          null,
          yieldResponse.meta,
          body,
        );
        setHistory(exploreStore.getHistory());
        setActiveId(entry.id);
      }
    } catch (err) {
      if (err.name === 'AbortError') return;
      setError(err instanceof ApiError ? err : new ApiError({ message: String(err) }));
      setResult(null);
      setSeries(null);
      exploreStore.clearResult();
    } finally {
      setBusy(false);
    }
  }, [state, setSearchParams]);

  /** Restore a stored site. Reads from memory: no Earth Engine calls. */
  const restore = useCallback((id) => {
    const restoredState = exploreStore.restore(id);
    if (!restoredState) return;
    const stored = exploreStore.getResult();
    setState(restoredState);
    setResult(stored.result);
    setSeries(stored.series);
    setMeta(stored.meta);
    setError(null);
    setActiveId(id);
    setSearchParams(encodeQuery(restoredState), { replace: true });
  }, [setSearchParams]);

  const forget = useCallback((id) => {
    exploreStore.forget(id);
    setHistory(exploreStore.getHistory());
    setActiveId((current) => (current === id ? null : current));
  }, []);

  const clearHistory = useCallback(() => {
    exploreStore.clearHistory();
    setHistory([]);
    setActiveId(null);
  }, []);

  // The year ceiling is data-derived on the server -- probed from ERA5-Land's
  // last published image rather than computed from the calendar. Reading it
  // from /api/presets rather than assuming it here is what lets a finished
  // recent quarter be selectable at all; an earlier version capped the UI at
  // last calendar year and refused a quarter that had ended months before.
  const bounds = presets?.baseline?.year_bounds;
  const maxYear = bounds?.max ?? DEFAULTS.year;
  const minYear = bounds?.min ?? 2000;
  const years = useMemo(() => {
    const top = Number(maxYear) || DEFAULTS.year;
    const floor = Number(minYear) || 2000;
    const span = Math.min(10, top - floor + 1);
    return Array.from({ length: span }, (_, i) => top - i);
  }, [maxYear, minYear]);
  const availability = presets?.baseline?.data_availability;

  return (
    <div className="page page--wide">
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'minmax(300px, 380px) 1fr',
          minHeight: 'calc(100vh - var(--nav-height))',
        }}
      >
        <aside
          style={{
            borderRight: '1px solid var(--border)',
            padding: '1.5rem',
            overflowY: 'auto',
            maxHeight: 'calc(100vh - var(--nav-height))',
          }}
        >
          <h2 style={{ marginTop: 0, fontSize: '1.2rem' }}>Query</h2>

          <div className="field">
            <div className="field__label">
              <span>Jump to a city</span>
            </div>
            <div className="chip-row">
              {CITIES.map((city) => (
                <button
                  key={city.key}
                  className={`chip${
                    Math.abs(city.lat - state.lat) < 0.02 &&
                    Math.abs(city.lon - state.lon) < 0.02
                      ? ' is-active'
                      : ''
                  }`}
                  onClick={() => set({ lat: city.lat, lon: city.lon })}
                >
                  {city.name}
                </button>
              ))}
            </div>
            <p className="field__hint">Or click anywhere on the map.</p>
          </div>

          <div className="grid grid--2" style={{ gap: '0.6rem' }}>
            <div className="field">
              <div className="field__label">
                <span>Latitude</span>
              </div>
              <input
                type="number"
                step="0.0001"
                value={state.lat}
                onChange={(e) => set({ lat: e.target.value })}
              />
            </div>
            <div className="field">
              <div className="field__label">
                <span>Longitude</span>
              </div>
              <input
                type="number"
                step="0.0001"
                value={state.lon}
                onChange={(e) => set({ lon: e.target.value })}
              />
            </div>
          </div>

          <div className="field">
            <div className="field__label">
              <span>Area half-size</span>
              <span className="field__value">
                {(Number(state.half) * 111.32).toFixed(2)} km
              </span>
            </div>
            <input
              type="range"
              min="0.002"
              max="0.025"
              step="0.001"
              value={state.half}
              onChange={(e) => set({ half: e.target.value })}
            />
            <p className="field__hint">
              Capped at ~30 km². The bound is the main defence against burning
              satellite quota on one oversized request.
            </p>
          </div>

          <div className="field">
            <div className="field__label">
              <span>Window</span>
            </div>
            <div className="chip-row">
              {['yearly', 'quarterly', 'monthly', 'daily'].map((mode) => (
                <button
                  key={mode}
                  className={`chip${state.mode === mode ? ' is-active' : ''}`}
                  onClick={() => set({ mode })}
                >
                  {mode}
                </button>
              ))}
            </div>
          </div>

          <div className="grid grid--2" style={{ gap: '0.6rem' }}>
            <div className="field">
              <div className="field__label">
                <span>Year</span>
              </div>
              <select value={state.year} onChange={(e) => set({ year: e.target.value })}>
                {years.map((year) => (
                  <option key={year} value={year}>
                    {year}
                  </option>
                ))}
              </select>
            </div>

            {state.mode === 'quarterly' && (
              <div className="field">
                <div className="field__label">
                  <span>Quarter</span>
                </div>
                <select
                  value={state.quarter}
                  onChange={(e) => set({ quarter: e.target.value })}
                >
                  {[1, 2, 3, 4].map((q) => (
                    <option key={q} value={q}>
                      Q{q}
                    </option>
                  ))}
                </select>
              </div>
            )}

            {(state.mode === 'monthly' || state.mode === 'daily') && (
              <div className="field">
                <div className="field__label">
                  <span>Month</span>
                </div>
                <select value={state.month} onChange={(e) => set({ month: e.target.value })}>
                  {Array.from({ length: 12 }, (_, i) => i + 1).map((m) => (
                    <option key={m} value={m}>
                      {new Date(2000, m - 1, 1).toLocaleString('en', { month: 'long' })}
                    </option>
                  ))}
                </select>
              </div>
            )}
          </div>

          <div className="field">
            <div className="field__label">
              <span>Panel cleaning</span>
              <span className="field__value">
                {state.clean === '' ? 'rain only' : `every ${state.clean}d`}
              </span>
            </div>
            <select value={state.clean} onChange={(e) => set({ clean: e.target.value })}>
              <option value="">Rain only (never manually cleaned)</option>
              <option value="7">Weekly</option>
              <option value="14">Fortnightly</option>
              <option value="30">Monthly</option>
              <option value="90">Quarterly</option>
            </select>
            <p className="field__hint">
              Rain-only is the honest default for an unmaintained roof. In an arid city
              this choice moves the answer by several per cent; in Mumbai it barely
              registers, because the monsoon already cleans.
            </p>
          </div>

          <SiteHistory
            entries={history}
            activeId={activeId}
            onRestore={restore}
            onForget={forget}
            onClear={clearHistory}
          />

          <button
            className="btn btn--primary btn--lg"
            style={{ width: '100%', marginTop: '0.5rem' }}
            onClick={run}
            disabled={busy}
          >
            {busy ? (
              <>
                <span className="spinner" /> Computing…
              </>
            ) : (
              'Compute potential'
            )}
          </button>

          {meta.guestRemaining != null && (
            <p className="field__hint" style={{ marginTop: '0.7rem' }}>
              {meta.guestRemaining} of {meta.guestAllowance} computations left this session.{' '}
              {meta.cache === 'HIT' && (
                <strong>That one was cached, so it cost you nothing.</strong>
              )}
            </p>
          )}

          {availability?.latest_available_date && (
            <p className="field__hint" style={{ marginTop: '0.8rem' }}>
              Irradiance data runs to <strong>{availability.latest_available_date}</strong>{' '}
              ({availability.source}). A window reaching past it returns a visibly partial
              result rather than a quietly smaller number.
            </p>
          )}

          <p className="source-note">
            Every computation reports which of its inputs came from a fallback rather than
            a measurement. Look for the data-quality badge on the result.
          </p>
        </aside>

        <div style={{ position: 'relative', minHeight: '480px' }}>
          <MapPanel
            lat={Number(state.lat)}
            lon={Number(state.lon)}
            halfSize={Number(state.half)}
            onPick={(lat, lon) => set({ lat: lat.toFixed(4), lon: lon.toFixed(4) })}
            request={buildRequest(state)}
            theme={theme}
            building={result?.geojson ?? null}
          />
        </div>
      </div>

      <ResultPanel
        result={result}
        series={series}
        error={error}
        busy={busy}
        meta={meta}
        onRetry={run}
      />
    </div>
  );
}
