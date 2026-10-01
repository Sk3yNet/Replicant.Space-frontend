// Tree tab: expand/collapse, filter, and remembering what was open (per browser, best effort).
const KEY = "rsweb-tree-open";
function treeNodes(kind) {
  const sel = kind === "sys" ? "details.tnode.sys" : kind === "dev" ? "details.tnode.dev" : "details.tnode";
  return document.querySelectorAll(sel);
}
function treeSet(kind, open) { treeNodes(kind).forEach(d => { d.open = open; }); treeSave(); }
function treeSave() {
  try { localStorage.setItem(KEY, JSON.stringify([...treeNodes("all")].filter(d => d.open).map(d => d.id))); } catch (e) {}
}
function treeRestore() {
  let ids = null;
  try { ids = JSON.parse(localStorage.getItem(KEY) || "null"); } catch (e) {}
  if (!ids) { treeSet("sys", true); return; }  // first visit: systems open, devices closed
  ids.forEach(id => { const d = document.getElementById(id); if (d) d.open = true; });
}
function treeFilter(q) {
  q = q.trim().toLowerCase();
  const all = [...treeNodes("all")];
  if (!q) { all.forEach(d => d.style.display = ""); return; }
  // a node is shown if it or anything inside it matches; matching branches are opened
  [...all].reverse().forEach(d => {
    const self = (d.dataset.search || "").includes(q);
    const kid = [...d.querySelectorAll(":scope > .tbody > .tchildren > details.tnode, :scope > .tchildren > details.tnode")]
      .some(k => k.style.display !== "none");
    d.style.display = self || kid ? "" : "none";
    if (kid) d.open = true;
  });
}
document.addEventListener("toggle", e => { if (e.target.matches && e.target.matches("details.tnode")) treeSave(); }, true);
document.addEventListener("DOMContentLoaded", treeRestore);
