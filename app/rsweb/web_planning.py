"""Blueprints, planning and AMI pages.

Routes split out of web.py by page area; they register on the shared router in the original order."""
from __future__ import annotations

from .web_core import *  # noqa: F401,F403


@router.get("/blueprints", response_class=HTMLResponse)
async def blueprints(request: Request, q: str = "", feature: str = "", user: str = Depends(current_user)):
    db, worker = request.app.state.db, request.app.state.worker
    refresh_msg = None
    # Opening the page re-reads the list if it is more than a minute old (one cheap GET).
    last = parse_ts(await db.kv_updated("blueprints"))
    if request.app.state.api.configured and not request.headers.get("x-live") \
            and (not last or datetime.now(timezone.utc) - last > timedelta(minutes=1)):
        try:
            added = await worker.sync_blueprints()
            if added:
                refresh_msg = "New: " + ", ".join(f_human(t) for t in added)
        except ApiError as e:
            refresh_msg = f"Could not refresh from the game: {e.message}"
    st = await load_state(request)
    bps = normalize_blueprints(await db.kv_get("blueprints", []))
    recent_cut = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    recent = {u["device_type"] for u in await db.kv_get("blueprint_unlocks", []) or [] if u.get("at", "") >= recent_cut}
    features = sorted({f for b in bps for f in b.get("features") or []})   # every row stays; the page filters live
    inv = {i.get("location"): i.get("items") or {} for i in st["inventory"]}
    lo.CLASS_PRINTERS.update(await db.kv_get("class_printers", {}) or {})
    prs = printers(st)
    from . import components as comp
    by_type = {b["device_type"]: b for b in bps}
    for b in bps:
        b["_components"] = comp.components(b)
        b["_carry"] = fl.carry_info(b)
        b["_specs"] = blueprint_specs(b)
        b["_total"] = comp.total_resources(b["device_type"], by_type) if b["_components"] else b["resources"]
        # a print with components is printed with them (components.py): it's affordable when the lot is
        b["_afford"] = {p["code"]: affordable(b["_total"], inv.get(p["location"], {})) for p in prs
                        if not p.get("pclass") or lo.is_class_bp(b)}
    return await page(request, user, "blueprints.html", "blueprints", blueprints=sorted(bps, key=lambda b: b.get("device_type", "")),
                      printers=prs, inv=inv, q=q, feature=feature, features=features, recent=recent, refresh_msg=refresh_msg,
                      exotic=sorted((b for b in bps if lo.is_class_bp(b)), key=lambda b: b.get("device_type", "")),
                      synced=await db.kv_updated("blueprints"))


@router.post("/blueprints/refresh", response_class=HTMLResponse)
async def blueprints_refresh(request: Request, user: str = Depends(current_user)):
    try:
        added = await request.app.state.worker.sync_blueprints()
    except ApiError as e:
        return HTMLResponse(f'<span class="lv-alert">Refresh failed: {html.escape(e.message)}</span>')
    return HTMLResponse("", headers={"HX-Refresh": "true"}) if added else HTMLResponse('<span class="muted">Up to date.</span>')


@router.post("/blueprints/plan", response_class=HTMLResponse)
async def blueprint_plan(request: Request, user: str = Depends(current_user)):
    return partial(request, "partials/plan.html", **await read_plan(request, await request.form()))


@router.post("/blueprints/queue-plan", response_class=HTMLResponse)
async def blueprint_queue_plan(request: Request, user: str = Depends(current_user)):
    form = await request.form()
    p = await read_plan(request, form)
    printer = p["printer"]
    if not printer or not printer.get("device"):
        return HTMLResponse('<div class="result err">No printer to queue on.</div>')
    if not p["lines"]:
        return HTMLResponse('<div class="result err">Enter at least one quantity.</div>')
    vessel = printer["kind"] == "replicant"
    if vessel and sum(n for _, n in p["lines"]) > 1:
        return HTMLResponse('<div class="result err">Vessel printers have no queue and print one device at a time. '
                            'Plan 1 device, or pick an autofactory.</div>')
    gather = form.get("gather") == "on"
    assign = p["parts"] if p["parts"] and p["split_on"] else None
    steps = production.production_steps(printer["device"], printer["name"], p["lines"], p["short"],
                                        p["mining"] if gather else None,
                                        p["transport"] if form.get("deliver") == "on" else None,
                                        p["location"], gather, vessel_replicant=printer["code"] if vessel else None,
                                        assign=assign, flatpack=set(p["large"]) if p["flatpack_on"] and not vessel else None)
    on = f"{len({c for c, _, _ in assign})} autofactories at {p['location']}" if assign else printer["name"]
    title = f"production: {', '.join(f'{n}× {t}' for t, n in p['lines'])} on {on}"
    if gather and p["short"] and p["mining"]:
        title += f" + gather shortfall with {p['mining']['device_code']}"
    return await start_chain(request, user, title, steps[0], steps[1:], printer["device"])


@router.post("/blueprints/print", response_class=HTMLResponse)
async def blueprint_print(request: Request, printer: str = Form(...), device_type: str = Form(...),
                          quantity: int = Form(1), flatpack: str = Form(""), user: str = Depends(current_user)):
    """Queue a print. Several autofactories at the chosen one's location share it, evenly by print time. A large
    (modular) device can be printed compacted."""
    from .modular import modular_type
    kind, _, code = printer.partition(":")
    st = await load_state(request)
    bpm = {b["device_type"]: b for b in normalize_blueprints(await request.app.state.db.kv_get("blueprints", []))}
    flat = flatpack == "on" and kind != "replicant" and modular_type(device_type, bpm)
    me = next((d for d in st["devices"] if d.get("device_code") == code), None)
    peers = [d for d in st["devices"] if me and d.get("location") == me.get("location")
             and "autofactory" in (d.get("device_type") or "") and "enqueue_print" in (d.get("available_commands") or [])]
    if kind == "replicant" or quantity <= 1 or len(peers) < 2:
        return await queue_print(request, user, kind, code, device_type, quantity, flat)
    bps = {b["device_type"]: b for b in normalize_blueprints(await request.app.state.db.kv_get("blueprints", []))}
    load = {d["device_code"]: printqueue.load_seconds(d, bps) for d in peers}
    resp = None
    for f, k in printqueue.split(peers, device_type, quantity, bps, load):
        resp = await queue_print(request, user, kind, f["device_code"], device_type, k, flat)
    return resp


@router.get("/ami", response_class=HTMLResponse)
async def ami(request: Request, user: str = Depends(current_user)):
    st = await load_state(request)
    db = request.app.state.db
    ctrls = []
    for d in st["devices"]:
        if "ami" not in (d.get("features") or []) and "set_directive" not in (d.get("available_commands") or []):
            continue
        kind = controller_kind(d.get("device_type"))
        last = await db.fetchone(
            "SELECT * FROM events WHERE device_code=? AND event LIKE 'ami.%.digest' ORDER BY seq DESC LIMIT 1",
            (d["device_code"],))
        last_dir = await db.fetchone(
            "SELECT * FROM events WHERE device_code=? AND event LIKE 'directive.%' ORDER BY seq DESC LIMIT 1",
            (d["device_code"],))
        candidates = [x for x in st["devices"] if x.get("controller_device_code") == d["device_code"]
                      or (x.get("location") == d.get("location") and amis.can_adopt(d, x))]
        names = await device_directives(request, d)
        csugg = await suggestions(request, d.get("location"))
        ctrls.append({"d": d, "kind": kind, "digest": row_event(last) if last else None, "sugg": csugg,
                      "sys": csugg.get("system"),
                      "directive": row_event(last_dir) if last_dir else None,
                      "directives": names, "first_fields": cmdspec.directive_fields(names[0]) if names else [],
                      "candidates": candidates})
    sugg = await suggestions(request, None)
    return await page(request, user, "ami.html", "ami", ctrls=ctrls, sugg=sugg, **await ami_schedules_ctx(request, st))


@router.post("/ami/{code}/refresh-targets", response_class=HTMLResponse)
async def ami_refresh_targets(request: Request, code: str, user: str = Depends(current_user)):
    dev = await _device(request, code)
    star = star_of(dev.get("location"))
    if not star:
        return HTMLResponse('<span class="lv-alert small">Unknown location.</span>')
    n, err = await refresh_targets(request, star)
    if err:
        return HTMLResponse(f'<span class="lv-alert small">Could not read {html.escape(star)}: {html.escape(str(err))}</span>')
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.get("/ami/{code}/directive-form", response_class=HTMLResponse)
async def ami_directive_form(request: Request, code: str, directive: str = "", user: str = Depends(current_user)):
    dev = await _device(request, code)
    sugg = await suggestions(request, dev.get("location"))
    return partial(request, "partials/fields.html", fields=cmdspec.directive_fields(directive),
                   sugg=sugg, sys_targets=sugg.get("system"), uid=f"d-{code}", self_code=code)


@router.post("/ami/{code}/adopt", response_class=HTMLResponse)
async def ami_adopt(request: Request, code: str, user: str = Depends(current_user)):
    form = await request.form()
    devs = form.getlist("devices")
    cmd = form.get("command") or "adopt"
    return await run_action(request, user, "POST", f"/devices/{code}", {"command": cmd, "devices": devs},
                            f"{cmd} {len(devs)} device(s) on {code}")


@router.post("/ami/{code}/directive", response_class=HTMLResponse)
async def ami_directive(request: Request, code: str, user: str = Depends(current_user)):
    form = await request.form()
    try:
        body = directive_body(form)
    except ValueError as e:  # FormError is a ValueError
        return partial(request, "partials/action_result.html", ok=False, status=400, error=str(e),
                       label="set_directive", method="POST", path=f"/devices/{code}", response=None)
    return await run_action(request, user, "POST", f"/devices/{code}", body, f"{code} directive {body['directive']}")


__all__ = [n for n in dir() if not n.startswith("__")]   # everything, underscore helpers too


@router.get("/blueprints/missing", response_class=HTMLResponse)
async def missing_blueprints(request: Request, hidden: int = 0, user: str = Depends(current_user)):
    """Device types with no blueprint on the account, where they were seen and what holds them (missingbp.py)."""
    from . import missingbp as mb, components as comp
    db = request.app.state.db
    s = await mb.refresh(db)
    bps = {b["device_type"]: b for b in normalize_blueprints(await db.kv_get("blueprints", []))}
    rows = mb.rows(s, bps, await db.kv_get("devices", []) or [], await db.kv_get(comp.LOCKED_KV, {}) or {},
                   show_hidden=bool(hidden))
    return await page(request, user, "missing_blueprints.html", "missingbp", rows=rows, show_hidden=bool(hidden),
                      hidden_count=len(s["hidden"]), refreshed=s.get("refreshed_at"), known=len(bps))


@router.post("/blueprints/missing/add")
async def missing_blueprints_add(request: Request, user: str = Depends(current_user)):
    from . import missingbp as mb
    form = await request.form()
    await mb.add(request.app.state.db, str(form.get("device_type") or ""), str(form.get("note") or ""))
    return Response(status_code=200, headers={"HX-Redirect": "/blueprints/missing"})


@router.post("/blueprints/missing/hide")
async def missing_blueprints_hide(request: Request, user: str = Depends(current_user)):
    from . import missingbp as mb
    form = await request.form()
    await mb.hide(request.app.state.db, str(form.get("device_type") or ""), form.get("unhide") is None)
    return Response(status_code=200, headers={"HX-Redirect": "/blueprints/missing" + ("?hidden=1" if form.get("unhide") is not None else "")})
