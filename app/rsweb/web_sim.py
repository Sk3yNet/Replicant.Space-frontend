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
                      lessons=lessons, runs=list(reversed(s.get("runs") or [])))


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
