// Countdown timers: any element with data-ends (ISO time) shows time remaining.
function fmtLeft(ms) {
  if (ms <= 0) return "done";
  const s = Math.floor(ms / 1000), d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600),
        m = Math.floor((s % 3600) / 60), sec = s % 60;
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${sec}s`;
  return `${sec}s`;
}
function tick() {
  const now = Date.now();
  document.querySelectorAll("[data-ends]").forEach(el => {
    const end = Date.parse(el.dataset.ends), start = Date.parse(el.dataset.start || el.dataset.ends);
    const left = end - now;
    const out = el.querySelector(".left") || el;
    out.textContent = fmtLeft(left);
    if (left > 0) el.dataset.ticking = "1";
    else if (el.dataset.ticking) {   // reached zero while the page was open: a live section shows what happened
      delete el.dataset.ticking;
      if (el.closest("[data-live]")) document.dispatchEvent(new Event("live:due"));
    }
    const bar = el.querySelector(".bar > i");
    if (bar && end > start) bar.style.width = Math.max(0, Math.min(100, 100 * (now - start) / (end - start))) + "%";
    el.classList.toggle("done", left <= 0);
  });
}
setInterval(tick, 1000);
document.addEventListener("htmx:afterSettle", tick);
document.addEventListener("DOMContentLoaded", tick);

// Toasts disappear on their own.
document.addEventListener("htmx:sseMessage", e => {
  if (e.detail.type === "notify") {
    document.querySelectorAll("#toasts .toast").forEach(t => {
      if (!t.dataset.timer) t.dataset.timer = setTimeout(() => t.remove(), 12000);
    });
  }
  if (e.detail.type === "event") {
    const feed = document.getElementById("live-feed");
    if (feed) while (feed.children.length > 200) feed.lastElementChild.remove();
  }
});

// Fill a JSON textarea from a <select> of templates (device commands, AMI directives).
function fillTemplate(sel, targetId) {
  const opt = sel.options[sel.selectedIndex];
  const t = document.getElementById(targetId);
  if (t && opt && opt.dataset.tpl !== undefined) t.value = opt.dataset.tpl;
}
// Forms marked data-danger-check ask before running destructive commands.
const DANGEROUS = ["decommission", "change_owner", "deactivate", "withdraw", "clear_queue", "release", "clear_directive"];
// Only the form submission itself is checked — not the GET that loads a command's fields.
document.addEventListener("htmx:confirm", e => {
  const f = e.detail.elt;
  if (!(f.matches && f.matches("form[data-danger-check]")) || e.detail.verb === "get") return;
  const cmd = (f.querySelector("[name=command]") || {}).value;
  if (DANGEROUS.includes(cmd)) {
    e.preventDefault();
    if (confirm(`Really run "${cmd}"?`)) e.detail.issueRequest(true);
  }
});

// Collect resources: copy the server's fill plan into the amount boxes, or clear them.
function fillCargo(id) {
  document.querySelectorAll(`#${id} input[data-fill]`).forEach(i => { i.value = i.dataset.fill || ""; });
}
function clearCargo(id) {
  document.querySelectorAll(`#${id} input`).forEach(i => { i.value = ""; });
}

// Firefox shows only an <option>'s text in a <datalist> drop-down, not its value (Chrome and Safari
// show both). Put the value in front of the description there, e.g. "AEMEROTH — star · M0".
const IS_FIREFOX = /firefox/i.test(navigator.userAgent);
function fixDatalists(root) {
  if (!IS_FIREFOX) return;
  (root || document).querySelectorAll("datalist option").forEach(o => {
    const v = o.value, t = (o.textContent || "").trim();
    if (v && t && !t.startsWith(v)) o.textContent = `${v} — ${t}`;
  });
}
document.addEventListener("DOMContentLoaded", () => fixDatalists());
document.addEventListener("htmx:afterSettle", () => fixDatalists());

// Terraforming console: a world card isn't redrawn while one of its controls is held (a slider dragged on a touchscreen
// takes no focus, so the refresh's focus guard misses it); a refresh or another control's answer arriving meanwhile is
// held back and fetched again once the control is let go. A slider's number follows it while it moves.
(() => {
  let busy = null, held = false;
  document.addEventListener("pointerdown", (e) => {
    const card = e.target.closest && e.target.closest(".tf-world");
    if (card && e.target.closest("input, select, label")) busy = card.id;
  }, true);
  const release = () => {
    const id = busy;
    if (!id) return;
    setTimeout(() => {
      if (busy !== id) return;
      busy = null;
      const card = document.getElementById(id);
      if (held && card && window.htmx) htmx.ajax("GET", card.getAttribute("hx-get"), { target: card, swap: "outerHTML" });
      held = false;
    }, 600);
  };
  document.addEventListener("pointerup", release, true);
  document.addEventListener("pointercancel", release, true);
  document.addEventListener("input", (e) => {
    if (!e.target.matches(".tf-set input[type=range]")) return;
    const n = e.target.nextElementSibling;
    if (n) n.textContent = (+e.target.value).toFixed(2);
  });
  document.addEventListener("htmx:beforeSwap", (e) => {
    const t = e.detail.target;
    if (busy && t && t.id === busy) { e.detail.shouldSwap = false; held = true; }
  });
})();

// Dashboard cards: reorder (drag the ⠿ handle, or ▲ ▼) and fold (▾); both remembered in this browser.
(function () {
  const KO = "dash:order", KC = "dash:folded";
  const get = (k, d) => { try { return JSON.parse(localStorage.getItem(k)) || d; } catch (e) { return d; } };
  const put = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} };
  const cards = box => [...box.children].filter(c => c.dataset && c.dataset.card);
  const save = box => put(KO, cards(box).map(c => c.dataset.card));
  function tools(card) {
    if (card.querySelector(":scope > .card-tools")) return;
    const t = document.createElement("span");
    t.className = "card-tools";
    t.innerHTML = '<button type="button" class="ghost" data-act="drag" title="Drag to move">⠿</button>' +
      '<button type="button" class="ghost" data-act="up" title="Move up">▲</button>' +
      '<button type="button" class="ghost" data-act="down" title="Move down">▼</button>' +
      '<button type="button" class="ghost" data-act="fold" title="Fold / unfold">▾</button>';
    card.prepend(t);
  }
  function init() {
    const box = document.getElementById("dash-cards");
    if (!box) return;
    const order = get(KO, []), folded = get(KC, []);
    const orig = cards(box), by = Object.fromEntries(orig.map(c => [c.dataset.card, c]));
    if (order.some(id => by[id])) {
      order.filter(id => by[id]).forEach(id => box.appendChild(by[id]));
      orig.forEach((c, i) => {   // a card that's new since the order was saved: after the card it follows in the page
        if (order.includes(c.dataset.card)) return;
        const prev = orig.slice(0, i).reverse().find(p => p !== c);
        box.insertBefore(c, prev ? prev.nextElementSibling : box.firstElementChild);
      });
    }
    cards(box).forEach(c => { tools(c); c.classList.toggle("folded", folded.includes(c.dataset.card)); });
    box.addEventListener("click", e => {
      const b = e.target.closest(".card-tools button"); if (!b) return;
      const c = b.closest(".card"), act = b.dataset.act;
      if (act === "fold") {
        c.classList.toggle("folded");
        put(KC, cards(box).filter(x => x.classList.contains("folded")).map(x => x.dataset.card));
      } else if (act === "up" && c.previousElementSibling) { box.insertBefore(c, c.previousElementSibling); save(box); }
      else if (act === "down" && c.nextElementSibling) { box.insertBefore(c.nextElementSibling, c); save(box); }
    });
    let dragged = null;
    box.addEventListener("pointerdown", e => {
      const b = e.target.closest('.card-tools [data-act="drag"]'); if (b) b.closest(".card").draggable = true;
    });
    box.addEventListener("dragstart", e => { dragged = e.target.closest(".card"); dragged && dragged.classList.add("dragging"); });
    box.addEventListener("dragover", e => {
      const over = e.target.closest(".card");
      if (!dragged || !over || over === dragged || over.parentNode !== box) return;
      e.preventDefault();
      const r = over.getBoundingClientRect();
      const after = (e.clientX - r.left) / r.width + (e.clientY - r.top) / r.height > 1;
      box.insertBefore(dragged, after ? over.nextElementSibling : over);
    });
    box.addEventListener("dragend", () => {
      if (dragged) { dragged.classList.remove("dragging"); dragged.draggable = false; dragged = null; save(box); }
    });
    // a card refreshed by htmx loses its tools: put them back
    document.addEventListener("htmx:afterSwap", e => { const c = e.detail.target; if (c && c.dataset && c.dataset.card) tools(c); });
    const reset = document.getElementById("dash-reset");
    if (reset) reset.addEventListener("click", e => { e.preventDefault(); put(KO, []); put(KC, []); location.reload(); });
  }
  document.addEventListener("DOMContentLoaded", init);
})();

// Live sections: parts of a page are redrawn from the app's own cached state (never a game call) when the server reports a
// change: at most once a minute, only while the tab is visible, with one request for the whole page. A live section is
// an element marked data-live with an id, or any card of a page whose <main> has data-live-cards (cards that fill
// themselves with hx-get, or hold a script, are left alone). A data-live element with its own hx-get asks that again.
// A section isn't redrawn while it holds focus or a form's typed-in input, or for a minute and a half after a button or form in it
// was used (its answer stays readable); open <details> stay open. The header's live switch pauses it (remembered in this
// browser). For a minute after an order (any button or form that changes something) reported changes show
// within seconds (at most one redraw every 8 s); so do they for a minute after a countdown in a section reaches zero. A page script that filters or decorates a section redoes that on the live:swapped event.
(() => {
  const KEY = "live:off", MIN_MS = 60000, SETTLE_MS = 3000, HOLD_MS = 90000, FAST_MS = 60000, FAST_GAP_MS = 8000;
  const SELF = /load|revealed|intersect|every|sse:/;
  const seen = {};
  let pending = false, last = Date.now(), timer = null, busy = false, fastUntil = 0;
  const off = () => { try { return localStorage.getItem(KEY) === "1"; } catch (e) { return false; } };
  const fills = el => el.hasAttribute("hx-get") && SELF.test(el.getAttribute("hx-trigger") || "");
  function sections(root) {   // [[key, element]] in page order; the same keys for the live page and a fresh copy
    const main = root.querySelector("main"), out = [], n = {};
    if (!main) return out;
    const add = (k, el) => { n[k] = (n[k] || 0) + 1; out.push([n[k] > 1 ? k + "#" + n[k] : k, el]); };
    main.querySelectorAll("[data-live][id]").forEach(el => add("#" + el.id, el));
    if (main.hasAttribute("data-live-cards")) main.querySelectorAll(".card").forEach(el => {
      if (el.closest("[data-live], [data-no-live]") || el.parentElement.closest(".card") || fills(el) || el.querySelector("script")
          || [...el.querySelectorAll("[hx-get]")].some(fills)) return;
      const h = el.querySelector("h1, h2, h3");
      add(el.id ? "#" + el.id : "card:" + (el.dataset.card || (h ? h.textContent.trim() : "")), el);
    });
    return out;
  }
  function schedule() {
    pending = true;
    if (timer || busy || off() || document.hidden || !sections(document).length) return;
    const fast = Date.now() < fastUntil;   // just after an order: follow its result closely
    timer = setTimeout(run, Math.max(SETTLE_MS, last + (fast ? FAST_GAP_MS : MIN_MS) - Date.now()));
  }
  function blocked(el) {
    const a = document.activeElement;
    if (a && el.contains(a) && a.matches("input, select, textarea, [contenteditable]")) return true;
    if (Date.now() - (+el.dataset.liveUsed || 0) < HOLD_MS) return true;
    return !!el.querySelector("[data-dirty]");
  }
  function swap(el, html) {
    const open = [...el.querySelectorAll("details")].map(d => d.open);
    el.innerHTML = html;
    el.querySelectorAll("details").forEach((d, i) => { if (open[i] !== undefined) d.open = open[i]; });
    if (window.htmx) htmx.process(el);
    tick();
    fixDatalists(el);
    el.dispatchEvent(new CustomEvent("live:swapped", { bubbles: true }));
  }
  async function run() {
    timer = null;
    if (off() || document.hidden || busy) return;
    pending = false; busy = true; last = Date.now();
    try {
      const mine = sections(document).filter(([, el]) => !blocked(el));
      mine.filter(([, el]) => fills(el)).forEach(([, el]) =>
        htmx.ajax("GET", el.getAttribute("hx-get"), { source: el, target: el, swap: el.getAttribute("hx-swap") || "innerHTML" }));
      const diff = mine.filter(([, el]) => !fills(el));
      if (!diff.length) return;
      const u = new URL(location.href);
      u.searchParams.delete("refresh");
      const r = await fetch(u, { headers: { "X-Live": "1" }, credentials: "same-origin" });
      if (!r.ok || r.redirected) return;
      const fresh = new Map(sections(new DOMParser().parseFromString(await r.text(), "text/html")));
      diff.forEach(([k, el]) => {
        const nu = fresh.get(k);
        if (!nu || blocked(el)) return;
        const html = nu.innerHTML, prev = k in seen ? seen[k] : el.innerHTML;
        seen[k] = html;
        if (html !== prev) swap(el, html);
      });
    } catch (e) {
    } finally {
      busy = false;
      if (pending) schedule();
    }
  }
  function badge() {
    const b = document.getElementById("live-switch");
    if (!b) return;
    b.hidden = !sections(document).length;
    b.classList.toggle("off", off());
    b.title = off() ? "Live updates paused: click to resume" : "Parts of this page update by themselves: click to pause";
  }
  const section = el => el && el.closest && (el.closest("[data-live]") || el.closest("main[data-live-cards] .card"));
  document.addEventListener("sse:state", schedule);
  document.addEventListener("live:due", () => { fastUntil = Date.now() + FAST_MS; clearTimeout(timer); timer = null; schedule(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden && pending) schedule(); });
  // typing into a form holds its section back (a page's own filter boxes outside forms don't)
  document.addEventListener("input", e => { if (e.target.form && section(e.target)) e.target.setAttribute("data-dirty", ""); });
  document.addEventListener("htmx:beforeRequest", e => {
    const s = section(e.detail.elt), cfg = e.detail.requestConfig, t = cfg && cfg.triggeringEvent && cfg.triggeringEvent.type;
    if (s && t && !/^(sse:|load|revealed|intersect|every)/.test(t)) s.dataset.liveUsed = Date.now();   // a click, a change, a submit
  });
  document.addEventListener("htmx:afterRequest", e => {
    const f = e.detail.elt && e.detail.elt.closest && e.detail.elt.closest("form");
    if (f && e.detail.successful) f.querySelectorAll("[data-dirty]").forEach(i => i.removeAttribute("data-dirty"));
    const verb = e.detail.requestConfig && e.detail.requestConfig.verb;
    if (e.detail.successful && verb && verb !== "get") { fastUntil = Date.now() + FAST_MS; clearTimeout(timer); timer = null; schedule(); }
  });
  document.addEventListener("DOMContentLoaded", () => {
    badge();
    const b = document.getElementById("live-switch");
    if (b) b.addEventListener("click", () => {
      try { off() ? localStorage.removeItem(KEY) : localStorage.setItem(KEY, "1"); } catch (e) {}
      badge();
      if (!off()) { last = 0; schedule(); }
    });
  });
})();
