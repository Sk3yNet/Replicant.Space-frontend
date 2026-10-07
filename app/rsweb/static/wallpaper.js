// The desktop wallpaper (Octos add-on): the galaxy map, one system, or a cycle through your systems.
// Link: /wallpaper/<slug>/?view=galaxy|system|cycle&star=X&labels=1&rotate=0.4&refresh=5&cycle=60
//        &cover=1&fleets=1&supply=1&production=1&hud=right|left|off#key=rsw_…
// The key stays in the #fragment (never sent to the server, so never in access logs) and goes out as a header.
const base = new URL("..", import.meta.url);            // …/wallpaper/<slug>/
// the client's version: scripts are loaded with ?v=<version>, so a redeploy is never hidden by a cached copy
const version = new URL(import.meta.url).searchParams.get("v") || "";
const q = new URLSearchParams(location.search);
const key = new URLSearchParams(location.hash.slice(1)).get("key") || "";
const view = (q.get("view") || "galaxy").toLowerCase();
const star = (q.get("star") || "").trim().toUpperCase();
const labels = q.get("labels") !== "0";
const rotate = Math.max(0, Math.min(5, parseFloat(q.get("rotate") ?? "0.4") || 0));
const refreshMin = Math.max(1, parseInt(q.get("refresh") || "5", 10));
const cycleSec = Math.max(10, parseInt(q.get("cycle") || "60", 10));
const cover = q.get("cover") !== "0";          // relay/hub range spheres
const fleets = q.get("fleets") !== "0";        // fleet markers (galaxy) and fleets in the caption (systems)
const supply = q.get("supply") !== "0";        // supply lines between fleets' systems
const production = q.get("production") !== "0"; // sparkles: drones mining each resource, per system
// resource colours: the galaxy's sparkles and the panel's stockpile rows (keep in step with map.js RES_COLOR)
const RES_CSS = { structural: "#b0bec5", conductive: "#ffa726", silicates: "#e6c88f", carbon: "#a1887f", volatiles: "#4dd0e1",
                  rares: "#e040fb" };
const hud = ["left", "right", "off"].includes(q.get("hud")) ? q.get("hud") : "right";   // the dashboard panel
const headers = { "X-Wallpaper-Key": key };
let hudData = null;
const $ = id => document.getElementById(id);

function message(html) { $("msg").innerHTML = html; $("msg").hidden = false; }
function problem(e) {
  const m = String(e?.message || e);
  if (m.startsWith("401"))
    message("<div><b>This wallpaper link no longer works.</b><br>Make a new one under Account › Desktop wallpaper.</div>");
  else if (m.startsWith("404"))
    message("<div>Nothing to show here yet: the wallpaper is turned off, or this system has no stored scan.</div>");
  else   // the server is unreachable for now: keep what's on screen, say so quietly; the next refresh tries again
    $("caption").innerHTML += `<small>can't reach the server (${m}) — retrying</small>`;
}

async function get(path) {
  const r = await fetch(new URL(path, base), { headers, cache: "no-store" });
  if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
  return r;
}

// --- dashboard panel: stockpiles with their 48-hour trend, what your devices are doing, your fleets --------------
const ICON = { mining: "⛏", explore: "◎", trade: "⇄" };
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = n => { n = Number(n) || 0; const a = Math.abs(n);
  return a >= 1e6 ? (n / 1e6).toFixed(1) + "M" : a >= 1e4 ? (n / 1e3).toFixed(1) + "k" : Math.round(n).toLocaleString(); };
function fleetLine(f) {
  const doing = f.phase ? f.phase + (f.state === "stalled" ? " · stalled" : "") : f.state;
  const at = (f.stars || [])[0] || f.home || "?";
  return `<li class="${f.state === "stalled" ? "stalled" : ""}"${f.note ? ` title="${esc(f.note)}"` : ""}>` +
    `<span class="ic">${ICON[f.role] || "⚑"}</span><b>${esc(f.name)}</b> <span class="dim">${esc(doing)}</span>` +
    `<br><span class="dim">${esc(at)}${f.target && f.target !== at ? ` → ${esc(f.target)}` : ""} · ` +
    `${[f.working && `${f.working} working`, f.moving && `${f.moving} moving`, f.idle && `${f.idle} idle`].filter(Boolean).join(", ") || `${f.members} devices`}</span></li>`;
}
const SUPPLY_STATE = { moving: "in transit", ferrying: "ferrying", active: "running", planned: "planned" };
function supplyLine(l) {
  const n = l.state === "moving" ? ` (${l.in_transit})` : l.state === "ferrying" && l.freighters ? ` (${l.freighters})` : "";
  return `<li class="sup ${esc(l.kind)} ${esc(l.state)}"><span class="ic">${l.kind === "trade" ? "⇄" : "➜"}</span>` +
    `${esc(l.from)} → ${esc(l.to)} <span class="dim">${SUPPLY_STATE[l.state] || esc(l.state)}${n}</span>` +
    `<br><span class="dim">${esc(l.from_fleet)}${l.to_fleet ? ` → ${esc(l.to_fleet)}` : ""}</span></li>`;
}
function drawHud() {
  const d = hudData;
  if (!d || hud === "off") return;
  const dv = d.devices, tot = Math.max(1, dv.total);
  const bar = ["working", "moving", "idle"].map(k => `<i class="b-${k}" style="width:${100 * dv[k] / tot}%"></i>`).join("");
  const res = d.resources.map(r => `<tr><td><i class="rdot" style="background:${RES_CSS[r.name] || "#888"}"></i>${esc(r.name)}</td><td class="n">${num(r.qty)}</td>` +
    `<td>${r.spark ? `<svg width="90" height="20"><polyline points="${r.spark}"/></svg>` : ""}</td>` +
    `<td class="n ${r.change > 0 ? "up" : r.change < 0 ? "down" : ""}">${r.change ? (r.change > 0 ? "▲" : "▼") + num(Math.abs(r.change)) : ""}</td></tr>`).join("");
  $("hud").innerHTML =
    `<h3>Devices <span class="dim">${dv.total}</span></h3><div class="wpbar">${bar}</div>` +
    `<div class="counts"><b class="c-working">${dv.working} working</b> · <b class="c-moving">${dv.moving} moving</b> · ` +
    `<b class="c-idle">${dv.idle} idle</b></div>` +
    (res ? `<h3>Stockpiles <span class="dim">48 h</span></h3><table>${res}</table>` : "") +
    (d.fleets.length ? `<h3>Fleets</h3><ul>${d.fleets.map(fleetLine).join("")}</ul>` : "") +
    (supply && (d.supply || []).length ? `<h3>Supply</h3><ul>${d.supply.map(supplyLine).join("")}</ul>` : "");
  $("hud").className = hud;
  $("hud").hidden = false;
}
async function loadHud() {
  if (hud === "off" && !fleets && !supply) return;
  hudData = await (await get("api/hud.json")).json();
  drawHud();
}
function fleetsAt(code) {
  if (!hudData) return "";
  const here = fleets ? hudData.fleets.filter(f => (f.stars || []).includes(code) || f.target === code) : [];
  const lines = supply ? (hudData.supply || []).filter(l => l.from === code || l.to === code) : [];
  return here.length || lines.length ? `<ul class="fl">${here.map(fleetLine).join("")}${lines.map(supplyLine).join("")}</ul>` : "";
}

async function showSystem(code, note = "") {
  const svg = await (await get(`api/system/${encodeURIComponent(code)}`)).text();
  $("sys").innerHTML = svg;
  const el = $("sys").querySelector("svg.system");
  if (el && !labels) el.classList.add("nolabels");
  $("sys").hidden = false;
  $("caption").innerHTML = `${code}<small>${note}updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</small>` +
    fleetsAt(code);
}

async function galaxy() {
  $("map").hidden = false;
  window.MAP_OPTS = { dataUrl: new URL("api/map.json", base).href, headers, labels, rotate, refreshMinutes: refreshMin,
                      focus: star || null, onError: problem, cover, fleets, supply, production, moving: true };
  await import(new URL(`static/map.js?v=${encodeURIComponent(version)}`, base).href);
}

async function system() {
  if (!star) { message("<div>Pick a system for this view (the <b>System</b> setting).</div>"); return; }
  const go = () => showSystem(star).catch(problem);
  await go();
  setInterval(go, refreshMin * 60000);
}

async function cycle() {
  let list = [], i = 0;
  const reload = async () => { list = (await (await get("api/systems.json")).json()).systems.map(s => s.star); };
  await reload();
  if (!list.length) { message("<div>No scanned systems with your devices yet.</div>"); return; }
  const next = () => { const s = list[i++ % list.length]; return showSystem(s, `${(i - 1) % list.length + 1} of ${list.length} · `).catch(problem); };
  await next();
  setInterval(next, cycleSec * 1000);
  setInterval(() => reload().catch(problem), refreshMin * 60000);
}

// bottom-right corner: which client (and add-on, when the Octos launcher says) is running
$("ver").textContent = [version && `client ${version}`, q.get("addon") && `add-on ${q.get("addon")}`].filter(Boolean).join(" · ");

if (!key) {
  message("<div><b>No wallpaper key in this link.</b><br>Copy the whole wallpaper link from Account › Desktop wallpaper.</div>");
} else {
  // the panel first (system captions list the fleets there), then the view; a panel failure never blocks the view
  loadHud().catch(problem).finally(() => ({ galaxy, system, cycle }[view] || galaxy)().catch(problem));
  setInterval(() => loadHud().catch(problem), refreshMin * 60000);
}
