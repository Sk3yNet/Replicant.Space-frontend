"""Diagnostics, snapshots, performance and the live stream.

Routes split out of web.py by page area; they register on the shared router in the original order."""
from __future__ import annotations

from .web_core import *  # noqa: F401,F403


@router.get("/diagnostics", response_class=HTMLResponse)
async def diagnostics(request: Request, user: str = Depends(current_user)):
    db = request.app.state.db
    snap = await db.kv_get("snapshot_last", None)
    status = await db.kv_get("snapshot_status", {}) or {}
    tmpl = "partials/diagnostics_body.html" if request.headers.get("hx-request") else "diagnostics.html"
    ctx = {"snap": snap, "diag": (snap or {}).get("diagnosis"), "status": status,
           "feedback_sent": await db.kv_get("feedback_sent", []) or [], "feedback_types": FEEDBACK_TYPES,
           "viability": await request.app.state.worker.automations.viability_report(),
           "snap_sections": snapmod.SECTIONS, "snap_on": await _snapshot_sections(db)}
    if request.query_params.get("perf"):
        from . import perf
        return partial(request, "partials/perf.html", perf=await perf.report(db))
    if tmpl.startswith("partials"):
        return partial(request, tmpl, **ctx)
    return await page(request, user, tmpl, "diagnostics", **ctx)


@router.post("/diagnostics/snapshot", response_class=HTMLResponse)
async def diagnostics_snapshot(request: Request, user: str = Depends(current_user)):
    form = await request.form()
    stars = {x.strip().upper() for x in re.split(r"[,\s]+", form.get("stars") or "") if x.strip()} or None
    db = request.app.state.db
    if form.get("sections_form"):   # the checkboxes were on the page (unchecked ones aren't sent)
        sections = {k for k in form.getlist("section") if k in snapmod.SECTION_KEYS}
        await db.kv_set("snapshot_sections", sorted(sections))
    else:
        sections = await _snapshot_sections(db)
    st = await db.kv_get("snapshot_status", {}) or {}
    t = getattr(request.app.state, "snapshot_task", None)
    if st.get("state") == "running" and t and not t.done():
        return HTMLResponse('<div class="result err">A snapshot is already running.</div>')
    await db.kv_set("snapshot_status", {"state": "running", "done": 0, "total": 60, "at": now_iso()})
    request.app.state.snapshot_task = asyncio.create_task(_snapshot_run(request.app, stars, sections))
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/devices/{code}/decommission-at", response_class=HTMLResponse)
async def device_decommission_at(request: Request, code: str, factory: str = Form(""), cancel: str = Form(""),
                                 user: str = Depends(current_user)):
    """Take the device to an autofactory and decommission it there (decommission.py), or cancel that."""
    from . import decommission as dc
    db = request.app.state.db
    q = await db.kv_get(dc.KV, {}) or {}
    st = await load_state(request)
    dev = next((d for d in st["devices"] if d.get("device_code") == code), None)
    if not dev:
        return HTMLResponse('<div class="result err">Unknown device.</div>')
    if cancel:
        e = q.pop(code, None)
        await db.kv_set(dc.KV, q)
        if e:   # take the pins off again; it stays fleetless (spare) where it is
            body = {"configuration": {"remove_tags": [t for t in dev.get("tags") or [] if t.startswith(("to:", "at:"))],
                                      "add_tags": ["spare"]}}
            await call_action(request, user, "PATCH", f"/devices/{code}", body, f"{code}: decommission canceled")
        return HTMLResponse('<div class="result ok">Canceled — the device stays where it is, as a spare.</div>',
                            headers={"HX-Refresh": "true"})
    fac = next((f for f in dc.factories(st["devices"], {}, None) if f["code"] == factory), None)
    if not fac:
        return HTMLResponse('<div class="result err">Pick one of your autofactories.</div>')
    body = {"configuration": dc.retag(dev, fac, lo.to_tag, lo.at_tag)}
    out = await call_action(request, user, "PATCH", f"/devices/{code}", body, f"{code}: to autofactory {factory} to be decommissioned")
    if not out.get("ok"):
        return HTMLResponse(f'<div class="result err">Could not tag it: {html.escape(str(out.get("error")))}</div>')
    q[code] = {"factory": factory, "at": fac["location"], "asked_at": now_iso()}
    await db.kv_set(dc.KV, q)
    await request.app.state.worker.automations.log("decommission", f"{code} ({dev.get('device_type')}) queued to be "
                                                   f"decommissioned at autofactory {factory} ({fac['location']})")
    return HTMLResponse(f'<div class="result ok">Queued: it goes to {html.escape(fac["location"])} (the loadout pass carries it) '
                        'and is decommissioned there.</div>', headers={"HX-Refresh": "true"})


@router.post("/devices/{code}/cancel-travel", response_class=HTMLResponse)
async def device_cancel_travel(request: Request, code: str, user: str = Depends(current_user)):
    """Cancel a trip. Documented only for replicants (DELETE /replicants/{code}/travel), so a vessel hosting one goes
    through its replicant; any other device tries the device equivalent and shows what the game says. A canceled
    trip turns back: travel.cancelled carries origin and return_time_seconds."""
    st = await load_state(request)
    rep = next((c for c, r in st["replicants"].items() if r.get("hosted_device_code") == code), None)
    path = f"/replicants/{rep}/travel" if rep else f"/devices/{code}/travel"
    return await run_action(request, user, "DELETE", path, {}, f"cancel travel of {code}" + (f" (replicant {rep})" if rep else ""))


@router.post("/diagnostics/feedback", response_class=HTMLResponse)
async def diagnostics_feedback(request: Request, kind: str = Form("bug"), body: str = Form(""), context: str = Form(""),
                               user: str = Depends(current_user)):
    """Send feedback to the game's developers (POST /v1/feedback). Optionally adds the app version and the latest
    snapshot's time so a bug report can be matched to what was going on."""
    text = body.strip()
    if kind not in FEEDBACK_TYPES:
        return HTMLResponse('<div class="result err">Pick bug, idea or typo.</div>')
    if len(text) < 5:
        return HTMLResponse('<div class="result err">Write a few words first.</div>')
    db = request.app.state.db
    if context == "on":
        snap = await db.kv_get("snapshot_last", None) or {}
        text += f"\n\n— sent from the Replicant Space web client {appver.VERSION}" + (
            f"; latest snapshot {snap.get('captured_at')}" if snap.get("captured_at") else "")
    text = text[:2000]   # docs: body up to 2000 characters
    sent = await db.kv_get("feedback_sent", []) or []
    hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(timespec="seconds")
    if len([x for x in sent if x.get("at", "") >= hour_ago]) >= 10:
        return HTMLResponse('<div class="result err">The game takes 10 feedback messages an hour — try again later.</div>')
    try:
        resp = await request.app.state.api.post("/feedback", {"type": kind, "body": text})
    except ApiError as e:
        return HTMLResponse(f'<div class="result err">Not sent: {html.escape(e.message)}</div>')
    sent = ([{"at": now_iso(), "type": kind, "body": text[:300], "by": user,
              "status": (resp or {}).get("status") if isinstance(resp, dict) else None}] + sent)[:20]
    await db.kv_set("feedback_sent", sent)
    await request.app.state.worker.automations.log("feedback", f"{kind} sent to the developers by {user}")
    return HTMLResponse('<div class="result ok">Sent — thank you. The developers received it'
                        + (f' ({html.escape(str(resp.get("status")))})' if isinstance(resp, dict) and resp.get("status") else "")
                        + '.</div>')


@router.get("/diagnostics/snapshot.json")
async def diagnostics_download(request: Request, user: str = Depends(current_user)):
    snap = await request.app.state.db.kv_get("snapshot_last", None)
    if not snap:
        return HTMLResponse("No snapshot yet.", status_code=404)
    name = f"replicant-snapshot-{(snap.get('captured_at') or 'x')[:19].replace(':', '')}.json"
    # indented, json.dumps runs the pure-Python encoder over several MB: in a thread the loop keeps its turn meanwhile
    body = await asyncio.to_thread(json.dumps, snap, indent=1, default=str)
    return Response(body, media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/console", response_class=HTMLResponse)
async def console(request: Request, user: str = Depends(current_user)):
    st = await load_state(request)
    groups: dict[str, list] = {}
    for g, m, p_, b, d in CONSOLE_PRESETS:
        groups.setdefault(g, []).append((m, p_, b, d))
    devs = sorted(st["devices"], key=lambda d: (d.get("device_type") or "", d.get("device_code") or ""))
    locs = sorted({d.get("location") for d in devs if d.get("location")} | set(st["locations"] or {}))
    from . import repeats as rp
    return await page(request, user, "console.html", "console", preset_groups=groups, fields=CONSOLE_FIELDS,
                      repeats=await request.app.state.db.kv_get(rp.KV, []) or [], repeat_minutes=rp.DEFAULT_MINUTES,
                      reps=[(c, r.get("name") or c) for c, r in st["replicants"].items()],
                      devs=[(d.get("device_code"), d.get("device_type") or "") for d in devs if d.get("device_code")],
                      locs=locs, stars=sorted({star_of(x) for x in locs}),
                      tags=sorted({t for d in devs for t in d.get("tags") or []}))


@router.post("/console", response_class=HTMLResponse)
async def console_run(request: Request, method: str = Form("GET"), path: str = Form(...), body: str = Form(""),
                      user: str = Depends(current_user)):
    method = method.upper()
    if method not in ("GET", "POST", "PATCH", "DELETE", "PUT"):
        method = "GET"
    try:
        parsed = parse_json_field(body)
    except ValueError as e:
        return partial(request, "partials/action_result.html", ok=False, status=400, error=f"Bad JSON: {e}",
                       label="console", method=method, path=path, response=None)
    path = path.strip()
    params = None
    if "?" in path:
        path, qs = path.split("?", 1)
        params = dict(p.split("=", 1) if "=" in p else (p, "") for p in qs.split("&") if p)
    if method == "GET":
        try:
            resp = await request.app.state.api.request("GET", path, params=params)
            return partial(request, "partials/action_result.html", ok=True, status=200, error=None, label="console",
                           method=method, path=path, response=resp)
        except ApiError as e:
            return partial(request, "partials/action_result.html", ok=False, status=e.status, error=e.message,
                           label="console", method=method, path=path, response=e.body)
    return await run_action(request, user, method, path, parsed, f"console {method} {path}")


@router.post("/console/repeat", response_class=HTMLResponse)
async def console_repeat(request: Request, user: str = Depends(current_user)):
    """Send the console's request every N minutes until stopped (Console › Repeating)."""
    from . import repeats as rp
    form = await request.form()
    method = (form.get("method") or "POST").upper()
    path = (form.get("path") or "").strip()
    if method not in ("POST", "PATCH", "PUT") or not path:
        return HTMLResponse('<span class="lv-alert">Only a POST, PATCH or PUT command can repeat.</span>')
    try:
        body = parse_json_field(form.get("body"))
        every = max(1, int(form.get("every_minutes") or rp.DEFAULT_MINUTES))
    except ValueError as e:
        return HTMLResponse(f'<span class="lv-alert">{html.escape(str(e))}</span>')
    await add_repeats(request, user, [((form.get("name") or "").strip() or f"{method} {path}", method, path, body)],
                      every, form.get("stop_on_find") == "on")
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/console/repeats/{rid}/{action}", response_class=HTMLResponse)
async def console_repeat_action(request: Request, rid: str, action: str, user: str = Depends(current_user)):
    from . import repeats as rp
    db, eng = request.app.state.db, request.app.state.worker.automations
    async with eng.lock:
        items = await db.kv_get(rp.KV, []) or []
        r = next((x for x in items if x.get("id") == rid), None)
        if not r:
            return HTMLResponse('<span class="lv-alert">Repeat not found.</span>')
        if action == "toggle":
            r["enabled"] = not r.get("enabled", True)
            r["errors"] = 0
        elif action == "delete":
            items = [x for x in items if x.get("id") != rid]
        await db.kv_set(rp.KV, items)
        if action == "run":
            await eng.run_due_repeats(only=rid)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.get("/live/stream")
async def live(request: Request, user: str = Depends(current_user)):
    st = request.app.state
    q = st.hub.subscribe()
    env = templates.env

    async def gen():
        try:
            yield "retry: 5000\n\n"
            last_rate = 0.0
            while True:
                if await request.is_disconnected():
                    break
                try:
                    kind, data = await asyncio.wait_for(q.get(), timeout=10)
                except asyncio.TimeoutError:
                    kind, data = None, None
                if kind == "event":
                    yield _sse("event", env.get_template("partials/event_row.html").render(e=data, live=True))
                elif kind == "notify":
                    yield _sse("notify", env.get_template("partials/toast.html").render(n=data))
                    yield _sse("badge", env.get_template("partials/badge.html").render(n=await notify.unread_errors(st.db)))
                elif kind == "state":
                    yield _sse("state", str(data))
                now = asyncio.get_running_loop().time()
                if now - last_rate > 10:
                    last_rate = now
                    yield _sse("rate", env.get_template("partials/rate.html").render(
                        rate=st.api.rate_status(), stream_state=st.worker.stream_state))
                    yield ": keepalive\n\n"
        finally:
            st.hub.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/automations/rules-panel", response_class=HTMLResponse)
async def automations_rules_panel(request: Request, ids: str = "", open: str = "", user: str = Depends(current_user)):
    """The settings cards for some rules, for the page they work on."""
    want = [i for i in ids.split(",") if i in auto.RULES_BY_ID]
    rules = [r for r in auto.RULES if r.id in want]
    kw: dict = {}
    if "auto_survey" in want:
        st = await load_state(request)
        kw = {"vessels": [d for d in st["devices"] if "vessel" in (d.get("device_type") or "") or d.get("stow_capacity")],
              "surveyed": len(await request.app.state.db.kv_get("surveyed", {}) or {})}
    return partial(request, "partials/rule_cards.html", rules=rules, s=await request.app.state.worker.automations.settings(),
                   open=bool(open), **kw)


@router.post("/automations/rules/{rule_id}/toggle", response_class=HTMLResponse)
async def automations_rule_toggle(request: Request, rule_id: str, user: str = Depends(current_user)):
    """Switch a rule on or off from the overview, leaving its options alone."""
    if rule_id not in auto.RULES_BY_ID:
        return HTMLResponse("unknown rule", status_code=404)
    eng = request.app.state.worker.automations
    s = await eng.settings()
    cfg = s["rules"][rule_id]
    cfg["enabled"] = not cfg.get("enabled")
    await eng.save_settings(s)
    await eng.log(rule_id, f"{'enabled' if cfg['enabled'] else 'disabled'} by {user}")
    return HTMLResponse(f'<span class="{"lv-done" if cfg["enabled"] else "muted"} small">{"on" if cfg["enabled"] else "off"} · saved</span>')


@router.get("/automations", response_class=HTMLResponse)
async def automations_page(request: Request, user: str = Depends(current_user)):
    eng = request.app.state.worker.automations
    jobs = await eng.jobs()
    active = [j for j in jobs if j["status"] in ("running", "waiting")]
    finished = [j for j in reversed(jobs) if j["status"] not in ("running", "waiting")][:15]
    entries = [{**e, "sev": auto.severity(e)} for e in reversed(await request.app.state.db.kv_get("automation_log", []) or [])]
    sev_counts = Counter(e["sev"] for e in entries)
    devs = await request.app.state.db.kv_get("devices", []) or []
    cargo = {j["id"]: auto.job_cargo(j, devs) for j in active + finished}
    return await page(request, user, "automations.html", "automations", rules=auto.RULES, s=await eng.settings(),
                      rule_home=RULE_HOME, active_jobs=active, finished=finished, entries=entries, sev_counts=sev_counts,
                      cargo=cargo)


@router.get("/automations/schedule-form", response_class=HTMLResponse)
async def automations_schedule_form(request: Request, target: str = "", user: str = Depends(current_user)):
    rep = _schedule_rep((await load_state(request))["devices"], target)
    if not rep:
        return HTMLResponse('<p class="muted small">Pick a controller.</p>')
    names = await device_directives(request, rep)
    sugg = await suggestions(request, rep.get("location") if not target.startswith("kind:") else None)
    return partial(request, "partials/directive_picker.html", code=rep["device_code"], names=names,
                   fields=cmdspec.directive_fields(names[0]) if names else [], sugg=sugg,
                   sys_targets=sugg.get("system") if not target.startswith("kind:") else None,
                   uid="sched", self_code=rep["device_code"])


@router.post("/automations/schedules", response_class=HTMLResponse)
async def automations_schedule_add(request: Request, user: str = Depends(current_user)):
    form = await request.form()
    target = (form.get("target") or "").strip()
    if not target:
        return HTMLResponse('<span class="lv-alert">Pick a controller.</span>')
    try:
        body = directive_body(form)
    except ValueError as e:
        return HTMLResponse(f'<span class="lv-alert">{html.escape(str(e))}</span>')
    try:
        every = max(1, int(form.get("every_minutes") or 30))
    except ValueError:
        every = 30
    eng = request.app.state.worker.automations
    items = await eng.schedules()
    items.append({"id": f"s{int(datetime.now(timezone.utc).timestamp() * 1000)}", "name": (form.get("name") or "").strip(),
                  "target": target, "star": (form.get("star") or "").strip().upper() if target.startswith("kind:") else "",
                  "directive": body["directive"], "configuration": body.get("configuration") or {},
                  "every_minutes": every, "only_idle": form.get("only_idle") == "on", "adopt": form.get("adopt") == "on",
                  "launch": form.get("launch") == "on", "enabled": True, "last_run": None, "last_result": None})
    await eng.save_schedules(items)
    await eng.log("ami_schedules", f"schedule added by {user}: {body['directive']} on {target} every {every} min")
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/automations/schedules/{sid}/{action}", response_class=HTMLResponse)
async def automations_schedule_action(request: Request, sid: str, action: str, user: str = Depends(current_user)):
    eng = request.app.state.worker.automations
    async with eng.lock:
        items = await eng.schedules()
        sched = next((x for x in items if x.get("id") == sid), None)
        if not sched:
            return HTMLResponse('<span class="lv-alert">Schedule not found.</span>')
        if action == "toggle":
            sched["enabled"] = not sched.get("enabled", True)
        elif action == "delete":
            items = [x for x in items if x.get("id") != sid]
        elif action == "run":
            results = await eng.run_schedule(sched, manual=True)
            await eng.save_schedules(items)
            return HTMLResponse(f'<span class="small">{html.escape("; ".join(results))}</span>', headers={"HX-Trigger": "schedules-changed"})
        await eng.save_schedules(items)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/automations/rules/{rule_id}", response_class=HTMLResponse)
async def automations_rule(request: Request, rule_id: str, user: str = Depends(current_user)):
    rule = auto.RULES_BY_ID.get(rule_id)
    if not rule:
        return HTMLResponse("unknown rule", status_code=404)
    form = await request.form()
    eng = request.app.state.worker.automations
    s = await eng.settings()
    cfg = s["rules"][rule_id]
    was = cfg.get("enabled")
    cfg["enabled"] = form.get("enabled") == "on"
    for o in rule.options:
        raw = form.get(o.name)
        if o.kind == "bool":
            cfg[o.name] = raw == "on"
        elif o.kind == "int":
            try:
                cfg[o.name] = max(0, int(raw or o.default))
            except ValueError:
                pass
        elif o.kind == "float":
            try:
                cfg[o.name] = max(0.0, float(raw if raw not in (None, "") else o.default))
            except ValueError:
                pass
        elif raw is not None:
            cfg[o.name] = raw if (not o.options or raw in o.options) else o.default
    await eng.save_settings(s)
    await eng.log(rule_id, (f"{'enabled' if cfg['enabled'] else 'disabled'} by {user}" if was != cfg["enabled"]
                            else f"options changed by {user}"))
    return HTMLResponse(f'<span class="{"lv-done" if cfg["enabled"] else "muted"} small">{"on" if cfg["enabled"] else "off"} · saved</span>')


@router.post("/automations/dry-run", response_class=HTMLResponse)
async def automations_dry_run(request: Request, user: str = Depends(current_user)):
    form = await request.form()
    eng = request.app.state.worker.automations
    s = await eng.settings()
    s["dry_run"] = form.get("dry_run") == "on"
    await eng.save_settings(s)
    await eng.log("engine", f"dry run {'on' if s['dry_run'] else 'off'} ({user})")
    return HTMLResponse('<span class="small lv-done">saved</span>')


@router.post("/automations/jobs/{job_id}/cancel", response_class=HTMLResponse)
async def automations_cancel(request: Request, job_id: str, user: str = Depends(current_user)):
    await request.app.state.worker.automations.cancel(job_id)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/automations/jobs/{job_id}/confirm", response_class=HTMLResponse)
async def automations_confirm(request: Request, job_id: str, user: str = Depends(current_user)):
    """Send a trip held at the sector range."""
    await request.app.state.worker.automations.confirm_trip(job_id)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/automations/survey-now", response_class=HTMLResponse)
async def automations_survey_now(request: Request, vessel: str = Form(...), user: str = Depends(current_user)):
    """Run the auto-survey rule for a vessel where it is now (as if it had just arrived)."""
    eng = request.app.state.worker.automations
    dev = await _device(request, vessel)
    loc = dev.get("location")
    if not loc:
        return HTMLResponse('<span class="lv-alert">Unknown vessel location — wait for the next device sync.</span>')
    s = await eng.settings()
    async with eng.lock:
        before = len(await eng.jobs())
        await eng.rule_auto_survey(vessel, loc, auto.star_of(loc), await eng.stowed_in(vessel), s["rules"]["auto_survey"])
        made = len(await eng.jobs()) - before
    if s["dry_run"]:
        return HTMLResponse('<span class="muted">Dry run: see the log below for the plan.</span>', headers={"HX-Refresh": "true"})
    if not made:
        return HTMLResponse('<span class="muted">Nothing to do — no un-surveyed bodies, no scan data, or no free survey drones. See the log.</span>',
                            headers={"HX-Refresh": "true"})
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.get("/partials/automation-jobs", response_class=HTMLResponse)
async def p_automation_jobs(request: Request, user: str = Depends(current_user)):
    jobs = await request.app.state.worker.automations.jobs()
    active = [j for j in jobs if j["status"] in ("running", "waiting")]
    devs = await request.app.state.db.kv_get("devices", []) or []
    return partial(request, "partials/automation_jobs.html", active=active,
                   cargo={j["id"]: auto.job_cargo(j, devs) for j in active})


# =====================================================================================
# tree view: systems → devices → stowed devices
# =====================================================================================


@router.get("/tree", response_class=HTMLResponse)
async def tree_view(request: Request, user: str = Depends(current_user)):
    st = await load_state(request)
    db = request.app.state.db
    bps = normalize_blueprints(await db.kv_get("blueprints", []))
    carriers = {d["device_code"] for d in st["devices"] if d.get("device_code") and carrier_mod.is_carrier(d, bps)}
    systems = build_tree(st["devices"], st["replicants"], await db.kv_get("stowed_map", {}) or {}, carriers)
    lcfg = await request.app.state.worker.automations.loadout_cfg()
    phases: dict[str, str] = {}   # system -> the fleets stationed there
    for f in lo.stationed_fleets(lcfg):
        phases[f["home"]] = ", ".join(x for x in (phases.get(f["home"]), f["name"]) if x)
    return await page(request, user, "tree.html", "tree", systems=systems, phases=phases, order_commands=order_commands,
                      dangerous=DANGEROUS, synced=await db.kv_updated("devices"))


__all__ = [n for n in dir() if not n.startswith("__")]   # everything, underscore helpers too
