import * as THREE from 'three';

const D2R = Math.PI / 180;
const $ = id => document.getElementById(id);
const stage = $('stage');

// ───────────────────────────────────────────────────────────── scene

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0d1117);
scene.fog = new THREE.Fog(0x0d1117, 3.2, 8.5);

const camera = new THREE.PerspectiveCamera(42, 1, 0.05, 60);
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
renderer.shadowMap.enabled = true;
renderer.shadowMap.type = THREE.PCFSoftShadowMap;
stage.appendChild(renderer.domElement);

scene.add(new THREE.HemisphereLight(0x9fc5ff, 0x0a0d12, 0.85));
const key = new THREE.DirectionalLight(0xffffff, 1.5);
key.position.set(2.2, 3.4, 2.6);
key.castShadow = true;
key.shadow.mapSize.set(2048, 2048);
key.shadow.camera.left = key.shadow.camera.bottom = -1.6;
key.shadow.camera.right = key.shadow.camera.top = 1.6;
key.shadow.bias = -0.0012;
scene.add(key);
const rim = new THREE.DirectionalLight(0x5f8cff, 0.6);
rim.position.set(-2.4, 1.2, -2.0);
scene.add(rim);

const floor = new THREE.Mesh(
  new THREE.CircleGeometry(4.2, 72).rotateX(-Math.PI / 2),
  new THREE.MeshStandardMaterial({ color: 0x11161d, roughness: 0.96, metalness: 0 }));
floor.receiveShadow = true;
scene.add(floor);

const grid = new THREE.PolarGridHelper(3.6, 12, 6, 72, 0x1d2630, 0x161d26);
grid.position.y = 0.002;
scene.add(grid);

// ───────────────────────────────────────── bearing ring + ticks

const ringGroup = new THREE.Group();
ringGroup.position.y = 0.004;
scene.add(ringGroup);
{
  const r = 1.55;
  for (let a = 0; a < 360; a += 15) {
    const major = a % 45 === 0;
    const len = major ? 0.12 : 0.055;
    const tick = new THREE.Mesh(
      new THREE.PlaneGeometry(0.008, len).rotateX(-Math.PI / 2),
      new THREE.MeshBasicMaterial({ color: major ? 0x3d4a58 : 0x242d38 }));
    const rad = a * D2R;
    tick.position.set(Math.sin(rad) * (r + len / 2), 0, Math.cos(rad) * (r + len / 2));
    tick.rotation.y = rad;
    ringGroup.add(tick);
  }
  const front = new THREE.Mesh(
    new THREE.ConeGeometry(0.045, 0.1, 3).rotateX(-Math.PI / 2),
    new THREE.MeshBasicMaterial({ color: 0x2f3b48 }));
  front.position.set(0, 0, r + 0.17);
  ringGroup.add(front);
}

// ───────────────────────────────────────────────────── eye displays
// Two GC9A01 240x240 round panels, drawn to canvas textures.

function makeEye() {
  const cv = document.createElement('canvas');
  cv.width = cv.height = 240;
  const tex = new THREE.CanvasTexture(cv);
  tex.colorSpace = THREE.SRGBColorSpace;
  tex.anisotropy = 4;
  return { cv, ctx: cv.getContext('2d'), tex };
}
const eyeL = makeEye(), eyeR = makeEye();

function drawEye(eye, s) {
  const { ctx } = eye, S = 240, c = S / 2;
  ctx.clearRect(0, 0, S, S);
  ctx.fillStyle = '#05080c';
  ctx.beginPath(); ctx.arc(c, c, c, 0, 7); ctx.fill();

  // Russell circumplex: arousal opens the eyes and dilates the iris; pleasant
  // valence lifts the lower lid (smiling eyes); unpleasant valence lowers and
  // slants the upper lid: inner corners up when low-arousal (sad, worried),
  // down when high-arousal (tense, angry). Presets reproduce the old shapes.
  const v = s.valence || 0, a = s.arousal || 0, open = 1 - s.blink;
  const neg = Math.max(0, -v), pos = Math.max(0, v);
  const irisR = 52 * (s.iris_scale || 1) * (1 + 0.08 * a) * (1 - 0.15 * neg);
  const lidTop = (a >= 0 ? -0.06 * a : 0.76 * Math.pow(-a, 1.3)) + 0.3 * neg;
  const lidBot = 0.12 * Math.max(0, -a);
  const squint = 0.6 * pos;
  const tilt = 0.75 * neg * (a > 0.55 ? -1 : 1);

  // look down when unpleasant or low-energy, a little up when alert
  const gx = c + (s.gaze || 0) * 26,
        gy = c + 10 * neg + 8 * Math.max(0, -a) - 6 * Math.max(0, a);
  const pupilR = irisR * (0.38 + 0.12 * a);                  // dilates with arousal
  const bulge = 40 * pos;                                    // smiling lower lid
  const sparkle = Math.min(1, 3 * pos * Math.max(0, a));     // extra glint when excited

  // glow
  const g = ctx.createRadialGradient(gx, gy, irisR * 0.2, gx, gy, irisR * 2.3);
  g.addColorStop(0, s.colour + 'cc'); g.addColorStop(1, s.colour + '00');
  ctx.fillStyle = g;
  ctx.beginPath(); ctx.arc(gx, gy, irisR * 2.3, 0, 7); ctx.fill();

  // iris + highlight
  ctx.fillStyle = s.colour;
  ctx.beginPath(); ctx.arc(gx, gy, irisR, 0, 7); ctx.fill();
  ctx.fillStyle = '#06090d';
  ctx.beginPath(); ctx.arc(gx, gy, pupilR, 0, 7); ctx.fill();
  ctx.fillStyle = '#ffffff';
  ctx.globalAlpha = 0.85;
  ctx.beginPath(); ctx.arc(gx - irisR * 0.3, gy - irisR * 0.34, irisR * 0.21, 0, 7); ctx.fill();
  if (sparkle > 0.05) {
    ctx.globalAlpha = 0.85 * sparkle;
    ctx.beginPath(); ctx.arc(gx + irisR * 0.32, gy + irisR * 0.3, irisR * 0.1, 0, 7); ctx.fill();
  }
  ctx.globalAlpha = 1;

  // eyelids
  ctx.fillStyle = '#05080c';
  const topY = S * (lidTop + (1 - open) * 0.52);
  ctx.save(); ctx.translate(c, 0); ctx.rotate(tilt * (eye === eyeL ? -1 : 1));   // tilt > 0: inner corners up
  ctx.fillRect(-S, -S, S * 2, S + topY); ctx.restore();
  const botY = S * (1 - lidBot - (1 - open) * 0.48 - squint * 0.3);
  // lower lid: a parabola peaking `bulge` px above botY at the centre
  ctx.beginPath(); ctx.moveTo(0, S); ctx.lineTo(0, botY);
  ctx.quadraticCurveTo(c, botY - 2 * bulge, S, botY); ctx.lineTo(S, S); ctx.closePath(); ctx.fill();

  // bezel
  ctx.strokeStyle = '#1b222c'; ctx.lineWidth = 8;
  ctx.beginPath(); ctx.arc(c, c, c - 4, 0, 7); ctx.stroke();
  eye.tex.needsUpdate = true;
}

// ───────────────────────────────────────────────────────── robot
// Geometry comes from the Fusion CAD via tools/bake_urdf.py: 136 URDF links
// flattened into the four bodies that actually move. Nothing here is modelled
// by hand, so the twin cannot drift from the mechanical design.

const SHELL = { color: 0xd8dde3, roughness: 0.42, metalness: 0.06 };
const DARK  = { color: 0x39414c, roughness: 0.55, metalness: 0.35 };
const mat = p => new THREE.MeshStandardMaterial(p);

// URDF is Z-up, three.js is Y-up.
const robotRoot = new THREE.Group();
robotRoot.rotation.x = -Math.PI / 2;
scene.add(robotRoot);

const pivots = {};        // joint name -> { rot, axis, sign }
const eyeDiscs = [], eyeHosts = [];
let robotMeta = null;

function countMeshes(obj) {
  let n = 0;
  obj.traverse(o => { if (o.isMesh) n++; });
  return n;
}

// Floor, grid and bearing ring are authored for a 1 m-ish robot; rescale them
// around whatever the CAD actually is so the robot is not a speck in a room.
function worldScale(reach) {
  const k = Math.max(0.25, reach / 0.18);
  floor.scale.setScalar(k * 0.9);
  grid.scale.setScalar(k * 0.75);
  ringGroup.scale.setScalar(k * 0.62);
  spk.scale.setScalar(k * 0.45);
  scene.fog.near = 3.2 * k * 0.5;
  scene.fog.far = 8.5 * k * 0.5;
}

function parseSTL(buf) {
  const dv = new DataView(buf);
  const n = dv.getUint32(80, true);
  const pos = new Float32Array(n * 9);
  const nor = new Float32Array(n * 9);
  let o = 84;
  for (let i = 0; i < n; i++) {
    const nx = dv.getFloat32(o, true), ny = dv.getFloat32(o + 4, true),
          nz = dv.getFloat32(o + 8, true);
    o += 12;
    for (let v = 0; v < 3; v++) {
      const k = i * 9 + v * 3;
      pos[k]     = dv.getFloat32(o, true);
      pos[k + 1] = dv.getFloat32(o + 4, true);
      pos[k + 2] = dv.getFloat32(o + 8, true);
      nor[k] = nx; nor[k + 1] = ny; nor[k + 2] = nz;
      o += 12;
    }
    o += 2;                                  // attribute byte count
  }
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.BufferAttribute(pos, 3));
  g.setAttribute('normal', new THREE.BufferAttribute(nor, 3));
  g.computeBoundingSphere();
  return g;
}

async function loadMesh(url, material) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: ${r.status}`);
  const m = new THREE.Mesh(parseSTL(await r.arrayBuffer()), material);
  m.castShadow = true; m.receiveShadow = true;
  return m;
}

async function buildRobot() {
  const meta = await (await fetch('./robot.json')).json();
  robotMeta = meta;

  let parent = robotRoot;
  const jobs = [];
  for (const g of meta.chain) {
    let host = parent;
    if (g !== 'base') {
      const j = meta.joints[g];
      const pivot = new THREE.Group();
      pivot.matrixAutoUpdate = false;
      pivot.matrix.fromArray(j.origin_matrix);
      parent.add(pivot);

      const rot = new THREE.Group();
      pivot.add(rot);
      const ax = new THREE.Vector3().fromArray(j.axis_local).normalize();
      pivots[g] = { rot, axis: ax, sign: g === meta.chain[1] ? meta.frame.yaw_sign : 1 };
      host = rot;
    }
    for (const [kind, fn] of Object.entries(meta.groups[g] || {})) {
      jobs.push(loadMesh(`./meshes/${fn}`, mat(kind === 'shell' ? SHELL : DARK))
        .then(m => host.add(m)));
    }
    parent = host;
  }

  // eye displays, placed from the CAD (one modelled, one mirrored)
  const headHost = parent;
  [eyeL, eyeR].forEach((eye, i) => {
    const d = meta.displays[i];
    if (!d) return;
    eyeHosts.push(d);
    const holder = new THREE.Group();
    holder.matrixAutoUpdate = false;
    // face_matrix, not matrix: the LCD's own local Z is not the outward
    // normal, so the baker builds a frame whose +Z points out of the dome
    holder.matrix.fromArray(d.face_matrix || d.matrix);
    headHost.add(holder);
    const r = meta.eye_radius_m || 0.0162;
    const disc = new THREE.Mesh(
      new THREE.CircleGeometry(r, 40),
      new THREE.MeshBasicMaterial({ map: eye.tex, toneMapped: false,
                                    side: THREE.DoubleSide }));
    disc.position.z = 0.0015;   // sit just proud of the shell surface
    holder.add(disc);
    eyeDiscs.push(disc);
  });

  await Promise.all(jobs);

  // frame the camera on the real bounding box
  const bb = meta.bbox;
  const h = bb.max[2] - bb.min[2];
  orbit.ty = (bb.min[2] + bb.max[2]) / 2 - bb.min[2];
  orbit.dist = h * 2.6;

  // Start in FRONT of the robot. The CAD front vector is in URDF coords; after
  // the Z-up -> Y-up rotation (x,y,z) -> (x,z,-y) it becomes (fx, 0, -fy).
  const fw = meta.frame.front_world;
  orbit.az = Math.atan2(fw[0], -fw[1]) + 0.5;
  orbit.el = 0.22;

  $('fw').textContent = `${meta.source} · ${meta.bbox.size_mm.join(' × ')} mm`;

  // stand the robot on the floor
  robotRoot.position.y = -bb.min[2];

  // the scene furniture was sized for a room; scale it to the robot
  const reach = Math.max(bb.max[0] - bb.min[0], bb.max[1] - bb.min[1]);
  worldScale(reach);

  window.__twin = {                       // test handle for tools/shot.py
    meta, pivots, orbit, THREE, scene, send,
    meshCount: countMeshes(robotRoot),
    eyes: eyeDiscs.length,
    eyeWorld: eyeDiscs.map(d => {
      const v = new THREE.Vector3(); d.getWorldPosition(v); return v.toArray();
    }),
  };
}

buildRobot().catch(e => {
  $('fw').textContent = 'CAD load failed — run tools/bake_urdf.py';
  console.error(e);
});

// ─────────────────────────────────────────────────── speaker marker

const spk = new THREE.Group();
scene.add(spk);
{
  const post = new THREE.Mesh(
    new THREE.CylinderGeometry(0.004, 0.004, 0.42, 10),
    new THREE.MeshBasicMaterial({ color: 0x2e7d4f }));
  post.position.y = 0.21; spk.add(post);
  const head2 = new THREE.Mesh(
    new THREE.SphereGeometry(0.036, 20, 14),
    new THREE.MeshStandardMaterial({
      color: 0x3fb950, emissive: 0x1d6b30, roughness: 0.4 }));
  head2.position.y = 0.45; head2.castShadow = true; spk.add(head2);
  const ring = new THREE.Mesh(
    new THREE.RingGeometry(0.05, 0.062, 32).rotateX(-Math.PI / 2),
    new THREE.MeshBasicMaterial({ color: 0x3fb950, transparent: true, opacity: 0.5 }));
  ring.position.y = 0.006; spk.add(ring);
}

// line of sight from the head to the speaker
const losMat = new THREE.LineBasicMaterial({ color: 0x3fb950, transparent: true, opacity: 0.3 });
const losPos = new THREE.BufferAttribute(new Float32Array(6), 3);
const losGeo = new THREE.BufferGeometry();
losGeo.setAttribute('position', losPos);
const los = new THREE.Line(losGeo, losMat);
scene.add(los);
const headWorld = new THREE.Vector3();

// ─────────────────────────────────────────────────── orbit controls

let orbit = { az: 0.42, el: 0.28, dist: 1.35, tx: 0, ty: 0.19, tz: 0 };
let drag = null;
renderer.domElement.addEventListener('pointerdown', e => {
  drag = { x: e.clientX, y: e.clientY };
  renderer.domElement.setPointerCapture(e.pointerId);
});
renderer.domElement.addEventListener('pointerup', e => {
  drag = null;
  try { renderer.domElement.releasePointerCapture(e.pointerId); } catch {}
});
renderer.domElement.addEventListener('pointermove', e => {
  if (!drag) return;
  orbit.az -= (e.clientX - drag.x) * 0.006;
  orbit.el = Math.max(-0.25, Math.min(1.35, orbit.el + (e.clientY - drag.y) * 0.005));
  drag = { x: e.clientX, y: e.clientY };
});
renderer.domElement.addEventListener('wheel', e => {
  e.preventDefault();
  orbit.dist = Math.max(0.45, Math.min(5, orbit.dist * (1 + Math.sign(e.deltaY) * 0.1)));
}, { passive: false });

function updateCamera() {
  const ce = Math.cos(orbit.el);
  camera.position.set(
    orbit.tx + Math.sin(orbit.az) * ce * orbit.dist,
    orbit.ty + Math.sin(orbit.el) * orbit.dist,
    orbit.tz + Math.cos(orbit.az) * ce * orbit.dist);
  camera.lookAt(orbit.tx, orbit.ty, orbit.tz);
}

function resize() {
  const w = stage.clientWidth, h = stage.clientHeight;
  renderer.setSize(w, h);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
addEventListener('resize', resize);

// ─────────────────────────────────────────────────────── websocket

let sock = null, meta = null, last = null, lastBearing = null;

function send(o) { if (sock && sock.readyState === 1) sock.send(JSON.stringify(o)); }

function connect() {
  sock = new WebSocket(`ws://${location.host}/viewer`);
  sock.onopen = () => { $('fw').textContent = 'connected · 50 Hz'; };
  sock.onclose = () => {
    $('fw').textContent = 'disconnected — retrying';
    setTimeout(connect, 1200);
  };
  sock.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.type === 'init') { meta = m; buildControls(m); }
    else if (m.type === 'frame') onFrame(m);
  };
}

function buildControls(m) {
  const shells = $('shells');
  shells.innerHTML = '';
  m.characters.forEach(c => {
    const b = document.createElement('button');
    b.textContent = c.name; b.dataset.id = c.id;
    b.onclick = () => send({ type: 'shell', id: c.id });
    shells.appendChild(b);
  });

  const mo = $('motions');
  mo.innerHTML = '';
  m.motions.filter(x => x !== 'attend').forEach(id => {
    const b = document.createElement('button');
    b.textContent = id.replace(/_/g, ' ');
    b.onclick = () => send({ type: 'play', id });
    mo.appendChild(b);
  });

  const ex = $('exprs');
  ex.innerHTML = '';
  m.expressions.forEach(e => {
    const b = document.createElement('button');
    b.textContent = e; b.dataset.expr = e;
    b.onclick = () => send({ type: 'eyes', expression: e });
    ex.appendChild(b);
  });
}

function onFrame(f) {
  last = f;

  const JOINT_CH = { yaw: 'base_yaw', pitch: 'head_pitch', roll: 'head_roll' };
  for (const [name, p] of Object.entries(pivots)) {
    const deg = f.joints[JOINT_CH[name]];
    if (deg === undefined) continue;
    p.rot.quaternion.setFromAxisAngle(p.axis, p.sign * deg * D2R);
  }

  const eyeState = {
    valence: f.eyes.valence, arousal: f.eyes.arousal, gaze: f.eyes.gaze, blink: f.eyes.blink,
    colour: f.eyes.colour, iris_scale: f.eyes.iris_scale,
  };
  drawEye(eyeL, eyeState);
  drawEye(eyeR, eyeState);

  // bearing 0 = the CAD front vector, positive CCW seen from above
  const f0 = robotMeta ? robotMeta.frame.front_yaw_deg * D2R : 0;
  const sc = robotMeta ? Math.max(0.25, 0.182 / 0.18) : 1;
  const rad = f.speaker.deg * D2R + f0, d = f.speaker.dist * 0.18 * sc;
  // URDF world (x,y) -> three.js (x,-z) after the Z-up rotation
  spk.position.set(Math.cos(rad) * d, 0, -Math.sin(rad) * d);
  spk.visible = true;

  if (pivots.pitch) pivots.pitch.rot.getWorldPosition(headWorld);
  losPos.setXYZ(0, headWorld.x, headWorld.y, headWorld.z);
  losPos.setXYZ(1, spk.position.x, 0.45, spk.position.z);
  losPos.needsUpdate = true;
  losGeo.computeBoundingSphere();
  losMat.opacity = f.speaker.active ? 0.6 : 0.16;

  $('j0').textContent = f.joints.base_yaw.toFixed(1) + '°';
  $('j1').textContent = f.joints.head_pitch.toFixed(1) + '°';
  $('j2').textContent = f.joints.head_roll.toFixed(1) + '°';
  if (f.servos) {
    const over = (f.servos.over || []).length > 0;
    $('sv').textContent = `${f.servos.a.toFixed(1)}° / ${f.servos.b.toFixed(1)}°`;
    $('sv').style.color = over ? 'var(--bad)' : 'var(--fg)';
  }
  $('swapms').textContent = f.swap_ms == null ? '—' : f.swap_ms.toFixed(0) + ' ms';
  $('brain').innerHTML = `<span class="dot${f.brain ? ' up' : ''}"></span>` +
                         (f.brain ? 'connected' : 'waiting');

  const heading = f.joints.base_yaw;   // no head yaw exists
  let e = f.speaker.deg - heading;
  e = ((e + 540) % 360) - 180;
  $('err').textContent = Math.abs(e).toFixed(1) + '°';
  $('err').style.color = Math.abs(e) <= 15 ? 'var(--ok)' : 'var(--warn)';

  if (meta) {
    const c = meta.characters.find(x => x.id === f.character);
    if (c) {
      $('neckfirst').textContent = c.profile.lead_roll_deg + '°';
      $('profscale').textContent = `${c.profile.speed_scale} × ${c.profile.amplitude_scale}`;
    }
    [...$('shells').children].forEach(b => b.classList.toggle('on', b.dataset.id === f.character));
    [...$('exprs').children].forEach(b =>
      b.classList.toggle('on', b.dataset.expr === f.eyes.expression));
  }

  $('hud').innerHTML =
    `<b>${f.character_name}</b> &nbsp; base <b>${f.joints.base_yaw.toFixed(1)}°</b>` +
    ` &nbsp; pitch <b>${f.joints.head_pitch.toFixed(1)}°</b>` +
    ` &nbsp; roll <b>${f.joints.head_roll.toFixed(1)}°</b>` +
    (f.busy ? ' &nbsp; <b style="color:#58a6ff">moving</b>' : '') +
    (f.speaking ? ` &nbsp; <b style="color:#d2a8ff">“${f.say}”</b>` : '');

  const log = $('log');
  const html = (f.events || []).slice().reverse().map(e2 =>
    `<div><span class="k ${e2.kind}">${e2.kind}</span><span class="m">${
      e2.text.replace(/[<>&]/g, ch => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[ch]))
    }</span></div>`).join('');
  if (html !== log.dataset.prev) { log.innerHTML = html; log.dataset.prev = html; }
}

// ───────────────────────────────────────────────────────── controls

$('sdeg').oninput = e => {
  $('sdegv').textContent = e.target.value + '°';
  send({ type: 'speaker', deg: +e.target.value, dist: +$('sdist').value });
};
$('sdist').oninput = e => {
  $('sdistv').textContent = (+e.target.value).toFixed(1) + ' m';
  send({ type: 'speaker', deg: +$('sdeg').value, dist: +e.target.value });
};
const talk = $('talk');
let talking = false;
const startTalk = () => {
  if (talking) return;
  talking = true; talk.classList.add('on'); send({ type: 'speaking', on: true });
};
const stopTalk = () => {
  if (!talking) return;
  talking = false; talk.classList.remove('on'); send({ type: 'speaking', on: false });
};
talk.addEventListener('pointerdown', startTalk);
addEventListener('pointerup', stopTalk);
$('home').onclick = () => send({ type: 'home' });
$('utter').onkeydown = e => {
  if (e.key === 'Enter' && e.target.value.trim()) {
    send({ type: 'utter', text: e.target.value.trim() });
    e.target.value = '';
  }
};

// ──────────────────────────────────────────────────────────── loop

function tick() {
  requestAnimationFrame(tick);
  updateCamera();
  renderer.render(scene, camera);
}
resize();
connect();
tick();
