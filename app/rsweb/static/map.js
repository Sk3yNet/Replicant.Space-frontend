// 3D galaxy map of the star catalogue (positions in light-years from Sol).
import * as THREE from "three";
import { OrbitControls } from "three/addons/OrbitControls.js";

const el = document.getElementById("map");
const info = document.getElementById("map-info");
const tip = document.getElementById("map-tip");
const fromSel = document.getElementById("map-from");

const COLORS = { red: 0xff8a65, orange: 0xffb74d, yellow: 0xffe082, white: 0xf5f5f5, blue: 0x90caf9, "blue-white": 0xbbdefb, brown: 0xa1887f };
const RANGE = { ftl_relay: 7.5, relay: 7.5, system_hub: 15, hub: 15 };

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(window.devicePixelRatio);
el.prepend(renderer.domElement);
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(55, 1, 0.1, 5000);
camera.position.set(0, -90, 70);
camera.up.set(0, 0, 1);  // galactic north up
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;

function resize() {
  const w = el.clientWidth, h = el.clientHeight;
  renderer.setSize(w, h);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
window.addEventListener("resize", resize);
resize();

// round soft sprite for points
const dot = (() => {
  const c = document.createElement("canvas"); c.width = c.height = 64;
  const g = c.getContext("2d"); const grd = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  grd.addColorStop(0, "rgba(255,255,255,1)"); grd.addColorStop(0.35, "rgba(255,255,255,.8)"); grd.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = grd; g.fillRect(0, 0, 64, 64);
  return new THREE.CanvasTexture(c);
})();

// faint reference grid on the galactic plane
const grid = new THREE.PolarGridHelper(80, 8, 8, 64, 0x1c2638, 0x141b28);
grid.rotation.x = Math.PI / 2;
scene.add(grid);

let data = { stars: [], replicants: [] };
let starPoints, mineGroup = new THREE.Group(), coverGroup = new THREE.Group(), lineGroup = new THREE.Group();
const moveGroup = new THREE.Group();
scene.add(mineGroup, coverGroup, lineGroup, moveGroup);
const movers = [];   // {m: trip, cone, label el}
const labels = [];
let visible = [];
let measure = [];

const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const pos = s => new THREE.Vector3(s.position?.x || 0, s.position?.y || 0, s.position?.z || 0);
const byName = {};

function repStar() {
  const r = data.replicants.find(r => r.code === fromSel?.value) || data.replicants[0];
  if (!r) return null;
  return byName[r.star] || (r.position ? { designation: r.star, position: r.position } : null);
}

function build() {
  [starPoints].forEach(o => o && scene.remove(o));
  mineGroup.clear(); coverGroup.clear();
  labels.forEach(l => l.el.remove()); labels.length = 0;
  const onlyMine = document.getElementById("opt-mine").checked;
  visible = data.stars.filter(s => !onlyMine || s.devices > 0 || data.replicants.some(r => r.star === s.designation));

  const g = new THREE.BufferGeometry();
  const p = new Float32Array(visible.length * 3), col = new Float32Array(visible.length * 3);
  visible.forEach((s, i) => {
    const v = pos(s); p.set([v.x, v.y, v.z], i * 3);
    const c = new THREE.Color(COLORS[(s.color || "").toLowerCase()] ?? 0xdddddd);
    col.set([c.r, c.g, c.b], i * 3);
  });
  g.setAttribute("position", new THREE.BufferAttribute(p, 3));
  g.setAttribute("color", new THREE.BufferAttribute(col, 3));
  starPoints = new THREE.Points(g, new THREE.PointsMaterial({ size: 1.6, map: dot, vertexColors: true, transparent: true, depthWrite: false }));
  scene.add(starPoints);

  const ring = (color, size) => new THREE.Sprite(new THREE.SpriteMaterial({ map: dot, color, transparent: true, opacity: .55, depthWrite: false }));
  for (const s of visible) {
    const v = pos(s);
    const hasRep = data.replicants.some(r => r.star === s.designation);
    const marks = [];
    if (s.devices > 0) marks.push([0x6cb6ff, 3.2]);
    if (s.scanned) marks.push([0x4fd18b, 2.4]);
    if (s.has_hub || (s.infra || []).some(t => t.includes("hub"))) marks.push([0xf0b64f, 2.8]);
    if (hasRep) marks.push([0xff5fa2, 4.2]);
    for (const [c, sz] of marks) { const sp = ring(c); sp.scale.set(sz, sz, 1); sp.position.copy(v); mineGroup.add(sp); }
    for (const t of s.infra || []) {
      const r = Object.entries(RANGE).find(([k]) => t.includes(k));
      if (r) {
        const m = new THREE.Mesh(new THREE.SphereGeometry(r[1], 24, 16),
          new THREE.MeshBasicMaterial({ color: t.includes("hub") ? 0xf0b64f : 0x6cb6ff, wireframe: true, transparent: true, opacity: .07 }));
        m.position.copy(v); coverGroup.add(m);
      }
    }
    if (s.devices > 0 || hasRep || s.has_hub) {
      const d = document.createElement("div");
      d.className = "small"; d.textContent = s.designation;
      Object.assign(d.style, { position: "absolute", color: hasRep ? "#ff9fc8" : "#9fb3d1", pointerEvents: "none", fontFamily: "monospace", fontSize: "11px" });
      el.appendChild(d); labels.push({ el: d, v });
    }
  }
  coverGroup.visible = document.getElementById("opt-cover").checked;
}

// devices in transit between stars: a dashed route and an arrow that moves along it as time passes
const MOVE = 0xb48cff;
function buildMovers() {
  moveGroup.clear();
  movers.forEach(x => x.el.remove()); movers.length = 0;
  for (const m of data.moving || []) {
    const pts = [];
    m.segs.forEach((sg, i) => { if (i === 0) pts.push(pos({ position: sg.a })); pts.push(pos({ position: sg.b })); });
    const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(pts),
      new THREE.LineDashedMaterial({ color: MOVE, dashSize: .6, gapSize: .5, transparent: true, opacity: .75 }));
    line.computeLineDistances();
    moveGroup.add(line);
    const cone = new THREE.Mesh(new THREE.ConeGeometry(.45, 1.4, 12), new THREE.MeshBasicMaterial({ color: MOVE }));
    moveGroup.add(cone);
    const d = document.createElement("div");
    Object.assign(d.style, { position: "absolute", color: "#d9c8ff", pointerEvents: "none", fontFamily: "monospace", fontSize: "11px",
                             whiteSpace: "nowrap" });
    el.appendChild(d);
    movers.push({ m, cone, el: d, v: new THREE.Vector3() });
  }
}
const UP = new THREE.Vector3(0, 1, 0);
function fmt(s) { s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), mi = Math.floor(s % 3600 / 60);
  return h ? `${h}h ${mi}m` : mi ? `${mi}m` : `${s}s`; }
function placeMovers(now) {
  for (const x of movers) {
    const segs = x.m.segs;
    let s = segs.find(sg => now < sg.t1) || segs[segs.length - 1];
    if (now < segs[0].t0) s = segs[0];
    const f = Math.max(0, Math.min(1, (now - s.t0) / Math.max(1, s.t1 - s.t0)));
    const a = pos({ position: s.a }), b = pos({ position: s.b });
    x.v.copy(a).lerp(b, f);
    x.cone.position.copy(x.v);
    const dir = b.clone().sub(a);
    if (dir.lengthSq() > 0) x.cone.quaternion.setFromUnitVectors(UP, dir.normalize());   // the cone points where it's going
    const pct = Math.max(0, Math.min(100, Math.round(100 * (now - x.m.t0) / Math.max(1, x.m.t1 - x.m.t0))));
    x.el.textContent = `▶ ${x.m.label} → ${x.m.destination} · ${pct}% · ${now < x.m.t1 ? fmt((x.m.t1 - now) / 1000) : "arriving"}`;
  }
}

function drawLine(a, b, color = 0xff5fa2) {
  lineGroup.clear();
  const g = new THREE.BufferGeometry().setFromPoints([pos(a), pos(b)]);
  lineGroup.add(new THREE.Line(g, new THREE.LineBasicMaterial({ color })));
}

function focus(s) {
  const v = pos(s);
  controls.target.copy(v);
  camera.position.copy(v.clone().add(new THREE.Vector3(0, -25, 18)));
}

function show(s) {
  const from = repStar();
  const dist = from ? pos(from).distanceTo(pos(s)).toFixed(2) : null;
  const rep = fromSel?.value;
  if (from) drawLine(from, s);
  info.innerHTML = `
    <div class="row between"><h2>${esc(s.designation)}</h2><a class="small" href="/systems/${esc(s.designation)}">open system →</a></div>
    ${s.name ? `<div>${esc(s.name)}</div>` : ""}
    <dl class="kv small">
      <dt>Spectral</dt><dd>${esc(s.spectral_type || "?")} · ${esc(s.color || "")}</dd>
      <dt>Region</dt><dd>${esc(s.region || "?")}</dd>
      <dt>Est. planets</dt><dd>${s.estimated_planets ?? "?"}</dd>
      <dt>Entry point</dt><dd>${esc(s.entry_point || "?")}</dd>
      <dt>Position</dt><dd class="mono">${["x","y","z"].map(k => (s.position?.[k] ?? 0).toFixed(1)).join(", ")}</dd>
      ${dist ? `<dt>From ${esc(from.designation)}</dt><dd>${dist} ly (straight line)</dd>` : ""}
      <dt>Your devices</dt><dd>${s.devices || 0}</dd>
      ${(s.infra || []).length ? `<dt>Infrastructure</dt><dd>${esc(s.infra.join(", "))}</dd>` : ""}
      ${s.has_hub ? "<dt>Hub</dt><dd>yes</dd>" : ""}
    </dl>
    ${rep ? `<div class="row">
      <button class="small" hx-get="/api/route?replicant=${rep}&star=${esc(s.designation)}" hx-target="#route-out">travel estimate</button>
      <button class="small" hx-post="/replicants/${rep}/travel" hx-vals='{"destination":"${esc(s.designation)}","dry_run":"1"}' hx-target="#route-out">preview route</button>
    </div><div id="route-out"></div>` : ""}`;
  htmx.process(info);
}

// picking
const ray = new THREE.Raycaster(); ray.params.Points.threshold = 1.2;
const mouse = new THREE.Vector2();
function pick(ev) {
  const r = renderer.domElement.getBoundingClientRect();
  mouse.set(((ev.clientX - r.left) / r.width) * 2 - 1, -((ev.clientY - r.top) / r.height) * 2 + 1);
  ray.setFromCamera(mouse, camera);
  const hit = starPoints && ray.intersectObject(starPoints)[0];
  return hit ? visible[hit.index] : null;
}
renderer.domElement.addEventListener("pointermove", ev => {
  const s = pick(ev);
  if (s) { tip.style.display = "block"; tip.style.left = ev.offsetX + 12 + "px"; tip.style.top = ev.offsetY + 12 + "px"; tip.textContent = `${esc(s.designation)} ${s.spectral_type || ""}`; }
  else tip.style.display = "none";
});
let downAt = null;
renderer.domElement.addEventListener("pointerdown", ev => downAt = [ev.clientX, ev.clientY]);
renderer.domElement.addEventListener("pointerup", ev => {
  if (!downAt || Math.hypot(ev.clientX - downAt[0], ev.clientY - downAt[1]) > 4) return;
  const s = pick(ev); if (!s) return;
  if (ev.shiftKey) {
    measure.push(s); if (measure.length > 2) measure = [s];
    if (measure.length === 2) {
      drawLine(measure[0], measure[1], 0x4fd18b);
      info.innerHTML = `<h2>Measure</h2><p>${esc(measure[0].designation)} ↔ ${esc(measure[1].designation)}: <b>${pos(measure[0]).distanceTo(pos(measure[1])).toFixed(2)} ly</b></p>`;
    }
    return;
  }
  show(s);
});

document.getElementById("map-search").addEventListener("change", e => {
  const s = byName[e.target.value.trim().toUpperCase()]; if (s) { focus(s); show(s); }
});
["opt-mine"].forEach(id => document.getElementById(id).addEventListener("change", build));
document.getElementById("opt-cover").addEventListener("change", e => coverGroup.visible = e.target.checked);
document.getElementById("btn-top").addEventListener("click", () => { camera.position.set(controls.target.x, controls.target.y - 0.01, controls.target.z + 160); });
document.getElementById("btn-home").addEventListener("click", () => { const s = repStar(); if (s) { focus(s); show(s); } });

const tmp = new THREE.Vector3();
function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
  const showLabels = document.getElementById("opt-labels").checked;
  const w = el.clientWidth, h = el.clientHeight;
  for (const l of labels) {
    tmp.copy(l.v).project(camera);
    const vis = showLabels && tmp.z < 1 && Math.abs(tmp.x) < 1 && Math.abs(tmp.y) < 1;
    l.el.style.display = vis ? "block" : "none";
    if (vis) { l.el.style.left = ((tmp.x + 1) / 2 * w + 8) + "px"; l.el.style.top = ((1 - tmp.y) / 2 * h - 6) + "px"; }
  }
  placeMovers(Date.now());
  moveGroup.visible = document.getElementById("opt-moving").checked;
  for (const x of movers) {
    tmp.copy(x.v).project(camera);
    const vis = moveGroup.visible && tmp.z < 1 && Math.abs(tmp.x) < 1 && Math.abs(tmp.y) < 1;
    x.el.style.display = vis ? "block" : "none";
    if (vis) { x.el.style.left = ((tmp.x + 1) / 2 * w + 10) + "px"; x.el.style.top = ((1 - tmp.y) / 2 * h + 6) + "px"; }
  }
}

fetch("/api/map.json", { credentials: "same-origin" }).then(r => r.json()).then(d => {
  data = d;
  d.stars.forEach(s => byName[s.designation] = s);
  document.getElementById("star-list").innerHTML = d.stars.map(s => `<option value="${esc(s.designation)}">`).join("");
  build();
  buildMovers();
  if (!d.stars.length) {
    info.innerHTML = `<p class="muted">No star catalogue cached yet. It syncs every 30 minutes, or use “refresh catalogue”.</p>`;
  } else {
    const mv = (d.moving || []).map(m => `<li>${esc(m.label)}: ${esc(m.origin)} → ${esc(m.destination)}</li>`).join("");
    info.innerHTML = `<p>${d.stars.length} stars · catalogue generated ${esc(d.generated_at || "?")}.</p>` +
      (mv ? `<p class="small"><b>In transit between stars</b></p><ul class="small">${mv}</ul>` : "") +
      `<p class="muted small">Drag to orbit, scroll to zoom, click a star for details.</p>`;
    const s = repStar(); if (s) focus(s);
  }
  animate();
});
