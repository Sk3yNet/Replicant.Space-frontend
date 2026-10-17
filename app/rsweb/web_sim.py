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
    catalog = sorted((s.get("catalog") or {}).values(), key=lambda x: (x.get("location") or "", x.get("code") or ""))
    return await page(request, user, "simulations.html", "simulations", s=s, loc=loc, reps_here=reps_here, catalog=catalog,
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
    errors += sim_order.check(steps, {b["device_type"]: b for b in normalize_blueprints(
        await request.app.state.db.kv_get("blueprints", []))})
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
async def simulations_queue(request: Request, scenario: str = Form(...), interface: str = Form(""),
                            user: str = Depends(current_user)):
    """Approve a scenario (like a contract): its entry cost (compute cores: spares pinned there, else printed and
    delivered) and a replicant go to that interface; the run is started here once they're there."""
    eng = request.app.state.worker.automations
    s = await eng.sim_state()
    if s.get("active"):
        return _msg("A simulation is running: queue the next one when it ends.", ok=False)
    x = (s.get("catalog") or {}).get(interface.strip().upper()) if interface.strip() else None
    if x:   # the run happens at the interface it was approved at
        if x["code"] != (s.get("interface") or {}).get("code"):
            s["interface"] = {"code": x["code"], "location": x["location"], "found": True}
        if x.get("scenarios"):
            s["scenarios"] = x["scenarios"]
    s["prep"] = {"scenario": scenario, "location": (s.get("interface") or {}).get("location") or sim.DATACENTER,
                 "queued_at": now_iso(), "by": user}
    await eng.sim_save(s)
    ctype, need = sim.cost(sim.scenario(s, scenario))
    what = f"{need} {ctype.replace('_', ' ')}(s) and a replicant go" if need else "a replicant goes (no entry cost)"
    return _msg(f"Approved: {what} to {s['prep']['location']}; you're told when it's ready.")


@router.post("/simulations/scan", response_class=HTMLResponse)
async def simulations_scan(request: Request, user: str = Depends(current_user)):
    """List the scenarios of every known replicant interface now."""
    eng = request.app.state.worker.automations
    async with eng.lock:
        s = await eng.sim_state()
        out = await eng.sim_catalog(s, force=True)
        await eng.sim_save(s)
    return _msg("; ".join(out) or "No replicant interface known yet: a device scan from a replicant at a datacenter finds one.",
                ok=bool(out))


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
            acts = (f'<a href="/fleet?sim={html.escape(str(sid))}">devices</a> · <a href="/map?sim={html.escape(str(sid))}">galaxy</a> '
                    + (f'· <a href="/terraforming?sim={html.escape(str(sid))}"><b>terraforming controls</b></a> '
                       if "terraform" in str(r.get("scenario_code") or "") else "")
                    + f'<button class="small ghost" hx-post="/simulations/end" hx-vals=\'{{"sim_id": "{html.escape(str(sid))}"}}\' '
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


@router.get("/simulations/world/{sim_id}")
async def simulations_world(sim_id: str, user: str = Depends(current_user)):
    from fastapi.responses import RedirectResponse
    return RedirectResponse(f"/fleet?sim={sim_id}", status_code=303)

def _dur(v) -> str:
    try:
        s = int(v)
    except (TypeError, ValueError):
        return ""
    return f"{s // 3600}h {s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m {s % 60:02d}s"
