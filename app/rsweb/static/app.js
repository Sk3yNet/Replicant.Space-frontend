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
