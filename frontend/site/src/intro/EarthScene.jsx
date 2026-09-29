/**
 * The hero: a real Earth, turning India into view, watched by a satellite.
 *
 * Every moving part encodes something true about the project, which is the
 * test each one had to pass to stay in:
 *
 * - **The surface is NASA imagery**, Blue Marble by day and Black Marble city
 *   lights by night. The earlier procedural surface drew plausible landmass
 *   that was not any real place -- on a site about India, India was not on it.
 *   Both images are US Government works and in the public domain.
 * - **The terminator is fixed to the sun, and the planet turns through it.**
 *   Normals are in view space and the sun direction is constant, so
 *   continents rotate from day into night the way they physically do, and the
 *   city lights come on as they cross.
 * - **The amber points are the ten evaluation cities**, at their real
 *   coordinates, read from the same list the validation suite uses. They are
 *   the data, not ornament.
 * - **The satellite flies a sun-synchronous polar orbit**, which is how MODIS
 *   on Terra and Aqua images the planet. It orbits inertially while the Earth
 *   turns beneath it, so its ground track walks westward -- also true.
 *
 * One orchestrated moment: on load the globe swings India into view and
 * settles. After that it only turns slowly, and it leans toward the pointer,
 * which is motion answering the visitor rather than competing with the copy.
 *
 * Guardrails, because this is the heaviest thing on the site: it honours
 * `prefers-reduced-motion` (static, India facing), caps device pixel ratio at
 * 2, is lazily loaded on its own chunk, and the textures stream behind the
 * CSS poster so first paint is never blocked.
 */

import { Suspense, useMemo, useRef } from 'react';
import { Canvas, useFrame, useLoader, useThree } from '@react-three/fiber';
import * as THREE from 'three';
import { CITIES } from '../data/cities.js';

/* -------------------------------------------------------------------------- */
/* Geometry helpers                                                            */
/* -------------------------------------------------------------------------- */

/**
 * Latitude/longitude to a point on the sphere, matching three.js's
 * SphereGeometry UV layout for an equirectangular texture whose left edge is
 * the antimeridian. Getting this wrong puts every city in the ocean, so it is
 * derived from the geometry's own parameterisation rather than guessed.
 */
function latLonToVec3(lat, lon, radius = 1) {
  const phi = ((lon + 180) * Math.PI) / 180;
  const theta = ((90 - lat) * Math.PI) / 180;
  return new THREE.Vector3(
    -radius * Math.sin(theta) * Math.cos(phi),
    radius * Math.cos(theta),
    radius * Math.sin(theta) * Math.sin(phi),
  );
}

// India's centroid, near enough for framing.
const INDIA = { lat: 22.5, lon: 79.0 };
const INDIA_VEC = latLonToVec3(INDIA.lat, INDIA.lon);

/** The spin angle that brings India to face the camera (+z). */
const INDIA_FACING = -Math.atan2(INDIA_VEC.x, INDIA_VEC.z);

/** The tilt that brings India's latitude to the vertical centre. */
const INDIA_TILT = (INDIA.lat * Math.PI) / 180;

// The sun, in view space. Upper right and slightly in front, so India sits in
// morning light with the terminator visible across the eastern limb.
const SUN_DIRECTION = new THREE.Vector3(1.0, 0.42, 0.72).normalize();

/* -------------------------------------------------------------------------- */
/* Shaders                                                                     */
/* -------------------------------------------------------------------------- */

const SURFACE_VERT = /* glsl */ `
  varying vec3 vNormal;
  varying vec3 vView;
  varying vec2 vUv;
  void main() {
    vNormal = normalize(normalMatrix * normal);
    vec4 viewPosition = modelViewMatrix * vec4(position, 1.0);
    vView = normalize(-viewPosition.xyz);
    vUv = uv;
    gl_Position = projectionMatrix * viewPosition;
  }
`;

const SURFACE_FRAG = /* glsl */ `
  uniform sampler2D uDay;
  uniform sampler2D uNight;
  uniform vec3 uSun;
  uniform vec3 uHaze;
  uniform vec3 uSky;
  uniform float uFade;
  varying vec3 vNormal;
  varying vec3 vView;
  varying vec2 vUv;

  void main() {
    vec3 day = texture2D(uDay, vUv).rgb;
    float lights = texture2D(uNight, vUv).r;

    float sunDot = dot(vNormal, normalize(uSun));
    // Soft terminator: the atmosphere scatters light past the geometric edge.
    float dayMix = smoothstep(-0.16, 0.30, sunDot);

    // Ocean mask from the imagery itself -- water is where blue dominates.
    float ocean = smoothstep(0.02, 0.12, day.b - max(day.r, day.g));

    // Specular on water only: the single strongest cue separating sea from
    // land at a glance.
    vec3 halfway = normalize(normalize(uSun) + vView);
    float specular = pow(max(dot(vNormal, halfway), 0.0), 60.0) * ocean * 0.55;

    // City lights, warm, only where it is dark.
    vec3 night = vec3(1.0, 0.72, 0.36) * pow(lights, 1.4) * 1.6;

    vec3 colour = day * (0.035 + 0.965 * dayMix) + night * (1.0 - dayMix) + specular * dayMix;

    // Aerosol haze toward the lit limb. On-message: aerosol is the project's
    // largest penalty term over the Indo-Gangetic plain.
    float limb = pow(1.0 - max(dot(vNormal, vView), 0.0), 3.0);
    colour += uHaze * limb * dayMix * 0.28;
    // Rayleigh blue hugging the lit limb, so the atmosphere shell outside the
    // disc and the scattering on it read as one continuous layer.
    colour += uSky * pow(limb, 1.6) * (0.25 + 0.75 * dayMix) * 0.55;

    // Opaque, fading up from black rather than from transparent. A
    // transparent surface is sorted with the atmosphere shell and the orbit
    // ring, and in practice did not draw at all; on a near-black background
    // a fade from black reads the same and needs no blending.
    gl_FragColor = vec4(colour * uFade, 1.0);
  }
`;

const ATMOSPHERE_VERT = /* glsl */ `
  varying vec3 vNormal;
  varying vec3 vView;
  void main() {
    vNormal = normalize(normalMatrix * normal);
    vec4 viewPosition = modelViewMatrix * vec4(position, 1.0);
    vView = normalize(-viewPosition.xyz);
    gl_Position = projectionMatrix * viewPosition;
  }
`;

// The glow outside the disc.
//
// Rendered on the shell's BACK faces, and that is where the first version went
// wrong: on a back face the outward normal points away from the camera, so
// max(dot(n, v), 0) was zero everywhere, the Fresnel term saturated to one,
// and the "atmosphere" drew as a hard, flat blue ring.
//
// Here -dot(n, v) is used instead. It is zero where the view ray grazes the
// shell's own silhouette and rises toward the planet's edge -- about 0.5 at a
// 1.16 shell -- so the glow is brightest against the planet and fades to
// nothing at its outer rim, which is what a scattering layer looks like.
const ATMOSPHERE_FRAG = /* glsl */ `
  uniform vec3 uColour;
  uniform vec3 uSun;
  uniform float uFade;
  varying vec3 vNormal;
  varying vec3 vView;
  void main() {
    float depth = clamp(-dot(vNormal, vView), 0.0, 1.0);
    float glow = pow(smoothstep(0.0, 0.6, depth), 1.9);
    float lit = smoothstep(-0.45, 0.65, dot(vNormal, normalize(uSun)));
    gl_FragColor = vec4(uColour, glow * (0.16 + 0.84 * lit) * 0.9 * uFade);
  }
`;

/* -------------------------------------------------------------------------- */
/* Pieces                                                                      */
/* -------------------------------------------------------------------------- */

/** The ten evaluation cities, pulsing gently at their real coordinates. */
function CityMarkers({ reducedMotion }) {
  const groupRef = useRef();
  const points = useMemo(
    () =>
      CITIES.map((city, i) => ({
        key: city.key,
        position: latLonToVec3(city.lat, city.lon, 1.006),
        // Staggered phase so the cities breathe out of step, not in unison.
        phase: i * 0.63,
      })),
    [],
  );

  useFrame((state) => {
    if (reducedMotion || !groupRef.current) return;
    const t = state.clock.elapsedTime;
    groupRef.current.children.forEach((marker, i) => {
      const halo = marker.children[1];
      if (!halo) return;
      const pulse = 0.5 + 0.5 * Math.sin(t * 1.6 + points[i].phase);
      halo.scale.setScalar(1 + pulse * 1.4);
      halo.material.opacity = 0.42 * (1 - pulse * 0.7);
    });
  });

  return (
    <group ref={groupRef}>
      {points.map((point) => (
        <group key={point.key} position={point.position}>
          <mesh>
            <sphereGeometry args={[0.0085, 12, 12]} />
            <meshBasicMaterial color="#ffc247" />
          </mesh>
          <mesh>
            <sphereGeometry args={[0.018, 12, 12]} />
            <meshBasicMaterial color="#ffa81f" transparent opacity={0.4} depthWrite={false} />
          </mesh>
        </group>
      ))}
    </group>
  );
}

/**
 * A sun-synchronous polar orbit, and the satellite on it.
 *
 * Inclined about 98 degrees, like Terra and Aqua, whose MODIS instruments
 * supply this project's land-surface temperature and aerosol retrievals. It
 * lives outside the spinning surface, so the planet turns beneath it.
 */
function Satellite({ reducedMotion }) {
  const satRef = useRef();
  const trailRef = useRef();
  const RADIUS = 1.32;

  // The orbit plane: polar, then tipped 8 degrees past the pole and rotated
  // off the camera axis so the ring reads as an ellipse, not a line.
  const orbitRotation = useMemo(() => new THREE.Euler(0.14, 0.72, 0.0), []);

  useFrame((state) => {
    if (!satRef.current) return;
    const angle = reducedMotion ? 0.9 : state.clock.elapsedTime * 0.32;
    satRef.current.position.set(RADIUS * Math.cos(angle), RADIUS * Math.sin(angle), 0);
    if (trailRef.current) {
      trailRef.current.rotation.z = angle;
    }
  });

  return (
    <group rotation={orbitRotation}>
      {/* The full orbit, faint. */}
      <mesh>
        <torusGeometry args={[RADIUS, 0.0016, 8, 200]} />
        <meshBasicMaterial color="#5aa9e6" transparent opacity={0.22} />
      </mesh>

      {/* A short bright trail behind the satellite, so direction is legible. */}
      <group ref={trailRef}>
        <mesh rotation={[0, 0, -0.55]}>
          <torusGeometry args={[RADIUS, 0.0034, 8, 60, 0.55]} />
          <meshBasicMaterial color="#9fd0f5" transparent opacity={0.55} />
        </mesh>
      </group>

      <group ref={satRef}>
        <mesh>
          <boxGeometry args={[0.022, 0.012, 0.012]} />
          <meshBasicMaterial color="#e6f2ff" />
        </mesh>
        {/* Solar panels: the instrument that watches the sun, carried by one. */}
        <mesh position={[0, 0.022, 0]}>
          <boxGeometry args={[0.006, 0.03, 0.001]} />
          <meshBasicMaterial color="#3987e5" />
        </mesh>
        <mesh position={[0, -0.022, 0]}>
          <boxGeometry args={[0.006, 0.03, 0.001]} />
          <meshBasicMaterial color="#3987e5" />
        </mesh>
      </group>
    </group>
  );
}

function Starfield({ count = 900, reducedMotion }) {
  const ref = useRef();
  const positions = useMemo(() => {
    const array = new Float32Array(count * 3);
    for (let i = 0; i < count; i += 1) {
      // Uniform on a shell: naive spherical sampling clumps at the poles.
      const theta = Math.random() * Math.PI * 2;
      const phi = Math.acos(2 * Math.random() - 1);
      const radius = 14 + Math.random() * 12;
      array[i * 3] = radius * Math.sin(phi) * Math.cos(theta);
      array[i * 3 + 1] = radius * Math.sin(phi) * Math.sin(theta);
      array[i * 3 + 2] = radius * Math.cos(phi);
    }
    return array;
  }, [count]);

  useFrame((_state, delta) => {
    if (reducedMotion || !ref.current) return;
    ref.current.rotation.y += delta * 0.004;
  });

  return (
    <points ref={ref}>
      <bufferGeometry>
        <bufferAttribute attach="attributes-position" args={[positions, 3]} />
      </bufferGeometry>
      <pointsMaterial size={0.05} color="#cfd8e3" sizeAttenuation transparent opacity={0.7} />
    </points>
  );
}

/* -------------------------------------------------------------------------- */
/* The planet                                                                  */
/* -------------------------------------------------------------------------- */

/** Set a uniform on the material the GPU actually reads. */
function setUniform(mesh, name, value) {
  const uniform = mesh?.material?.uniforms?.[name];
  if (uniform) uniform.value = value;
}

/** Ease-out cubic: fast start, gentle landing. */
const easeOutCubic = (t) => 1 - (1 - t) ** 3;

// The intro: India starts this far round the back, and swings in over INTRO_S.
const INTRO_OFFSET = 1.35;
const INTRO_S = 3.2;
// After the intro, one revolution in about two and a half minutes. Enough to
// read as alive; slow enough not to pull the eye off the headline.
const SPIN_RAD_PER_S = 0.042;

function Earth({ reducedMotion, onReady }) {
  const tiltRef = useRef();
  const spinRef = useRef();
  // The intro is timed from the first frame the planet actually draws, not
  // from the canvas clock. The clock starts when the canvas mounts, but the
  // surface waits on its textures -- so timing from the clock spent half the
  // intro while the screen was still blank.
  const startRef = useRef(null);
  const surfaceRef = useRef();
  const atmosphereRef = useRef();

  const [dayMap, nightMap] = useLoader(THREE.TextureLoader, [
    '/textures/earth-day.jpg',
    '/textures/earth-night.jpg',
  ]);
  dayMap.colorSpace = THREE.SRGBColorSpace;
  dayMap.anisotropy = 8;

  const surfaceUniforms = useMemo(
    () => ({
      uDay: { value: dayMap },
      uNight: { value: nightMap },
      uSun: { value: SUN_DIRECTION.clone() },
      uHaze: { value: new THREE.Color('#d8a35a') },
      uSky: { value: new THREE.Color('#4f96d9') },
      uFade: { value: reducedMotion ? 1 : 0 },
    }),
    [dayMap, nightMap, reducedMotion],
  );
  const atmosphereUniforms = useMemo(
    () => ({
      uColour: { value: new THREE.Color('#5aa9e6') },
      uSun: { value: SUN_DIRECTION.clone() },
      uFade: { value: reducedMotion ? 1 : 0 },
    }),
    [reducedMotion],
  );

  useFrame((state) => {
    if (!spinRef.current || !tiltRef.current) return;

    if (startRef.current === null) {
      startRef.current = state.clock.elapsedTime;
      onReady?.();
    }

    if (reducedMotion) {
      spinRef.current.rotation.y = INDIA_FACING;
      tiltRef.current.rotation.x = INDIA_TILT;
      return;
    }

    const t = state.clock.elapsedTime - startRef.current;
    // A short fade so the planet arrives rather than pops.
    const fade = Math.min(t / 0.9, 1);
    // Written through the live material, not the memoised uniforms object.
    //
    // react-three-fiber copies `uniforms` into the ShaderMaterial when it
    // constructs it, so the object built with useMemo is decoupled from the
    // one the GPU reads. Mutating it did nothing: the planet stayed at fade 0
    // and drew as a black disc. Measured in the browser, not assumed --
    // sameObject was false and the material's own uFade never left zero. The
    // original scene updated a uTime uniform the same way and was silently
    // broken for the same reason; it went unnoticed only because the shader
    // never read uTime.
    setUniform(surfaceRef.current, 'uFade', fade);
    setUniform(atmosphereRef.current, 'uFade', fade);

    const intro = Math.min(t / INTRO_S, 1);
    const eased = easeOutCubic(intro);
    const settled = Math.max(t - INTRO_S, 0);

    spinRef.current.rotation.y =
      INDIA_FACING - INTRO_OFFSET * (1 - eased) + settled * SPIN_RAD_PER_S;

    // Lean toward the pointer. Damped, so it follows rather than snaps.
    const targetX = INDIA_TILT + state.pointer.y * 0.09;
    const targetZ = -state.pointer.x * 0.06;
    tiltRef.current.rotation.x += (targetX - tiltRef.current.rotation.x) * 0.045;
    tiltRef.current.rotation.z += (targetZ - tiltRef.current.rotation.z) * 0.045;
  });

  return (
    <group ref={tiltRef} rotation={[INDIA_TILT, 0, 0]}>
      <group ref={spinRef} rotation={[0, INDIA_FACING - INTRO_OFFSET, 0]}>
        <mesh ref={surfaceRef}>
          <sphereGeometry args={[1, 128, 128]} />
          <shaderMaterial
            vertexShader={SURFACE_VERT}
            fragmentShader={SURFACE_FRAG}
            uniforms={surfaceUniforms}
          />
        </mesh>
        <CityMarkers reducedMotion={reducedMotion} />
      </group>

      {/* Outside the spin: the orbit is inertial and the planet turns under it. */}
      <Satellite reducedMotion={reducedMotion} />

      <mesh ref={atmosphereRef} scale={1.16}>
        <sphereGeometry args={[1, 64, 64]} />
        <shaderMaterial
          vertexShader={ATMOSPHERE_VERT}
          fragmentShader={ATMOSPHERE_FRAG}
          uniforms={atmosphereUniforms}
          side={THREE.BackSide}
          transparent
          blending={THREE.AdditiveBlending}
          depthWrite={false}
        />
      </mesh>
    </group>
  );
}

/* -------------------------------------------------------------------------- */

/**
 * Where the planet sits, by viewport shape.
 *
 * Wide: beside the headline, on the right. Narrow: centred and lower, behind
 * the copy, where the scrim keeps the text legible. One fixed offset pushed
 * the globe off-screen on a phone.
 */
function Framing({ children }) {
  const { size } = useThree();
  const aspect = size.width / Math.max(size.height, 1);
  const wide = aspect > 1.15;
  const position = wide ? [Math.min(1.05, 0.42 * aspect), 0.0, 0] : [0, -0.55, 0];
  const scale = wide ? 1 : 0.82;
  return (
    <group position={position} scale={scale}>
      {children}
    </group>
  );
}

export default function EarthScene({ reducedMotion = false, onReady }) {
  return (
    <Canvas
      // Capped at 2: a 3x display gains nothing visible here and triples the
      // fragment cost of two full-screen shaders.
      dpr={[1, 2]}
      camera={{ position: [0, 0, 3.9], fov: 40 }}
      gl={{ antialias: true, powerPreference: 'high-performance', alpha: true }}
      style={{ background: 'transparent' }}
    >
      <Starfield reducedMotion={reducedMotion} />
      <Framing>
        {/* The boundary sits inside the canvas, around the planet only, so the
            stars draw immediately and only the textured surface waits. Without
            it the whole scene suspended and the hero stayed blank. */}
        <Suspense fallback={null}>
          <Earth reducedMotion={reducedMotion} onReady={onReady} />
        </Suspense>
      </Framing>
    </Canvas>
  );
}
