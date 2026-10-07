// The desktop wallpaper (Octos add-on): the galaxy map, one system, or a cycle through your systems.
// Link: /wallpaper/<slug>/?view=galaxy|system|cycle&star=X&labels=1&rotate=0.4&refresh=5&cycle=60#key=rsw_…
// The key stays in the #fragment (never sent to the server, so never in access logs) and goes out as a header.
const base = new URL("..", import.meta.url);            // …/wallpaper/<slug>/
const q = new URLSearchParams(location.search);
const key = new URLSearchParams(location.hash.slice(1)).get("key") || "";
const view = (q.get("view") || "galaxy").toLowerCase();
const star = (q.get("star") || "").trim().toUpperCase();
const labels = q.get("labels") !== "0";
const rotate = Math.max(0, Math.min(5, parseFloat(q.get("rotate") ?? "0.4") || 0));
const refreshMin = Math.max(1, parseInt(q.get("refresh") || "5", 10));
const cycleSec = Math.max(10, parseInt(q.get("cycle") || "60", 10));
const headers = { "X-Wallpaper-Key": key };
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

async function showSystem(code, note = "") {
  const svg = await (await get(`api/system/${encodeURIComponent(code)}`)).text();
  $("sys").innerHTML = svg;
  const el = $("sys").querySelector("svg.system");
  if (el && !labels) el.classList.add("nolabels");
  $("sys").hidden = false;
  $("caption").innerHTML = `${code}<small>${note}updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</small>`;
}

async function galaxy() {
  $("map").hidden = false;
  window.MAP_OPTS = { dataUrl: new URL("api/map.json", base).href, headers, labels, rotate, refreshMinutes: refreshMin,
                      focus: star || null, onError: problem };
  await import(new URL("static/map.js", base).href);
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

if (!key) {
  message("<div><b>No wallpaper key in this link.</b><br>Copy the whole wallpaper link from Account › Desktop wallpaper.</div>");
} else {
  ({ galaxy, system, cycle }[view] || galaxy)().catch(problem);
}
