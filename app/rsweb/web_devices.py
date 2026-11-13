"""Device list, device pages, the autofactory print queue and replicants.

Routes split out of web.py by page area; they register on the shared router in the original order."""
from __future__ import annotations

from .web_core import *  # noqa: F401,F403


@router.get("/fleet", response_class=HTMLResponse)
async def fleet(request: Request, q: str = "", type: str = "", status: str = "", star: str = "", tag: str = "",
                owner: str = "", user: str = Depends(current_user)):
    st = await load_state(request)
    shown = await fleet_shown(request, st)
    devs = filter_devices(shown, q, type, status, star, tag, owner)
    # the page holds every device and filters in the browser (fleet.html: no round trip, no redraw of a thousand rows);
    # the rows outside the URL's filter start hidden. The table alone (hx-target fleet-table) holds just the matches.
    part = request.headers.get("hx-request") and request.headers.get("hx-target") == "fleet-table"
    rows = devs if part else filter_devices(shown, "", "", "", "", "", "")
    # owning replicants, by name: e.g. to reassign the devices of one gone into a simulation (change_owner)
    rnames = {(r.get("replicant_code") or k): (r.get("name") or k) for k, r in (st.get("replicants") or {}).items() if isinstance(r, dict)}
    owners = sorted(({c: rnames.get(c, c) for c in {d.get("replicant_code") for d in st["devices"]} if c}).items(),
                    key=lambda x: (str(x[1]).lower(), x[0]))
    types = sorted({d.get("device_type") or "?" for d in st["devices"]})
    stars = sorted({star_of(d.get("location")) for d in shown})
    tags = tag_options(st["devices"], tag)
    from .web_bulk import bulk_commands
    here = dict(bulk_commands(devs))   # every command any device takes, counted over the shown ones
    kw = dict(devices=devs, rows=rows, match={d.get("device_code") for d in devs}, types=types, stars=stars, tags=tags,
              owners=owners, bulk_cmds=[(c, here.get(c, 0)) for c, _ in bulk_commands(rows)], dangerous=DANGEROUS,
              f={"q": q, "type": type, "status": status, "star": star, "tag": tag, "owner": owner}, **{k: v for k, v in st.items() if k != "devices"})
    if part:
        return partial(request, "partials/fleet_table.html", **kw)
    return await page(request, user, "fleet.html", "fleet", **kw)


@router.get("/fleet/match")
async def fleet_match(request: Request, q: str = "", user: str = Depends(current_user)):
    """Codes of the devices whose data holds the search text: the device list's search box, filtered in the browser."""
    shown = await fleet_shown(request, await load_state(request))
    return JSONResponse([d.get("device_code") for d in filter_devices(shown, q, "", "", "", "", "")])


@router.post("/fleet/refresh", response_class=HTMLResponse)
async def fleet_refresh(request: Request, user: str = Depends(current_user)):
    try:
        await request.app.state.worker.sync_devices()
        msg = "Device list refreshed."
    except ApiError as e:
        msg = f"Refresh failed: {e.message}"
    return HTMLResponse(f'<span class="muted">{html.escape(msg)}</span>', headers={"HX-Trigger": "fleet-refreshed"})


@router.get("/devices/{code}", response_class=HTMLResponse)
async def device_detail(request: Request, code: str, user: str = Depends(current_user)):
    api = request.app.state.api
    err = None
    dev, logs = {}, []
    live = bool(request.headers.get("x-live"))   # a live redraw: the cached copy, no game calls (the log card isn't live)
    if not live:
        try:
            dev = await api.get(f"/devices/{code}")
        except ApiError as e:
            err = e.message
        try:
            logs = ((await api.get(f"/devices/{code}/logs", latest="true", limit=20)) or {}).get("events") or []
        except ApiError:
            pass
    if live or err:
        dev = next((d for d in (await load_state(request))["devices"] if d.get("device_code") == code), {})
    events = [row_event(r) for r in await request.app.state.db.fetchall(
        "SELECT * FROM events WHERE device_code=? ORDER BY seq DESC LIMIT 50", (code,))]
    st = await load_state(request)
    same_loc = [d for d in st["devices"] if d.get("location") == dev.get("location") and d.get("device_code") != code]
    carrier = await carrier_context(request, dev) if dev else None
    pq = await print_queue_ctx(request, code, dev) if dev and printqueue.has_queue(dev) else None
    from . import fleets as _fl
    trip = _fl.in_flight(dev) if dev else None
    timers = device_timers(dev, await request.app.state.db.fetchall(
        "SELECT * FROM timers WHERE device_code=?", (code,))) if dev else []
    host_rep = next((c for c, r in st["replicants"].items() if r.get("hosted_device_code") == code), None)
    eng = request.app.state.worker.automations
    doing = _fl.device_doing(code, dev, await eng.fleets(), await eng.jobs()) if dev else None
    from . import decommission as _dc
    dq = (await request.app.state.db.kv_get(_dc.KV, {}) or {}).get(code)
    cat = await request.app.state.db.kv_get("stars", {}) or {}
    dfac = [] if dq or "decommission" not in (dev.get("available_commands") or []) or "autofactory" in (dev.get("device_type") or "") \
        else _dc.factories(st["devices"], {s.get("designation"): s for s in cat.get("stars") or [] if isinstance(s, dict)},
                           dev.get("location") or (next((x for x in st["devices"] if x.get("device_code") in
                                                         (dev.get("stowed_in_device_code"), dev.get("attached_to_device_code"))), {})).get("location"))
    return await page(request, user, "device.html", "fleet", carrier=carrier, pq=pq, dev=dev, code=code, err=err, logs=logs, events=events,
                      trip=trip, timers=timers, doing=doing, host_rep=host_rep, decom_queued=dq, decom_factories=dfac,
                      repeats=[r for r in await request.app.state.db.kv_get("console_repeats", []) or []
                               if r.get("path", "").rstrip("/").upper() == f"/DEVICES/{code.upper()}"],
                      same_loc=same_loc, commands=order_commands(with_attach(dev, same_loc)),
                      dangerous=DANGEROUS)


@router.post("/devices/{code}/print-tags", response_class=HTMLResponse)
async def print_tags(request: Request, code: str, device_type: str = Form(...), original: str = Form(""),
                     tags: str = Form(""), user: str = Depends(current_user)):
    """Change the tags a print (printing now or waiting in the queue) will carry. The game can't edit a queued print,
    so the app keeps the change and applies it to the device the moment it's printed (engine: print.completed). The
    loadout order for it follows at once: a print moved to another fleet or system stops counting for the old one."""
    from . import loadouts as lo
    db = request.app.state.db
    old, new = printqueue.parse_tags(original), printqueue.parse_tags(tags)
    edits = await db.kv_get(printqueue.EDITS_KV, {}) or {}
    lst = [e for e in edits.get(code) or [] if not (e.get("device_type") == device_type and set(e.get("from") or []) == set(old))]
    if set(new) != set(old):
        lst.append({"device_type": device_type, "from": old, "to": new, "at": now_iso(), "by": user})
    edits[code] = lst
    if not lst:
        edits.pop(code, None)
    await db.kv_set(printqueue.EDITS_KV, edits)
    # the loadout order behind this print: re-aim it at the new to:/fleet: tags, or drop it if they're gone
    orders = await db.kv_get("loadout_orders", []) or []
    o = next((o for o in orders if o.get("factory") == code and o.get("device_type") == device_type and not o.get("device_code")
              and (not old or lo.to_tag(o.get("star") or "") in old)), None)
    if o:
        to = next((t[3:].upper() for t in new if t.startswith("to:")), None)
        fleet = next((t[6:] for t in new if t.startswith("fleet:")), None)
        if to:
            o["star"], o["fleet"] = to, fleet
        else:
            orders.remove(o)
        await db.kv_set("loadout_orders", orders)
    dev = await fetch_device(request, code)
    note = (f"{device_type}: will be tagged {', '.join(new) or '(no tags)'} when it's printed" if set(new) != set(old)
            else f"{device_type}: back to its original tags")
    return partial(request, "partials/print_queue.html", **await print_queue_ctx(request, code, dev,
                                                                                 outcome={"ok": True, "label": note, "method": "",
                                                                                          "path": "kept by the app", "status": "saved"}))


@router.get("/print-queue/locations", response_class=HTMLResponse)
async def print_queue_locations(request: Request, dest_star: str = "", user: str = Depends(current_user)):
    """<option>s for the 'deliver to' location list of a system: planets, moons, belts, Lagrange points, entry point, outer."""
    star = dest_star.strip().upper().split("-")[0]   # a typed location code (FALQUORYX-3-2) lists its system's spots
    if not star:
        return HTMLResponse('<option value="">— system first —</option>')
    t = await system_targets(request.app.state.db, star)
    keep = ("planet", "moon", "belt", "lagrange", "outer", "object")
    opts = ['<option value="">anywhere in ' + html.escape(star) + ' (entry point)</option>']
    for x in t["targets"]:
        if x["category"] in keep or x["category"].startswith(("planet", "moon")):
            text = f'{x["code"]} · {CATEGORY_LABEL.get(x["category"], x["category"])}' + (f' — {x["note"]}' if x.get("note") else "")
            opts.append(f'<option value="{html.escape(x["code"])}">{html.escape(text)}</option>')
    return HTMLResponse("".join(opts))


@router.get("/print-queue/group", response_class=HTMLResponse)
async def print_queue_group(request: Request, codes: str = "", user: str = Depends(current_user)):
    """Blueprints › Print queues: one card for the autofactories of one type at one spot (owner 2026-11-03). They share
    the app's queue (vqueue.py), so it's listed once, and the time is for all of them printing it."""
    return await render_print_queue_group(request, codes)


@router.post("/print-queue/group/remove", response_class=HTMLResponse)
async def print_queue_group_remove(request: Request, codes: str = Form(""), factory: str = Form(...),
                                   order: int = Form(...), user: str = Depends(current_user)):
    """Blueprints › Print queues: take a print off the shared app queue (vqueue.py), the panel's Remove for kept prints."""
    code = factory.strip().upper()
    dev = await fetch_device(request, code)
    it = next((x for x in printqueue.items(dev) if x["extra"].get("virtual") and x["extra"].get("order") == order), None)
    outcome = await call_action(request, user, "POST", f"/devices/{code}", {"command": "dequeue_print", "order": order},
                                f"remove {it['device_type'] if it else 'a print'} from {code}'s app queue")
    if outcome.get("ok") and it:
        db = request.app.state.db
        orders = await db.kv_get("loadout_orders", []) or []
        left = lo.forget_queued(orders, code, it["device_type"], max(1, int(it.get("quantity") or 1)))
        if len(left) != len(orders):
            await db.kv_set("loadout_orders", left)
    return await render_print_queue_group(request, codes, None if outcome.get("ok") else outcome.get("error"))


@router.get("/devices/{code}/print-queue", response_class=HTMLResponse)
async def print_queue_panel(request: Request, code: str, compact: int = 0, user: str = Depends(current_user)):
    dev = await fetch_device(request, code)
    return partial(request, "partials/print_queue.html", **await print_queue_ctx(request, code, dev, compact=bool(compact)))


@router.post("/devices/{code}/print-queue", response_class=HTMLResponse)
async def print_queue_action(request: Request, code: str, action: str = Form(...), index: int = Form(0),
                             device_type: str = Form(""), quantity: int = Form(1), dest_star: str = Form(""),
                             dest_loc: str = Form(""), dest_text: str = Form(""), flatpack: str = Form(""),
                             order: int | None = Form(None), user: str = Depends(current_user)):
    dev = await fetch_device(request, code)
    cmds = dev.get("available_commands") or []
    if action in ("top", "up", "down"):
        from . import stock
        q = printqueue.items(dev)
        to = {"top": 0, "up": index - 1, "down": index + 1}[action]
        if not 0 <= index < len(q) or not 0 <= to < len(q):
            return HTMLResponse('<div class="result err">That print isn\'t in the queue any more.</div>', status_code=400)
        eng = request.app.state.worker.automations
        async with eng.lock:
            ok, msg = await eng.requeue(code, stock.moved(q, index, to), f"#{index + 1} {action} by hand")
        dev = await fetch_device(request, code)
        return partial(request, "partials/print_queue.html", **await print_queue_ctx(
            request, code, dev, {"label": f"move #{index + 1} {q[index]['device_type']} to #{to + 1}", "ok": ok,
                                 "method": "POST", "path": f"/devices/{code}", "status": 200 if ok else 409,
                                 "error": None if ok else msg, "response": msg if ok else None}))
    if action == "remove":
        # `index` is the game's 0-based queue position (the panel shows #1 for index 0)
        body, label = {"command": "dequeue_print", "index": index}, f"remove #{index + 1} from {code}'s queue"
        it = next((x for x in printqueue.items(dev) if x["api_index"] == index), None)
        if order is not None and not (it and it["extra"].get("order") == order):
            it = next((x for x in printqueue.items(dev) if x["extra"].get("virtual") and x["extra"].get("order") == order), None)
        if it and it["extra"].get("virtual") and it["extra"].get("order") is not None:
            # kept in the app's queue (vqueue.py): taken off there by its order, not by a position that may have moved
            body = {"command": "dequeue_print", "order": it["extra"]["order"]}
            label = f"remove #{it['index']} {it['device_type']} from {code}'s app queue"
            index = it["api_index"]
    elif action == "clear":
        body, label = {"command": "clear_queue"}, f"clear {code}'s print queue"
    elif action == "cancel":
        cmd = next((c for c in ("deactivate", "cancel_print", "cancel") if c in cmds), "deactivate")
        body, label = {"command": cmd}, f"cancel {code}'s current print"
        if str(dev.get("status") or "").startswith("waiting_for_resources") and printqueue.items(dev):
            # a waiting print is still first in the queue: deactivate answers "Nothing to deactivate" (live 2026-11-02)
            body, label = {"command": "dequeue_print", "index": 0}, f"remove {code}'s waiting print (#1)"
    elif action == "add" and device_type:
        q = max(1, quantity)
        body, label = {"command": "enqueue_print", "device_type": device_type, "quantity": q}, f"enqueue {q}× {device_type} on {code}"
        from .modular import modular_type
        if flatpack == "on" and modular_type(device_type, {b["device_type"]: b for b in normalize_blueprints(
                await request.app.state.db.kv_get("blueprints", []))}):
            body["flatpack"], label = True, label + " (compacted)"
        loc = (dest_text or dest_loc or "").strip().upper()
        star = (dest_star or "").strip().upper() or star_of(loc)
        if (loc and not lo.is_place(loc)) or (star and not lo.is_place(star)):
            dev2 = await fetch_device(request, code)
            return partial(request, "partials/print_queue.html", **await print_queue_ctx(
                request, code, dev2, {"label": label, "ok": False, "method": "POST", "path": f"/devices/{code}", "status": 400,
                                      "error": f"{loc or star} isn't a location code (e.g. FALQUORYX-BELT-1 or ITHVALAI-3-L4). "
                                               "To put a print in a fleet, use Print what's missing on the fleet.",
                                      "response": None}))
        if loc and star_of(loc) != star:
            dev2 = await fetch_device(request, code)
            return partial(request, "partials/print_queue.html", **await print_queue_ctx(
                request, code, dev2, {"label": label, "ok": False, "error": f"{loc} isn't in {star}", "method": "POST",
                                      "path": f"/devices/{code}", "status": 400, "response": None}))
        here = star_of(dev.get("location"))
        tags: list[str] = []
        if star and star != here:
            tags.append(lo.to_tag(star))          # the loadout pass delivers it to that system …
        if loc:
            tags.append(lo.at_tag(loc))           # … and on to the exact spot (pinned there)
            if not star or star == here:
                body["oncomplete"] = {"command": "travel", "destination": loc}   # same system: the game sends it on completion
        if tags:
            body["tags"] = tags
        if star or loc:
            label += f" → {loc or star}"
        if star and star != here:
            orders = await request.app.state.db.kv_get("loadout_orders", []) or []
            orders += [{"star": star, "device_type": device_type, "factory": code, "at": now_iso(), "manual": True,
                        **({"location": loc} if loc else {})} for _ in range(q)]
            await request.app.state.db.kv_set("loadout_orders", orders)
    else:
        return HTMLResponse('<div class="result err">Unknown queue action.</div>', status_code=400)
    outcome = await call_action(request, user, "POST", f"/devices/{code}", body, label)
    if outcome.get("ok") and action in ("remove", "clear", "cancel"):
        # prints taken off the queue stop counting as on their way to a fleet (or the fleet never gets them)
        db = request.app.state.db
        orders = await db.kv_get("loadout_orders", []) or []
        if action == "clear":
            left = lo.forget_queued(orders, code)
        elif action == "remove":
            it = next((x for x in printqueue.items(dev) if x["api_index"] == index), None)
            left = lo.forget_queued(orders, code, it["device_type"], max(1, int(it.get("quantity") or 1))) if it else orders
        else:
            have, _ = lo.queued_counts(dev)
            cur = next(iter(have - Counter(x["device_type"] for x in printqueue.items(dev))), None)
            left = lo.forget_queued(orders, code, cur, 1) if cur else orders
        if len(left) != len(orders):
            await db.kv_set("loadout_orders", left)
    dev = await fetch_device(request, code)  # re-read so the panel shows the game's view
    return partial(request, "partials/print_queue.html", **await print_queue_ctx(request, code, dev, outcome))


@router.get("/devices/{code}/command-form", response_class=HTMLResponse)
async def device_command_form(request: Request, code: str, command: str = "", rid: str = "", user: str = Depends(current_user)):
    dev = await _device(request, code)
    if not command:
        return HTMLResponse('<p class="muted small">Pick a command to see its fields.</p>')
    if command == "set_directive":
        names = await device_directives(request, dev)
        sugg = await suggestions(request, dev.get("location"))
        return partial(request, "partials/directive_picker.html", code=code, names=names,
                       fields=cmdspec.directive_fields(names[0]) if names else [],
                       sugg=sugg, sys_targets=sugg.get("system"), uid=f"d-{code}", self_code=code, rid=rid)
    if command == "prospect":   # observatory: pick an aim instead of typing a vector
        from .observatory import AIMS
        cat = await request.app.state.db.kv_get("stars", {}) or {}
        return partial(request, "partials/prospect_form.html", code=code, aims=AIMS, here=star_of(dev.get("location")),
                       stars=sorted(s.get("designation") for s in cat.get("stars") or [] if isinstance(s, dict) and s.get("designation")),
                       description=cmdspec.DESCRIPTIONS.get(command, ""), uid=f"c-{code}")
    fields = cmdspec.COMMANDS.get(command)
    chain = await chain_context(request, code) if command in MOVE_COMMANDS else {}
    if command == "collect_resources":
        chain["cargo"] = await cargo_context(request.app.state.db, request.app.state.api, dev,
                                             normalize_blueprints(await request.app.state.db.kv_get("blueprints", [])))
    here = dev.get("location")
    if not here:   # stowed or attached (a matrix in a vessel has no location): where its carrier is
        devs = (await load_state(request))["devices"]
        here = where_is(dev, {d.get("device_code"): d for d in devs})
    sugg = await suggestions(request, here)
    return partial(request, "partials/command_form.html", code=code, command=command, fields=fields or [],
                   description=cmdspec.DESCRIPTIONS.get(command, ""),
                   known=fields is not None, sugg=sugg, sys_targets=sugg.get("system"),
                   uid=f"c-{code}", self_code=code, directives=None, rid=rid, **chain)


@router.post("/devices/{code}/command", response_class=HTMLResponse)
async def device_command(request: Request, code: str, user: str = Depends(current_user)):
    form = await request.form()
    command = (form.get("command") or "").strip()

    def bad(msg: str) -> HTMLResponse:
        return partial(request, "partials/action_result.html", ok=False, status=400, error=msg,
                       label=command or "command", method="POST", path=f"/devices/{code}", response=None)

    if not command:
        return bad("Pick a command")
    if command == "set_directive":
        try:
            body = directive_body(form)
        except ValueError as e:
            return bad(str(e))
        return await run_action(request, user, "POST", f"/devices/{code}", body, f"{code} directive {body['directive']}")
    try:
        body = cmdspec.parse_fields(cmdspec.COMMANDS.get(command, []), form)
        extra = parse_json_field(form.get("args")) or {}
        if not isinstance(extra, dict):
            return bad("Extra arguments must be a JSON object")
    except cmdspec.FormError as e:
        return bad(str(e))
    except ValueError as e:
        return bad(f"Bad JSON: {e}")
    if command == "prospect" and form.get("aim") and form.get("aim") != "custom":
        from .observatory import aim_vector
        dev = await _device(request, code)
        cat = await request.app.state.db.kv_get("stars", {}) or {}
        pos = {s.get("designation"): s.get("position") for s in cat.get("stars") or [] if isinstance(s, dict)}
        try:
            vec = aim_vector(form.get("aim"), pos.get(star_of(dev.get("location"))),
                             pos.get((form.get("aim_star") or "").strip().upper()))
        except ValueError as e:
            return bad(str(e))
        body.pop("direction", None)
        if vec is not None:
            body["direction"] = vec
    if form.get("repeat"):
        if command in ("enqueue_print", "message", "set_directive"):
            return bad(f"{command} can't repeat")
        try:
            every = max(1, int(form.get("every_minutes") or 10))
        except ValueError:
            return bad("Repeat every: give a number of minutes")
        await add_repeats(request, user, [(f"{command} {code}", "POST", f"/devices/{code}", {"command": command, **body, **extra})],
                          every, form.get("stop_on_find") == "on")
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    if command == "enqueue_print":   # a replicant's vessel prints with the replicant's own print, one at a time
        st = await load_state(request)
        rep = next((c for c, r in st["replicants"].items() if r.get("hosted_device_code") == code), None)
        if rep:
            dtype = body.get("device_type") or extra.get("device_type")
            if not dtype:
                return bad("Pick a device type to print")
            n = int(body.get("quantity") or extra.get("quantity") or 1)
            note = (f"A vessel prints one device at a time — sent 1 of the {n} asked for." if n > 1 else None)
            if body.get("tags") or extra.get("tags"):
                note = (note + " " if note else "") + "Tags aren't part of the vessel's print — add them once it's printed."
            out = await call_action(request, user, "POST", f"/replicants/{rep}/print", {"device_type": dtype},
                                    f"{rep} prints {dtype} (vessel {code})")
            return render_action(request, out, note=note)
    if command == "message":   # BobNet through a relay: no double sends
        if await bobnet_repeat(request.app.state.db, body.get("channel") or "", body.get("text") or ""):
            return bad(f"Already sent that to {body.get('channel')} in the last {BOBNET_REPEAT_MINUTES} minutes")
        out = await call_action(request, user, "POST", f"/devices/{code}", {"command": command, **body, **extra},
                                f"{code} {command}")
        if out["ok"]:
            await bobnet_note_sent(request.app.state.db, body.get("channel") or "", body.get("text") or "")
        return render_action(request, out)
    # stow / attach / detach from either side: the command goes to the device the game expects it on
    if command == "stow" and body.get("cargo"):
        cargo = body.pop("cargo")
        return await run_action(request, user, "POST", f"/devices/{cargo}", {"command": "stow", "target": code},
                                f"stow {cargo} in {code}")
    if command == "stow":
        body.pop("cargo", None)
    if command == "stow" and body.get("target"):   # the hold takes it, else the carrier's attach points (up the chain)
        devs = (await load_state(request))["devices"]
        by = {d.get("device_code"): d for d in devs}
        cc = fl.cargo_carrier(by.get(body["target"]) or {}, devs, fl.stowable(by.get(code) or {}), fl.attachable(by.get(code) or {}),
                                fl.is_matrix(by.get(code) or {})) if body["target"] in by else None
        if cc and cc["mode"] == "attach":
            car = cc["carrier"]["device_code"]
            return await run_action(request, user, "POST", f"/devices/{car}", {"command": "attach", "device": code},
                                    f"{car}: attach {code}")
        if cc:
            body["target"] = cc["carrier"]["device_code"]
    if command == "attach":
        carrier, cargo = (body.get("carrier"), code) if body.get("carrier") else (code, body.get("device"))
        if not cargo:
            return bad("Pick the device to attach, or the carrier to attach this one to")
        return await run_action(request, user, "POST", f"/devices/{carrier}", {"command": "attach", "device": cargo},
                                f"{carrier}: attach {cargo}")
    if command == "deploy" and body.get("cargo"):   # on the carrier's page: a device from its hold
        cargo = body.pop("cargo")
        trip = fl.in_flight(await _device(request, code))
        if trip:
            return bad(f"{code} is traveling to {trip['destination']} — deploy once it has arrived, or the device "
                       "comes out between systems with no location")
        return await run_action(request, user, "POST", f"/devices/{cargo}", {"command": "deploy"}, f"deploy {cargo} from {code}")
    if command == "deploy":
        body.pop("cargo", None)
    if command == "detach" and not body.get("device"):
        me = await _device(request, code)
        host = me.get("attached_to_device_code")
        if host:   # on the attached device's own page: its carrier lets go of it
            dev_h = await _device(request, host)
            trip = fl.in_flight(dev_h)
            if trip:
                return bad(f"{host} is traveling to {trip['destination']} — detach once it has arrived, or the device "
                           "comes out between systems with no location")
            return await run_action(request, user, "POST", f"/devices/{host}", {"command": "detach", "device": code},
                                    f"{host}: detach {code}")
    if command in ("deploy", "detach"):
        # Seen live 2026-10-06: slingshot E28DBE58 deployed while its carrier was mid-surge came out between systems,
        # with no location, and can't be reached since. Refuse while the carrier is moving.
        st = await load_state(request)
        by = {d.get("device_code"): d for d in st["devices"]}
        stowed = await request.app.state.db.kv_get("stowed_map", {}) or {}
        dev = by.get(code) or {}
        carrier = code if command == "detach" else (dev.get("stowed_in_device_code") or dev.get("attached_to_device_code")
                                                    or next((c for c, kids in stowed.items() if code in kids), None))
        trip = fl.in_flight(by.get(carrier) or {}) if carrier else None
        if trip:
            return bad(f"{carrier} is traveling to {trip['destination']} — {command} once it has arrived, or the device "
                       "comes out between systems with no location")
    if command in MOVE_COMMANDS and not form.get("confirm_far"):
        warn = await long_cruise_warning(request, code, str(body.get("destination") or extra.get("destination") or ""))
        if warn:   # a long cruise inside a system: say so, and send only once confirmed
            hidden = "".join(f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(str(v))}">'
                             for k, v in form.multi_items() if k != "confirm_far")
            return HTMLResponse(
                f'<form class="result small lv-alert" hx-post="/devices/{html.escape(code)}/command" hx-target="this" '
                f'hx-swap="outerHTML">{hidden}<input type="hidden" name="confirm_far" value="1">⚠ {html.escape(warn)} '
                f'<button class="small">Send anyway</button></form>')
    if command in MOVE_COMMANDS:
        try:
            followups = parse_chain(form, code, None)
        except ValueError as e:
            return bad(str(e))
        if followups:
            dest = str(body.get("destination") or "").upper()
            first = auto.step(f"{code} → {dest}", f"/devices/{code}", {"command": command, **body, **extra},
                              wait=["travel.arrived"], match={"destination": dest} if dest else None, critical=True)
            first["wait_device"] = code
            return await start_chain(request, user, f"{code} → {dest}, then {len(followups)} step(s)", first, followups, code)
    if command == "travel" and body.get("destination") and not body.get("via") and not extra.get("via"):
        return await planned_device_trip(request, user, code, {"command": command, **body, **extra})
    return await run_action(request, user, "POST", f"/devices/{code}", {"command": command, **body, **extra},
                            f"{command} on {code}")


@router.post("/devices/{code}/carrier", response_class=HTMLResponse)
async def device_carrier(request: Request, code: str, user: str = Depends(current_user)):
    form = await request.form()
    try:
        dev = {**await _device(request, code), **(await request.app.state.api.get(f"/devices/{code}") or {})}
    except ApiError:
        dev = await _device(request, code)
    ctx = await carrier_context(request, dev)
    if not ctx:
        return HTMLResponse('<div class="result err">This device can\'t carry anything.</div>')
    launch, stow, recall = set(form.getlist("launch")), set(form.getlist("stow")), set(form.getlist("recall"))
    if not (launch or stow or recall):
        return HTMLResponse('<div class="result err">Tick at least one launch, stow or recall box.</div>')
    eng = request.app.state.worker.automations
    radii, far_au = await eng.cruise_radii(), await eng.max_cruise_au()
    from .fleets import far_apart
    needs_move = any(r["code"] in stow and not r["same_loc"]
                     and (not r["mobile"] or far_apart(r.get("location"), dev.get("location"), radii, far_au))
                     for r in ctx["rows"])
    if needs_move and not ctx["travel_path"]:
        return HTMLResponse('<div class="result err">Some picked devices can\'t travel, and this vessel has no travel command to go and get them.</div>')
    mode = {r["code"]: r["mode"] for r in ctx["rows"]}
    for m, have, room, what in (("stow", ctx["carried"], ctx["capacity"], "in a vessel that holds"),
                                ("attach", ctx["attached"], ctx["points"], "on a carrier with attach points for")):
        if room is None:
            continue
        after = have - len([c for c in launch if mode.get(c) == m]) + len([c for c in stow if c not in launch and mode.get(c) == m])
        if after > room:
            return HTMLResponse(f'<div class="result err">That would put {after} devices {what} {int(room)}.</div>')
    steps = carrier_mod.plan(code, dev.get("location"), ctx["rows"], launch, stow, recall,
                             ctx["travel_path"] or f"/devices/{code}", return_after=form.get("return_after") == "on",
                             radii=radii, max_cruise_au=far_au)
    if not steps:
        return HTMLResponse('<div class="result err">Nothing to do for that selection.</div>')
    bits = [f"{len(launch)} launch" if launch else "", f"{len(stow)} pick-up" if stow else "", f"{len(recall - stow)} recall" if recall - stow else ""]
    return await start_chain(request, user, f"{code}: " + ", ".join(b for b in bits if b), steps[0], steps[1:], code)


@router.post("/devices/{code}/collect-all", response_class=HTMLResponse)
async def device_collect_all(request: Request, code: str, user: str = Depends(current_user)):
    """Load as much as fits from the stock where the transport is, in proportion to what's there."""
    dev = await _device(request, code)
    c = await cargo_context(request.app.state.db, request.app.state.api, dev,
                            normalize_blueprints(await request.app.state.db.kv_get("blueprints", [])))
    if not c["plan"]:
        why = ("the hold is full" if c["free"] == 0 else "nothing is stockpiled here" if not c["available"]
               else "the hold capacity is unknown — enter amounts by hand")
        return partial(request, "partials/action_result.html", ok=False, status=0, error=f"Nothing to collect: {why}.",
                       label="collect all", method="POST", path=f"/devices/{code}", response=None)
    return await run_action(request, user, "POST", f"/devices/{code}", {"command": "collect_resources", "resources": c["plan"]},
                            f"collect all ({int(sum(c['plan'].values()))} units) on {code}")


@router.post("/devices/{code}/tags", response_class=HTMLResponse)
async def device_tags(request: Request, code: str, add: str = Form(""), remove: str = Form(""),
                      user: str = Depends(current_user)):
    cfg: dict = {}
    if add.strip():
        cfg["add_tags"] = [t.strip() for t in add.split(",") if t.strip()]
    if remove.strip():
        cfg["remove_tags"] = [t.strip() for t in remove.split(",") if t.strip()]
    return await run_action(request, user, "PATCH", f"/devices/{code}", {"configuration": cfg}, f"tags on {code}")


# --- replicants -------------------------------------------------------------------------
@router.get("/replicants/{code}", response_class=HTMLResponse)
async def replicant_detail(request: Request, code: str, near_page: int = 1, user: str = Depends(current_user)):
    st = await load_state(request)
    rep = st["replicants"].get(code, {})
    try:
        rep = {**rep, **(await request.app.state.api.get(f"/replicants/{code}") or {})}
    except ApiError as e:
        rep["_error"] = e.message
    nearby, near = [], {}
    near_page = max(1, near_page)
    try:
        near = (await request.app.state.api.get(f"/replicants/{code}/stars", per_page=20, page=near_page)) or {}
        nearby = near.get("stars") or []
    except ApiError:
        pass
    near_pages = int(near.get("total_pages") or 0) or (near_page + 1 if len(nearby) >= 20 else near_page)
    blueprints = normalize_blueprints(await request.app.state.db.kv_get("blueprints", []))
    devices = [d for d in st["devices"] if d.get("replicant_code") == code]
    by = {d.get("device_code"): d for d in st["devices"]}
    stowed = await request.app.state.db.kv_get("stowed_map", {}) or {}
    # a slingshot stowed in a vessel is where its carrier is (same hold as the replicant's matrix counts as together)
    slingshots = sorted(({**d, "_at": where_is(d, by, stowed)} for d in st["devices"] if "slingshot" in (d.get("device_type") or "")),
                        key=lambda d: d["_at"] or "")
    sl_locs = {d["_at"] for d in slingshots if d["_at"]}
    matrices = sorted(({**d, "_at": where_is(d, by, stowed)} for d in st["devices"]
                       if "matrix" in (d.get("device_type") or "") and "container" not in (d.get("device_type") or "")),
                      key=lambda d: (d.get("device_type") != "empty_replicant_matrix", d["_at"] or ""))
    # empty matrices installed somewhere: transfer (same location, instant) or teleport (another system, over the FTL network)
    empties = [m for m in matrices if m.get("device_type") == "empty_replicant_matrix" and m["_at"]]
    matrices = [m for m in matrices if m["_at"] in sl_locs]   # linking needs the matrix at the slingshot
    explored = {r["star"] for r in await request.app.state.db.fetchall("SELECT star FROM systems")}
    from .census import destination_systems
    yours = {star_of(d.get("location")) for d in st["devices"] if d.get("location")}
    dest_systems = destination_systems(await request.app.state.db.kv_get("stars", {}) or {}, explored | yours, yours,
                                       rep.get("location") or rep.get("current_location"))
    return await page(request, user, "replicant.html", "fleet", rep=rep, code=code, nearby=nearby, near_page=near_page,
                      dest_systems=dest_systems,
                      near_pages=near_pages, explored=explored,
                      blueprints=blueprints, devices=devices, slingshots=slingshots, matrices=matrices, empties=empties,
                      SLINGSHOT_MIN=SLINGSHOT_MIN_CAPACITY, timers=[t for t in await active_timers(request)
                                                                      if code in (t.get("device_code"), t.get("replicant_code"))])


@router.post("/replicants/{code}/travel", response_class=HTMLResponse)
async def replicant_travel(request: Request, code: str, destination: str = Form(""), dry_run: str = Form(""),
                           user: str = Depends(current_user)):
    form0 = await request.form()   # the destination picker: typed code, else the spot, else the system
    destination = (form0.get("destination__custom") or destination or form0.get("destination__star") or "").strip().upper()
    if not destination:
        return HTMLResponse('<div class="result err">Pick a system (and a spot in it), or type a destination.</div>')
    host = ((await load_state(request))["replicants"].get(code) or {}).get("hosted_device_code")
    # the job runner's route planning (surge hops, a hub, the off-ramp): the game's own route takes none of them
    via, ramp = (await request.app.state.worker.automations.trip_plan(host, destination)) if host else ([], None)
    if "via" in form0:
        via = [x for x in str(form0.get("via") or "").upper().split(",") if x.strip()]
    if dry_run:
        api, path = request.app.state.api, f"/replicants/{code}/travel"
        try:
            direct = await api.post(path, {"destination": destination, "dry_run": True})
        except ApiError as e:
            return partial(request, "partials/action_result.html", ok=False, status=e.status, error=e.message,
                           label="route preview", method="POST", path=path, response=e.body)
        resp, saved, refused, hop_first = direct, None, None, (await ramp_note(request, ramp) if ramp else None)
        if via:
            try:
                resp = await api.post(path, {"destination": destination, "via": via, "dry_run": True})
                d_s, v_s = hubroute.total_seconds(direct), hubroute.total_seconds(resp)
                saved = (d_s - v_s, d_s) if d_s and v_s else None
                if saved and saved[0] <= 0:   # no faster than the game's own route: go direct
                    resp, via, saved = direct, [], None
            except ApiError as e:
                if loop_refused(e.message):   # Go hops there first, then cruises the rest
                    resp, hop_first = direct, HOP_FIRST_NOTE.format(via=", ".join(via))
                else:
                    resp, refused, via = direct, e.message, []
        return partial(request, "partials/route_preview.html", code=code, destination=destination, r=resp, via=via,
                       saved=saved, refused=refused, hop_first=hop_first, hm=hubroute.hm,
                       legs=resp.get("route") if isinstance(resp.get("route"), list) else [resp.get("route")] if resp.get("route") else [],
                       sugg=await suggestions(request, destination), uid=f"r-{code}", **await chain_context(request, host, code))
    form = await request.form()
    body = {"destination": destination, **({"via": via} if via else {})}
    try:
        followups = parse_chain(form, host, code)
    except ValueError as e:
        return partial(request, "partials/action_result.html", ok=False, status=400, error=str(e),
                       label="travel", method="POST", path=f"/replicants/{code}/travel", response=None)
    if ramp:   # lands at the entry point: the job runner hops to the off-ramp there (trip_plan)
        return await start_chain(request, user, f"{code} → {destination} (hops to {ramp} at arrival)"
                                 + (f", then {len(followups)} step(s)" if followups else ""),
                                 ramp_step(f"/replicants/{code}/travel", {"destination": destination}, host), followups, host)
    if followups:
        first = auto.step(f"{code} → {destination}", f"/replicants/{code}/travel", body,
                          wait=["travel.arrived"], match={"destination": destination}, critical=True)
        first["wait_device"] = host  # the arrival event is reported for the host vessel
        return await start_chain(request, user, f"{code} → {destination}, then {len(followups)} step(s)", first, followups, host)
    path = f"/replicants/{code}/travel"
    out = await call_action(request, user, "POST", path, body, f"{code} → {destination}" + (f" via {', '.join(via)}" if via else ""))
    if not out["ok"] and via and host and loop_refused(out["error"]):
        return await hop_first_chain(request, user, path, {"destination": destination}, host, via)
    return render_action(request, out)


@router.post("/replicants/{code}/move-to", response_class=HTMLResponse)
async def replicant_move_to(request: Request, code: str, target: str = Form(...), how: str = Form("transfer"),
                            user: str = Depends(current_user)):
    """Move the replicant into an empty matrix: transfer (docs: same location, instant) or teleport (another system,
    through the FTL network, ~30 s offline; needs an FTL link between the two systems)."""
    how = "teleport" if how == "teleport" else "transfer"
    target = target.strip().upper()
    return await run_action(request, user, "POST", f"/replicants/{code}/{how}", {"target": target}, f"{code}: {how} into {target}")


@router.post("/replicants/{code}/slingshot", response_class=HTMLResponse)
async def replicant_slingshot(request: Request, code: str, slingshot: str = Form(...), user: str = Depends(current_user)):
    """Fire a slingshot (docs /docs/ftl-slingshots/): the replicant must be at a deployed slingshot, which must be linked
    to an empty matrix (wherever that matrix is now) and have ≥80 % capacity. POST /replicants/{code}/teleport
    {"target": <slingshot>} sends the replicant's consciousness to that matrix; the slingshot drops to 5 %."""
    slingshot = slingshot.strip().upper()
    st = await load_state(request)
    by = {d.get("device_code"): d for d in st["devices"]}
    stowed = await request.app.state.db.kv_get("stowed_map", {}) or {}
    sl = by.get(slingshot) or {}
    rep = st["replicants"].get(code) or {}
    here = rep.get("location") or rep.get("current_location")
    sl_at = where_is(sl, by, stowed) if sl else None   # stowed: its carrier's location
    path = f"/replicants/{code}/teleport"

    def refuse(msg: str) -> HTMLResponse:
        return render_action(request, {"label": f"slingshot {slingshot}", "method": "POST", "path": path, "ok": False,
                                       "status": 400, "error": msg, "response": None})
    if sl and here and sl_at != here:
        return refuse(f"{slingshot} is at {sl_at or sl.get('status')}, the replicant is at {here} — "
                      "go to the slingshot first")
    if sl.get("status") == "stowed":   # live 2026-10-22: 400 "Slingshot must be deployed"
        return refuse(f"{slingshot} is stowed in {sl.get('stowed_in_device_code') or 'a vessel'} — "
                      "the game fires only a deployed slingshot: deploy it first")
    cap = capacity(sl.get("operational_capacity"))   # sometimes a 0–1 fraction
    if cap is not None and cap < SLINGSHOT_MIN_CAPACITY:
        return refuse(f"{slingshot} is at {cap:.0f}% — it needs at least {SLINGSHOT_MIN_CAPACITY:.0f}% "
                      "(a maintenance drone recharges it)")
    if sl and not sl.get("linked_device"):
        return refuse(f"{slingshot} isn't linked to a matrix yet")
    return await run_action(request, user, "POST", path, {"target": slingshot},
                            f"{code}: slingshot via {slingshot} → {sl.get('linked_device') or '?'}")


@router.post("/slingshots/{slingshot}/link", response_class=HTMLResponse)
async def slingshot_link(request: Request, slingshot: str, matrix: str = Form(...), user: str = Depends(current_user)):
    """Link a slingshot to an empty replicant matrix at the same location (docs: the matrix is stowed in a vessel, which
    then carries it to the destination; the link holds wherever it goes)."""
    slingshot, matrix = slingshot.strip().upper(), matrix.strip().upper()
    st = await load_state(request)
    by = {d.get("device_code"): d for d in st["devices"]}
    stowed = await request.app.state.db.kv_get("stowed_map", {}) or {}
    sl, mx = by.get(slingshot) or {}, by.get(matrix) or {}
    if sl and mx and where_is(mx, by, stowed) != where_is(sl, by, stowed):
        return render_action(request, {"label": f"link {slingshot} → {matrix}", "method": "PATCH", "path": f"/devices/{slingshot}",
                                       "ok": False, "status": 400, "response": None,
                                       "error": f"{matrix} is at {where_is(mx, by, stowed) or '?'}, {slingshot} at "
                                                f"{where_is(sl, by, stowed) or '?'} — they must be together to link"})
    return await run_action(request, user, "PATCH", f"/devices/{slingshot}", {"configuration": {"linked_device": matrix}},
                            f"link slingshot {slingshot} → {matrix}")


@router.post("/replicants/{code}/scan", response_class=HTMLResponse)
async def replicant_scan(request: Request, code: str, user: str = Depends(current_user)):
    resp = await run_action(request, user, "POST", f"/replicants/{code}/scan", {}, f"system scan by {code}")
    # Cache the scan for the system view if it succeeded.
    data = getattr(request.state, "action_response", None)
    if isinstance(data, dict) and data.get("planets") is not None:
        star = (data.get("star") or {}).get("designation")
        if not star:
            rep = (await load_state(request))["replicants"].get(code, {})
            star = star_of(rep.get("location") or rep.get("current_location"))
        if star:
            await request.app.state.db.execute(
                "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)", (star, json.dumps(data), now_iso()))
    return resp


@router.post("/replicants/{code}/mine", response_class=HTMLResponse)
async def replicant_mine(request: Request, code: str, resource_type: str = Form(""), stop: str = Form(""),
                         user: str = Depends(current_user)):
    if stop:
        return await run_action(request, user, "DELETE", f"/replicants/{code}/mine", None, f"stop mining {code}")
    return await run_action(request, user, "POST", f"/replicants/{code}/mine", {"resource_type": resource_type},
                            f"{code} mine {resource_type}")


@router.post("/replicants/{code}/print", response_class=HTMLResponse)
async def replicant_print(request: Request, code: str, device_type: str = Form(""), command: str = Form(""),
                          quantity: int = Form(1), user: str = Depends(current_user)):
    if command:  # cancel the current print (vessels have no queue)
        return await run_action(request, user, "POST", f"/replicants/{code}/print", {"command": command},
                                f"{code} print {command}")
    return await queue_print(request, user, "replicant", code, device_type, quantity)


@router.post("/replicants/{code}/message", response_class=HTMLResponse)
async def replicant_message(request: Request, code: str, channel: str = Form("#general"), text: str = Form(...),
                            user: str = Depends(current_user)):
    db = request.app.state.db
    text = text.strip()
    if not text:
        return HTMLResponse('<div class="result err">Nothing to send.</div>')
    if await bobnet_repeat(db, channel, text):   # per channel: the same text to another channel still goes
        return HTMLResponse(f'<div class="result err">Already sent that to {html.escape(channel)} in the last '
                            f'{BOBNET_REPEAT_MINUTES} minutes — not sending it there again (other channels are fine).</div>')
    out = await call_action(request, user, "POST", f"/replicants/{code}/message", {"channel": channel, "text": text},
                            f"BobNet {channel}")
    if not out["ok"]:
        return render_action(request, out)
    await bobnet_note_sent(db, channel, text)
    # X-Sent: the form clears its text box only when the message went out
    return HTMLResponse(f'<div class="result ok">Sent to {html.escape(channel)}.</div>', headers={"X-Sent": "1"})


__all__ = [n for n in dir() if not n.startswith("__")]   # everything, underscore helpers too
