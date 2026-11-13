"""Loadouts, contracts (in-game events) and fleet forms.

Routes split out of web.py by page area; they register on the shared router in the original order."""
from __future__ import annotations

from .web_core import *  # noqa: F401,F403


@router.get("/loadouts")
async def loadouts_page(request: Request, user: str = Depends(current_user)):
    """Home fleets are stationed fleets now, on the Fleets page."""
    return RedirectResponse("/fleets", status_code=303)


@router.post("/loadouts/phases", response_class=HTMLResponse)
async def loadouts_save_phases(request: Request, user: str = Depends(current_user)):
    form = await request.form()
    cfg = await request.app.state.worker.automations.loadout_cfg()
    for ph in cfg["phases"]:
        pid = ph["id"]
        ph["name"] = (form.get(f"name:{pid}") or ph["name"]).strip()
        try:
            ph["order"] = int(form.get(f"order:{pid}") or ph.get("order") or 0)
        except ValueError:
            pass
        wants = {}
        for key, val in form.multi_items():
            if key.startswith(f"want:{pid}:") and str(val).strip() != "":
                try:
                    wants[key[len(f"want:{pid}:"):]] = max(0, int(val))
                except ValueError:
                    pass
        ph["wants"] = wants
        stock = {}
        for key, val in form.multi_items():
            if key.startswith(f"stock:{pid}:") and str(val).strip() != "":
                try:
                    if int(val) > 0:
                        stock[key[len(f"stock:{pid}:"):]] = int(val)
                except ValueError:
                    pass
        ph["stock"] = stock
        ph["pick_matrix"] = bool(form.get(f"pick_matrix:{pid}"))   # fleets on it fetch a spare replicant matrix
    new = (form.get("new_phase") or "").strip()
    if new:
        pid = re.sub(r"[^a-z0-9]+", "-", new.lower()).strip("-") or "phase"
        while any(p["id"] == pid for p in cfg["phases"]):
            pid += "-2"
        copy = next((p for p in cfg["phases"] if p["id"] == form.get("copy_from")), None)
        cfg["phases"].append({"id": pid, "name": new, "order": max([p.get("order", 0) for p in cfg["phases"]] or [0]) + 1,
                              "wants": dict((copy or {}).get("wants") or {}), "stock": dict((copy or {}).get("stock") or {})})
    await save_loadouts(request, cfg)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/loadouts/phases/{pid}/delete", response_class=HTMLResponse)
async def loadouts_delete_phase(request: Request, pid: str, user: str = Depends(current_user)):
    """Delete a template. Fleets that followed it keep the loadout they had, as a custom loadout."""
    eng = request.app.state.worker.automations
    cfg = await eng.loadout_cfg()
    items = cfg["fleets"]
    for f in items:
        if f.get("template") == pid:
            f["template"] = None
    cfg["phases"] = [p for p in cfg["phases"] if p["id"] != pid]
    await save_loadouts(request, cfg)
    await eng.save_fleets(items)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/loadouts/settings", response_class=HTMLResponse)
async def loadouts_settings(request: Request, user: str = Depends(current_user)):
    form = await request.form()
    cfg = await request.app.state.worker.automations.loadout_cfg()
    cfg["ignore_tags"] = sorted({t.strip().lower() for t in re.split(r"[,\s]+", form.get("ignore_tags") or "") if t.strip()}
                                - {lo.SPARE})
    for k in ("print_missing", "need_stock", "carriers_return", "gather_spares"):
        cfg["settings"][k] = form.get(k) == "on"
    cfg["settings"]["spare_depot"] = (form.get("spare_depot") or "").strip().upper()
    try:
        cfg["settings"]["max_cruise_au"] = max(0.0, float(form.get("max_cruise_au") or lo.DEFAULT_SETTINGS["max_cruise_au"]))
    except ValueError:
        pass
    try:   # blank = the default; 0 = no limit
        v = form.get("max_supply_ly")
        cfg["settings"]["max_supply_ly"] = max(0.0, float(v if v not in (None, "") else lo.DEFAULT_SETTINGS["max_supply_ly"]))
    except ValueError:
        pass
    await save_loadouts(request, cfg)
    return HTMLResponse('<span class="lv-done small">Saved.</span>', headers={"HX-Refresh": "true"})


@router.post("/loadouts/apply", response_class=HTMLResponse)
async def loadouts_apply(request: Request, star: str = Form(""), fleet: str = Form(""), user: str = Depends(current_user)):
    eng = request.app.state.worker.automations
    only = {x for x in (star, fleet) if x} or None
    async with eng.lock:
        lines = await eng.apply_loadouts(only, manual=True)
    body = "".join(f"<li>{html.escape(line)}</li>" for line in lines) or \
        "<li>Nothing to do: every stationed fleet matches its loadout.</li>"
    what = f" to {html.escape(fleet or star)}" if only else ""
    return HTMLResponse(f'<div class="result ok"><strong>Applied{what}</strong><ul class="small">{body}</ul>'
                        f'<div class="small muted">Follow the jobs below or on the Automations page.</div></div>',
                        headers={"HX-Trigger": "loadouts-changed"})


@router.post("/loadouts/orders/clear", response_class=HTMLResponse)
async def loadouts_clear_orders(request: Request, fleet: str = Form(""), user: str = Depends(current_user)):
    """Forget pending print orders (all, or one fleet's) so they stop counting as incoming."""
    db = request.app.state.db
    orders = await db.kv_get("loadout_orders", []) or []
    await db.kv_set("loadout_orders", [o for o in orders if fleet and o.get("fleet") != fleet])
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.get("/game-events", response_class=HTMLResponse)
async def game_events_page(request: Request, user: str = Depends(current_user)):
    return await page(request, user, "game_events.html", "gameevents", **await game_events_ctx(request))


@router.post("/game-events/{des}/bring", response_class=HTMLResponse)
async def game_event_bring(request: Request, des: str, replicant: str = Form(...), user: str = Depends(current_user)):
    e, _ = await _event(request, des)
    if not e:
        return HTMLResponse('<div class="result err">That event is no longer open.</div>')
    return await run_action(request, user, "POST", f"/replicants/{replicant}/travel", {"destination": e["location"]},
                            f"{replicant} → {e['location']} for {e['title']}")


@router.post("/game-events/{des}/deliver", response_class=HTMLResponse)
async def game_event_deliver(request: Request, des: str, user: str = Depends(current_user)):
    e, _ = await _event(request, des)
    if not e:
        return HTMLResponse('<div class="result err">That event is no longer open.</div>')
    plan = e["plan"]
    ctrl = plan["controller"]
    if not plan["legs"]:
        msg = "Nothing to move: everything needed is already at the location." if not plan["short"] else \
            f"Not enough in {e['star']}: missing {', '.join(f'{int(q)} {r}' for r, q in plan['missing'].items())}."
        return HTMLResponse(f'<div class="result {"ok" if not plan["short"] else "err"}">{html.escape(msg)}</div>')
    if not ctrl:
        return HTMLResponse(f'<div class="result err">No in-system AMI transport controller in {html.escape(e["star"])} to deliver with.</div>')
    code = ctrl["device_code"]
    steps = []
    for leg in plan["legs"][:3]:
        st = auto.step(f"{code}: deliver {', '.join(f'{q} {r}' for r, q in leg['requirement'].items())} {leg['collect']} → {leg['deliver']}",
                       f"/devices/{code}", {"command": "set_directive", "directive": "delivery",
                                            "configuration": {"route": {"collect": leg["collect"], "deliver": leg["deliver"]},
                                                              "requirement": leg["requirement"]}},
                       wait=["directive.completed"], timeout=6 * 3600)
        steps += [st, auto.step(f"{code}: launch", f"/devices/{code}", {"command": "launch"})]
    # only wait between legs; the last one finishes on its own
    if steps:
        steps[-2]["wait"] = []
    return await start_chain(request, user, f"event {e['title']}: deliver to {e['location']}", steps[0], steps[1:], code)


@router.post("/game-events/{des}/fulfil", response_class=HTMLResponse)
async def game_event_fulfil(request: Request, des: str, replicant: str = Form(""), user: str = Depends(current_user)):
    e, ctx = await _event(request, des)
    if not e:
        return HTMLResponse('<div class="result err">That event is no longer open.</div>')
    tmpl = (ctx["settings"].get("fulfil") or "").strip() or gev.DEFAULT_FULFIL
    rep = replicant or (e["prog"]["present"][0]["code"] if e["prog"]["present"] else "")
    best = (e["prog"].get("best") or {}).get("name") or "default"
    filled = (tmpl.replace("{designation}", des).replace("{replicant}", rep).replace("{location}", e["location"])
              .replace("{criteria}", best))
    method, _, rest = filled.partition(" ")
    path, _, body = rest.partition(" ")
    try:
        payload = json.loads(body) if body.strip() else None
    except ValueError:
        return HTMLResponse('<div class="result err">The fulfill body isn\'t valid JSON.</div>')
    return await run_action(request, user, method.upper(), path, payload, f"fulfill {e['title']} ({des})")


@router.post("/game-events/approve", response_class=HTMLResponse)
async def game_event_approve(request: Request, key: str = Form(...), on: str = Form(""), user: str = Depends(current_user)):
    """Approve (or withdraw approval for) one contract, pre-approve every contract from a location, or switch on
    auto-approval at locations with your beacon: auto-fulfil only takes approved or pre-approved ones."""
    db = request.app.state.db
    approvals = await gev.approvals_of(db)
    if on:
        approvals[key] = {"at": now_iso(), "by": user}
    else:
        approvals.pop(key, None)
    await db.kv_set(gev.APPROVALS_KV, approvals)
    return HTMLResponse("")   # saved in place: no reload (a reload let the browser restore ticks by position)


@router.post("/game-events/{des}/option", response_class=HTMLResponse)
async def game_event_option(request: Request, des: str, option: str = Form(""), user: str = Depends(current_user)):
    """Pick which option a contract with several is fulfilled with ("" = whichever is ready first)."""
    db = request.app.state.db
    choices = await db.kv_get(gev.CHOICE_KV, {}) or {}
    if option:
        choices[des] = option
    else:
        choices.pop(des, None)
    await db.kv_set(gev.CHOICE_KV, choices)
    await _supply_now(request)   # devices it asks for follow the option
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/game-events/{des}/supply", response_class=HTMLResponse)
async def game_event_supply(request: Request, des: str, action: str = Form("start"), user: str = Depends(current_user)):
    """Deliver devices (start) or stop: leftover devices tagged for it become spare again on the next pass."""
    db = request.app.state.db
    e, _ = await _event(request, des)
    if not e and action == "start":
        return HTMLResponse('<div class="result err">That event is no longer open.</div>')
    supply = await db.kv_get("contract_supply", {}) or {}
    entry = supply.setdefault(des, {})
    entry["active"] = action == "start"
    if action != "start":
        entry["print_ok"] = {}   # stopping withdraws the authorization for prints not yet ordered
    await db.kv_set("contract_supply", supply)
    await _supply_now(request)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/game-events/{des}/authorize-prints", response_class=HTMLResponse)
async def game_event_authorize_prints(request: Request, des: str, user: str = Depends(current_user)):
    """Allow printing what no spare covers (as of the last check): print_ok = ordered so far + still short."""
    db = request.app.state.db
    e, _ = await _event(request, des)
    if not e:
        return HTMLResponse('<div class="result err">That event is no longer open.</div>')
    supply = await db.kv_get("contract_supply", {}) or {}
    entry = supply.setdefault(des, {})
    ok = entry.setdefault("print_ok", {})
    for r in entry.get("rows") or []:
        if r.get("short"):
            ok[r["type"]] = max(int(ok.get(r["type"]) or 0), int(r.get("ordered") or 0) + int(r["short"]))
    entry["active"] = True
    await db.kv_set("contract_supply", supply)
    await _supply_now(request)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/game-events/{des}/restart-prints", response_class=HTMLResponse)
async def game_event_restart_prints(request: Request, des: str, user: str = Depends(current_user)):
    """Prints that never came out (dropped from a queue, a factory gone): orders no factory still holds are forgotten,
    then what's short is authorized and printed again."""
    from . import contractsupply as cs
    from . import printqueue as pq
    db = request.app.state.db
    e, _ = await _event(request, des)
    if not e:
        return HTMLResponse('<div class="result err">That event is no longer open.</div>')
    tag = cs.contract_tag(des)
    held = Counter()   # this contract's prints still in a factory: queued or printing
    for f in await db.kv_get("devices", []) or []:
        its = list(pq.items(f)) if pq.has_queue(f) else []
        if isinstance(f.get("printing"), dict):
            its.append(f["printing"])
        for it in its:
            if tag in (it.get("tags") or []):
                held[it.get("device_type")] += int(it.get("quantity") or 1)
    supply = await db.kv_get("contract_supply", {}) or {}
    entry = supply.setdefault(des, {})
    kept = []
    for o in entry.get("orders") or []:
        n = min(int(o.get("n") or 1), held[o.get("type")])
        if n:
            kept.append({**o, "n": n})
            held[o.get("type")] -= n
    entry["orders"], entry["active"] = kept, True
    await db.kv_set("contract_supply", supply)
    await _supply_now(request)   # counts again without the lost prints: they show as short
    supply = await db.kv_get("contract_supply", {}) or {}
    entry = supply.setdefault(des, {})
    ok = entry.setdefault("print_ok", {})
    for r in entry.get("rows") or []:
        if r.get("short"):
            ok[r["type"]] = int(r.get("ordered") or 0) + int(r["short"])
    await db.kv_set("contract_supply", supply)
    await _supply_now(request)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/game-events/settings", response_class=HTMLResponse)
async def game_event_settings(request: Request, fulfil: str = Form(""), user: str = Depends(current_user)):
    s = await request.app.state.db.kv_get("event_settings", {}) or {}
    s["fulfil"] = fulfil.strip()
    await request.app.state.db.kv_set("event_settings", s)
    return HTMLResponse('<span class="lv-done small">Saved.</span>')


@router.post("/wallpaper-settings", response_class=HTMLResponse)
async def wallpaper_settings(request: Request, action: str = Form(...), label: str = Form(""), key_id: str = Form(""),
                             user: str = Depends(current_user)):
    db = request.app.state.db
    if action not in ("enable", "disable", "create", "revoke"):
        return HTMLResponse('<div class="result err">Unknown action.</div>', status_code=400)
    async with _wp_lock:
        st = wp.normalize(await db.kv_get(wp.KV, {}))
        new = None
        if action == "enable":
            st["enabled"] = True
        elif action == "disable":
            st["enabled"] = False
        elif action == "create":
            key, rec = wp.new_key(label)
            st["keys"].append(rec)
            st["enabled"] = True
            new = wp.link(_origin(request), key)
        elif action == "revoke":
            st["keys"] = [k for k in st["keys"] if k["id"] != key_id]
        await db.kv_set(wp.KV, st)
    await request.app.state.worker.automations.log("wallpaper", f"wallpaper {action} by {user}"
                                                   + (f" ({label or 'wallpaper'})" if action == "create" else ""))
    return partial(request, "partials/wallpaper_card.html", **await wallpaper_ctx(request), new_link=new)


@router.get("/wallpaper/{slug}/", response_class=HTMLResponse)
async def wallpaper_page(request: Request, slug: str):
    """The wallpaper page itself: no data in it (that needs the key), so it's served to anyone while enabled."""
    if not wp.slug_ok(slug) or not wp.normalize(await request.app.state.db.kv_get(wp.KV, {}))["enabled"]:
        return _wp_off()
    return templates.TemplateResponse(request, "wallpaper.html", {"request": request, "base": f"/wallpaper/{slug}"},
                                      headers={"Cache-Control": "no-cache"})   # always the current version's script URLs


@router.get("/wallpaper/{slug}/static/{path:path}")
async def wallpaper_static(request: Request, slug: str, path: str):
    if not wp.slug_ok(slug) or path not in wp.STATIC:
        return _wp_off()
    from fastapi.responses import FileResponse
    return FileResponse(HERE / "static" / path, headers={"Cache-Control": "public, max-age=3600"})


@router.get("/wallpaper/{slug}/api/map.json")
async def wallpaper_map(request: Request, slug: str):
    _, bad = await _wp_auth(request, slug)
    return bad or await _wp_cached(request, "map", lambda: map_payload(request, request.query_params.get("part") or "all"))


@router.get("/wallpaper/{slug}/api/hud.json")
async def wallpaper_hud(request: Request, slug: str):
    """The wallpaper's dashboard panel: stockpiles with their 48-hour trend, what your devices are doing, your fleets'
    missions and your supply lines."""
    _, bad = await _wp_auth(request, slug)
    return bad or await _wp_cached(request, "hud", lambda: _hud_payload(request))


@router.get("/wallpaper/{slug}/api/systems.json")
async def wallpaper_systems(request: Request, slug: str):
    """Systems to cycle through: where your devices or replicants are, busiest first, scanned ones only."""
    _, bad = await _wp_auth(request, slug)
    if bad:
        return bad
    st = await load_state(request)
    count = Counter(star_of(d.get("location")) for d in st["devices"] if d.get("location"))
    for r in st["replicants"].values():
        count[star_of(r.get("location") or r.get("current_location"))] += 0
    from . import season
    reset = await season.reset_at(request.app.state.db)
    scanned = {r["star"] for r in await request.app.state.db.fetchall("SELECT star, updated_at FROM systems")
               if not season.stale(r["updated_at"], reset)}
    systems = [{"star": s, "devices": n} for s, n in sorted(count.items(), key=lambda kv: (-kv[1], kv[0])) if s and s in scanned]
    return JSONResponse({"systems": systems})


@router.get("/wallpaper/{slug}/api/system/{star}", response_class=HTMLResponse)
async def wallpaper_system(request: Request, slug: str, star: str):
    _, bad = await _wp_auth(request, slug)
    if bad:
        return bad
    star = star.upper()
    row = await request.app.state.db.fetchone("SELECT data FROM systems WHERE star=?", (star,))
    if not row:
        return HTMLResponse(f'<p class="wp-note">No scan of {html.escape(star)} stored yet.</p>', status_code=404)
    scan = json.loads(row["data"])
    view = await system_view_model(request, star, scan, await load_state(request))
    return partial(request, "partials/system_svg.html", star=star, scan=scan, view=view)


__all__ = [n for n in dir() if not n.startswith("__")]   # everything, underscore helpers too
