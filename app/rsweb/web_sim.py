"""Economy › Simulations: queue a run (compute cores and a replicant to the datacenter's interface), start it, follow
it, and read the reports of past runs (sim.py, sim_engine.py)."""
from __future__ import annotations

import html

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from . import sim, sim_order
from .db import now_iso
from .shapes import normalize_blueprints
from .web import current_user, load_state, page, run_action

router = APIRouter()


def _msg(text: str, ok: bool = True) -> HTMLResponse:
    return HTMLResponse(f'<div class="result {"ok" if ok else "err"}">{html.escape(text)}</div>',
                        headers={"HX-Refresh": "true"} if ok else {})


def _default_order() -> str:
    from .sim_order import DEFAULT
    return DEFAULT


@router.get("/simulations", response_class=HTMLResponse)
async def simulations_page(request: Request, user: str = Depends(current_user)):
    eng = request.app.state.worker.automations
    s = await eng.sim_state()
    st = await load_state(request)
    loc = (s.get("interface") or {}).get("location") or sim.DATACENTER
    reps_here = [c for c, r in st["replicants"].items() if (r.get("location") or r.get("current_location")) == loc]
    scen = (s.get("prep") or {}).get("scenario") or "mining_rush"
    lessons = {sc.get("code"): sim.lessons(s.get("runs") or [], (s.get("learn") or {}).get(sc.get("code")), sc.get("code"))
               for sc in (s.get("scenarios") or sim.DOC_SCENARIOS)}
    return await page(request, user, "simulations.html", "simulations", s=s, loc=loc, reps_here=reps_here,
                      scenarios=s.get("scenarios") or sim.DOC_SCENARIOS, variants=sim.VARIANTS, cur=scen,
                      lessons=lessons, runs=list(reversed(s.get("runs") or [])),
                      bp_types=sorted({b["device_type"] for b in normalize_blueprints(
                          await request.app.state.db.kv_get("blueprints", [])) if b.get("device_type")}),
                      resources=sim_order.RESOURCES,
                      orders=s.get("orders") or {}, order_choice=s.get("order_choice") or "Default",
                      order_text=(s.get("orders") or {}).get(s.get("order_choice") or "") or _default_order(),
                      virt_devices=await request.app.state.db.kv_get("sim:devices", []) or [],
                      virt_stock=await request.app.state.db.kv_get("sim:inventory", []) or [])


@router.post("/simulations/orders", response_class=HTMLResponse)
async def simulations_order_save(request: Request, name: str = Form(...), text: str = Form(""),
                                 user: str = Depends(current_user)):
    """Save a build order (and choose it for the next run)."""
    from . import sim_order
    name = name.strip()[:40] or "Default"
    steps, errors = sim_order.parse(text)
    if errors:
        return _msg("Not saved: " + "; ".join(errors), ok=False)
    eng = request.app.state.worker.automations
    s = await eng.sim_state()
    s.setdefault("orders", {})[name] = text.strip()
    s["order_choice"] = name
    await eng.sim_save(s)
    return _msg(f"Saved {name} ({len(steps)} steps); the next run follows it.")


@router.post("/simulations/orders/use", response_class=HTMLResponse)
async def simulations_order_use(request: Request, name: str = Form(...), delete: str = Form(""),
                                user: str = Depends(current_user)):
    eng = request.app.state.worker.automations
    s = await eng.sim_state()
    if delete:
        (s.get("orders") or {}).pop(name, None)
        if s.get("order_choice") == name:
            s.pop("order_choice", None)
        await eng.sim_save(s)
        return _msg(f"Deleted {name}.")
    s["order_choice"] = name
    await eng.sim_save(s)
    return _msg(f"The next run follows {name}.")


@router.post("/simulations/queue", response_class=HTMLResponse)
async def simulations_queue(request: Request, scenario: str = Form(...), user: str = Depends(current_user)):
    eng = request.app.state.worker.automations
    s = await eng.sim_state()
    if s.get("active"):
        return _msg("A simulation is running: queue the next one when it ends.", ok=False)
    s["prep"] = {"scenario": scenario, "location": (s.get("interface") or {}).get("location") or sim.DATACENTER,
                 "queued_at": now_iso(), "by": user}
    await eng.sim_save(s)
    return _msg(f"Queued: compute cores and a replicant go to {s['prep']['location']}; you're told when it's ready.")


@router.post("/simulations/cancel", response_class=HTMLResponse)
async def simulations_cancel(request: Request, user: str = Depends(current_user)):
    eng = request.app.state.worker.automations
    s = await eng.sim_state()
    s.pop("prep", None)
    await eng.sim_save(s)
    return _msg("Canceled (cores already pinned there stay pinned).")


@router.post("/simulations/interface", response_class=HTMLResponse)
async def simulations_interface(request: Request, code: str = Form(...), location: str = Form(""),
                                user: str = Depends(current_user)):
    eng = request.app.state.worker.automations
    s = await eng.sim_state()
    s["interface"] = {"code": code.strip().upper(), "location": location.strip().upper() or sim.DATACENTER}
    await eng.sim_load_scenarios(s)
    await eng.sim_save(s)
    return _msg(f"Interface set: {s['interface']['code']} at {s['interface']['location']}.")


@router.post("/simulations/start", response_class=HTMLResponse)
async def simulations_start(request: Request, scenario: str = Form(...), replicant: str = Form(""),
                            user: str = Depends(current_user)):
    ok, text = await request.app.state.worker.automations.sim_start(scenario, replicant.strip().upper() or None)
    return _msg(text, ok=ok)


@router.post("/simulations/abandon", response_class=HTMLResponse)
async def simulations_abandon(request: Request, user: str = Depends(current_user)):
    eng = request.app.state.worker.automations
    s = await eng.sim_state()
    a, iface = s.get("active"), (s.get("interface") or {}).get("code")
    if not a or not iface:
        return _msg("No simulation running.", ok=False)
    out = await run_action(request, user, "DELETE", f"/devices/{iface}/simulate/{a.get('id')}", None,
                           f"abandon simulation {a.get('id')}")
    await eng.sim_finish(await eng.sim_state(), "abandoned")
    return out


def _iface(s: dict) -> str | None:
    return (s.get("interface") or {}).get("code")


@router.get("/simulations/research", response_class=HTMLResponse)
async def simulations_research(request: Request, user: str = Depends(current_user)):
    """Research topics at the datacenter's interface (GET /devices/{interface}/research): each unlocks blueprints."""
    from .api import ApiError
    s = await request.app.state.worker.automations.sim_state()
    iface = _iface(s)
    if not iface:
        return HTMLResponse('<p class="muted small">The interface is not known yet: a replicant at the datacenter finds it.</p>')
    try:
        topics = (await request.app.state.api.get(f"/devices/{iface}/research") or {}).get("research") or []
    except ApiError as e:
        return HTMLResponse(f'<p class="result err">{html.escape(e.message)}</p>')
    if not topics:
        return HTMLResponse('<p class="muted small">No topics: all done, or none released at this datacenter yet.</p>')
    out = ['<table class="small">']
    for t in topics:
        code = html.escape(str(t.get("topic") or ""))
        btn = ('<span class="lv-done">✓ done</span>' if t.get("completed") else
               f'<button class="small" hx-post="/simulations/research/{code}" hx-target="next .rs-out" '
               f'hx-confirm="Research {html.escape(str(t.get("title") or code))}? Your replicant must be at the interface.">Research</button>'
               f'<span class="rs-out"></span>')
        out.append(f'<tr><td><b>{html.escape(str(t.get("title") or code))}</b> <span class="muted mono">{code}</span>'
                   f'<div class="muted">{html.escape(str(t.get("description") or ""))}</div></td><td>{btn}</td></tr>')
    return HTMLResponse("".join(out) + "</table>")


@router.post("/simulations/research/{topic}", response_class=HTMLResponse)
async def simulations_research_do(request: Request, topic: str, user: str = Depends(current_user)):
    s = await request.app.state.worker.automations.sim_state()
    iface = _iface(s)
    if not iface:
        return _msg("The interface is not known yet.", ok=False)
    return await run_action(request, user, "POST", f"/devices/{iface}/research/{topic}", None, f"research {topic}")


@router.get("/simulations/leaderboard", response_class=HTMLResponse)
async def simulations_leaderboard(request: Request, scenario: str = "", user: str = Depends(current_user)):
    """GET /leaderboards/simulations (each scenario's completions and best time), or one scenario's top 10."""
    from .api import ApiError
    api = request.app.state.api
    try:
        if scenario:
            b = await api.get(f"/leaderboards/simulations/{scenario}") or {}
            rows = "".join(f'<tr><td>{e.get("rank")}</td><td>{html.escape(str(e.get("name") or e.get("replicant_code")))}</td>'
                           f'<td class="num">{_dur(e.get("score_seconds"))}</td><td class="num">{e.get("resources_mined") or ""}</td>'
                           f'<td class="num">{e.get("devices_printed") if e.get("devices_printed") is not None else ""}</td></tr>'
                           for e in b.get("entries") or [])
            return HTMLResponse(f'<div class="small"><b>{html.escape(str(b.get("scenario_name") or scenario))}</b> top 10</div>'
                                f'<table class="small"><tr><th>#</th><th>Replicant</th><th class="num">Time</th><th class="num">Mined</th>'
                                f'<th class="num">Printed</th></tr>{rows}</table>')
        b = await api.get("/leaderboards/simulations") or {}
    except ApiError as e:
        return HTMLResponse(f'<p class="result err">{html.escape(e.message)}</p>')
    rows = "".join(f'<tr><td><a href="#" hx-get="/simulations/leaderboard?scenario={html.escape(str(x.get("scenario_code")))}" '
                   f'hx-target="#sim-board">{html.escape(str(x.get("scenario_name") or x.get("scenario_code")))}</a></td>'
                   f'<td class="num">{x.get("completions")}</td><td class="num">{_dur(x.get("best_time_seconds"))}</td></tr>'
                   for x in b.get("scenarios") or [])
    return HTMLResponse(f'<table class="small"><tr><th>Scenario</th><th class="num">Completions</th><th class="num">Best</th></tr>'
                        f'{rows or "<tr><td colspan=3 class=muted>No completions yet.</td></tr>"}</table><div id="sim-board"></div>')


@router.get("/simulations/active", response_class=HTMLResponse)
async def simulations_active(request: Request, user: str = Depends(current_user)):
    """GET /devices/{interface}/simulate/active: the runs the game has going, each with End early and its world."""
    from .api import ApiError
    s = await request.app.state.worker.automations.sim_state()
    iface = _iface(s)
    if not iface:
        return HTMLResponse('<p class="muted small">No interface known.</p>')
    try:
        b = await request.app.state.api.get(f"/devices/{iface}/simulate/active") or {}
    except ApiError as e:
        return HTMLResponse(f'<p class="result err">{html.escape(e.message)}</p>')
    runs = [r for r in b.get("simulations") or [] if isinstance(r, dict)]
    if not runs:
        return HTMLResponse('<p class="muted small">No simulation running.</p>')
    rows = []
    for r in runs:
        sid, name = r.get("simulation_id"), str(r.get("replicant_name") or "")
        left = _left(r.get("started_at"), r.get("timeout_hours"))
        acts = ""
        if r.get("is_mine"):
            acts = (f'<a href="/simulations/world/{html.escape(str(sid))}">devices &amp; galaxy</a> '
                    f'<button class="small ghost" hx-post="/simulations/end" hx-vals=\'{{"sim_id": "{html.escape(str(sid))}"}}\' '
                    f'hx-target="#sim-result" hx-confirm="End simulation #{html.escape(str(sid))} ({html.escape(name)}) now? '
                    f'No score is recorded and the entry cost is not refunded.">End early</button>')
        rows.append(f'<tr><td>#{html.escape(str(sid))}</td><td>{html.escape(str(r.get("scenario_name") or r.get("scenario_code") or ""))}</td>'
                    f'<td>{html.escape(name)}{"" if r.get("is_mine") else " <span class=muted>(not yours)</span>"}</td>'
                    f'<td class="muted">{left}</td><td>{acts}</td></tr>')
    return HTMLResponse('<table class="small"><tr><th>Run</th><th>Scenario</th><th>Replicant</th><th>Time left</th><th></th></tr>'
                        + "".join(rows) + "</table>")


def _left(started: str | None, hours) -> str:
    from datetime import datetime, timedelta, timezone
    try:
        t0 = datetime.fromisoformat(str(started))
        if t0.tzinfo is None:   # the game gives it without an offset: UTC (live 2026-10-15)
            t0 = t0.replace(tzinfo=timezone.utc)
        end = t0 + timedelta(hours=float(hours))
    except (TypeError, ValueError):
        return ""
    secs = int((end - datetime.now(timezone.utc)).total_seconds())
    return "timed out" if secs <= 0 else f"{secs // 3600} h {secs % 3600 // 60:02d} min"


@router.post("/simulations/end", response_class=HTMLResponse)
async def simulations_end(request: Request, sim_id: str = Form(...), user: str = Depends(current_user)):
    """End one run early (DELETE /devices/{interface}/simulate/{id}); the app's own run is closed with it."""
    eng = request.app.state.worker.automations
    s = await eng.sim_state()
    iface = _iface(s)
    if not iface:
        return _msg("No interface known.", ok=False)
    out = await run_action(request, user, "DELETE", f"/devices/{iface}/simulate/{sim_id}", None, f"end simulation {sim_id}")
    s = await eng.sim_state()
    if s.get("active") and str(s["active"].get("id")) == str(sim_id):
        await eng.sim_finish(s, "abandoned")
    return out


@router.get("/simulations/world/{sim_id}", response_class=HTMLResponse)
async def simulations_world(request: Request, sim_id: str, user: str = Depends(current_user)):
    """One run's own world, read live through its replicant: devices, stock and the virtual galaxy around it."""
    from .api import ApiError
    eng = request.app.state.worker.automations
    s = await eng.sim_state()
    iface, api = _iface(s), request.app.state.api
    run, err = {}, None
    try:
        b = await api.get(f"/devices/{iface}/simulate/active") or {} if iface else {}
        run = next((r for r in b.get("simulations") or [] if str(r.get("simulation_id")) == sim_id), {})
    except ApiError as e:
        err = e.message
    a = s.get("active") or {}
    rep = a.get("replicant") if str(a.get("id")) == sim_id else None
    if not rep and run:
        reps = await request.app.state.db.kv_get("replicants", {}) or {}
        rep = run.get("replicant_code") or next((c for c, r in reps.items() if isinstance(r, dict)
                                                 and r.get("name") == run.get("replicant_name")), None)
    devices, stars, here = [], [], {}
    if rep:
        try:
            devices = (await api.get(f"/replicants/{rep}/devices") or {}).get("devices") or []
            sb = await api.get(f"/replicants/{rep}/stars", per_page=50) or {}
            stars, here = sb.get("stars") or [], sb.get("replicant_position") or {}
        except ApiError as e:
            err = e.message
    names = {x.get("designation") for x in stars} | {str(d.get("location") or "").split("-")[0] for d in devices}
    stock = [x for x in await request.app.state.db.kv_get("sim:inventory", []) or []
             if str(x.get("location") or "").split("-")[0] in names]
    occupied = {str(d.get("location") or "").split("-")[0] for d in devices if d.get("location")}
    return await page(request, user, "sim_world.html", "simulations", sim_id=sim_id, run=run or a, rep=rep, err=err,
                      devices=sorted(devices, key=lambda d: (str(d.get("device_type")), str(d.get("device_code")))),
                      stock=stock, galaxy=_galaxy(stars, here, occupied))


def _galaxy(stars: list[dict], here: dict, occupied: set[str]) -> dict:
    """Top-down (x, z) positions scaled into a 600 × 600 box around the replicant."""
    pts = []
    for x in stars:
        p = x.get("position") or {}
        pts.append({"name": x.get("designation"), "dx": float(p.get("x") or 0) - float(here.get("x") or 0),
                    "dz": float(p.get("z") or 0) - float(here.get("z") or 0), "explored": bool(x.get("explored")),
                    "life": bool(x.get("has_life")), "dist": x.get("distance_from_replicant"),
                    "color": str(x.get("color") or "").lower(), "occupied": x.get("designation") in occupied})
    r = max([max(abs(p["dx"]), abs(p["dz"])) for p in pts] + [1.0])
    for p in pts:
        p["sx"], p["sy"] = round(300 + p["dx"] / r * 270, 1), round(300 + p["dz"] / r * 270, 1)
    return {"stars": pts, "radius": round(r, 2)}

def _dur(v) -> str:
    try:
        s = int(v)
    except (TypeError, ValueError):
        return ""
    return f"{s // 3600}h {s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m {s % 60:02d}s"
