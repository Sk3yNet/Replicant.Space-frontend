// The command screen (cookie ui=bridge): pages open as a window over the galaxy map without reloading the screen.
// A link or a GET form inside the app loads its page with the header X-Window (base.html then sends only the page's
// <main> and scripts) into #win-body, and the address bar shows the page's own URL, so reload, back and shared links
// work as before. /map closes the window. A page is fetched when the pointer goes down, so it is often there by the click.
(() => {
  const SKIP = /^\/(oauth2|static|api|_tenant|wallpaper|bridge|live)(\/|$)|\.(json|csv|txt|png|svg|zip)$/;
  const html = document.documentElement;
  const $ = id => document.getElementById(id);
  const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) {} };
  let ctl = null, pre = null;   // the window load in flight; a fetch started on pointerdown {url, at, p}

  function inApp(a) {
    if (!a || !a.getAttribute) return null;
    const h = a.getAttribute("href");
    if (!h || h.startsWith("#") || a.target || a.hasAttribute("download") || a.hasAttribute("data-full")
        || a.hasAttribute("hx-get") || a.hasAttribute("hx-post")) return null;
    const u = new URL(h, location.href);
    if (u.origin !== location.origin || SKIP.test(u.pathname)) return null;
    if (u.pathname === location.pathname && u.search === location.search && u.hash) return null;   // a jump within the page
    return u;
  }
  const isMap = u => u.pathname === "/map" && !u.search;
  const get = u => fetch(u, { headers: { "X-Window": "1" }, credentials: "same-origin" });

  // Inline scripts run once per load, each in its own scope (a page opened twice would otherwise redeclare its
  // constants); its top-level functions stay global for the page's onclick handlers.
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

  function markRail(active) {
    document.querySelectorAll(".rail-item").forEach(it => {
      const on = [...it.querySelectorAll(".fly a")].some(a => a.dataset.key === active) || it.querySelector(".rail-btn").dataset.key === active;
      it.querySelector(".rail-btn").classList.toggle("on", on);
    });
    document.querySelectorAll(".fly a").forEach(a => a.classList.toggle("on", a.dataset.key === active));
  }

  function close(push) {
    const w = $("win"), body = $("win-body");
    w.classList.add("closed");
    body.innerHTML = "";
    document.title = "Galaxy · Replicant Space";
    markRail("map");
    if (push) history.pushState({ win: 1 }, "", "/map");
  }

  async function open(u, push, keepScroll) {
    closeFly();
    if (isMap(u)) return close(push);
    if (ctl) ctl.abort();
    const mine = ctl = new AbortController();
    const w = $("win"), body = $("win-body");
    w.classList.add("loading");
    try {
      const p = pre && pre.url === u.href && Date.now() - pre.at < 5000 ? pre.p : get(u);
      pre = null;
      const r = await p;
      if (mine.signal.aborted) return;
      if (!r.ok && r.status !== 404 && r.status !== 422 || !(r.headers.get("content-type") || "").includes("text/html")) {
        location.href = u.href;   // not a page (a download, an error): let the browser have it
        return;
      }
      const text = await r.text();
      if (mine.signal.aborted) return;
      const final = new URL(r.url || u.href);
      if (final.pathname === "/map" && !final.search) { close(false); if (push) history.pushState({ win: 1 }, "", "/map"); return; }
      body.innerHTML = text;
      runScripts(body);
      const main = body.querySelector("main");
      const title = (main && main.dataset.title || "").trim();
      $("win-title").textContent = title;
      document.title = (title ? title + " · " : "") + "Replicant Space";
      markRail(main && main.dataset.active);
      w.classList.remove("closed");
      if (push) history.pushState({ win: 1 }, "", final.pathname + final.search + final.hash);
      if (!keepScroll) body.scrollTop = 0;
      if (final.hash) { const t = document.getElementById(final.hash.slice(1)); if (t) t.scrollIntoView(); }
      if (window.htmx) htmx.process(body);
      if (window.tick) tick();
      if (window.fixDatalists) fixDatalists(body);
      document.dispatchEvent(new CustomEvent("win:loaded", { detail: { url: final.href } }));
    } catch (e) {
      if (e.name !== "AbortError") location.href = u.href;
    } finally {
      if (ctl === mine) { ctl = null; w.classList.remove("loading"); }
    }
  }
  window.bridgeOpen = (href, push = true) => open(new URL(href, location.href), push);

  // --- the rail's fly-outs -------------------------------------------------------------------------------------
  function closeFly(except) {
    document.querySelectorAll(".fly").forEach(f => { if (f !== except) f.hidden = true; });
  }

  document.addEventListener("pointerdown", e => {
    if (e.button) return;
    const u = inApp(e.target.closest && e.target.closest("a"));
    if (u && !isMap(u) && !(pre && pre.url === u.href)) pre = { url: u.href, at: Date.now(), p: get(u) };
  }, true);

  document.addEventListener("click", e => {
    if (e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const a = e.target.closest && e.target.closest("a");
    if (a && a.dataset.fly) {   // a group with several pages: show its list
      e.preventDefault();
      const f = a.parentElement.querySelector(".fly");
      closeFly(f);
      f.hidden = !f.hidden;
      return;
    }
    if (!e.target.closest(".fly")) closeFly();
    const acct = document.querySelector(".acct-menu[open]");
    if (acct && !acct.contains(e.target)) acct.open = false;
    const u = inApp(a);
    if (!u) return;
    e.preventDefault();
    if (acct) acct.open = false;
    if (html.classList.contains("ol-open") && a.closest("#outliner")) html.classList.remove("ol-open");
    open(u, true);
  });

  document.addEventListener("submit", e => {   // a plain GET form (filters, searches): its page in the window
    const f = e.target;
    if (e.defaultPrevented || f.hasAttribute("hx-get") || f.hasAttribute("hx-post") || f.hasAttribute("onsubmit")
        || (f.getAttribute("method") || "get").toLowerCase() !== "get" || f.target) return;
    const u = new URL(f.getAttribute("action") || location.pathname, location.href);
    if (u.origin !== location.origin || SKIP.test(u.pathname)) return;
    e.preventDefault();
    u.search = new URLSearchParams(new FormData(f, e.submitter)).toString();
    open(u, true);
  });

  window.addEventListener("popstate", () => open(new URL(location.href), false, true));

  document.addEventListener("DOMContentLoaded", () => {
    history.replaceState({ win: 1 }, "");
    const w = $("win");
    if (store("bridge:max") === "1") w.classList.add("max");
    $("win-max").addEventListener("click", () => { w.classList.toggle("max"); store("bridge:max", w.classList.contains("max") ? "1" : "0"); });
    if (store("bridge:ol") === "0") html.classList.add("ol-hidden");
    $("outliner-toggle").addEventListener("click", () => {
      if (matchMedia("(max-width: 800px)").matches) html.classList.toggle("ol-open");
      else { html.classList.toggle("ol-hidden"); store("bridge:ol", html.classList.contains("ol-hidden") ? "0" : "1"); }
    });
    document.addEventListener("keydown", e => {
      if (e.key === "Escape" && !e.target.closest("input, textarea, select")) {
        if (html.classList.contains("ol-open")) html.classList.remove("ol-open");
        else if (!document.querySelector(".fly:not([hidden])")) { if (!w.classList.contains("closed")) close(true); }
        else closeFly();
      }
    });
  });

  // the outliner's folds stay as you left them across its redraws
  const OLK = "bridge:folds";
  const folds = () => { try { return JSON.parse(store(OLK) || "{}"); } catch (e) { return {}; } };
  document.addEventListener("htmx:afterSwap", e => {
    if (!e.detail.target || e.detail.target.id !== "outliner") return;
    const f = folds();
    e.detail.target.querySelectorAll("details[data-key]").forEach(d => {
      if (d.dataset.key in f) d.open = f[d.dataset.key];
      d.addEventListener("toggle", () => { const g = folds(); g[d.dataset.key] = d.open; store(OLK, JSON.stringify(g)); });
    });
  });
})();
