// The desk (cookie ui=desk, Joe 2026-11-26): every page opens as its own floating window over the live galaxy map, as
// many as you like (several device pages side by side). Windows drag by their bar, resize from the corner, collapse to a
// tab in the top bar, go full size, and close. Pages load with the header X-Window (base.html sends only <main> and
// scripts), as in the command screen. A link inside a window opens in that window, except a device, system or replicant
// page, which gets its own (or brings forward the one already open). The layout is remembered in this browser.
(() => {
  const SKIP = /^\/(oauth2|static|api|_tenant|wallpaper|bridge|desk|live|lan)(\/|$)|\.(json|csv|txt|png|svg|zip)$/;
  const DETAIL = /^\/(devices|systems|replicants)\/[^/]+\/?$/;   // pages you open several of at once
  const KEY = "desk:layout";
  const $ = id => document.getElementById(id);
  const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) {} };
  let zTop = 20, placed = 0, saving = null;

  const desk = () => $("desk");
  const wins = () => [...desk().querySelectorAll(".desk-win:not(.closed)")];
  const here = u => u.pathname + u.search;

  function inApp(a) {
    if (!a || !a.getAttribute) return null;
    const h = a.getAttribute("href");
    if (!h || h.startsWith("#") || a.target || a.hasAttribute("download") || a.hasAttribute("data-full")
        || a.hasAttribute("hx-get") || a.hasAttribute("hx-post")) return null;
    const u = new URL(h, location.href);
    if (u.origin !== location.origin || SKIP.test(u.pathname)) return null;
    return u;
  }
  const isMap = u => u.pathname === "/map" && !u.search;

  // inline scripts run once per load, each in its own scope; top-level functions stay global for onclick handlers
  // (as bridge.js; with two windows of the same page the later one's functions win)
  function runScripts(root) {
    root.querySelectorAll("script").forEach(old => {
      const s = document.createElement("script");
      [...old.attributes].forEach(a => s.setAttribute(a.name, a.value));
      if (!old.src && (!old.type || old.type === "text/javascript")) {
        const code = old.textContent.replace(/document\.addEventListener\(\s*["']DOMContentLoaded["']\s*,/g, "__winReady(");
        const names = [...code.matchAll(/^\s{0,2}(?:async\s+)?function\s+([\w$]+)/gm)].map(m => m[1]);
        s.textContent = "(function(){const __winReady=f=>f();\n" + code + "\n;" + names.map(n => `if(typeof ${n}==="function")window.${n}=${n};`).join("") + "})();";
      } else s.textContent = old.textContent;
      old.replaceWith(s);
    });
  }

  // --- windows -------------------------------------------------------------------------------------------------
  function frame() {
    const w = document.createElement("section");
    w.className = "desk-win metal";
    w.innerHTML = '<div class="dw-bar plate"><span class="dw-title">…</span><span class="spacer"></span>'
      + '<button type="button" class="dw-fold" title="Collapse">▁</button><button type="button" class="dw-max" title="Full size">⤢</button>'
      + '<button type="button" class="dw-x" title="Close">✕</button></div><div class="dw-body"></div>';
    desk().appendChild(w);
    return w;
  }
  function geom(w, g) {
    const W = desk().clientWidth, H = desk().clientHeight;
    if (!g) {   // cascade from the top left
      const n = placed++ % 8;
      g = { x: 24 + n * 30, y: 16 + n * 26, w: Math.min(780, W - 60), h: Math.min(580, H - 40) };
    }
    const width = Math.max(260, Math.min(g.w, W)), height = Math.max(120, Math.min(g.h, H));
    w.style.width = width + "px";
    w.style.height = height + "px";
    w.style.left = Math.max(0, Math.min(g.x, W - 120)) + "px";
    w.style.top = Math.max(0, Math.min(g.y, H - 40)) + "px";
  }
  function title(w) {
    const main = w.querySelector(".dw-body main");
    const t = (main && main.dataset.title || "").trim() || w.dataset.url || "";
    w.querySelector(".dw-title").textContent = t;
    w.querySelector(".dw-title").title = w.dataset.url || "";
    return t;
  }
  function focus(w) {
    if (!w) return;
    w.style.zIndex = ++zTop;
    wins().forEach(x => x.classList.toggle("focus", x === w));
    if (w.dataset.url) history.replaceState({ desk: 1 }, "", w.dataset.url);
    document.title = (title(w) ? title(w) + " · " : "") + "Replicant Space";
  }
  function find(u) {
    const key = here(u);
    return wins().find(w => w.dataset.url === key)
      || (!DETAIL.test(u.pathname) && !u.search && wins().find(w => new URL(w.dataset.url, location.href).pathname === u.pathname));
  }

  async function load(w, u, keepScroll) {
    const body = w.querySelector(".dw-body");
    w.classList.add("loading");
    try {
      const r = await fetch(u, { headers: { "X-Window": "1" }, credentials: "same-origin" });
      if (!r.ok && r.status !== 404 && r.status !== 422 || !(r.headers.get("content-type") || "").includes("text/html")) {
        location.href = u.href;   // a download or an error: let the browser have it
        return;
      }
      const text = await r.text();
      const final = new URL(r.url || u.href);
      if (isMap(final)) { remove(w); return; }
      body.innerHTML = text;
      w.dataset.url = here(final);
      runScripts(body);
      title(w);
      if (!keepScroll) body.scrollTop = 0;
      if (final.hash) { const t = body.querySelector("#" + CSS.escape(final.hash.slice(1))); if (t) t.scrollIntoView(); }
      if (window.htmx) htmx.process(body);
      if (window.tick) tick();
      if (window.fixDatalists) fixDatalists(body);
      if (w.classList.contains("focus")) focus(w);
      document.dispatchEvent(new CustomEvent("win:loaded", { detail: { url: final.href } }));
    } catch (e) {
      body.innerHTML = '<p class="muted small" style="padding:12px">Could not load ' + u.pathname + ".</p>";
    } finally {
      w.classList.remove("loading");
      save();
    }
  }
  function open(u, g) {
    if (isMap(u)) return null;
    const had = find(u);
    if (had) { unfold(had); focus(had); return had; }
    const w = frame();
    w.dataset.url = here(u);
    geom(w, g);
    focus(w);
    load(w, u);
    return w;
  }
  window.deskOpen = href => open(new URL(href, location.href));

  function remove(w) { w.remove(); tabs(); save(); const top = wins().sort((a, b) => b.style.zIndex - a.style.zIndex)[0]; if (top) focus(top); else history.replaceState({ desk: 1 }, "", "/map"); }
  function unfold(w) { w.classList.remove("folded"); tabs(); }
  function tabs() {
    const t = $("desk-tabs");
    if (!t) return;
    t.innerHTML = "";
    wins().filter(w => w.classList.contains("folded")).forEach(w => {
      const b = document.createElement("button");
      b.type = "button"; b.className = "small"; b.textContent = w.querySelector(".dw-title").textContent; b.title = w.dataset.url || "";
      b.addEventListener("click", () => { unfold(w); focus(w); save(); });
      t.appendChild(b);
    });
  }

  // --- the layout, remembered in this browser -------------------------------------------------------------------
  function save() {
    clearTimeout(saving);
    saving = setTimeout(() => {
      const list = wins().filter(w => w.dataset.url).sort((a, b) => a.style.zIndex - b.style.zIndex).map(w => ({
        u: w.dataset.url, x: parseInt(w.style.left) || 0, y: parseInt(w.style.top) || 0, w: w.offsetWidth, h: w.offsetHeight,
        f: w.classList.contains("folded") ? 1 : 0, m: w.classList.contains("max") ? 1 : 0 }));
      store(KEY, JSON.stringify(list));
    }, 200);
  }
  function saved() { try { return JSON.parse(store(KEY) || "[]") || []; } catch (e) { return []; } }
  function apply(w, it) {
    geom(w, it);
    w.classList.toggle("folded", !!it.f);
    w.classList.toggle("max", !!it.m);
  }

  function tidy() {   // side by side in a grid, unfolded
    const ws = wins();
    if (!ws.length) return;
    const W = desk().clientWidth, H = desk().clientHeight, gap = 8;
    const cols = Math.ceil(Math.sqrt(ws.length)), rows = Math.ceil(ws.length / cols);
    const cw = (W - gap * (cols + 1)) / cols, ch = (H - gap * (rows + 1)) / rows;
    ws.forEach((w, i) => {
      w.classList.remove("folded", "max");
      geom(w, { x: gap + (i % cols) * (cw + gap), y: gap + Math.floor(i / cols) * (ch + gap), w: cw, h: ch });
    });
    tabs(); save();
  }

  // --- moving a window by its bar --------------------------------------------------------------------------------
  let drag = null;
  document.addEventListener("pointerdown", e => {
    const w = e.target.closest && e.target.closest(".desk-win");
    if (!w) return;
    focus(w);
    const bar = e.target.closest(".dw-bar");
    if (!bar || e.target.closest("button") || e.button || w.classList.contains("max")) return;
    drag = { w, dx: e.clientX - w.offsetLeft, dy: e.clientY - w.offsetTop, id: e.pointerId };
    bar.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  document.addEventListener("pointermove", e => {
    if (!drag || e.pointerId !== drag.id) return;
    const W = desk().clientWidth, H = desk().clientHeight;
    drag.w.style.left = Math.max(-drag.w.offsetWidth + 80, Math.min(e.clientX - drag.dx, W - 80)) + "px";
    drag.w.style.top = Math.max(0, Math.min(e.clientY - drag.dy, H - 30)) + "px";
  });
  document.addEventListener("pointerup", e => {
    if (drag && e.pointerId === drag.id) { drag = null; save(); }
    else if (e.target.closest && e.target.closest(".desk-win")) save();   // a resize from the corner ends here too
  });
  if (window.ResizeObserver) {
    const ro = new ResizeObserver(() => save());
    new MutationObserver(ms => ms.forEach(m => m.addedNodes.forEach(n => n.classList && n.classList.contains("desk-win") && ro.observe(n))))
      .observe(document.documentElement, { childList: true, subtree: true });
    document.addEventListener("DOMContentLoaded", () => document.querySelectorAll(".desk-win").forEach(w => ro.observe(w)));
  }

  // --- links, forms and the window buttons ---------------------------------------------------------------------------
  document.addEventListener("click", e => {
    if (e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const w = e.target.closest && e.target.closest(".desk-win");
    const btn = e.target.closest && e.target.closest(".dw-bar button");
    if (w && btn) {
      if (btn.classList.contains("dw-x")) remove(w);
      else if (btn.classList.contains("dw-fold")) { w.classList.toggle("folded"); tabs(); save(); }
      else if (btn.classList.contains("dw-max")) { w.classList.toggle("max"); save(); }
      return;
    }
    const launch = document.querySelector(".desk-launch[open]");
    if (launch && !launch.contains(e.target)) launch.open = false;
    const acct = document.querySelector(".acct-menu[open]");
    if (acct && !acct.contains(e.target)) acct.open = false;
    const a = e.target.closest && e.target.closest("a");
    if (a && a.hasAttribute("data-desk-map")) { e.preventDefault(); return; }   // the map is always there behind
    const u = inApp(a);
    if (!u) return;
    if (w && here(u) === w.dataset.url && u.hash) return;   // a jump within the page
    e.preventDefault();
    if (launch) launch.open = false;
    if (acct) acct.open = false;
    if (w && !(DETAIL.test(u.pathname) && new URL(w.dataset.url, location.href).pathname !== u.pathname)) {
      if (isMap(u)) return;
      const other = find(u);
      if (other && other !== w && DETAIL.test(u.pathname)) { unfold(other); focus(other); return; }
      load(w, u);
    } else open(u);
  });

  document.addEventListener("submit", e => {   // a plain GET form (filters, searches): its page in its window
    const f = e.target;
    if (e.defaultPrevented || f.hasAttribute("hx-get") || f.hasAttribute("hx-post") || f.hasAttribute("onsubmit")
        || (f.getAttribute("method") || "get").toLowerCase() !== "get" || f.target) return;
    const u = new URL(f.getAttribute("action") || location.pathname, location.href);
    if (u.origin !== location.origin || SKIP.test(u.pathname)) return;
    e.preventDefault();
    u.search = new URLSearchParams(new FormData(f, e.submitter)).toString();
    const w = f.closest(".desk-win");
    if (w) load(w, u); else open(u);
  });

  // pages share one document: a swap aimed at an id lands in the window that asked (two device windows have the same ids)
  document.addEventListener("htmx:beforeSwap", e => {
    const src = (e.detail.requestConfig && e.detail.requestConfig.elt) || e.detail.elt, t = e.detail.target;
    const w = src && src.closest && src.closest(".desk-win");
    if (w && t && t.id && !w.contains(t)) {
      const mine = w.querySelector("#" + CSS.escape(t.id));
      if (mine) e.detail.target = mine;
    }
  });

  document.addEventListener("keydown", e => {
    if (e.key !== "Escape" || e.target.closest("input, textarea, select")) return;
    const m = document.querySelector(".desk-launch[open], .acct-menu[open]");
    if (m) m.open = false;
  });

  document.addEventListener("DOMContentLoaded", () => {
    const first = desk().querySelector(".desk-win[data-first]");
    let keep = null;
    if (first && !first.classList.contains("closed")) {
      first.dataset.url = here(new URL(location.href));
      title(first);
      keep = first;
    } else if (first) first.remove();
    const list = saved();
    let matched = false;
    for (const it of list) {
      if (!it || !it.u) continue;
      if (keep && it.u === keep.dataset.url && !matched) { apply(keep, it); keep.style.zIndex = ++zTop; matched = true; continue; }
      const u = new URL(it.u, location.href);
      if (SKIP.test(u.pathname) || isMap(u)) continue;
      const w = frame();
      w.dataset.url = it.u;
      apply(w, it);
      w.style.zIndex = ++zTop;
      w.querySelector(".dw-title").textContent = it.u;
      load(w, u, true);
    }
    if (keep && !matched) geom(keep);
    tabs();
    if (keep) { keep.classList.remove("folded"); focus(keep); }
    else { const top = wins().pop(); if (top) focus(top); }
    $("desk-tidy").addEventListener("click", tidy);
    $("desk-clear").addEventListener("click", () => { wins().forEach(w => w.remove()); tabs(); save(); history.replaceState({ desk: 1 }, "", "/map"); });
    save();
  });
})();
