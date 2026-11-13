"""Pages, htmx partials, actions and the live update stream."""
from __future__ import annotations

from .web_core import *  # noqa: F401,F403


# =====================================================================================
# pages
# =====================================================================================
@router.get("/healthz")
async def healthz():
    return {"ok": True}


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: str = Depends(current_user)):
    st = await load_state(request)
    visit = await notify.touch_visit(request.app.state.db, user, request.app.state.settings.visit_gap_minutes) \
        if not request.headers.get("hx-request") and not request.headers.get("x-live") else None
    digest = None
    if visit and not visit.get("digest_dismissed"):
        digest = await notify.build_digest(request.app.state.db, visit["baseline_at"])
    alerts = await request.app.state.db.fetchall(
        "SELECT * FROM notifications WHERE read=0 AND level IN ('error','warning','alert','mention') ORDER BY id DESC LIMIT 10")
    recent = [row_event(r) for r in await request.app.state.db.fetchall(
        "SELECT * FROM events WHERE event NOT LIKE 'ami.%.digest' AND event != 'bobnet.new' ORDER BY seq DESC LIMIT 25")]
    sync = {k: await request.app.state.db.kv_get(f"sync:{k}") for k in ("account", "devices", "inventory")}
    cat = await request.app.state.db.kv_get("stars", {}) or {}
    from . import megas
    megas_rows = await megas.dashboard_rows(request.app.state.db)
    tour = await megas.station_tour(request.app.state.db, [m["star"] for m in megas_rows if m["station"]])
    names = {m["star"]: m["items"] for m in megas_rows}
    ctx = await base_ctx(request, user, "dashboard", visit=visit, megas_rows=megas_rows,
                         station_tour=[{**t, "items": names.get(t["star"], [])} for t in tour], digest=digest, alerts=alerts, recent=recent,
                         fleet=fleet_summary(st["devices"]), timers=await active_timers(request),
                         sync=sync, stock_piles=stock_piles(st.get("inventory"), cat), **st)
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@router.post("/digest/dismiss", response_class=HTMLResponse)
async def digest_dismiss(request: Request, user: str = Depends(current_user)):
    await request.app.state.db.execute("UPDATE visitors SET digest_dismissed=1 WHERE email=?", (user,))
    return HTMLResponse("")


@router.get("/digest", response_class=HTMLResponse)
async def digest_view(request: Request, hours: int = 24, user: str = Depends(current_user)):
    hours = max(1, min(hours, 24 * 90))
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    digest = await notify.build_digest(request.app.state.db, since)
    return await page(request, user, "digest.html", "dashboard", digest=digest, hours=hours)


@router.get("/api/timers.json")
async def timers_json(request: Request, user: str = Depends(current_user)):
    """Raw timer rows, for diagnosing what the In progress list is built from."""
    return JSONResponse(await request.app.state.db.fetchall("SELECT * FROM timers ORDER BY ends_at"))


@router.get("/partials/achievements", response_class=HTMLResponse)
async def p_achievements(request: Request, refresh: str = "", user: str = Depends(current_user)):
    return partial(request, "partials/achievements.html", ach=await achievements_status(request, force=bool(refresh)))


@router.get("/partials/news", response_class=HTMLResponse)
async def p_news(request: Request, refresh: str = "", user: str = Depends(current_user)):
    from . import news
    if refresh:
        await request.app.state.worker.automations.news_pass(force=True)
    return partial(request, "partials/news.html", n=await request.app.state.db.kv_get(news.KV, {}) or {})


@router.get("/partials/backlog", response_class=HTMLResponse)
async def p_backlog(request: Request, hours: int = 168, card: str = "backlog", user: str = Depends(current_user)):
    """Dashboard › Logistics backlog, Trends and Print queues: what's waiting on logistics, and the account's shape, over time
    (metrics.py, sampled every 10 minutes)."""
    from . import metrics
    hours = hours if hours in (48, 168, 720) else 168
    if card == "stock":
        return await _stock_card(request, hours)
    s = await metrics.series(request.app.state.db, hours)
    g = lambda k: s.get(k) or []  # noqa: E731
    if card == "trends":
        charts = [("Devices by fleet role", {k[5:]: v for k, v in sorted(s.items()) if k.startswith("role:") and v}),
                  ("Mining output per hour (estimate)", {k[5:]: v for k, v in sorted(s.items()) if k.startswith("mine:") and v
                                                         and any(x for _, x in v)}),
                  ("Systems you can command", {"commandable": g("cmd_systems"), "relay network": g("relay_systems")})]
        title = "Trends"
    elif card == "queues":
        charts = [("Prints waiting per printer", {k[6:]: v for k, v in sorted(s.items()) if k.startswith("queue:") and v})]
        title = "Print queues"
    else:
        charts = [("Resources not in a receiving stockpile", {"in other stockpiles": g("res_away"),
                   "set aside for contracts and trades": g("res_deals"), "in logistics holds": g("res_holds")}),
                  ("Devices waiting to be moved", {"waiting": g("dev_waiting"), "by a replicant": g("dev_waiting_rep")}),
                  ("Logistics requests", {"open": g("req_open"), "unmet": g("req_unmet")}),
                  ("Request age, hours", {"oldest": g("req_age_max_h"), "median": g("req_age_med_h"),
                                          "lead time (24 h median)": g("lead_med_h")}),
                  ("Logistics carriers, %", {"busy": g("carriers_busy_pct"), "hold fill on trips": g("hold_fill_pct")}),
                  ("Missions and orders", {"stalled missions": g("missions_stalled"), "auto-retrying": g("missions_retrying"),
                                           "orders expired (24 h)": g("expired_24h")})]
        title = "Logistics backlog"
    return partial(request, "partials/backlog.html", hours=hours, card=card, title=title,
                   charts=[(t, metrics.chart(lines, hours), 0) for t, lines in charts])


@router.get("/partials/story", response_class=HTMLResponse)
async def p_story(request: Request, refresh: str = "", user: str = Depends(current_user)):
    """The season's story (GET /overview, synced with the star catalog every 30 minutes)."""
    if refresh:
        await request.app.state.worker.sync_overview()
    from . import season
    db = request.app.state.db
    ov = await db.kv_get("overview", {}) or {}
    return partial(request, "partials/story.html", ov=ov, season_end=season.likely_end(ov),
                   hold=await db.kv_get(season.HOLD_KV, None), snap=await db.kv_get(season.SNAP_KV, None),
                   snap_summary=season.summary(await db.kv_get(season.SNAP_KV, None)),
                   outages=[{**w, "times": season.window_times(w)} for w in await db.kv_get(season.OUTAGE_KV, []) or []
                            if w.get("state") in (None, "pending", "holding")])


@router.post("/season/hold", response_class=HTMLResponse)
async def season_hold(request: Request, action: str = Form("pause"), user: str = Depends(current_user)):
    """Pause the engine for a season change, or resume it (only you lift a hold)."""
    from . import season
    db, eng = request.app.state.db, request.app.state.worker.automations
    if action == "resume":
        await db.kv_set(season.HOLD_KV, None)
        await eng.log("season", f"automations resumed ({user})")
    else:
        await db.kv_set(season.SNAP_KV, await season.snapshot(db))   # a fresh copy of the galaxy as it is now
        await db.kv_set(season.HOLD_KV, {"since": now_iso(), "by": user})
        await eng.log("season", f"automations paused for the season change ({user})")
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.get("/season/report", response_class=HTMLResponse)
async def season_report(request: Request, user: str = Depends(current_user)):
    """What the time skip changed: the copy kept before the season change against the galaxy now."""
    from . import season
    db = request.app.state.db
    st = await load_state(request)
    rep = season.compare(await db.kv_get(season.SNAP_KV, None), st["devices"], st["replicants"],
                         await db.kv_get("stars", {}) or {})
    return await page(request, user, "season_report.html", "dashboard", rep=rep,
                      season_now=await db.kv_get(season.SEASON_KV, None))


@router.post("/outage/add", response_class=HTMLResponse)
async def outage_add(request: Request, text: str = Form(""), day: str = Form(""), time: str = Form(""),
                     minutes: str = Form(""), lead: str = Form(""), after: str = Form(""), user: str = Depends(current_user)):
    """Schedule a game outage (season.py): from the announcement's text, or a UTC date and time."""
    from datetime import datetime as _dt
    from . import season
    from .db import now_iso as _now_iso
    got = season.parse_announcement(text) if text.strip() else None
    if not got and day and time:
        try:
            got = (_dt.fromisoformat(f"{day}T{time}").replace(tzinfo=timezone.utc), int(minutes or 60))
        except ValueError:
            got = None
    if not got:
        return HTMLResponse('<span class="err">Could not read a date and a UTC time from that.</span>')
    start, mins = got
    if minutes.strip().isdigit() and not text.strip():
        mins = int(minutes)
    def num(v: str, d: int) -> int:
        return int(v) if v.strip().isdigit() else d
    w = {"id": f"o{int(start.timestamp())}", "start": start.isoformat(), "minutes": mins,
         "lead": num(lead, season.LEAD_MIN), "after": num(after, season.AFTER_MIN), "state": "pending",
         "note": " ".join(text.split())[:200], "by": user, "at": _now_iso()}
    db = request.app.state.db
    wins = [x for x in await db.kv_get(season.OUTAGE_KV, []) or [] if x["id"] != w["id"]]
    await db.kv_set(season.OUTAGE_KV, sorted(wins + [w], key=lambda x: x["start"]))
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/outage/remove", response_class=HTMLResponse)
async def outage_remove(request: Request, id: str = Form(""), user: str = Depends(current_user)):
    from . import season
    db = request.app.state.db
    await db.kv_set(season.OUTAGE_KV, [x for x in await db.kv_get(season.OUTAGE_KV, []) or [] if x["id"] != id])
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.get("/partials/tutorials", response_class=HTMLResponse)
async def p_tutorials(request: Request, refresh: str = "", user: str = Depends(current_user)):
    return partial(request, "partials/tutorials.html", tut=await tutorials_status(request, force=bool(refresh)))


@router.get("/partials/timers", response_class=HTMLResponse)
async def p_timers(request: Request, user: str = Depends(current_user)):
    return partial(request, "partials/timers.html", timers=await active_timers(request))


@router.get("/partials/fleet-summary", response_class=HTMLResponse)
async def p_fleet_summary(request: Request, user: str = Depends(current_user)):
    st = await load_state(request)
    return partial(request, "partials/fleet_summary.html", fleet=fleet_summary(st["devices"]), **st)


@router.get("/partials/rate", response_class=HTMLResponse)
async def p_rate(request: Request, user: str = Depends(current_user)):
    return partial(request, "partials/rate.html", rate=request.app.state.api.rate_status(),
                   stream_state=request.app.state.worker.stream_state)


from .web_devices import *  # noqa: F401,F403,E402   (device list, device pages, the autofactory print queue and replicants)
from .web_systems import *  # noqa: F401,F403,E402   (systems, mining prospects, the star census and system pages)
from .web_map import *  # noqa: F401,F403,E402   (the galaxy map and its relay, hub and prospecting overlays)
from .web_planning import *  # noqa: F401,F403,E402   (blueprints, planning and ami pages)
from .web_activity import *  # noqa: F401,F403,E402   (events, notifications, messages and bobnet, account and console)
from .web_diag import *  # noqa: F401,F403,E402   (diagnostics, snapshots, performance and the live stream)
from .web_deals import *  # noqa: F401,F403,E402   (loadouts, contracts (in-game events) and fleet forms)
