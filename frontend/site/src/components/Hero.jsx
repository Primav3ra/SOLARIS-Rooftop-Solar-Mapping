import { Suspense, lazy, useEffect, useRef, useState } from 'react';
import { specificYieldRows } from '../data/reports.js';
import { CITIES } from '../data/cities.js';

const EarthScene = lazy(() => import('../intro/EarthScene.jsx'));

/** True when the visitor has asked for less motion. Re-evaluated if they change it. */
export function usePrefersReducedMotion() {
  const [reduced, setReduced] = useState(
    () => window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false,
  );
  useEffect(() => {
    const query = window.matchMedia('(prefers-reduced-motion: reduce)');
    const onChange = (event) => setReduced(event.matches);
    query.addEventListener('change', onChange);
    return () => query.removeEventListener('change', onChange);
  }, []);
  return reduced;
}

/**
 * Whether WebGL is actually available.
 *
 * Checked by attempting a context rather than sniffing the user agent. A
 * failure here is common and boring -- a blocklisted driver, a locked-down
 * browser, a low-end device -- and must produce the poster, not a blank space
 * where the hero should be.
 */
function useWebGL() {
  const [available, setAvailable] = useState(null);
  useEffect(() => {
    try {
      const canvas = document.createElement('canvas');
      const context =
        canvas.getContext('webgl2') ||
        canvas.getContext('webgl') ||
        canvas.getContext('experimental-webgl');
      setAvailable(Boolean(context));
      context?.getExtension('WEBGL_lose_context')?.loseContext();
    } catch {
      setAvailable(false);
    }
  }, []);
  return available;
}

/**
 * Parallax: the planet drifts at a fraction of the scroll speed.
 *
 * Written to a CSS custom property from a rAF-throttled listener, so scrolling
 * never re-renders React and never touches layout -- the transform runs on the
 * compositor. Limited to the hero, the one layer where a depth cue earns its
 * place; parallax across reading pages fights legibility and is a known
 * vestibular trigger.
 */
function useScrollDepth(ref, disabled) {
  useEffect(() => {
    if (disabled || !ref.current) return undefined;
    let frame = 0;
    const update = () => {
      frame = 0;
      ref.current?.style.setProperty('--hero-scroll', String(window.scrollY));
    };
    const onScroll = () => {
      if (!frame) frame = requestAnimationFrame(update);
    };
    window.addEventListener('scroll', onScroll, { passive: true });
    update();
    return () => {
      window.removeEventListener('scroll', onScroll);
      if (frame) cancelAnimationFrame(frame);
    };
  }, [ref, disabled]);
}

/**
 * The static poster, drawn where the planet will appear.
 *
 * Pure CSS, so it costs nothing and always renders: a lit limb, a terminator,
 * a rim of scattered light -- the same cues the shader draws. It sits at the
 * globe's own position so the handoff is a cross-fade in place rather than a
 * pop, and it is the permanent fallback on WebGL failure.
 */
function Poster({ hidden }) {
  return (
    <div className={`hero__poster${hidden ? ' hero__poster--hidden' : ''}`} aria-hidden="true">
      <div className="hero__poster-disc" />
    </div>
  );
}

/**
 * The telemetry stream: real records, scrolling faintly.
 *
 * Each entry is one city-year from the committed validation report -- its
 * coordinates, its reference irradiance and the specific yield the model
 * produced. It is instrumentation, not decoration: every number in it is
 * true and traceable to evals/reports/latest.json, which is the standard the
 * rest of the site holds itself to and the reason invented telemetry would
 * have been the wrong way to build this.
 */
function Telemetry({ paused }) {
  const rows = specificYieldRows();
  if (!rows.length) return null;
  const records = rows.map((row) => {
    const city = CITY_COORDS[row.city];
    const coord = city ? `${city.lat.toFixed(2)}N ${city.lon.toFixed(2)}E` : '';
    return `${row.city.toUpperCase()} ${row.year}  ${coord}  GHI ${Math.round(
      row.reference_ghi_kwh_m2,
    )}  YIELD ${Math.round(row.specific_yield_kwh_per_kwp_yr)} kWh/kWp`;
  });
  // Doubled, so the loop is seamless: the animation translates by exactly one
  // copy's width and restarts on an identical frame.
  const line = records.join('     ');
  return (
    <div className={`hero__telemetry${paused ? ' hero__telemetry--paused' : ''}`} aria-hidden="true">
      <div className="hero__telemetry-track">
        <span>{line}</span>
        <span>{line}</span>
      </div>
    </div>
  );
}

// Coordinates for the telemetry line, matched by the report's city name.
const CITY_COORDS = Object.fromEntries(CITIES.map((c) => [c.name, c]));

export default function Hero({ children }) {
  const reducedMotion = usePrefersReducedMotion();
  const webgl = useWebGL();
  const [sceneReady, setSceneReady] = useState(false);
  const sectionRef = useRef(null);
  useScrollDepth(sectionRef, reducedMotion);

  const showScene = webgl === true;

  return (
    <section ref={sectionRef} className="hero">
      <div className="hero__scene">
        <Poster hidden={showScene && sceneReady} />
        {showScene && (
          <Suspense fallback={null}>
            <EarthScene reducedMotion={reducedMotion} onReady={() => setSceneReady(true)} />
          </Suspense>
        )}
      </div>

      {/* A scrim, so the copy keeps its contrast ratio over whatever the scene
          happens to be showing at that moment. */}
      <div className="hero__scrim" aria-hidden="true" />

      <div className="page hero__content">{children}</div>

      <p className="hero__legend">
        <span className="hero__legend-dot" aria-hidden="true" />
        The ten cities the model was validated against, at their true coordinates. The
        ring is a sun-synchronous polar orbit, the kind MODIS flies.
      </p>

      <Telemetry paused={reducedMotion} />
    </section>
  );
}
