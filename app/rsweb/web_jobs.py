"""Economy › Jobs: the colony stations' job boards (game v4.0.0; stations.py). Stations and their details read from
anywhere; a board only with a replicant at the station (live 2026-11-09), so other boards show as last read. Accept,
complete and abandon by hand, or hand a job to the runner."""
from __future__ import annotations

import html

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from . import stations as sj
from .web import call_action, current_user, load_state, page, partial, render_action, run_action, start_chain

router = APIRouter()


async def jobs_ctx(request: Request) -> dict:
    db = request.app.state.db
    st = await load_state(request)
    sts = ((await db.kv_get(sj.KV_STATIONS, {})) or {})
    stations = sts.get("stations") or []
    boards = await db.kv_get(sj.KV_BOARDS, {}) or {}
    mine = await db.kv_get(sj.KV_MINE, {}) or {}
    cfg = await sj.runner_cfg(db)
    from .shapes import normalize_inventory
    inv = {i.get("location"): i.get("items") or {} for i in normalize_inventory(await db.kv_get("inventory", []))}
    active = []
    for j in sj.accepted(mine):
        ob = sj.objective(j, stations)
        met, note = sj.progress(ob, inv, st["devices"])
        here = [c for loc in ob["complete_at"] for c in sj.reps_at(st["replicants"], st["devices"], loc)]
        active.append({**j, "ob": ob, "met": met, "note": note, "here": here, "kind": ob["kind"],
                       "run": str(j.get("id")) in set(cfg.get("run") or []),
                       "supplied": (cfg.get("supplied") or {}).get(str(j.get("id")))})
    rows = sj.board_rows(stations, boards, mine)
    station_rows = []
    for s in stations:
        slug = s.get("slug")
        fs = sj.factions(s)
        tot = sum(float((fs.get(r) or {}).get("influence") or 0) for r in sj.ROLES) or 1.0
        station_rows.append({**s, "facs": [{"role": r, "name": (fs.get(r) or {}).get("name") or r,
                                            "summary": (fs.get(r) or {}).get("summary") or "",
                                            "influence": float((fs.get(r) or {}).get("influence") or 0),
                                            "share": float((fs.get(r) or {}).get("influence") or 0) / tot,
                                            "rep": sj.reputation(mine, slug, r)} for r in sj.ROLES],
                             "board": boards.get(slug) or {}, "open": sum(1 for x in rows if x["station_slug"] == slug)})
    reps = [(c, r.get("name") or c, r.get("location") or r.get("current_location")) for c, r in st["replicants"].items()]
    return {"my_jobs": active, "rows": rows, "stations": station_rows, "stats": mine.get("stats") or {},
            "mine_at": mine.get("at"), "boards_at": sts.get("at"), "cooldown": sj.cooldown_until(mine), "cfg": cfg,
            "roles": sj.ROLES, "runnable": sj.RUNNABLE, "kind_label": sj.kind_label, "reps": reps,
            "kinds": sorted({r["kind"] for r in rows})}


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_page(request: Request, user: str = Depends(current_user)):
    return await page(request, user, "jobs.html", "jobs", **await jobs_ctx(request))


@router.post("/jobs/refresh", response_class=HTMLResponse)
async def jobs_refresh(request: Request, user: str = Depends(current_user)):
    errs = await sj.refresh(request.app.state.api, request.app.state.db, boards=True, background=False)
    if errs:
        return HTMLResponse(f'<span class="result err">{html.escape("; ".join(errs))}</span>', headers={"HX-Refresh": "true"})
    return HTMLResponse("", headers={"HX-Refresh": "true"})


async def _after(request: Request, resp: HTMLResponse) -> HTMLResponse:
    """Re-read your jobs after a command, so the page shows the new state."""
    try:
        await sj.refresh(request.app.state.api, request.app.state.db, boards=False, background=False)
    except Exception:
        pass
    return resp


@router.post("/jobs/{jid}/accept", response_class=HTMLResponse)
async def jobs_accept(request: Request, jid: int, run: str = Form(""), user: str = Depends(current_user)):
    out = await call_action(request, user, "POST", f"/jobs/{jid}/accept", None, f"accept station job {jid}")
    resp = render_action(request, out)
    if run and out.get("ok"):
        db = request.app.state.db
        cfg = await sj.runner_cfg(db)
        cfg["run"] = sorted({*map(str, cfg.get("run") or []), str(jid)})
        await db.kv_set(sj.KV_RUNNER, cfg)
    return await _after(request, resp)


@router.post("/jobs/{jid}/complete", response_class=HTMLResponse)
async def jobs_complete(request: Request, jid: int, user: str = Depends(current_user)):
    return await _after(request, await run_action(request, user, "POST", f"/jobs/{jid}/complete", None, f"complete station job {jid}"))


@router.post("/jobs/{jid}/abandon", response_class=HTMLResponse)
async def jobs_abandon(request: Request, jid: int, user: str = Depends(current_user)):
    return await _after(request, await run_action(request, user, "DELETE", f"/jobs/{jid}", None, f"abandon station job {jid}"))


@router.post("/jobs/{jid}/run", response_class=HTMLResponse)
async def jobs_run(request: Request, jid: int, on: str = Form(""), user: str = Depends(current_user)):
    """Hand an accepted job to the runner, or take it back."""
    db = request.app.state.db
    cfg = await sj.runner_cfg(db)
    run = set(map(str, cfg.get("run") or []))
    (run.add if on else run.discard)(str(jid))
    cfg["run"] = sorted(run)
    await db.kv_set(sj.KV_RUNNER, cfg)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/jobs/settings", response_class=HTMLResponse)
async def jobs_settings(request: Request, user: str = Depends(current_user)):
    form = await request.form()
    db = request.app.state.db
    cfg = await sj.runner_cfg(db)
    cfg["auto"] = bool(form.get("auto"))
    cfg["tier2"] = bool(form.get("tier2"))
    cfg["stations"] = [s for s in form.getlist("stations") if sj.valid_slug(str(s))]
    cfg["factions"] = [r for r in form.getlist("factions") if r in sj.ROLES] or list(sj.ROLES)
    cfg["kinds"] = [k for k in form.getlist("kinds") if k in sj.RUNNABLE] or list(sj.RUNNABLE)
    await db.kv_set(sj.KV_RUNNER, cfg)
    return HTMLResponse('<span class="result ok">Saved.</span>')


@router.get("/jobs/station/{slug}", response_class=HTMLResponse)
async def jobs_station(request: Request, slug: str, user: str = Depends(current_user)):
    """A station's detail (contributors leaderboard, your reputation), read when its row is opened."""
    from .api import ApiError
    if not sj.valid_slug(slug):
        return HTMLResponse('<span class="result err">Unknown station.</span>')
    db = request.app.state.db
    try:
        d = await request.app.state.api.get(f"/stations/{slug}", background=False) or {}
        cache = await db.kv_get(sj.KV_DETAIL, {}) or {}
        cache[slug] = {**d, "at": sj._now().isoformat(timespec="seconds")}
        await db.kv_set(sj.KV_DETAIL, cache)
    except ApiError as e:
        d = ((await db.kv_get(sj.KV_DETAIL, {})) or {}).get(slug)
        if not d:
            return HTMLResponse(f'<span class="result err">{html.escape(e.message)}</span>')
    return partial(request, "partials/station_detail.html", d=d, roles=sj.ROLES)


@router.post("/jobs/{jid}/scan", response_class=HTMLResponse)
async def jobs_scan(request: Request, jid: int, replicant: str = Form(...), user: str = Depends(current_user)):
    """Scan jobs: the replicant flies to the body, scans, flies back to the station and completes the job."""
    from . import automations as auto
    db = request.app.state.db
    stations = ((await db.kv_get(sj.KV_STATIONS, {})) or {}).get("stations") or []
    j = next((x for x in sj.accepted(await db.kv_get(sj.KV_MINE, {}) or {}) if str(x.get("id")) == str(jid)), None)
    if not j:
        return HTMLResponse('<span class="result err">That job isn\'t one of your accepted jobs.</span>')
    ob = sj.objective(j, stations)
    if not ob["target"] or not ob["station"]:
        return HTMLResponse('<span class="result err">The job doesn\'t name a body to scan: do it by hand.</span>')
    rep = html.escape(replicant)
    steps = [auto.step(f"{rep} → {ob['target']}", f"/replicants/{replicant}/travel", {"destination": ob["target"]},
                       wait=["travel.arrived"], match={"destination": ob["target"]}),
             auto.step(f"{rep}: scan {ob['target']}", f"/replicants/{replicant}/scan", {}),
             auto.step(f"{rep} → {ob['station']}", f"/replicants/{replicant}/travel", {"destination": ob["station"]},
                       wait=["travel.arrived"], match={"destination": ob["station"]}),
             auto.step(f"complete {j.get('title')}", f"/jobs/{jid}/complete", None)]
    return await start_chain(request, user, f"station job {j.get('title')}: scan {ob['target']}", steps[0], steps[1:], None)
