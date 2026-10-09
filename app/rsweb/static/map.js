// 3D galaxy map of the star catalog (positions in light-years from Sol).
import * as THREE from "three";
import { OrbitControls } from "three/addons/OrbitControls.js";

// The Galaxy page, and the desktop wallpaper (window.MAP_OPTS: {dataUrl, headers, labels, rotate, refreshMinutes,
// focus, cover, moving, fleets, supply, production, onlyMine}); in the wallpaper the page's controls are absent, so every control lookup is optional.
const OPTS = window.MAP_OPTS || {};
const $ = id => document.getElementById(id);
const checked = (id, dflt) => $(id) ? $(id).checked : dflt;
const el = $("map");
const info = $("map-info") || document.createElement("div");
const tip = $("map-tip") || document.createElement("div");
const fromSel = $("map-from");

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
if (OPTS.rotate) { controls.autoRotate = true; controls.autoRotateSpeed = OPTS.rotate; }

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

// thin ring for markers around a star (your devices, scanned, hub, replicant, fleet): rings nest without the stacked
// glow that filled sprites made
const ringTex = (() => {
  const c = document.createElement("canvas"); c.width = c.height = 128;
  const g = c.getContext("2d");
  g.strokeStyle = "rgba(255,255,255,1)"; g.lineWidth = 3;
  g.beginPath(); g.arc(64, 64, 56, 0, Math.PI * 2); g.stroke();
  return new THREE.CanvasTexture(c);
})();

// four-point sparkle, so production reads differently from the (round) stars
const sparkTex = (() => {
  const c = document.createElement("canvas"); c.width = c.height = 64;
  const g = c.getContext("2d");
  const grd = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  grd.addColorStop(0, "rgba(255,255,255,1)"); grd.addColorStop(.25, "rgba(255,255,255,.9)"); grd.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = grd;
  g.beginPath();
  for (let i = 0; i < 8; i++) {
    const a = i * Math.PI / 4, r = i % 2 ? 6 : 32;
    g.lineTo(32 + Math.cos(a) * r, 32 + Math.sin(a) * r);
  }
  g.closePath(); g.fill();
  return new THREE.CanvasTexture(c);
})();

// what each system is producing right now: one sparkle per drone mining a resource, each resource on its own orbit
const RES_COLOR = { structural: 0xb0bec5, conductive: 0xffa726, silicates: 0xe6c88f, carbon: 0xa1887f, volatiles: 0x4dd0e1,
                    rares: 0xe040fb };
const RES_ORDER = ["structural", "conductive", "silicates", "carbon", "volatiles", "rares"];
window.RES_COLOR = RES_COLOR;

// stars: a crisp core with a small halo (the soft sprite read as a glow round every star)
const starTex = (() => {
  const c = document.createElement("canvas"); c.width = c.height = 64;
  const g = c.getContext("2d"); const grd = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  grd.addColorStop(0, "rgba(255,255,255,1)"); grd.addColorStop(.18, "rgba(255,255,255,1)");
  grd.addColorStop(.32, "rgba(255,255,255,.25)"); grd.addColorStop(.6, "rgba(255,255,255,.04)"); grd.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = grd; g.fillRect(0, 0, 64, 64);
  return new THREE.CanvasTexture(c);
})();

// faint reference grid on the galactic plane
const grid = new THREE.PolarGridHelper(80, 8, 8, 64, 0x1c2638, 0x141b28);
grid.rotation.x = Math.PI / 2;
scene.add(grid);

let data = { stars: [], replicants: [] };
let starPoints, mineGroup = new THREE.Group(), coverGroup = new THREE.Group(), lineGroup = new THREE.Group();
const moveGroup = new THREE.Group(), fleetGroup = new THREE.Group(), supplyGroup = new THREE.Group();
const prospectGroup = new THREE.Group();
scene.add(mineGroup, coverGroup, lineGroup, moveGroup, fleetGroup, supplyGroup, prospectGroup);
const supplyLines = [];   // {curve, dots: [sprite], el, v}
const sparkGroup = new THREE.Group(); scene.add(sparkGroup);
const measureGroup = new THREE.Group(); scene.add(measureGroup);   // right-click measuring: its points and line
const sparkles = [];      // {sp, c: center, r, a0, w, tilt}
const fleetLabels = [];   // {el, v, dy}
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
  const onlyMine = checked("opt-mine", !!OPTS.onlyMine);
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
  starPoints = new THREE.Points(g, new THREE.PointsMaterial({ size: 1.3, map: starTex, vertexColors: true, transparent: true, depthWrite: false }));
  scene.add(starPoints);

  const ring = color => new THREE.Sprite(new THREE.SpriteMaterial({ map: ringTex, color, transparent: true, opacity: .55, depthWrite: false }));
  sparkGroup.clear(); sparkles.length = 0;
  for (const s of visible) {
    const v = pos(s);
    const hasRep = data.replicants.some(r => r.star === s.designation);
    const marks = [];
    if (s.devices > 0) marks.push([0x6cb6ff, 3.2]);
    if (s.scanned) marks.push([0x4fd18b, 2.4]);
    if (s.has_hub || (s.infra || []).some(t => t.includes("hub"))) marks.push([0xf0b64f, 2.8]);
    if (hasRep) marks.push([0xff5fa2, 4.2]);
    const fresh = s.prospected && !s.scanned;   // found by our observatories, nothing has scanned it yet
    if (fresh) marks.push([0xfff27a, 3.6]);
    for (const [c, sz] of marks) { const sp = ring(c); sp.scale.set(sz, sz, 1); sp.position.copy(v); mineGroup.add(sp); }
    RES_ORDER.forEach((res, k) => {
      const n = Math.min(10, (s.mining || {})[res] || 0);
      for (let i = 0; i < n; i++) {
        const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: sparkTex, color: RES_COLOR[res], transparent: true, depthWrite: false }));
        sp.scale.set(.7, .7, 1); sparkGroup.add(sp);
        sparkles.push({ sp, c: v, r: 2.4 + k * .3, a0: i / n * Math.PI * 2 + k, w: .25 - k * .025, tilt: (k % 2 ? 1 : -1) * .35 });
      }
    });
    for (const t of s.infra || []) {
      const r = Object.entries(RANGE).find(([k]) => t.includes(k));
      if (r) {
        const m = new THREE.Mesh(new THREE.SphereGeometry(r[1], 24, 16),
          new THREE.MeshBasicMaterial({ color: t.includes("hub") ? 0xf0b64f : 0x6cb6ff, wireframe: true, transparent: true, opacity: .07 }));
        m.position.copy(v); coverGroup.add(m);
      }
    }
    if (s.devices > 0 || hasRep || s.has_hub || fresh) {
      const d = document.createElement("div");
      d.className = "small"; d.textContent = s.designation;
      Object.assign(d.style, { position: "absolute", color: hasRep ? "#ff9fc8" : fresh && !s.devices ? "#fff27a" : "#9fb3d1", pointerEvents: "none", fontFamily: "monospace", fontSize: "11px" });
      el.appendChild(d); labels.push({ el: d, v });
    }
  }
  coverGroup.visible = checked("opt-cover", OPTS.cover !== false);
}

// fleets: a ring at each system with fleet members, a label with what the fleet is doing, and a dashed line to the
// system its mission is headed for
const FLEET = 0x3fd0c9;
const ROLE_ICON = { mining: "⛏", explore: "◎", trade: "⇄" };
function buildFleets() {
  fleetGroup.clear();
  fleetLabels.forEach(l => l.el.remove()); fleetLabels.length = 0;
  const perStar = {};
  for (const f of data.fleets || []) {
    const here = (f.stars || []).map(n => byName[n]).filter(Boolean);
    here.forEach((s, i) => {
      const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: ringTex, color: FLEET, transparent: true, opacity: i ? .45 : .8, depthWrite: false }));
      const sz = i ? 2.0 : 3.8; sp.scale.set(sz, sz, 1); sp.position.copy(pos(s)); fleetGroup.add(sp);
    });
    const at = here[0] || byName[f.home];
    if (!at) continue;
    const to = f.target && f.target !== at.designation ? byName[f.target] : null;
    if (to) {
      const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints([pos(at), pos(to)]),
        new THREE.LineDashedMaterial({ color: FLEET, dashSize: .35, gapSize: .7, transparent: true, opacity: .55 }));
      line.computeLineDistances(); fleetGroup.add(line);
      const tg = new THREE.Sprite(new THREE.SpriteMaterial({ map: ringTex, color: FLEET, transparent: true, opacity: .45, depthWrite: false }));
      tg.scale.set(3.4, 3.4, 1); tg.position.copy(pos(to)); fleetGroup.add(tg);
    }
    const n = perStar[at.designation] = (perStar[at.designation] || 0) + 1;
    const doing = f.phase ? `${f.phase}${f.state === "stalled" ? " (stalled)" : ""}` : f.state;
    const counts = [f.working && `${f.working} working`, f.moving && `${f.moving} moving`, f.idle && `${f.idle} idle`].filter(Boolean).join(", ");
    const d = document.createElement("div");
    d.innerHTML = `${ROLE_ICON[f.role] || "⚑"} <b>${esc(f.name)}</b> · ${esc(doing)}${to ? ` → ${esc(to.designation)}` : ""}` +
      (counts ? `<span style="opacity:.65"> · ${counts}</span>` : "");
    if (f.note) d.title = f.note;
    Object.assign(d.style, { position: "absolute", color: f.state === "stalled" ? "#ffb74d" : "#8ee8e3", pointerEvents: "none",
                             fontFamily: "monospace", fontSize: "11px", whiteSpace: "nowrap", textShadow: "0 1px 2px #000" });
    el.appendChild(d);
    fleetLabels.push({ el: d, v: pos(at), dy: 8 + n * 14 });
  }
}

// supply lines between systems: an arc bowed above the galactic plane, amber for materials, blue for trade.
// Planned (configured, nothing moving yet) is faint and dotted, a running ferry or mission is solid, and while
// something is traveling along it, dots flow from source to destination.
const SUPPLY = { materials: 0xffb74d, trade: 0x6cb6ff };
const SUPPLY_TEXT = { materials: "#ffd59a", trade: "#a9d3ff" };
function buildSupply() {
  supplyGroup.clear();
  supplyLines.forEach(x => x.el.remove()); supplyLines.length = 0;
  const fromSame = {};   // labels of lines leaving the same system are stacked, not drawn over each other
  for (const l of data.supply || []) {
    const a = byName[l.from], b = byName[l.to];
    if (!a || !b) continue;
    const pa = pos(a), pb = pos(b);
    const mid = pa.clone().lerp(pb, .5); mid.z += Math.max(1.5, pa.distanceTo(pb) * .22);
    const curve = new THREE.QuadraticBezierCurve3(pa, mid, pb);
    const color = SUPPLY[l.kind] ?? SUPPLY.materials;
    const planned = l.state === "planned";
    const mat = planned
      ? new THREE.LineDashedMaterial({ color, dashSize: .25, gapSize: .35, transparent: true, opacity: .45 })
      : new THREE.LineBasicMaterial({ color, transparent: true, opacity: l.state === "moving" ? .9 : .6 });
    const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(curve.getPoints(48)), mat);
    if (planned) line.computeLineDistances();
    supplyGroup.add(line);
    const dots = [];
    if (l.state === "moving") for (let i = 0; i < 4; i++) {
      const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: dot, color, transparent: true, depthWrite: false }));
      sp.scale.set(1.1, 1.1, 1); supplyGroup.add(sp); dots.push(sp);
    }
    const what = { moving: `${l.in_transit} in transit`, ferrying: `ferrying${l.freighters ? ` (${l.freighters})` : ""}`,
                   active: l.kind === "trade" ? "trade run" : "delivering", planned: "planned" }[l.state] || l.state;
    const d = document.createElement("div");
    d.textContent = `${l.from_fleet}${l.to_fleet ? ` → ${l.to_fleet}` : ""} · ${what}`;
    Object.assign(d.style, { position: "absolute", color: SUPPLY_TEXT[l.kind] || "#ffd59a", opacity: planned ? .55 : .9,
                             pointerEvents: "none", fontFamily: "monospace", fontSize: "10px", whiteSpace: "nowrap",
                             textShadow: "0 1px 2px #000" });
    el.appendChild(d);
    const k = fromSame[l.from] = (fromSame[l.from] ?? -1) + 1;
    supplyLines.push({ curve, dots, el: d, v: curve.getPoint(.5), dy: -14 - 12 * k });
  }
}

// observatories prospecting: a faint cone from the system along the direction, darker as the scan progresses (at most
// ~15 % opaque, and never hiding what's behind it). Reach / width come from past finds ("≈" while still a guess).
const PROSPECT = 0x7fe3ff, DOWN = new THREE.Vector3(0, -1, 0);
const prospectCones = [];
function buildProspects() {
  prospectGroup.clear();
  prospectCones.forEach(x => x.el.remove()); prospectCones.length = 0;
  for (const p of data.prospects || []) {
    const dir = new THREE.Vector3(...(p.direction || [1, 0, 0])).normalize();
    const h = p.reach, r = h * Math.tan(p.half_angle * Math.PI / 180);
    const geo = new THREE.ConeGeometry(r, h, 40, 1, true);
    geo.translate(0, -h / 2, 0);   // apex at the system, opening along -y …
    const mat = new THREE.MeshBasicMaterial({ color: PROSPECT, transparent: true, opacity: .02, depthWrite: false,
                                              side: THREE.DoubleSide });
    const cone = new THREE.Mesh(geo, mat);
    cone.position.copy(pos(p));
    cone.quaternion.setFromUnitVectors(DOWN, dir);   // … turned to the prospect's direction
    cone.raycast = () => {};                         // clicks go through to the stars behind it
    prospectGroup.add(cone);
    const d = document.createElement("div");
    Object.assign(d.style, { position: "absolute", color: "#bdf2ff", pointerEvents: "none", fontFamily: "monospace",
                             fontSize: "11px", whiteSpace: "nowrap" });
    el.appendChild(d);
    prospectCones.push({ p, mat, el: d, v: pos(p).add(dir.multiplyScalar(h * .5)) });
  }
}
function placeProspects(now, showLabels) {
  prospectGroup.visible = checked("opt-prospect", OPTS.prospect !== false);
  const w = el.clientWidth, hgt = el.clientHeight, v = new THREE.Vector3();
  for (const x of prospectCones) {
    const f = Math.max(0, Math.min(1, (now - x.p.t0) / Math.max(1, x.p.t1 - x.p.t0)));
    x.mat.opacity = .02 + .13 * f;
    v.copy(x.v).project(camera);
    const vis = prospectGroup.visible && showLabels && v.z < 1 && Math.abs(v.x) < 1 && Math.abs(v.y) < 1;
    x.el.style.display = vis ? "block" : "none";
    if (vis) {
      x.el.textContent = `🔭 ${x.p.code} · ${Math.round(f * 100)}% · ${now < x.p.t1 ? fmt((x.p.t1 - now) / 1000) + " left" : "finishing"}` +
                         ` · ${x.p.learned ? "" : "≈"}${x.p.reach} ly`;
      x.el.style.left = ((v.x + 1) / 2 * w) + "px"; x.el.style.top = ((1 - v.y) / 2 * hgt) + "px";
    }
  }
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

function focus(s) {
  const v = pos(s);
  controls.target.copy(v);
  camera.position.copy(v.clone().add(new THREE.Vector3(0, -25, 18)));
}

function show(s) {
  const from = repStar();
  const dist = from ? pos(from).distanceTo(pos(s)).toFixed(2) : null;
  const rep = fromSel?.value;
  lineGroup.clear();   // no line on a left-click: lines are the right-click measuring tool's
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
      ${(s.drones || []).length ? `<dt>Drones</dt><dd>${s.drones.map(g => `${g.n} ${esc(g.kind)} <span class="muted">(${g.working} working${g.idle ? `, ${g.idle} idle` : ""}${g.moving ? `, ${g.moving} moving` : ""})</span>`).join("<br>")}</dd>` : ""}
      ${Object.keys(s.mining || {}).length ? `<dt>Mining now</dt><dd>${Object.entries(s.mining).map(([r, n]) => `${n} on ${esc(r)}`).join(", ")}</dd>` : ""}
      ${(s.infra || []).length ? `<dt>Infrastructure</dt><dd>${esc(s.infra.join(", "))}</dd>` : ""}
      ${s.has_hub ? "<dt>Hub</dt><dd>yes</dd>" : ""}
      ${s.prospected ? `<dt>Prospected</dt><dd>by ${esc(s.found_by || "an observatory")}${s.scanned ? "" : " · <b>not scanned yet</b>"}</dd>` : ""}
    </dl>
    ${rep ? `<div class="row">
      <button class="small" hx-get="/api/route?replicant=${encodeURIComponent(rep)}&star=${encodeURIComponent(s.designation)}" hx-target="#route-out">travel estimate</button>
      <button class="small" hx-post="/replicants/${encodeURIComponent(rep)}/travel" hx-vals='${esc(JSON.stringify({ destination: s.designation, dry_run: "1" }))}' hx-target="#route-out">preview route</button>
    </div><div id="route-out"></div>` : ""}`;
  if (window.htmx) htmx.process(info);
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
// measuring is right-click only (a right-drag still pans): 1st sets the first point, 2nd the second, 3rd clears both
renderer.domElement.addEventListener("contextmenu", ev => ev.preventDefault());
function measureMark(s) {
  const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: ringTex, color: 0x4fd18b, transparent: true, depthWrite: false }));
  sp.scale.set(3, 3, 1); sp.position.copy(pos(s)); measureGroup.add(sp);
}
function rightClick(ev) {
  if (measure.length >= 2) {            // third right-click: clear
    measure = []; measureGroup.clear();
    info.innerHTML = `<p class="muted">Measurement cleared. Right-click a star to start another.</p>`;
    return;
  }
  const s = pick(ev); if (!s) return;
  if (measure.length === 1 && measure[0] === s) return;
  measure.push(s); measureMark(s);
  if (measure.length === 1) {
    info.innerHTML = `<h2>Measure</h2><p>From <b>${esc(s.designation)}</b> — right-click a second star.</p>`;
    return;
  }
  const [a, b] = measure;
  measureGroup.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints([pos(a), pos(b)]),
                                  new THREE.LineBasicMaterial({ color: 0x4fd18b })));
  info.innerHTML = `<h2>Measure</h2><p>${esc(a.designation)} ↔ ${esc(b.designation)}: <b>${pos(a).distanceTo(pos(b)).toFixed(2)} ly</b></p>
    <p class="muted small">Right-click again to clear.</p>`;
}
renderer.domElement.addEventListener("pointerup", ev => {
  if (!downAt || Math.hypot(ev.clientX - downAt[0], ev.clientY - downAt[1]) > 4) return;
  if (ev.button === 2) { rightClick(ev); return; }
  if (ev.button !== 0) return;
  const s = pick(ev); if (!s) return;
  show(s);
});

$("map-search")?.addEventListener("change", e => {
  const s = byName[e.target.value.trim().toUpperCase()]; if (s) { focus(s); show(s); }
});
["opt-mine"].forEach(id => $(id)?.addEventListener("change", build));
$("opt-cover")?.addEventListener("change", e => coverGroup.visible = e.target.checked);
$("btn-top")?.addEventListener("click", () => { camera.position.set(controls.target.x, controls.target.y - 0.01, controls.target.z + 160); });
$("btn-home")?.addEventListener("click", () => { const s = repStar(); if (s) { focus(s); show(s); } });

const tmp = new THREE.Vector3();
// the wallpaper runs all day: cap it at 30 fps (the Galaxy page renders at full rate)
const minFrame = OPTS.dataUrl ? 1000 / 30 : 0;
let lastFrame = 0;
function animate(t = 0) {
  requestAnimationFrame(animate);
  if (minFrame && t - lastFrame < minFrame) return;
  lastFrame = t;
  controls.update();
  renderer.render(scene, camera);
  const showLabels = checked("opt-labels", OPTS.labels !== false);
  const w = el.clientWidth, h = el.clientHeight;
  for (const l of labels) {
    tmp.copy(l.v).project(camera);
    const vis = showLabels && tmp.z < 1 && Math.abs(tmp.x) < 1 && Math.abs(tmp.y) < 1;
    l.el.style.display = vis ? "block" : "none";
    if (vis) { l.el.style.left = ((tmp.x + 1) / 2 * w + 8) + "px"; l.el.style.top = ((1 - tmp.y) / 2 * h - 6) + "px"; }
  }
  fleetGroup.visible = checked("opt-fleets", OPTS.fleets !== false);
  for (const l of fleetLabels) {
    tmp.copy(l.v).project(camera);
    const vis = fleetGroup.visible && tmp.z < 1 && Math.abs(tmp.x) < 1 && Math.abs(tmp.y) < 1;
    l.el.style.display = vis ? "block" : "none";
    if (vis) { l.el.style.left = ((tmp.x + 1) / 2 * w + 8) + "px"; l.el.style.top = ((1 - tmp.y) / 2 * h - 6 + l.dy) + "px"; }
  }
  sparkGroup.visible = checked("opt-production", OPTS.production !== false);
  if (sparkGroup.visible) {
    const ts = t / 1000;
    for (const x of sparkles) {
      const a = x.a0 + ts * x.w;
      x.sp.position.set(x.c.x + Math.cos(a) * x.r, x.c.y + Math.sin(a) * x.r, x.c.z + Math.sin(a) * x.r * x.tilt);
      x.sp.material.opacity = .55 + .45 * Math.sin(ts * 2.3 + x.a0 * 3);   // twinkle
      x.sp.material.rotation = ts * .6 + x.a0;
    }
  }
  supplyGroup.visible = checked("opt-supply", OPTS.supply !== false);
  const flow = (Date.now() % 4000) / 4000;
  for (const x of supplyLines) {
    x.dots.forEach((sp, i) => sp.position.copy(x.curve.getPoint((flow + i / x.dots.length) % 1)));
    tmp.copy(x.v).project(camera);
    const vis = supplyGroup.visible && showLabels && tmp.z < 1 && Math.abs(tmp.x) < 1 && Math.abs(tmp.y) < 1;
    x.el.style.display = vis ? "block" : "none";
    if (vis) { x.el.style.left = ((tmp.x + 1) / 2 * w) + "px"; x.el.style.top = ((1 - tmp.y) / 2 * h + x.dy) + "px"; }
  }
  placeMovers(Date.now());
  placeProspects(Date.now(), showLabels);
  moveGroup.visible = checked("opt-moving", OPTS.moving !== false);
  for (const x of movers) {
    tmp.copy(x.v).project(camera);
    const vis = moveGroup.visible && tmp.z < 1 && Math.abs(tmp.x) < 1 && Math.abs(tmp.y) < 1;
    x.el.style.display = vis ? "block" : "none";
    if (vis) { x.el.style.left = ((tmp.x + 1) / 2 * w + 10) + "px"; x.el.style.top = ((1 - tmp.y) / 2 * h + 6) + "px"; }
  }
}

// Progressive: the stars first (part=core: a quick, small answer), then what's on them (part=overlay: drones, mining,
// ships in transit, fleets, supply lines) — the map is usable while the second part is still coming.
function partUrl(part) {
  const u = new URL(OPTS.dataUrl || "/api/map.json", location.href);
  u.searchParams.set("part", part);
  return u.href;
}
function fetchPart(part) {
  return fetch(partUrl(part), { credentials: "same-origin", headers: OPTS.headers || {} })
    .then(r => { if (!r.ok) throw new Error(`${r.status} ${r.statusText}`); return r.json(); });
}
function infoText(d, loadingOverlay) {
  const mv = (d.moving || []).map(m => `<li>${esc(m.label)}: ${esc(m.origin)} → ${esc(m.destination)}</li>`).join("");
  const src = d.sources || {};
  const parts = [src.catalogue != null && `${src.catalogue} from the game's catalogue` +
                   (src.catalogue_total != null && src.catalogue_total !== src.catalogue ? ` (it says ${src.catalogue_total})` : ""),
                 src.census && `${src.census} from censuses`, src.observatory && `${src.observatory} found by your observatories`,
                 (src.observatory_unplaced || []).length && `${src.observatory_unplaced.length} found without a position yet`].filter(Boolean);
  info.innerHTML = `<p>${d.stars.length} stars · catalog generated ${esc(d.generated_at || "?")}.</p>` +
    (parts.length ? `<p class="small muted">${esc(parts.join(" · "))}</p>` : "") +
    (loadingOverlay ? `<p class="small muted">Loading fleets, ships in transit and mining…</p>` : "") +
    (mv ? `<p class="small"><b>In transit between stars</b></p><ul class="small">${mv}</ul>` : "") +
    `<p class="muted small">Drag to orbit, scroll to zoom, click a star for details.</p>`;
}
function applyOverlay(o, first) {
  const per = o.per_star || {};
  for (const s of data.stars) Object.assign(s, per[s.designation] || { drones: [], mining: {} });
  Object.assign(data, { moving: o.moving || [], fleets: o.fleets || [], supply: o.supply || [], prospects: o.prospects || [] });
  build();
  buildMovers();
  buildProspects();
  buildFleets();
  buildSupply();
  if (first && data.stars.length) infoText(data, false);
}
function load(first) {
  return fetchPart("core").then(d => {
    const keep = { moving: data.moving, fleets: data.fleets, supply: data.supply, prospects: data.prospects };   // until the new overlay lands
    data = { ...keep, ...d };
    d.stars.forEach(s => byName[s.designation] = s);
    if ($("star-list")) $("star-list").innerHTML = d.stars.map(s => `<option value="${esc(s.designation)}">`).join("");
    build();
    if (first) {
      if (!d.stars.length) {
        info.innerHTML = `<p class="muted">No star catalog cached yet. It syncs every 30 minutes, or use “refresh catalog”.</p>`;
      } else {
        infoText(data, true);
        const s = (OPTS.focus && byName[OPTS.focus]) || repStar(); if (s) focus(s);
      }
      animate();
    }
    return fetchPart("overlay").then(o => applyOverlay(o, first));
  });
}
load(true).catch(e => { info.innerHTML = `<p class="muted">Could not load the map: ${esc(e.message)}</p>`; OPTS.onError?.(e); });
if (OPTS.refreshMinutes) setInterval(() => load(false).catch(e => OPTS.onError?.(e)), OPTS.refreshMinutes * 60000);

// Live: the page's event stream (base.html, sse "state") says when something departed, arrived or changed — redraw
// what's on the stars (ships in transit, fleets, supply lines, mining), at most every few seconds.
const LIVE = /^(travel\.|device\.|devices$|action$|automation$|mining\.)/;
let liveTimer = null, liveLast = 0;
function liveRefresh() {
  liveTimer = null; liveLast = Date.now();
  fetchPart("overlay").then(o => applyOverlay(o, false)).catch(() => {});
}
document.addEventListener("htmx:sseMessage", e => {
  if (e.detail.type !== "state" || !LIVE.test(String(e.detail.data || "")) || liveTimer || !data.stars.length) return;
  liveTimer = setTimeout(liveRefresh, Math.max(1500, 5000 - (Date.now() - liveLast)));
});
