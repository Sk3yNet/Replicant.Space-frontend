"""Economy › Simulations: queue a run (compute cores and a replicant to the datacenter's interface), start it, follow
it, and read the reports of past runs (sim.py, sim_engine.py)."""
from __future__ import annotations

import html

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from . import sim
from .db import now_iso
from .web import current_user, load_state, page, run_action

router = APIRouter()


def _msg(text: str, ok: bool = True) -> HTMLResponse:
    return HTMLResponse(f'<div class="result {"ok" if ok else "err"}">{html.escape(text)}</div>',
                        headers={"HX-Refresh": "true"} if ok else {})


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
                      virt_devices=await request.app.state.db.kv_get("sim:devices", []) or [],
                      virt_stock=await request.app.state.db.kv_get("sim:inventory", []) or [])


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
    """GET /devices/{interface}/simulate/active: the run as the game sees it."""
    from .api import ApiError
    s = await request.app.state.worker.automations.sim_state()
    iface = _iface(s)
    if not iface:
        return HTMLResponse('<p class="muted small">No interface known.</p>')
    try:
        b = await request.app.state.api.get(f"/devices/{iface}/simulate/active") or {}
    except ApiError as e:
        return HTMLResponse(f'<p class="result err">{html.escape(e.message)}</p>')
    import json as _json
    return HTMLResponse(f'<pre class="small">{html.escape(_json.dumps(b, indent=1, default=str)[:4000])}</pre>')


def _dur(v) -> str:
    try:
        s = int(v)
    except (TypeError, ValueError):
        return ""
    return f"{s // 3600}h {s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m {s % 60:02d}s"
