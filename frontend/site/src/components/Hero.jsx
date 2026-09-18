import { Suspense, lazy, useEffect, useState } from 'react';

const EarthScene = lazy(() => import('../intro/EarthScene.jsx'));

/** True when the visitor has asked for less motion. Re-evaluated if they change it. */
function usePrefersReducedMotion() {
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
 * The static poster.
 *
 * Pure CSS, so it costs nothing and always renders: a lit limb, a terminator,
 * and a rim light -- the same three cues the shader uses, which is why it reads
 * as the same image rather than as an apology for one.
 */
function Poster() {
  return (
    <div
      aria-hidden="true"
      style={{
        position: 'absolute',
        inset: 0,
        display: 'grid',
        placeItems: 'center',
      }}
    >
      <div
        style={{
          width: 'min(58vmin, 340px)',
          aspectRatio: '1',
          borderRadius: '50%',
          background:
            'radial-gradient(circle at 32% 30%, #2f6d97 0%, #13415f 38%, #071d2e 70%, #04121d 100%)',
          boxShadow:
            '0 0 0 1px rgb(90 169 230 / 0.35), 0 0 60px 6px rgb(90 169 230 / 0.18), inset -30px -20px 70px rgb(0 0 0 / 0.65)',
        }}
      />
    </div>
  );
}

export default function Hero({ children }) {
  const reducedMotion = usePrefersReducedMotion();
  const webgl = useWebGL();

  // Still probing, or unavailable: the poster. Reduced motion still gets the
  // scene, but static -- the geometry is informative even when it is not
  // moving, so replacing it entirely would remove content rather than motion.
  const showScene = webgl === true;

  return (
    <section
      style={{
        position: 'relative',
        minHeight: 'min(82vh, 720px)',
        display: 'grid',
        alignItems: 'center',
        overflow: 'hidden',
      }}
    >
      <div style={{ position: 'absolute', inset: 0, zIndex: 0 }}>
        {showScene ? (
          <Suspense fallback={<Poster />}>
            <EarthScene reducedMotion={reducedMotion} />
          </Suspense>
        ) : (
          <Poster />
        )}
      </div>

      {/* A scrim, so the copy keeps its contrast ratio over whatever the scene
          happens to be showing at that moment. */}
      <div
        aria-hidden="true"
        style={{
          position: 'absolute',
          inset: 0,
          zIndex: 1,
          background:
            'linear-gradient(to right, color-mix(in srgb, var(--bg) 82%, transparent) 0%, color-mix(in srgb, var(--bg) 55%, transparent) 48%, transparent 78%)',
        }}
      />

      <div
        className="page"
        style={{ position: 'relative', zIndex: 2, paddingTop: '3rem', paddingBottom: '3rem' }}
      >
        {children}
      </div>
    </section>
  );
}
