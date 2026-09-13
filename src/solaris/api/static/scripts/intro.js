const EARTH_DAY = 'https://threejs.org/examples/textures/planets/earth_atmos_2048.jpg';
const EARTH_CLOUDS = 'https://threejs.org/examples/textures/planets/earth_clouds_1024.png';
const SUN_TEX = 'https://threejs.org/examples/textures/planets/sun.jpg';

function loadTexture(loader, url) {
  return new Promise((resolve) => {
    loader.load(
      url,
      (texture) => resolve(texture),
      undefined,
      () => resolve(null),
    );
  });
}

function makeRadialSpriteTexture() {
  const size = 256;
  const canvas = document.createElement('canvas');
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext('2d');
  const g = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  g.addColorStop(0.0, 'rgba(255, 210, 120, 0.95)');
  g.addColorStop(0.22, 'rgba(255, 160, 60, 0.55)');
  g.addColorStop(0.55, 'rgba(255, 120, 30, 0.18)');
  g.addColorStop(1.0, 'rgba(255, 120, 30, 0.00)');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, size, size);
  const tex = new THREE.CanvasTexture(canvas);
  tex.minFilter = THREE.LinearFilter;
  tex.magFilter = THREE.LinearFilter;
  tex.wrapS = THREE.ClampToEdgeWrapping;
  tex.wrapT = THREE.ClampToEdgeWrapping;
  return tex;
}

export async function runIntroSequence(onIntroComplete) {
  const introLayer = document.getElementById('intro-layer');
  const storyLayer = document.getElementById('story-layer');
  const skipBtn = document.getElementById('skip-intro');
  const openMapBtn = document.getElementById('open-map-btn');
  const storyScroll = document.getElementById('story-scroll');
  const progressBar = document.getElementById('story-progress-bar');
  const chapterEl = document.getElementById('story-chapter');

  let done = false;
  let raf = 0;
  let resizeHandler = null;
  let renderer = null;

  function cleanup() {
    cancelAnimationFrame(raf);
    if (resizeHandler) window.removeEventListener('resize', resizeHandler);
    if (renderer) renderer.dispose();
  }

  function showStory() {
    if (done) return;
    done = true;
    cleanup();
    introLayer.classList.add('hide');
    setTimeout(() => storyLayer.classList.add('show'), 320);
  }

  skipBtn.addEventListener('click', showStory, { once: true });

  openMapBtn.addEventListener('click', () => {
    storyLayer.classList.remove('show');
    storyLayer.classList.add('hide');
    document.body.classList.add('app-active');
    onIntroComplete();
  });

  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reducedMotion || typeof THREE === 'undefined') {
    setTimeout(showStory, 360);
    return;
  }

  function updateStoryProgress() {
    if (!storyScroll || !progressBar || !chapterEl) return;
    const max = Math.max(1, storyScroll.scrollHeight - storyScroll.clientHeight);
    const p = Math.min(1, Math.max(0, storyScroll.scrollTop / max));
    progressBar.style.width = `${(p * 100).toFixed(2)}%`;

    const slides = storyScroll.querySelectorAll('.story-slide');
    if (!slides.length) return;
    const current = Math.min(
      slides.length,
      Math.max(1, Math.round(p * (slides.length - 1)) + 1)
    );
    chapterEl.textContent = `${String(current).padStart(2, '0')} / ${String(slides.length).padStart(2, '0')}`;
  }

  storyScroll?.addEventListener('scroll', () => requestAnimationFrame(updateStoryProgress), { passive: true });
  updateStoryProgress();

  const canvas = document.getElementById('intro-canvas');
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(48, window.innerWidth / window.innerHeight, 0.1, 260);
  camera.position.set(0, 1.5, 24);

  renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
  renderer.setSize(window.innerWidth, window.innerHeight);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));

  // Brighter, cleaner lighting so Earth reads as blue/green.
  const ambient = new THREE.AmbientLight(0x9fb7d8, 0.85);
  scene.add(ambient);
  scene.add(new THREE.HemisphereLight(0x8fd3ff, 0x0b1020, 0.55));
  const fill = new THREE.DirectionalLight(0xffffff, 0.55);
  fill.position.set(6, 4, 16);
  scene.add(fill);

  const textureLoader = new THREE.TextureLoader();
  const [earthTexture, cloudTexture, sunTexture] = await Promise.all([
    loadTexture(textureLoader, EARTH_DAY),
    loadTexture(textureLoader, EARTH_CLOUDS),
    loadTexture(textureLoader, SUN_TEX),
  ]);

  const starGeometry = new THREE.BufferGeometry();
  const starCount = 2600;
  const positions = new Float32Array(starCount * 3);
  for (let i = 0; i < starCount; i += 1) {
    positions[i * 3] = (Math.random() - 0.5) * 240;
    positions[i * 3 + 1] = (Math.random() - 0.5) * 130;
    positions[i * 3 + 2] = -Math.random() * 240;
  }
  starGeometry.setAttribute('position', new THREE.BufferAttribute(positions, 3));
  const starField = new THREE.Points(
    starGeometry,
    new THREE.PointsMaterial({ color: 0xbcdcff, size: 0.28, sizeAttenuation: true })
  );
  scene.add(starField);

  const sunMaterial = sunTexture
    ? new THREE.MeshBasicMaterial({ map: sunTexture })
    : new THREE.MeshBasicMaterial({ color: 0xffc857 });
  const sun = new THREE.Mesh(new THREE.SphereGeometry(2.35, 56, 56), sunMaterial);
  scene.add(sun);

  // Use a radial alpha texture so the glow is circular (no square edges).
  const sunGlow = new THREE.Sprite(new THREE.SpriteMaterial({
    map: makeRadialSpriteTexture(),
    transparent: true,
    opacity: 0.85,
    blending: THREE.AdditiveBlending,
    depthWrite: false,
  }));
  sunGlow.scale.set(8.8, 8.8, 1);
  sun.add(sunGlow);

  const sunlight = new THREE.PointLight(0xfff2c8, 3.2, 220);
  sunlight.position.set(0, 0, 0);
  sun.add(sunlight);

  const earthOrbit = new THREE.Group();
  scene.add(earthOrbit);

  const earth = new THREE.Mesh(
    new THREE.SphereGeometry(1.08, 64, 64),
    new THREE.MeshPhongMaterial({
      map: earthTexture || null,
      color: earthTexture ? 0xffffff : 0x2a7bd6,
      emissive: new THREE.Color(0x071a2b),
      emissiveIntensity: 0.22,
      shininess: 14,
      specular: new THREE.Color(0x223344),
    })
  );
  earth.position.set(11.3, -0.2, 0);
  earthOrbit.add(earth);

  const clouds = new THREE.Mesh(
    new THREE.SphereGeometry(1.12, 56, 56),
    new THREE.MeshPhongMaterial({
      map: cloudTexture || null,
      transparent: true,
      opacity: cloudTexture ? 0.42 : 0,
      depthWrite: false,
    })
  );
  earth.add(clouds);

  const atmosphere = new THREE.Mesh(
    new THREE.SphereGeometry(1.18, 56, 56),
    new THREE.MeshPhongMaterial({
      color: 0x76beff,
      transparent: true,
      opacity: 0.18,
      side: THREE.DoubleSide,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    })
  );
  earth.add(atmosphere);

  const mars = new THREE.Mesh(
    new THREE.SphereGeometry(0.36, 28, 28),
    new THREE.MeshPhongMaterial({ color: 0xc96f4b })
  );
  mars.position.set(16.6, 2.4, -3.6);
  scene.add(mars);

  const jupiter = new THREE.Mesh(
    new THREE.SphereGeometry(0.82, 38, 38),
    new THREE.MeshPhongMaterial({ color: 0xbe9f7b })
  );
  jupiter.position.set(-15.5, 3.2, -9.8);
  scene.add(jupiter);

  const orbitLine = new THREE.Mesh(
    new THREE.RingGeometry(11.22, 11.3, 180),
    new THREE.MeshBasicMaterial({
      color: 0x4f8ad0,
      transparent: true,
      opacity: 0.24,
      side: THREE.DoubleSide,
    })
  );
  orbitLine.rotation.x = Math.PI / 2;
  scene.add(orbitLine);

  const start = performance.now();
  const duration = 7600;

  function animate(now) {
    const elapsed = now - start;
    const t = Math.min(1, elapsed / duration);

    sun.rotation.y += 0.0035;
    earthOrbit.rotation.y += 0.0024;
    earth.rotation.y += 0.0075;
    clouds.rotation.y += 0.0089;
    atmosphere.rotation.y += 0.0035;
    starField.rotation.y += 0.00008;

    const earthWorld = new THREE.Vector3();
    earth.getWorldPosition(earthWorld);

    const zoomStart = 0.2;
    const zt = Math.max(0, (t - zoomStart) / (1 - zoomStart));
    const ease = zt * zt * (3 - 2 * zt);

    const goal = new THREE.Vector3(earthWorld.x + 1.0, earthWorld.y + 0.6, earthWorld.z + 3.0);
    camera.position.lerp(goal, 0.026 + ease * 0.12);
    camera.lookAt(earthWorld.x, earthWorld.y, earthWorld.z);

    renderer.render(scene, camera);

    if (t < 1 && !done) {
      raf = requestAnimationFrame(animate);
    } else {
      showStory();
    }
  }

  resizeHandler = () => {
    camera.aspect = window.innerWidth / window.innerHeight;
    camera.updateProjectionMatrix();
    renderer.setSize(window.innerWidth, window.innerHeight);
  };

  window.addEventListener('resize', resizeHandler);
  raf = requestAnimationFrame(animate);
}