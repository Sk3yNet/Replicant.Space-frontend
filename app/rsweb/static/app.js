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
