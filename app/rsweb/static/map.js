// 3D galaxy map of the star catalog (positions in light-years from Sol).
import * as THREE from "three";
import { OrbitControls } from "three/addons/OrbitControls.js";

// The Galaxy page, and the desktop wallpaper (window.MAP_OPTS: {dataUrl, headers, labels, rotate, refreshMinutes,
// focus, cover, moving, fleets, supply, production, onlyMine}); in the wallpaper the page's controls are absent, so every control lookup is optional.
const OPTS = window.MAP_OPTS || {};
const $ = id => document.getElementById(id);
const checked = (id, dflt) => $(id) ? $(id).checked : dflt;
// The Galaxy page's options are remembered in this browser (every opt-* checkbox), restored before the first draw.
const PREFS_KEY = "galaxy-map-options";
if (!OPTS.dataUrl) {
  try {
    const saved = JSON.parse(localStorage.getItem(PREFS_KEY) || "{}");
    document.querySelectorAll('input[type=checkbox][id^="opt-"]').forEach(c => { if (c.id in saved) c.checked = !!saved[c.id]; });
  } catch (e) { /* storage blocked: defaults */ }
  document.querySelectorAll('input[type=checkbox][id^="opt-"]').forEach(c => c.addEventListener("change", () => {
    try {
      const all = {};
      document.querySelectorAll('input[type=checkbox][id^="opt-"]').forEach(x => { all[x.id] = x.checked; });
      localStorage.setItem(PREFS_KEY, JSON.stringify(all));
    } catch (e) { /* not saved: fine */ }
  }));
}
// star markers by kind, each with its own checkbox (opt-mark-<kind>)
const MARKS = { devices: 0x6cb6ff, scanned: 0x4fd18b, hub: 0xf0b64f, replicant: 0xff5fa2, found: 0xfff27a, prospected: 0x7fe3ff };
const MARK_ORDER = ["scanned", "devices", "hub", "found", "replicant", "prospected"];   // innermost first
const markOn = kind => checked("opt-mark-" + kind, true);
// Each kind of ring has one size on every star: its place among the kinds switched on (innermost first), so the
// rings stay nested and evenly spaced however many are shown.
function markSize(kind) {
  const on = MARK_ORDER.filter(markOn);
  return 2.2 + 0.8 * Math.max(0, on.indexOf(kind));
}
const WAVE_S = 2.4;          // seconds per survey wave
const waves = [];            // survey-wave sprites (rebuilt with the markers)
function waveEdge() {        // the outermost ring switched on: where a wave fades out
  const on = MARK_ORDER.filter(markOn);
  return (on.length ? 2.2 + 0.8 * (on.length - 1) : 2.2) + 0.6;
}
function resizeMarks() {
  mineGroup.children.forEach(sp => {
    const k = sp.userData.mark;
    if (!k) return;
    if (k === "ward" || k === "survey") { sp.visible = checked("opt-mark-" + k, true); return; }
    sp.visible = markOn(k);
    const sz = markSize(k); sp.scale.set(sz, sz, 1);
  });
}
const el = $("map");
const info = $("map-info") || document.createElement("div");
const tip = $("map-tip") || document.createElement("div");
const fromSel = $("map-from");

const COLORS = { red: 0xff8a65, orange: 0xffb74d, yellow: 0xffe082, white: 0xf5f5f5, blue: 0x90caf9, "blue-white": 0xbbdefb, brown: 0xa1887f };

// phones report a device pixel ratio of 3: a canvas that size at full frame rate can exhaust a phone's GPU (the map
// froze a few seconds after focusing a system, as the keyboard closed and the canvas was resized). 2 is plenty.
const TOUCH = matchMedia("(pointer: coarse)").matches;
const renderer = new THREE.WebGLRenderer({ antialias: !TOUCH, powerPreference: "low-power" });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, TOUCH ? 1.5 : 2));
el.prepend(renderer.domElement);
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(55, 1, 0.1, 5000);
camera.position.set(0, -90, 70);
camera.up.set(0, 0, 1);  // galactic north up
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
if (OPTS.rotate) { controls.autoRotate = true; controls.autoRotateSpeed = OPTS.rotate; }

let lastSize = "", lastCamKey = "";   // lastCamKey: the camera the star labels were last placed for
function resize() {
  const w = el.clientWidth, h = el.clientHeight;
  if (!w || !h || `${w}x${h}` === lastSize) return;   // nothing to do (or hidden): don't reallocate the canvas
  lastSize = `${w}x${h}`;
  renderer.setSize(w, h);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}
// a phone's keyboard opening or closing fires a burst of resizes: handle the last one only
let resizeTimer = null;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(resize, 200); });
resize();
// if the browser drops the GL context anyway, say so and reload the map once it comes back, instead of a frozen canvas
renderer.domElement.addEventListener("webglcontextlost", ev => {
  ev.preventDefault();
  const n = document.createElement("div");
  n.className = "muted small"; n.textContent = "The map paused (the browser reclaimed graphics memory); it reloads when it can.";
  Object.assign(n.style, { position: "absolute", top: "8px", left: "8px", zIndex: 5 });
  el.appendChild(n);
});
renderer.domElement.addEventListener("webglcontextrestored", () => location.reload());

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
const othersGroup = new THREE.Group(), trafficGroup = new THREE.Group(), trailGroup = new THREE.Group();
const relayGroup = new THREE.Group();   // your relay networks: one color per network, curved links
scene.add(mineGroup, coverGroup, lineGroup, moveGroup, fleetGroup, supplyGroup, prospectGroup, othersGroup, trafficGroup, trailGroup, relayGroup);
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
  mineGroup.clear(); coverGroup.clear(); waves.length = 0;
  labels.forEach(l => l.el.remove()); labels.length = 0; lastCamKey = "";   // new labels: place them next frame
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
    if (s.devices > 0) marks.push("devices");
    if (s.scanned) marks.push("scanned");
    if (s.has_hub || (s.infra || []).some(t => t.includes("hub"))) marks.push("hub");
    if (hasRep) marks.push("replicant");
    const fresh = s.prospected && !s.scanned;   // found by our observatories, nothing has scanned it yet
    if (fresh) marks.push("found");
    if (s.prospected_from) marks.push("prospected");   // an observatory prospected from here (once per star)
    if (s.my_ward) {   // your own working ward: the same shield as other players' wards, in your color
      const sh = new THREE.Sprite(new THREE.SpriteMaterial({ map: shieldTex, color: 0x6cb6ff, transparent: true, depthWrite: false }));
      sh.scale.set(1.4, 1.4, 1); sh.position.copy(v).add(new THREE.Vector3(0, 0, 2.2)); sh.userData.mark = "ward";
      sh.visible = checked("opt-mark-ward", true); mineGroup.add(sh);
    }
    if (s.surveying) {   // being surveyed: a wave running out from the star to the edge of its rings (animate)
      const w = ring(0x4fd18b); w.position.copy(v); w.userData.wave = Math.random() * WAVE_S; w.userData.mark = "survey";
      w.visible = checked("opt-mark-survey", true); mineGroup.add(w); waves.push(w);
    }
    for (const kind of marks) {
      const sz = markSize(kind);
      const sp = ring(MARKS[kind]); sp.scale.set(sz, sz, 1); sp.position.copy(v); sp.userData.mark = kind; sp.visible = markOn(kind);
      mineGroup.add(sp);
    }
    RES_ORDER.forEach((res, k) => {
      const n = Math.min(10, (s.mining || {})[res] || 0);
      for (let i = 0; i < n; i++) {
        const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: sparkTex, color: RES_COLOR[res], transparent: true, depthWrite: false }));
        sp.scale.set(.7, .7, 1); sparkGroup.add(sp);
        sparkles.push({ sp, c: v, r: 2.4 + k * .3, a0: i / n * Math.PI * 2 + k, w: .25 - k * .025, tilt: (k % 2 ? 1 : -1) * .35 });
      }
    });
    if (s.reach) {   // one sphere per system: its working relay or hub that reaches furthest (inactive ones don't count)
      const m = new THREE.Mesh(new THREE.SphereGeometry(s.reach.ly, 24, 16),
        new THREE.MeshBasicMaterial({ color: s.reach.hub ? 0xf0b64f : 0x6cb6ff, wireframe: true, transparent: true,
                                      opacity: s.reach.hub ? .12 : .07 }));
      m.position.copy(v); coverGroup.add(m);
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
const HUB_LINK = 0xf0b64f;   // a hub's links (relays both linked to one hub: their own link isn't drawn)
function buildRelayNet() {
  relayGroup.clear();
  const rn = data.relay_net || { links: [], nodes: [], planned: [], suggested: [] };
  const arc = (a, b, mat) => {
    const pa = pos(a), pb = pos(b);
    const mid = pa.clone().lerp(pb, .5); mid.z += Math.max(1, pa.distanceTo(pb) * .3);
    const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(new THREE.QuadraticBezierCurve3(pa, mid, pb).getPoints(40)), mat);
    if (mat.isLineDashedMaterial) line.computeLineDistances();
    relayGroup.add(line);
  };
  const ring = (s, color, size, opacity = 1) => {
    const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: ringTex, color: new THREE.Color(color), transparent: true, opacity, depthWrite: false }));
    sp.scale.set(size, size, 1); sp.position.copy(pos(s)); relayGroup.add(sp);
  };
  // active networks solid, ones without a replicant in reach dashed and dimmer
  for (const l of rn.links) {
    const a = byName[l.a], b = byName[l.b]; if (!a || !b) continue;
    const c = new THREE.Color(l.hub ? HUB_LINK : l.color);   // a hub's link to a relay: the hub color
    arc(a, b, l.active ? new THREE.LineBasicMaterial({ color: c, transparent: true, opacity: .9 })
                       : new THREE.LineDashedMaterial({ color: c, dashSize: .5, gapSize: .4, transparent: true, opacity: .55 }));
  }
  for (const n of rn.nodes) { const s = byName[n.star]; if (s) ring(s, n.color, 2.2, n.active ? 1 : .5); }
  // suggested bridges: dashed gray through the new relay systems; relays on order: white rings
  for (const b of rn.suggested || []) {
    const chain = [b.from, ...b.stars, b.to].map(x => byName[x]);
    if (chain.some(x => !x)) continue;
    for (let i = 0; i < chain.length - 1; i++)
      arc(chain[i], chain[i + 1], new THREE.LineDashedMaterial({ color: 0x9aa4b2, dashSize: .3, gapSize: .5, transparent: true, opacity: .6 }));
    for (const x of b.stars) ring(byName[x], 0x9aa4b2, 1.8, .7);
  }
  for (const x of rn.planned || []) { const s = byName[x]; if (s) ring(s, 0xffffff, 2.6, .95); }
  relayGroup.visible = checked("opt-relaynet", OPTS.relayNet === true);
}
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

// other players: a coral ring where they have fixed devices (beacons, relays, wards, factories…), a shield where
// someone else's ward or hub is, and an arrow for each arrival / departure our beacons logged in the last hour (in:
// coming from the vector's side, out: leaving along it). All of it fades with distance from the point the view is
// centered on: full within FADE_NEAR ly, gone by FADE_FAR.
const OTHER = 0xff7a6b, OTHER_TEXT = "#ffb0a6", FADE_NEAR = 5, FADE_FAR = 30, ARROW_MS = 3600e3;
const shieldTex = (() => {
  const c = document.createElement("canvas"); c.width = c.height = 64;
  const g = c.getContext("2d");
  g.strokeStyle = "rgba(255,255,255,1)"; g.lineWidth = 4; g.lineJoin = "round";
  g.beginPath(); g.moveTo(32, 6); g.lineTo(54, 14); g.quadraticCurveTo(54, 44, 32, 58); g.quadraticCurveTo(10, 44, 10, 14);
  g.closePath(); g.stroke();
  return new THREE.CanvasTexture(c);
})();
const faders = [];   // {v, mats: [[material, base opacity]], el, born (ms, arrows only)}
function fadeAt(v) {
  const d = v.distanceTo(controls.target);
  return Math.max(0, Math.min(1, (FADE_FAR - d) / (FADE_FAR - FADE_NEAR)));
}
function label(text, color, title) {
  const d = document.createElement("div");
  d.textContent = text; if (title) d.title = title;
  Object.assign(d.style, { position: "absolute", color, pointerEvents: "none", fontFamily: "monospace", fontSize: "10px",
                           whiteSpace: "nowrap", textShadow: "0 1px 2px #000" });
  el.appendChild(d); return d;
}
function ago(ms) { return fmt((Date.now() - ms) / 1000) + " ago"; }
const othersBy = {};
function buildOthers() {
  othersGroup.clear(); trafficGroup.clear();
  faders.forEach(x => x.el?.remove()); faders.length = 0;
  for (const k in othersBy) delete othersBy[k];
  for (const o of data.others || []) {
    othersBy[o.star] = o;
    const v = pos(o), mats = [];
    if (o.n) {
      const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: ringTex, color: OTHER, transparent: true, depthWrite: false }));
      const sz = 2.6 + Math.min(2, o.n * .25); sp.scale.set(sz, sz, 1); sp.position.copy(v); othersGroup.add(sp);
      mats.push([sp.material, .8]);
    }
    if (o.ward) {
      const sh = new THREE.Sprite(new THREE.SpriteMaterial({ map: shieldTex, color: OTHER, transparent: true, depthWrite: false }));
      sh.scale.set(1.4, 1.4, 1); sh.position.copy(v).add(new THREE.Vector3(0, 0, 2.2)); othersGroup.add(sh);
      mats.push([sh.material, .9]);
    }
    const who = o.owners.map(w => w.name).join(", ");
    faders.push({ v, mats, el: who ? label(who, OTHER_TEXT) : null, dy: 10 });
  }
  for (const a of data.traffic || []) {
    const at = pos(a), dir = new THREE.Vector3(...a.vector).normalize(), far = at.clone().add(dir.clone().multiplyScalar(4));
    const [p0, p1] = a.way === "in" ? [far, at] : [at, far];
    const mat = new THREE.LineBasicMaterial({ color: OTHER, transparent: true });
    trafficGroup.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints([p0, p1]), mat));
    const head = new THREE.Mesh(new THREE.ConeGeometry(.35, 1.1, 10), new THREE.MeshBasicMaterial({ color: OTHER, transparent: true }));
    head.position.copy(p1); head.quaternion.setFromUnitVectors(UP, p1.clone().sub(p0).normalize()); trafficGroup.add(head);
    const what = Object.entries(a.types).map(([t, n]) => `${n > 1 ? n + " " : ""}${t}`).join(" + ");
    const el2 = label("", OTHER_TEXT);
    faders.push({ v: at, mats: [[mat, .9], [head.material, .9]], el: el2, born: a.at, dy: -16,
                  text: () => `${a.way === "in" ? "→" : "←"} ${a.who} · ${what} ${a.way === "in" ? "arrived" : "left"} ${ago(a.at)}` +
                              (a.guess ? ` · ${a.way === "in" ? "from" : "toward"} ≈ ${a.guess}` : "") });
  }
}
function placeOthers(now, showLabels, w, h) {
  othersGroup.visible = trafficGroup.visible = checked("opt-others", OPTS.others !== false);
  for (const x of faders) {
    let f = fadeAt(x.v);
    if (x.born) f *= Math.max(0, 1 - (now - x.born) / ARROW_MS);
    x.mats.forEach(([m, base]) => { m.opacity = base * f; m.visible = f > 0.01; });
    if (!x.el) continue;
    tmp.copy(x.v).project(camera);
    const vis = othersGroup.visible && showLabels && f > 0.15 && tmp.z < 1 && Math.abs(tmp.x) < 1 && Math.abs(tmp.y) < 1;
    x.el.style.display = vis ? "block" : "none";
    if (vis) {
      if (x.text) x.el.textContent = x.text();
      x.el.style.opacity = f;
      if (x.born) stackLabel(x.el, (tmp.x + 1) / 2 * w + 8, (1 - tmp.y) / 2 * h + x.dy);   // an arrow's label
      else { x.el.style.left = ((tmp.x + 1) / 2 * w + 8) + "px"; x.el.style.top = ((1 - tmp.y) / 2 * h + x.dy) + "px"; }
    }
  }
}

// the followed replicant's trail (Map › Trail): gold legs from each logged departure to the star its vector points
// at (dashed when the match is loose), a ring on their beacons' systems, and where they were last seen
const TRAIL = 0xffd54f;
let trailLabel = null;
function buildTrail() {
  trailGroup.clear(); trailLabel?.el.remove(); trailLabel = null;
  const t = data.trail; if (!t) return;
  for (const l of t.legs) {
    const a = pos({ position: l.a }), b = pos({ position: l.b });
    const mat = l.good ? new THREE.LineBasicMaterial({ color: TRAIL, transparent: true, opacity: .8 })
                       : new THREE.LineDashedMaterial({ color: TRAIL, dashSize: .4, gapSize: .4, transparent: true, opacity: .6 });
    const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints([a, b]), mat);
    if (!l.good) line.computeLineDistances();
    trailGroup.add(line);
    const head = new THREE.Mesh(new THREE.ConeGeometry(.3, .9, 10), new THREE.MeshBasicMaterial({ color: TRAIL }));
    head.position.copy(a.clone().lerp(b, .6)); head.quaternion.setFromUnitVectors(UP, b.clone().sub(a).normalize());
    trailGroup.add(head);
  }
  for (const n of t.beacons) {
    const s = byName[n]; if (!s) continue;
    const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: ringTex, color: TRAIL, transparent: true, opacity: .5, depthWrite: false }));
    sp.scale.set(2.2, 2.2, 1); sp.position.copy(pos(s)); trailGroup.add(sp);
  }
  if (t.last) {
    const v = pos({ position: t.last.position });
    const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: ringTex, color: TRAIL, transparent: true, depthWrite: false }));
    sp.scale.set(4.6, 4.6, 1); sp.position.copy(v); trailGroup.add(sp);
    const at = Date.parse(t.last.at);
    trailLabel = { v, el: label("", "#ffe58a"), text: () => `★ ${t.name} ${t.last.travel_type === "arrival" ? "arrived" : "left"} ${t.last.star}` +
                   (isNaN(at) ? "" : ` ${ago(at)}`) };
  }
}
function placeTrail(showLabels, w, h) {
  trailGroup.visible = checked("opt-trail", OPTS.trail !== false);
  if (!trailLabel) return;
  tmp.copy(trailLabel.v).project(camera);
  const vis = trailGroup.visible && showLabels && tmp.z < 1 && Math.abs(tmp.x) < 1 && Math.abs(tmp.y) < 1;
  trailLabel.el.style.display = vis ? "block" : "none";
  if (vis) { trailLabel.el.textContent = trailLabel.text(); trailLabel.el.style.left = ((tmp.x + 1) / 2 * w + 8) + "px";
             trailLabel.el.style.top = ((1 - tmp.y) / 2 * h + 22) + "px"; }
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
      ${othersBy[s.designation] ? `<dt>Other players</dt><dd>${othersBy[s.designation].owners.map(w => `${esc(w.name)}: ${Object.entries(w.types).map(([t, n]) => `${n > 1 ? n + "× " : ""}${esc(t.replace(/_/g, " "))}`).join(", ")}`).join("<br>") || ""}${othersBy[s.designation].ward && !othersBy[s.designation].n ? "warded by another player" : ""}${othersBy[s.designation].scanned_at ? ` <span class="muted">· scanned ${fmt((Date.now() - Date.parse(othersBy[s.designation].scanned_at)) / 1000)} ago</span>` : ""}</dd>` : ""}
      ${s.prospected_from ? `<dt>Prospected from</dt><dd>${esc(s.prospected_from.outcome || "")} · ${esc((s.prospected_from.at || "").slice(0, 10))}</dd>` : ""}
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
let downAt = null, pressTimer = null, pressed = false;
Object.assign(renderer.domElement.style, { webkitTouchCallout: "none", userSelect: "none", webkitUserSelect: "none" });   // iOS: no callout on hold
// touch screens have no right button: a long press (half a second, holding still) measures instead
renderer.domElement.addEventListener("pointerdown", ev => {
  downAt = [ev.clientX, ev.clientY]; pressed = false; clearTimeout(pressTimer);
  if (ev.pointerType === "touch" && ev.isPrimary) {
    const at = { clientX: ev.clientX, clientY: ev.clientY };
    pressTimer = setTimeout(() => { pressed = true; navigator.vibrate?.(15); rightClick(at); }, 500);
  }
});
renderer.domElement.addEventListener("pointermove", ev => {
  if (pressTimer && downAt && Math.hypot(ev.clientX - downAt[0], ev.clientY - downAt[1]) > 8) { clearTimeout(pressTimer); pressTimer = null; }
});
renderer.domElement.addEventListener("pointercancel", () => { clearTimeout(pressTimer); pressTimer = null; });
// measuring is right-click only (a right-drag still pans): 1st sets the first point, 2nd the second, 3rd clears both
renderer.domElement.addEventListener("contextmenu", ev => ev.preventDefault());
function measureMark(s) {
  const sp = new THREE.Sprite(new THREE.SpriteMaterial({ map: ringTex, color: 0x4fd18b, transparent: true, depthWrite: false }));
  sp.scale.set(3, 3, 1); sp.position.copy(pos(s)); measureGroup.add(sp);
}
function rightClick(ev) {
  if (measure.length >= 2) {            // third right-click: clear
    measure = []; measureGroup.clear();
    info.innerHTML = `<p class="muted">Measurement cleared. Right-click (or long-press) a star to start another.</p>`;
    return;
  }
  const s = pick(ev); if (!s) return;
  if (measure.length === 1 && measure[0] === s) return;
  measure.push(s); measureMark(s);
  if (measure.length === 1) {
    info.innerHTML = `<h2>Measure</h2><p>From <b>${esc(s.designation)}</b> — right-click (or long-press) a second star.</p>`;
    return;
  }
  const [a, b] = measure;
  measureGroup.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints([pos(a), pos(b)]),
                                  new THREE.LineBasicMaterial({ color: 0x4fd18b })));
  info.innerHTML = `<h2>Measure</h2><p>${esc(a.designation)} ↔ ${esc(b.designation)}: <b>${pos(a).distanceTo(pos(b)).toFixed(2)} ly</b></p>
    <p class="muted small">Right-click (or long-press) again to clear.</p>`;
}
renderer.domElement.addEventListener("pointerup", ev => {
  clearTimeout(pressTimer); pressTimer = null;
  if (pressed) { pressed = false; return; }   // the long press already measured: not a tap too
  if (!downAt || Math.hypot(ev.clientX - downAt[0], ev.clientY - downAt[1]) > 4) return;
  if (ev.button === 2) { rightClick(ev); return; }
  if (ev.button !== 0) return;
  const s = pick(ev); if (!s) return;
  show(s);
});

$("map-search")?.addEventListener("change", e => {
  const s = byName[e.target.value.trim().toUpperCase()]; if (s) { e.target.blur(); focus(s); show(s); }   // keyboard away first
});
["opt-mine"].forEach(id => $(id)?.addEventListener("change", build));
$("opt-cover")?.addEventListener("change", e => coverGroup.visible = e.target.checked);
[...Object.keys(MARKS), "ward", "survey"].forEach(kind => $("opt-mark-" + kind)?.addEventListener("change", resizeMarks));
$("opt-relaynet")?.addEventListener("change", e => relayGroup.visible = e.target.checked);
$("btn-top")?.addEventListener("click", () => { camera.position.set(controls.target.x, controls.target.y - 0.01, controls.target.z + 160); });
$("btn-home")?.addEventListener("click", () => { const s = repStar(); if (s) { focus(s); show(s); } });

const tmp = new THREE.Vector3();
// Text is hidden when zoomed out so far that a light-year is under TEXT_MIN_PX pixels on screen (unreadable anyway).
const TEXT_MIN_PX = 1.5, HALF_FOV = Math.tan(55 * Math.PI / 360);
// Labels next to arrows (ships in transit, other players' arrivals / departures) that land on top of each other are
// stacked: each placed below the ones already there, one line apart.
let stacked = [];
function stackLabel(e, x, y) {
  const wd = e.offsetWidth || 150;
  while (stacked.some(p => x < p.x + p.w && p.x < x + wd && Math.abs(p.y - y) < 13)) y += 13;
  stacked.push({ x, y, w: wd });
  e.style.left = x + "px"; e.style.top = y + "px";
}
// the wallpaper runs all day: cap it at 30 fps (the Galaxy page renders at full rate)
const minFrame = OPTS.dataUrl || TOUCH ? 1000 / 30 : 0;   // phones too: 30 fps is smooth enough and keeps them cool
let lastFrame = 0;
function animate(t = 0) {
  requestAnimationFrame(animate);
  if (minFrame && t - lastFrame < minFrame) return;
  lastFrame = t;
  controls.update();
  if (waves.length) {   // survey waves: grow from the star to the outer ring, fading as they go
    const edge = waveEdge(), ts = t / 1000;
    for (const w of waves) {
      const ph = ((ts + w.userData.wave) % WAVE_S) / WAVE_S;
      const sz = 0.4 + ph * (edge - 0.4); w.scale.set(sz, sz, 1); w.material.opacity = 0.85 * (1 - ph);
    }
  }
  renderer.render(scene, camera);
  const w = el.clientWidth, h = el.clientHeight;
  const readable = h / (2 * camera.position.distanceTo(controls.target) * HALF_FOV) >= TEXT_MIN_PX;
  const showLabels = checked("opt-labels", OPTS.labels !== false) && readable;
  stacked = [];
  // star labels only move with the camera: skip them on frames where it stood still (dozens of DOM writes a frame
  // otherwise, which a phone feels)
  const camKey = camera.matrixWorld.elements.join(",") + w + "x" + h + showLabels + labels.length;
  if (camKey !== lastCamKey) {
    lastCamKey = camKey;
    for (const l of labels) {
      tmp.copy(l.v).project(camera);
      const vis = showLabels && tmp.z < 1 && Math.abs(tmp.x) < 1 && Math.abs(tmp.y) < 1;
      if (vis !== l.vis) { l.el.style.display = vis ? "block" : "none"; l.vis = vis; }
      if (vis) { l.el.style.left = ((tmp.x + 1) / 2 * w + 8) + "px"; l.el.style.top = ((1 - tmp.y) / 2 * h - 6) + "px"; }
    }
  }
  fleetGroup.visible = checked("opt-fleets", OPTS.fleets !== false);
  for (const l of fleetLabels) {
    tmp.copy(l.v).project(camera);
    const vis = fleetGroup.visible && readable && tmp.z < 1 && Math.abs(tmp.x) < 1 && Math.abs(tmp.y) < 1;
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
  placeOthers(Date.now(), showLabels, w, h);
  placeTrail(showLabels, w, h);
  placeProspects(Date.now(), showLabels);
  moveGroup.visible = checked("opt-moving", OPTS.moving !== false);
  for (const x of movers) {
    tmp.copy(x.v).project(camera);
    const vis = moveGroup.visible && readable && tmp.z < 1 && Math.abs(tmp.x) < 1 && Math.abs(tmp.y) < 1;
    x.el.style.display = vis ? "block" : "none";
    if (vis) stackLabel(x.el, (tmp.x + 1) / 2 * w + 10, (1 - tmp.y) / 2 * h + 6);
  }
}

// Progressive: your systems first (part=core, scope=mine: devices, replicants, fleet homes, scans), then what's on
// them (part=overlay: drones, mining, ships in transit, fleets, supply lines) alongside the rest of the catalog
// (scope=rest) — the map is usable while the later parts are still coming. A refresh takes the whole core at once.
function partUrl(part, scope) {
  const u = new URL(OPTS.dataUrl || "/api/map.json", location.href);
  u.searchParams.set("part", part);
  if (scope) u.searchParams.set("scope", scope);
  return u.href;
}
function fetchPart(part, scope) {
  return fetch(partUrl(part, scope), { credentials: "same-origin", headers: OPTS.headers || {} })
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
  Object.assign(data, { moving: o.moving || [], fleets: o.fleets || [], supply: o.supply || [], prospects: o.prospects || [],
                        others: o.others || [], traffic: o.traffic || [], trail: o.trail || null,
                        relay_net: o.relay_net || { links: [], nodes: [] } });
  build();
  buildMovers();
  buildProspects();
  buildOthers();
  buildTrail();
  buildFleets();
  buildSupply();
  buildRelayNet();
  if (first && data.stars.length) infoText(data, false);
}
let focused = false;
function addRest(r) {
  const fresh = (r.stars || []).filter(s => !byName[s.designation]);
  fresh.forEach(s => { byName[s.designation] = s; data.stars.push(s); });
  if (!data.stars.length) {
    info.innerHTML = `<p class="muted">No star catalog cached yet. It syncs every 30 minutes, or use “refresh catalog”.</p>`;
    return;
  }
  if (fresh.length) {
    if ($("star-list")) $("star-list").innerHTML = data.stars.map(s => `<option value="${esc(s.designation)}">`).join("");
    build();
    infoText(data, false);
    if (!focused) { const s = (OPTS.focus && byName[OPTS.focus]) || repStar(); if (s) { focus(s); focused = true; } }
  }
}
function load(first) {
  return fetchPart("core", first ? "mine" : null).then(d => {
    const keep = { moving: data.moving, fleets: data.fleets, supply: data.supply, prospects: data.prospects,
                   others: data.others, traffic: data.traffic, trail: data.trail };   // until the new overlay lands
    data = { ...keep, ...d };
    d.stars.forEach(s => byName[s.designation] = s);
    if ($("star-list")) $("star-list").innerHTML = d.stars.map(s => `<option value="${esc(s.designation)}">`).join("");
    build();
    if (first) {
      if (!d.stars.length) {
        info.innerHTML = `<p class="muted">Loading the star catalog…</p>`;
      } else {
        infoText(data, true);
        const s = (OPTS.focus && byName[OPTS.focus]) || repStar(); if (s) { focus(s); focused = true; }
      }
      animate();
    }
    const overlay = fetchPart("overlay").then(o => applyOverlay(o, first));
    if (!first) return overlay;
    const rest = fetchPart("core", "rest").then(addRest);
    return Promise.all([overlay, rest]);
  });
}
load(true).catch(e => { info.innerHTML = `<p class="muted">Could not load the map: ${esc(e.message)}</p>`; OPTS.onError?.(e); });
if (OPTS.refreshMinutes) setInterval(() => load(false).catch(e => OPTS.onError?.(e)), OPTS.refreshMinutes * 60000);

// Live: the page's event stream (base.html, sse "state") says when something departed, arrived or changed — redraw
// what's on the stars (ships in transit, fleets, supply lines, mining), at most every few seconds.
const LIVE = /^(travel\.|device\.|devices$|action$|automation$|mining\.|traffic$|trail$)/;
let liveTimer = null, liveLast = 0;
function liveRefresh() {
  liveTimer = null; liveLast = Date.now();
  fetchPart("overlay").then(o => applyOverlay(o, false)).catch(() => {});
}
document.addEventListener("htmx:sseMessage", e => {
  if (e.detail.type !== "state" || !LIVE.test(String(e.detail.data || "")) || liveTimer || !data.stars.length) return;
  liveTimer = setTimeout(liveRefresh, Math.max(1500, 5000 - (Date.now() - liveLast)));
});
