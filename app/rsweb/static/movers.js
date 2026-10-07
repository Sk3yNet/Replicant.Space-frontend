// Devices in transit: place each arrow on its route for the current time (legs are timed), refresh every 2 s.
(function () {
  function fmt(s) { s = Math.max(0, Math.round(s)); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60);
    return h ? h + "h " + m + "m" : m ? m + "m" : s + "s"; }
  function tick() {
    const now = Date.now();
    const svg = document.querySelector("svg.system"); if (!svg) return;
    const W = svg.viewBox.baseVal.width || 760;
    document.querySelectorAll("svg.system .mover").forEach(function (g) {
      const segs = JSON.parse(g.dataset.segs), t0 = +g.dataset.t0, t1 = +g.dataset.t1;
      let s = segs.find(x => now < x[5]) || segs[segs.length - 1];
      if (now < segs[0][4]) s = segs[0];
      const f = Math.max(0, Math.min(1, (now - s[4]) / Math.max(1, s[5] - s[4])));
      const x = s[0] + (s[2] - s[0]) * f, y = s[1] + (s[3] - s[1]) * f;
      const ang = Math.atan2(s[3] - s[1], s[2] - s[0]) * 180 / Math.PI;
      g.querySelector(".mover-arrow").setAttribute("transform", "translate(" + x + " " + y + ") rotate(" + ang + ")");
      const lab = g.querySelector(".mover-label"), right = x > 0.55 * W;   // near the right edge: label on the left
      lab.setAttribute("x", right ? x - 10 : x + 10); lab.setAttribute("y", y < 20 ? y + 18 : y - 8);
      lab.setAttribute("text-anchor", right ? "end" : "start");
      const pct = Math.max(0, Math.min(100, Math.round(100 * (now - t0) / Math.max(1, t1 - t0))));
      lab.textContent = g.dataset.label + " · " + pct + "% · " + (now < t1 ? fmt((t1 - now) / 1000) : "arriving");
    });
  }
  tick(); setInterval(tick, 2000);
})();
