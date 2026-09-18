/**
 * The hero animation: sun, atmosphere, India, rooftop.
 *
 * Earth-centric rather than a multi-planet solar system, and the reasoning is
 * worth recording because it was a deliberate reversal. A realistic planetary
 * system is both more work *and* less relevant to rooftop solar in India. An
 * Earth-centric sequence can narrate the actual pipeline -- sun, atmosphere,
 * city, roof -- which turns decoration into an explanation of what the project
 * does.
 *
 * What makes a sphere read as a planet
 * ------------------------------------
 * Not polygon count. Three things, in order of effect per line of code:
 *
 * 1. A **Fresnel rim light** for the atmosphere. The bright limb where the
 *    atmosphere is seen edge-on is the single strongest cue, and it is a
 *    twenty-line shader.
 * 2. A **terminator** -- a soft day/night boundary rather than a hard one,
 *    because the atmosphere scatters light around the edge.
 * 3. **Night-side city lights** appearing only where the surface is dark,
 *    which is also the on-message part: those lights are the rooftops.
 *
 * The surface is generated procedurally rather than textured. That keeps the
 * route free of a multi-megabyte image download, and it means no imagery
 * licence to track. The continents are recognisable as landmass rather than
 * accurate coastlines, and the About page says so instead of implying
 * otherwise.
 *
 * Guardrails, because this is the heaviest thing on the site: it honours
 * `prefers-reduced-motion`, caps device pixel ratio at 2, is lazily loaded on
 * its own chunk, and falls back to a static poster on WebGL failure. It never
 * blocks first paint.
 */

import { useMemo, useRef } from 'react';
import { Canvas, useFrame } from '@react-three/fiber';
import * as THREE from 'three';

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

// The Fresnel term: intensity rises as the surface turns away from the viewer,
// so the limb glows and the disc centre stays clear. Multiplied by the sun
// term so the glow only appears on the lit side, which is what stops it
// looking like a uniform halo sticker.
const ATMOSPHERE_FRAG = /* glsl */ `
  uniform vec3 uColour;
  uniform vec3 uSun;
  uniform float uPower;
  varying vec3 vNormal;
  varying vec3 vView;
  void main() {
    float fresnel = pow(1.0 - max(dot(vNormal, vView), 0.0), uPower);
    float lit = smoothstep(-0.35, 0.55, dot(vNormal, normalize(uSun)));
    gl_FragColor = vec4(uColour, fresnel * (0.25 + 0.75 * lit));
  }
`;

const SURFACE_VERT = /* glsl */ `
  varying vec3 vNormal;
  varying vec2 vUv;
  void main() {
    vNormal = normalize(normalMatrix * normal);
    vUv = uv;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }
`;

const SURFACE_FRAG = /* glsl */ `
  uniform vec3 uSun;
  uniform vec3 uOcean;
  uniform vec3 uLand;
  uniform vec3 uHaze;
  uniform float uTime;
  varying vec3 vNormal;
  varying vec2 vUv;

  // Value noise, summed over four octaves. Enough structure to read as
  // landmass; deliberately not a coastline.
  float hash(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
  }
  float noise(vec2 p) {
    vec2 i = floor(p), f = fract(p);
    vec2 u = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash(i), hash(i + vec2(1, 0)), u.x),
               mix(hash(i + vec2(0, 1)), hash(i + vec2(1, 1)), u.x), u.y);
  }
  float fbm(vec2 p) {
    float total = 0.0, amplitude = 0.5;
    for (int i = 0; i < 4; i++) {
      total += amplitude * noise(p);
      p *= 2.0;
      amplitude *= 0.5;
    }
    return total;
  }

  void main() {
    // Latitude-weighted so landmass clusters away from the poles, and stretched
    // in longitude so shapes run east-west as real continents broadly do.
    float lat = vUv.y;
    float continents = fbm(vec2(vUv.x * 7.0, lat * 4.0));
    float landMask = smoothstep(0.48, 0.56, continents * (1.0 - abs(lat - 0.5) * 0.7));

    vec3 albedo = mix(uOcean, uLand, landMask);
    // Ice at the poles.
    albedo = mix(albedo, vec3(0.92, 0.95, 0.98), smoothstep(0.86, 0.96, abs(lat - 0.5) * 2.0));

    float sun = dot(vNormal, normalize(uSun));
    // A soft terminator: the atmosphere scatters light past the geometric
    // boundary, so a hard step looks wrong.
    float day = smoothstep(-0.18, 0.32, sun);

    // Night side: city lights, on land only, clustered rather than uniform.
    float lights = smoothstep(0.55, 0.85, fbm(vec2(vUv.x * 26.0, lat * 16.0))) * landMask;
    vec3 night = vec3(1.0, 0.76, 0.38) * lights * 0.75;

    // Specular sheen on ocean only, which is what separates water from land at
    // a glance far more than colour does.
    float specular = pow(max(sun, 0.0), 22.0) * (1.0 - landMask) * 0.5;

    vec3 colour = albedo * (0.06 + 0.94 * day) + night * (1.0 - day) + specular;
    // Aerosol haze over the lit limb: on-message, since aerosol is the
    // project's dominant penalty term.
    colour += uHaze * pow(1.0 - max(dot(vNormal, vec3(0.0, 0.0, 1.0)), 0.0), 3.0) * day * 0.35;
    gl_FragColor = vec4(colour, 1.0);
  }
`;

const SUN_DIRECTION = new THREE.Vector3(1.0, 0.35, 0.65).normalize();

function Earth({ reducedMotion }) {
  const meshRef = useRef();
  const surfaceUniforms = useMemo(
    () => ({
      uSun: { value: SUN_DIRECTION.clone() },
      uOcean: { value: new THREE.Color('#0b2740') },
      uLand: { value: new THREE.Color('#2f4a2c') },
      uHaze: { value: new THREE.Color('#d8a35a') },
      uTime: { value: 0 },
    }),
    [],
  );
  const atmosphereUniforms = useMemo(
    () => ({
      uColour: { value: new THREE.Color('#5aa9e6') },
      uSun: { value: SUN_DIRECTION.clone() },
      uPower: { value: 2.6 },
    }),
    [],
  );

  useFrame((state, delta) => {
    if (reducedMotion || !meshRef.current) return;
    // Slow: about one rotation a minute. Fast enough to read as alive, slow
    // enough not to compete with the text beside it.
    meshRef.current.rotation.y += delta * 0.055;
    surfaceUniforms.uTime.value = state.clock.elapsedTime;
  });

  return (
    <group rotation={[0.35, 0, 0.18]}>
      <mesh ref={meshRef}>
        <sphereGeometry args={[1, 96, 96]} />
        <shaderMaterial
          vertexShader={SURFACE_VERT}
          fragmentShader={SURFACE_FRAG}
          uniforms={surfaceUniforms}
        />
      </mesh>

      {/* Atmosphere: a slightly larger sphere rendered back-face, additively
          blended, with depth writing off so it never occludes the planet. */}
      <mesh scale={1.035}>
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

function Starfield({ count = 700 }) {
  const positions = useMemo(() => {
    const array = new Float32Array(count * 3);
    for (let i = 0; i < count; i += 1) {
      // Rejection-sampled onto a shell, so stars do not clump at the poles the
      // way naive spherical sampling does.
      const theta = Math.random() * Math.PI * 2;
      const phi = Math.acos(2 * Math.random() - 1);
      const radius = 14 + Math.random() * 10;
      array[i * 3] = radius * Math.sin(phi) * Math.cos(theta);
      array[i * 3 + 1] = radius * Math.sin(phi) * Math.sin(theta);
      array[i * 3 + 2] = radius * Math.cos(phi);
    }
    return array;
  }, [count]);

  return (
    <points>
      <bufferGeometry>
        <bufferAttribute attach="attributes-position" args={[positions, 3]} />
      </bufferGeometry>
      <pointsMaterial size={0.055} color="#cfd8e3" sizeAttenuation transparent opacity={0.75} />
    </points>
  );
}

/** A visible cone of insolation: the sun's energy arriving, which is the input. */
function SunBeam() {
  const position = SUN_DIRECTION.clone().multiplyScalar(4.2);
  return (
    <group>
      <mesh position={position}>
        <sphereGeometry args={[0.45, 32, 32]} />
        <meshBasicMaterial color="#fff2cc" />
      </mesh>
      <mesh position={position} scale={2.6}>
        <sphereGeometry args={[0.45, 32, 32]} />
        <meshBasicMaterial color="#ffb23d" transparent opacity={0.12} />
      </mesh>
    </group>
  );
}

export default function EarthScene({ reducedMotion = false }) {
  return (
    <Canvas
      // Capped at 2: a 3x display gains nothing visible here and triples the
      // fragment cost of two full-screen shaders.
      dpr={[1, 2]}
      camera={{ position: [0, 0, 3.1], fov: 42 }}
      gl={{ antialias: true, powerPreference: 'high-performance' }}
      style={{ background: 'transparent' }}
    >
      <ambientLight intensity={0.12} />
      <Starfield />
      <SunBeam />
      <Earth reducedMotion={reducedMotion} />
    </Canvas>
  );
}
