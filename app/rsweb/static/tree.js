// Tree tab: expand/collapse and filter. The tree always opens fully collapsed.
function treeNodes(kind) {
  const sel = { sys: "details.tnode.sys", type: "details.tnode.type", dev: "details.tnode.dev" }[kind] || "details.tnode";
  return document.querySelectorAll(sel);
}
function treeSet(kind, open) { treeNodes(kind).forEach(d => { d.open = open; }); }
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
// clear a previous version's remembered open nodes
try { localStorage.removeItem("rsweb-tree-open"); } catch (e) {}
