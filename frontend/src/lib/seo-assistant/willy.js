import * as THREE from 'three';

/**
 * @param {HTMLCanvasElement} canvas
 * @param {{ mode?: 'dock' | 'hero' }} [options]
 *   hero = login stage: larger framing, neck + face follow the pointer
 *   dock = floating launcher (wave + eye follow)
 */
export function mountWilly(canvas, options = {}) {
const container = canvas.parentElement;
const toggle = container.querySelector('.motion-toggle');
const checklist = false;
const admirer = false;
const pricing = false;
const faq = false;
const hero = options.mode === 'hero';
const farewell = !hero;
const reduced = matchMedia('(prefers-reduced-motion: reduce)');
try {
  const renderer = new THREE.WebGLRenderer({ canvas, alpha: true, antialias: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.setClearColor(0x000000, 0);
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  const scene = new THREE.Scene();
  const camera = new THREE.OrthographicCamera(-2, 2, 2.55, -1.95, 0.1, 30);
  camera.position.set(0, 0, 10);
  // A little working agent: screen face, antenna, and a report in its hand.
  // Real bevelled meshes give it a soft, tactile toy finish from every angle.
  const world = new THREE.Group();
  scene.add(world);
  const agent = new THREE.Group();
  let modelParent = agent;
  const shell = new THREE.MeshStandardMaterial({ color: 0xeee3c1, roughness: 0.38, metalness: 0.12 });
  const dark = new THREE.MeshStandardMaterial({ color: 0x151412, roughness: 0.48 });
  const cream = new THREE.MeshStandardMaterial({ color: 0xf1e5bf, roughness: 0.48 });
  const clay = new THREE.MeshStandardMaterial({ color: 0xb86119, roughness: 0.4 });
  const glow = new THREE.MeshStandardMaterial({ color: 0x20a789, emissive: 0x10876d, emissiveIntensity: 0.3 });
  function roundedBox(width, height, depth, material, x, y, z, radius = 0.12) {
    const w = width / 2 - radius, h = height / 2 - radius;
    const shape = new THREE.Shape();
    shape.moveTo(-w, -h); shape.lineTo(w, -h); shape.lineTo(w, h);
    shape.lineTo(-w, h); shape.closePath();
    const geometry = new THREE.ExtrudeGeometry(shape, {
      depth: Math.max(0.01, depth - radius * 2), bevelEnabled: true, bevelSegments: 5,
      steps: 1, bevelSize: radius, bevelThickness: Math.min(radius, depth / 2),
    });
    geometry.center();
    const mesh = new THREE.Mesh(geometry, material);
    mesh.position.set(x, y, z);
    modelParent.add(mesh);
    return mesh;
  }
  function sphere(radius, material, x, y, z) {
    const mesh = new THREE.Mesh(new THREE.SphereGeometry(radius, 24, 16), material);
    mesh.position.set(x, y, z); modelParent.add(mesh); return mesh;
  }
  roundedBox(pricing ? 1.8 : 1.5, pricing ? 1.2 : 0.97, 0.85, shell, 0, 0.62, 0);
  roundedBox(pricing ? 1.5 : 1.22, pricing ? 0.9 : 0.66, 0.12, dark, 0, 0.62, 0.46, 0.06);
  const eyes = [-0.28, 0.28].map(x => {
    if (!admirer) return roundedBox(0.13, 0.22, 0.09, glow, x, 0.67, 0.55, 0.035);
    const heart = new THREE.Shape();
    heart.moveTo(0, -0.13);
    heart.bezierCurveTo(-0.04, -0.08, -0.16, 0, -0.14, 0.085);
    heart.bezierCurveTo(-0.12, 0.17, -0.035, 0.17, 0, 0.09);
    heart.bezierCurveTo(0.035, 0.17, 0.12, 0.17, 0.14, 0.085);
    heart.bezierCurveTo(0.16, 0, 0.04, -0.08, 0, -0.13);
    const mesh = new THREE.Mesh(new THREE.ExtrudeGeometry(heart, {
      depth: 0.04, bevelEnabled: true, bevelThickness: 0.008, bevelSize: 0.008, bevelSegments: 2,
    }), new THREE.MeshStandardMaterial({color:0xd47761, emissive:0x9a4030, emissiveIntensity:0.3, roughness:0.4}));
    mesh.position.set(x, 0.67, 0.55); agent.add(mesh); return mesh;
  });
  const smileCurve = new THREE.QuadraticBezierCurve3(
    new THREE.Vector3(-0.16, 0.45, 0.55), new THREE.Vector3(0, 0.34, 0.58), new THREE.Vector3(0.16, 0.45, 0.55),
  );
  const smileGeometry = new THREE.TubeGeometry(smileCurve, 16, 0.022, 8, false);
  smileGeometry.translate(0, -0.43, -0.55);
  const smile = new THREE.Mesh(smileGeometry, glow);
  smile.position.set(0, pricing ? 0.48 : 0.43, 0.55);
  agent.add(smile);
  const cheeks = pricing ? [-0.43, 0.43].map(x => {
    const cheek = sphere(0.065, new THREE.MeshStandardMaterial({color:0xd47761,transparent:true,opacity:0}), x, 0.47, 0.56);
    cheek.scale.set(1,0.45,0.25); return cheek;
  }) : [];
  // Higher tiers get genuinely different expressions, not just a stretched smile.
  const happyEyes = pricing ? [-0.28, 0.28].map(x => {
    const curve = new THREE.QuadraticBezierCurve3(new THREE.Vector3(-0.09,0,0), new THREE.Vector3(0,0.14,0), new THREE.Vector3(0.09,0,0));
    const eye = new THREE.Mesh(new THREE.TubeGeometry(curve,16,0.023,8,false),glow);
    eye.position.set(x,0.67,0.55); eye.visible=false; agent.add(eye); return eye;
  }) : [];
  let grin = null;
  if (pricing) {
    const shape = new THREE.Shape();
    shape.moveTo(-0.25,0.035); shape.quadraticCurveTo(0,-0.025,0.25,0.035);
    shape.quadraticCurveTo(0,-0.27,-0.25,0.035);
    grin = new THREE.Mesh(new THREE.ExtrudeGeometry(shape,{depth:0.015,bevelEnabled:false}),glow);
    grin.position.set(0,0.47,0.57); grin.visible=false; agent.add(grin);
  }
  roundedBox(0.15, 0.36, 0.15, dark, 0, pricing ? 1.35 : 1.22, 0, 0.05);
  sphere(0.13, clay, 0, pricing ? 1.56 : 1.43, 0);
  sphere(0.19, clay, pricing ? -0.96 : -0.81, 0.64, 0);
  sphere(0.19, clay, pricing ? 0.96 : 0.81, 0.64, 0);
  // Articulate at the neck. The torso stays behind the console at all angles.
  const head = new THREE.Group();
  head.position.y = 0.62;
  for (const part of [...agent.children]) {
    part.position.y -= 0.62;
    head.add(part);
  }
  agent.add(head);
  // A visible neck joins the independently moving head to the fixed torso.
  sphere(0.13, dark, 0, 0.075, 0);
  roundedBox(1.05, 0.82, 0.72, shell, 0, -0.38, 0, 0.14);
  roundedBox(0.26, 0.2, 0.1, cream, 0, -0.27, 0.39, 0.035);
  sphere(0.043, clay, 0, -0.27, 0.46);
  roundedBox(0.37, 0.28, 0.62, dark, -0.31, -0.91, 0.11, 0.1);
  roundedBox(0.37, 0.28, 0.62, dark, 0.31, -0.91, 0.11, 0.1);
  const leftArm = roundedBox(0.28, 0.62, 0.3, shell, -0.68, -0.28, 0, 0.1);
  leftArm.rotation.z = -0.3;
  sphere(0.16, clay, -0.77, -0.6, 0.03);
  const rightArm = roundedBox(0.28, 0.55, 0.3, shell, 0.68, -0.13, 0.04, 0.1);
  rightArm.rotation.z = 0.55;
  // The checklist scene needs an empty hand so its seven-job board is clear.
  // Hero / farewell keep a free waving / pointing arm — no clipboard prop.
  if (!checklist && !admirer && !pricing && !faq && !farewell && !hero) {
    roundedBox(0.67, 0.86, 0.13, cream, 0.95, -0.23, 0.38, 0.045);
    [0.18, 0.3, 0.44].forEach((height, i) => {
      roundedBox(0.1, height, 0.045, i === 2 ? clay : shell, 0.76 + i * 0.18, -0.49 + height / 2, 0.47, 0.016);
    });
    roundedBox(0.38, 0.045, 0.045, dark, 0.95, 0.05, 0.47, 0.015);
  }
  const rightHand = sphere(0.14, clay, 0.73, -0.52, 0.53);
  agent.rotation.set(0, 0, 0);
  agent.scale.setScalar(0.68);
  agent.position.set(-0.85, 1.32, -0.25);
  world.add(agent);

  const MONO = "'Space Mono', ui-monospace, monospace";
  const SANS = "'Bricolage Grotesque', system-ui, sans-serif";
  // Texels per world unit, clamped: below the floor type breaks up, above the
  // ceiling it costs memory for detail no display resolves.
  const TEXEL_FLOOR = 190;
  const TEXEL_CEIL = 1600;
  const prints = [];
  let texels = TEXEL_FLOOR;

  function print(text, width, height, x, y, z, parent, color = '#38422b', size = 46, font = SANS) {
    const surface = document.createElement('canvas');
    const texture = new THREE.CanvasTexture(surface);
    texture.colorSpace = THREE.SRGBColorSpace;
    texture.generateMipmaps = false;
    texture.minFilter = THREE.LinearFilter;
    texture.magFilter = THREE.LinearFilter;
    texture.anisotropy = renderer.capabilities.getMaxAnisotropy();

    function draw() {
      const pixels = Math.round(width * texels);
      surface.width = pixels;
      surface.height = Math.max(2, Math.round(pixels * height / width));
      // The original geometry was laid out against a 768px canvas; keeping that
      // as the unit means resolution can change without the layout moving.
      const scale = surface.width / 768;
      const ctx = surface.getContext('2d');
      ctx.clearRect(0, 0, surface.width, surface.height);
      ctx.fillStyle = color;
      ctx.textAlign = 'left';
      ctx.textBaseline = 'middle';
      // Fit by shrinking the type, never by squeezing it: canvas `maxWidth`
      // scales glyphs horizontally, which on Space Mono is instantly obvious.
      let px = size * scale;
      const room = surface.width - 30 * scale;
      ctx.font = `600 ${px}px ${font}`;
      const measured = ctx.measureText(text).width;
      if (measured > room) {
        px *= room / measured;
        ctx.font = `600 ${px}px ${font}`;
      }
      ctx.fillText(text, 15 * scale, surface.height / 2);
      texture.needsUpdate = true;
    }

    draw();
    prints.push(draw);
    const mesh = new THREE.Mesh(new THREE.PlaneGeometry(width, height), new THREE.MeshBasicMaterial({ map: texture, transparent: true, depthWrite: false }));
    mesh.position.set(x, y, z); parent.add(mesh);
    return mesh;
  }

  // No chest lettering in the dock — at launcher size it reads as a watermark
  // over the face. The panel / “Ask Willy” chip already name him.

  // Redraw every label at the resolution it is about to be shown at. Called
  // from the resize pass, where the frustum width is known; the 12% deadband
  // keeps a drag-resize from re-rasterising the whole console every frame.
  function retexture(next) {
    const wanted = Math.max(TEXEL_FLOOR, Math.min(TEXEL_CEIL, next));
    if (Math.abs(wanted - texels) / texels < 0.12) return;
    texels = wanted;
    prints.forEach(draw => draw());
  }
  let wavingArm = null;
  if (farewell || hero) {
    // Hero: full figure stays inside the canvas (no arm clipped by the login
    // divider). Wave upward like the dock pose instead of pointing sideways.
    agent.position.set(hero ? -0.05 : -0.12, hero ? -0.02 : -0.1, hero ? 0.15 : 0.2);
    agent.scale.setScalar(hero ? 1.02 : 0.92);
    if (hero) agent.rotation.y = -0.06;
    // Pivot the entire forearm and hand at the shoulder, so they stay attached.
    wavingArm = new THREE.Group();
    wavingArm.position.set(0.67, 0.05, 0.12);
    agent.add(wavingArm);
    rightArm.position.set(0.1, -0.25, 0);
    rightArm.rotation.z = 0;
    rightHand.position.set(0.1, -0.57, 0);
    wavingArm.add(rightArm, rightHand);
    wavingArm.rotation.z = hero ? 1.95 : 2.05;
    head.rotation.z = hero ? -0.04 : -0.06;
  }
  world.rotation.set(0, 0, 0);

  scene.add(new THREE.HemisphereLight(0xfff7e5, 0x4c4432, 3));
  const key = new THREE.DirectionalLight(0xfff5dc, 5);
  key.position.set(-3, 4, 5);
  scene.add(key);
  const rim = new THREE.DirectionalLight(0xe7bda2, 2.2);
  rim.position.set(4, 0, 2);
  scene.add(rim);
  let paused = reduced.matches;
  let visible = true;
  let frame = 0;
  let last = 0;
  let elapsed = 0;
  let targetX = 0;
  let targetY = 0;
  let pointer = null;
  // Keep expressions within the flat screen, clear of its bevelled border.
  function containExpression(part) {
    if (!part.geometry.boundingBox) part.geometry.computeBoundingBox();
    const bounds = part.geometry.boundingBox;
    part.position.x = THREE.MathUtils.clamp(part.position.x,
      -0.64 - bounds.min.x * part.scale.x, 0.64 - bounds.max.x * part.scale.x);
    part.position.y = THREE.MathUtils.clamp(part.position.y,
      -0.34 - bounds.min.y * part.scale.y, 0.34 - bounds.max.y * part.scale.y);
  }
  let happiness = 0;
  let targetHappiness = 0;
  function render(time = 0) {
    frame = 0;
    const delta = Math.min((time - last) / 1000, 0.04);
    last = time;
    if (!paused) {
      elapsed += delta;
      if (wavingArm) {
        if (hero) {
          // Gentle upward wave - keeps the hand inside the canvas.
          wavingArm.rotation.z = 1.95 + Math.sin(elapsed * Math.PI * 2 / 1.4) * 0.18;
          wavingArm.rotation.y = 0;
        } else {
          // A continuous, gentle wave with no pause between cycles.
          wavingArm.rotation.z = 2.05 + Math.sin(elapsed * Math.PI * 2 / 1.2) * 0.24;
        }
      }
      updateGaze();
      // Time-based damping keeps the response consistent at any refresh rate.
      const follow = 1 - Math.exp(-14 * delta);
      // Hero (login) and the default agent always turn the neck toward the
      // pointer. Marketing modes (pricing / faq / …) keep the torso fixed and
      // only slide the eyes on the screen.
      if ((admirer || pricing || faq || farewell) && !hero) {
        // Heart-shaped eyes slide inside the screen; head and body stay fixed.
        eyes.forEach((eye, i) => {
          const x = (i === 0 ? -0.28 : 0.28) + Math.sin(targetX) * 0.085;
          const y = 0.05 - Math.sin(targetY) * (faq ? 0.04 : 0.07);
          eye.position.x += (x - eye.position.x) * follow;
          eye.position.y += (y - eye.position.y) * follow;
        });
      } else {
        head.rotation.y += (targetX - head.rotation.y) * follow;
        head.rotation.x += (targetY - head.rotation.x) * follow;
        head.rotation.z = (hero ? -0.04 : 0) + Math.sin(elapsed * 1.3) * 0.015;
        const blink = elapsed % 5.2;
        eyes.forEach(eye => { eye.scale.y = blink > 4.95 ? 0.15 : 1; });
        // Pupils drift slightly inside the face toward the pointer too.
        if (hero) {
          eyes.forEach((eye, i) => {
            const baseX = i === 0 ? -0.28 : 0.28;
            const x = baseX + Math.sin(targetX) * 0.06;
            const y = 0.05 - Math.sin(targetY) * 0.05;
            eye.position.x += (x - eye.position.x) * follow;
            eye.position.y += (y - eye.position.y) * follow;
          });
        }
      }
    }
    if (pricing) {
      const blend = paused ? 1 : 1 - Math.exp(-10 * delta);
      happiness += (targetHappiness - happiness) * blend;
      smile.scale.set(1 + happiness * 0.25, 0.7 + happiness * 0.5, 1);
      smile.visible = happiness < 1.65;
      grin.visible = happiness >= 1.65;
      grin.scale.setScalar(0.9 + happiness * 0.04);
      eyes.forEach((eye,i) => {
        eye.scale.y = 1 - happiness * 0.12;
        eye.visible = happiness < 2.5;
        happyEyes[i].visible = happiness >= 2.5;
        happyEyes[i].position.copy(eye.position);
      });
      cheeks.forEach(cheek => { cheek.material.opacity = happiness * 0.29; });
      celebration.forEach((sparkle,i) => {
        sparkle.visible = happiness >= Math.floor(i/2) + 0.55;
        const pulse = paused ? 1 : 0.9 + Math.sin(elapsed*2.8 + i)*0.1;
        sparkle.scale.setScalar(pulse);
        sparkle.rotation.z = paused ? 0 : Math.sin(elapsed*1.4+i)*0.13;
      });
      [smile, grin, ...eyes, ...happyEyes, ...cheeks].forEach(containExpression);
    }
    renderer.render(scene, camera);
    if (!paused && visible && !document.hidden) frame = requestAnimationFrame(render);
  }
  function resume() {
    if (!frame) { last = performance.now(); frame = requestAnimationFrame(render); }
  }
  const observer = new ResizeObserver(([entry]) => {
    const { width, height } = entry.contentRect;
    renderer.setSize(width, height, false);
    const aspect = width / Math.max(height, 1);
    const viewHeight = farewell ? Math.max(3.1, 2.7 / aspect) : faq ? Math.max(3.55, 2.8 / aspect) : pricing ? Math.max(1.78, 2.35 / aspect) : admirer ? Math.max(1.6, 3.4 / aspect) : checklist ? Math.max(3.15, 3.25 / aspect) : hero ? Math.max(3.7, 3.2 / aspect) : Math.max(4.8, 3.55 / aspect);
    camera.left = -viewHeight * aspect / 2;
    camera.right = viewHeight * aspect / 2;
    // Hero framing: keep the full figure (feet to antenna) inside the canvas.
    camera.top = faq ? -1.5 + viewHeight : pricing ? -0.04 + viewHeight : admirer ? 0.08 + viewHeight : hero ? (0.05 + viewHeight / 2) : (farewell ? 0.15 : faq ? 0.15 : checklist ? 0.1 : 0.28) + viewHeight / 2;
    camera.bottom = faq ? -1.5 : pricing ? -0.04 : admirer ? 0.08 : hero ? (0.05 - viewHeight / 2) : (farewell ? 0.15 : faq ? 0.15 : checklist ? 0.1 : 0.28) - viewHeight / 2;
    camera.updateProjectionMatrix();
    // Device pixels the console is actually drawn across, per world unit.
    retexture((width * renderer.getPixelRatio()) / (camera.right - camera.left));
    resume();
  });
  observer.observe(canvas.parentElement);
  new IntersectionObserver(([entry]) => { visible = entry.isIntersecting; if (visible) resume(); }).observe(canvas);
  const headPosition = new THREE.Vector3();
  function updateGaze() {
    if (!pointer) return;
    const bounds = canvas.getBoundingClientRect();
    if (!bounds.width || !bounds.height) return;
    // Use a stable face pivot; rotating the head must not move its own target.
    headPosition.copy(agent.position);
    headPosition.y += 0.62 * agent.scale.y;
    world.localToWorld(headPosition);
    headPosition.project(camera);
    const faceX = bounds.left + (headPosition.x + 1) * bounds.width / 2;
    const faceY = bounds.top + (1 - headPosition.y) * bounds.height / 2;
    const dx = pointer.x - faceX;
    const dy = pointer.y - faceY;
    const depth = bounds.width * 0.65;
    // Continuous look angles: no clipped range where the gaze stops changing.
    targetX = Math.atan2(dx, depth);
    targetY = Math.atan2(dy, Math.hypot(depth, dx));
  }
  document.addEventListener('pointermove', event => {
    if (event.pointerType === 'touch') return;
    pointer = { x: event.clientX, y: event.clientY };
  }, { passive: true });
  function resetGaze() { pointer = null; targetX = targetY = 0; }
  // Leaving the hero is fine. Reset only when leaving the browser document.
  document.documentElement.addEventListener('pointerleave', resetGaze);
  document.addEventListener('pointercancel', resetGaze);
  window.addEventListener('blur', resetGaze);
  function updateToggle() {
    if (!toggle) return;
    toggle.textContent = paused ? '▶' : 'Ⅱ';
    toggle.setAttribute('aria-pressed', String(paused));
    toggle.setAttribute('aria-label', `${paused ? 'Play' : 'Pause'} 3D animation`);
  }
  toggle?.addEventListener('click', () => { paused = !paused; updateToggle(); resume(); });
  reduced.addEventListener('change', () => { paused = reduced.matches; updateToggle(); resume(); });
  if (pricing) {
    const cards = [...document.querySelectorAll('#pricing [data-plan-tier]')];
    let hoverTier = 0;
    let focusTier = 0;
    function updateHappiness() {
      targetHappiness = hoverTier || focusTier;
      canvas.parentElement.setAttribute('aria-label', `Robot above the Agency plan. ${['Relaxed', 'Happy about Starter', 'Even happier about Growth', 'Happiest about Agency'][targetHappiness]}. Its eyes follow the pointer.`);
      resume();
    }
    cards.forEach(card => {
      const tier = Number(card.dataset.planTier);
      card.addEventListener('pointerenter', () => { hoverTier = tier; updateHappiness(); });
      card.addEventListener('pointerleave', () => { if (hoverTier === tier) hoverTier = 0; updateHappiness(); });
      card.addEventListener('focusin', () => { focusTier = tier; updateHappiness(); });
      card.addEventListener('focusout', event => {
        if (!card.contains(event.relatedTarget)) { focusTier = 0; updateHappiness(); }
      });
    });
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden) resume(); });
  canvas.addEventListener('webglcontextlost', event => { event.preventDefault(); cancelAnimationFrame(frame); canvas.parentElement.hidden = true; if (toggle) toggle.hidden = true; canvas.setAttribute('aria-label', 'Willy requires WebGL to display.'); });
  updateToggle();
  resume();
  // Space Mono and Bricolage Grotesque come from the page's stylesheet, so the
  // first rasterisation can happen before they are ready and fall back. Draw
  // the labels again once they land.
  if (document.fonts?.ready) {
    document.fonts.ready.then(() => { prints.forEach(draw => draw()); resume(); });
  }
} catch {
  // Keep a useful message when the browser cannot render the 3D model.
  canvas.parentElement.hidden = true;
  if (toggle) toggle.hidden = true;
  canvas.setAttribute('aria-label', 'Willy requires WebGL to display.');
}

}

