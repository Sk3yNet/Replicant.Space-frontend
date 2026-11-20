"""The galaxy map and its relay, hub and prospecting overlays.

Routes split out of web.py by page area; they register on the shared router in the original order."""
from __future__ import annotations

from .web_core import *  # noqa: F401,F403


# --- galaxy map ---------------------------------------------------------------------------
@router.post("/locations/{code}/contribute", response_class=HTMLResponse)
async def location_contribute(request: Request, code: str, user: str = Depends(current_user)):
    """Give devices to a megastructure (docs: POST /locations/{code}/contribute {"devices": [...]}; accepted ones are
    consumed, the rest come back as rejected)."""
    form = await request.form()
    codes = [c.strip().upper() for c in form.getlist("devices") if c.strip()]
    if not codes:
        return HTMLResponse('<div class="result err">Tick at least one device.</div>')
    # only a device's owner, at the structure, can give it (Delta gate, Joe 2026-11-20): hand the others to a replicant
    # of yours that is there first
    from . import megaorders as mo
    st = await load_state(request)
    by = {d.get("device_code"): d for d in st["devices"]}
    present = mo.present_replicants(code.upper(), st["replicants"], st["devices"])
    moved = []
    for c in codes:
        d = by.get(c) or {}
        if present and d.get("replicant_code") and d["replicant_code"] not in present:
            try:
                await request.app.state.api.post(f"/devices/{c}", {"command": "change_owner", "target": present[0]})
                moved.append(c)
            except ApiError as e:
                return HTMLResponse(f'<div class="result err">Handing {c} to {present[0]} failed: {html.escape(e.message)}</div>')
    return await run_action(request, user, "POST", f"/locations/{code.upper()}/contribute", {"devices": codes},
                            f"contribute {len(codes)} device(s) to {code.upper()}")


async def mega_order_ctx(db, code: str, data: Any = None) -> dict:
    """Context for partials/mega_order.html: the order, its device supply rows and the types the structure still needs."""
    from . import contractsupply as cs
    from . import locread
    from . import megaorders as mo
    code = code.upper()
    if data is None:
        data = await db.kv_get(f"loc:{code}", None)
    morder = (await db.kv_get(mo.KV, {}) or {}).get(code)
    mega = mo.mega_of(data)
    mtypes = mo.remaining(mega) if mega.get("requirements") is not None else dict((morder or {}).get("remaining") or {})
    msupply = (await db.kv_get(cs.KV, {}) or {}).get(mo.key(code)) or {}
    return {"code": code, "morder": morder, "mtypes": mtypes, "msupply": msupply}


@router.post("/locations/{code}/deliveries", response_class=HTMLResponse)
async def mega_order_save(request: Request, code: str, user: str = Depends(current_user)):
    """Schedule (or change) a delivery to a megastructure (megaorders.py): the ticked types, an amount each (blank: all
    it still needs), and whether to print what no spare covers."""
    from . import locread
    from . import megaorders as mo
    db, code = request.app.state.db, code.upper()
    form = await request.form()
    try:
        data = await locread.read(db, request.app.state.api, code, mo.READ_S)
    except ApiError as e:
        data = None
        err = e.message
    else:
        err = None
    ctx = await mega_order_ctx(db, code, data)
    types = {}
    for t in form.getlist("bring"):
        raw = str(form.get(f"n:{t}") or "").strip()
        types[t] = int(raw) if raw.isdigit() else 0
    if not types:
        return partial(request, "partials/mega_order.html", **ctx, msg="Tick at least one device type.")
    mega = mo.mega_of(data)
    if not mega and not ctx["morder"]:
        return partial(request, "partials/mega_order.html", **ctx, msg=f"{code} isn't a megastructure ({err or 'no requirements listed'}).")
    orders = await db.kv_get(mo.KV, {}) or {}
    old = orders.get(code)
    if old and old.get("status") != "done":
        old.update(types=types, print=bool(form.get("print")), status="active")
        if mega.get("requirements") is not None:
            old["remaining"] = mo.remaining(mega)
        old["want"] = mo.want(old)
    else:
        orders[code] = mo.new_order(code, types, bool(form.get("print")), mega, now_iso())
        orders[code]["read_at"] = now_iso() if mega.get("requirements") is not None else None
        orders[code]["want"] = mo.want(orders[code])
    await db.kv_set(mo.KV, orders)
    await request.app.state.worker.automations.log(
        "contracts", f"{user}: delivery to megastructure {orders[code].get('title') or code} ({code}) scheduled: "
        + ", ".join(f"{n or 'all'} {t.replace('_', ' ')}" for t, n in sorted(types.items())))
    ctx = await mega_order_ctx(db, code, data)
    return partial(request, "partials/mega_order.html", **ctx,
                   msg="Saved. Logistics picks spares within a few minutes (Logistics page lists the devices on their way).")


@router.post("/megastructures/orders/{code}/{action}", response_class=HTMLResponse)
async def mega_order_action(request: Request, code: str, action: str, user: str = Depends(current_user)):
    """Pause, resume or remove a scheduled megastructure delivery."""
    from . import megaorders as mo
    db, code = request.app.state.db, code.upper()
    orders = await db.kv_get(mo.KV, {}) or {}
    o = orders.get(code)
    msg = ""
    if o is None:
        msg = "No delivery scheduled there."
    elif action == "pause":
        o["status"], msg = "paused", "Paused: devices on their way stay tagged for it."
    elif action == "resume":
        o["status"], msg = "active", "Resumed."
    elif action == "remove":
        orders.pop(code)
        msg = "Removed: its devices become spare on the next pass."
    await db.kv_set(mo.KV, orders)
    ctx = await mega_order_ctx(db, code)
    return partial(request, "partials/mega_order.html", **ctx, msg=msg)


@router.get("/megastructures", response_class=HTMLResponse)
async def megastructures(request: Request, user: str = Depends(current_user)):
    """The all-time megastructure contribution leaderboard (GET /leaderboards/megastructure)."""
    try:
        board, err = (await request.app.state.api.get("/leaderboards/megastructure") or {}).get("leaderboard") or [], None
    except ApiError as e:
        board, err = [], e.message
    from . import megaorders as mo
    db = request.app.state.db
    orders = [await mega_order_ctx(db, loc) for loc in sorted(await db.kv_get(mo.KV, {}) or {})]
    return await page(request, user, "megastructures.html", "systems", board=board, err=err, orders=orders)


@router.get("/map/relays", response_class=HTMLResponse)
async def map_relays(request: Request, user: str = Depends(current_user)):
    """Map › Relays: your relay networks (relaynet.py), relay requests on their way, and bridges between networks."""
    from . import relaynet
    from . import relayroll as rr
    from . import ward_orders as wo
    st = await load_state(request)
    pos = await relaynet.star_pos(request.app.state.db)
    bps = await _bp_map(request)
    rn = relaynet.networks(st["devices"], pos, bps)
    rstars = relaynet.replicant_stars(st["replicants"], st["devices"])
    relaynet.coverage(rn, pos, rstars)
    roll = rr.state(await request.app.state.db.kv_get(rr.KV, {}))
    br = await relaynet.bridges_async(rn, pos, long=roll["long"])
    db = request.app.state.db
    relays, stations = await db.kv_get(wo.kv_key("relay"), {}) or {}, await db.kv_get(wo.kv_key("station"), {}) or {}
    orders = {**stations, **relays}
    for b in br:
        b["ordered"] = [s for s in b["stars"] if s in (stations if s in b["long"] else relays)]
    ranges = {**{x: n["range"] for x, n in rn["nodes"].items()}, **{x: rn["long_hop"] for x in stations}}
    up_next = rr.queue(roll["seeds"], pos, set(rn["nodes"]), set(orders) | set(roll["ordered"]), set(roll["skipped"]),
                       float(roll["radius"]), bool(roll["chained"]), radii=roll["radii"], ranges=ranges, hop=rn["hop"],
                       long_hop=rn["long_hop"] if roll["long"] else 0.0) if roll["seeds"] else []
    from .shapes import as_amounts
    sbp = bps.get(relaynet.STATION)
    station = {"known": bool(sbp), "range": rn["long_hop"], "hop": rn["hop"],
               "cost": as_amounts((sbp or {}).get("resources")), "relay_cost": as_amounts((bps.get("ftl_relay") or {}).get("resources"))}
    targets = await db.kv_get(rr.TARGETS_KV, {}) or {}
    return await page(request, user, "relays.html", "relays", nets=rn["networks"], bridges=br, targets=targets,
                      orders=orders, stations=stations, max_new=relaynet.MAX_BRIDGE, roll=roll, up_next=up_next[:15],
                      n_next=len(up_next), known=set(pos), station=station)


@router.post("/map/relays/rollout", response_class=HTMLResponse)
async def map_relays_rollout(request: Request, action: str = Form(...), seeds: str = Form(""), radius: float = Form(50),
                             chained: str = Form(""), long: str = Form(""), user: str = Depends(current_user)):
    """Relay rollout (relayroll.py): save its settings and start, pause, or retry the skipped systems. Each seed's radius
    comes from its r_<SEED> field or a SEED:ly entry in the seeds list; one equal to the default isn't kept."""
    from . import relayroll as rr
    db = request.app.state.db
    eng = request.app.state.worker.automations
    form = await request.form()
    async with eng.lock:
        roll = rr.state(await db.kv_get(rr.KV, {}))
        if action in ("start", "save"):
            clamp = lambda v: max(1.0, min(1000.0, float(v)))  # noqa: E731
            radius = clamp(radius)
            names, radii = [], {}
            for x in seeds.replace(",", " ").split():
                star, _, ly = x.strip().upper().partition(":")
                if not star:
                    continue
                names.append(star)
                raw = ly or str(form.get(f"r_{star}") or "").strip()
                try:
                    if raw and clamp(raw) != radius:
                        radii[star] = clamp(raw)
                except ValueError:
                    pass
            roll.update(seeds=list(dict.fromkeys(names)), radius=radius, radii=radii, chained=bool(chained), long=bool(long))
        if action == "start":
            if not roll["seeds"]:
                return HTMLResponse('<span class="lv-alert small">Name at least one seed system.</span>')
            roll.update(on=True, streak=0, by=user, paused=False)
        elif action == "stop":
            roll["on"] = False   # orders sending or switching on a relay stay; pause_relay_prints drops the prints
        elif action == "retry":
            roll.update(skipped={}, streak=0)
        await db.kv_set(rr.KV, roll)
        if action == "stop":
            await eng.pause_relay_prints(user)
        if roll["on"]:
            await eng.relay_rollout_pass()
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/map/relays/target", response_class=HTMLResponse)
async def map_relays_target(request: Request, action: str = Form(...), star: str = Form(""), user: str = Depends(current_user)):
    """Relay targets: add a system (its chain to a hub's network is ordered right away), check one again now, or remove
    one (relay orders already made stay; cancel them on the Wards & hubs page)."""
    from . import relaynet
    from . import relayroll as rr
    from .db import now_iso
    db = request.app.state.db
    eng = request.app.state.worker.automations
    stars = [x.strip().upper() for x in star.replace(",", " ").split() if x.strip()]
    if not stars:
        return HTMLResponse('<span class="lv-alert small">Name a system.</span>')
    pos = await relaynet.star_pos(db)
    async with eng.lock:
        targets = await db.kv_get(rr.TARGETS_KV, {}) or {}
        if action == "add":
            unknown = [s for s in stars if s not in pos]
            if unknown:
                return HTMLResponse(f'<span class="lv-alert small">{html.escape(", ".join(unknown))}: not in the star catalog.</span>')
            for s in stars:
                targets.setdefault(s, {"at": now_iso(), "by": user, "status": "planning"})
            await db.kv_set(rr.TARGETS_KV, targets)
            await eng.log("wards", f"relay target {', '.join(stars)} added (by {user})")
        elif action == "remove":
            for s in stars:
                targets.pop(s, None)
            await db.kv_set(rr.TARGETS_KV, targets)
        if action in ("add", "check"):
            for s in stars:
                await eng.relay_target_pass(only=s)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/map/relays/route", response_class=HTMLResponse)
async def map_relays_route(request: Request, stars: str = Form(...), long: str = Form(""), user: str = Depends(current_user)):
    """Add route: a relay request for every system on a bridge (as the Systems list's add relay); the systems in `long`
    get a long-range station instead (deep space relay station)."""
    eng = request.app.state.worker.automations
    wide = {x.strip().upper() for x in long.split(",") if x.strip()}
    out = []
    async with eng.lock:
        for s in [x.strip().upper() for x in stars.split(",") if x.strip()][:relaynet_max()]:
            out.append(f"{s}: {await eng.order_outpost('station' if s in wide else 'relay', s, user)}")
    return HTMLResponse('<div class="result small">' + "<br>".join(html.escape(x) for x in out) + "</div>",
                        headers={"HX-Refresh": "true"})


@router.get("/map", response_class=HTMLResponse)
async def galaxy_map(request: Request, user: str = Depends(current_user)):
    st = await load_state(request)
    focus = None
    if sim_view(request):   # the virtual galaxy: start at the run's vessel
        focus = next((star_of(d.get("location")) for d in st["devices"] if "vessel" in str(d.get("device_type"))
                      and d.get("location")), None) or next((star_of(d.get("location")) for d in st["devices"]
                                                             if d.get("location")), None)
    return await page(request, user, "map.html", "map", replicants=st["replicants"], map_focus=focus)


@router.get("/api/map.json")
async def map_data(request: Request, user: str = Depends(current_user)):
    part = request.query_params.get("part") or "all"
    # scope is part of the key: before, a swoop run could answer scope=rest with the scope=mine stars
    key = f"{part}?{request.query_params.get('scope') or ''}?{sim_view(request) or ''}"
    # a swoop run is on: a map redraw took up to 21 s of the event loop, so any answer we have is reused
    focused = request.app.state.worker.automations.focused()
    body = await cached_answer(request, _MAP_LAST, key,
                               lambda b: float("inf") if focused else map_fresh_for(part, b),
                               lambda: map_payload(request, part))
    return Response(body, media_type="application/json")


MAP_FRESH = {"overlay": 20.0, "core": 120.0, "all": 60.0}   # seconds a map answer is served again before a rebuild


def map_fresh_for(part: str, build_s: float) -> float:
    """How long a map answer is reused: every open map tab asks for the overlay on each device event (at most every
    5 s), and a build took 30-45 s on a busy server (live 2026-11-18, 92 requests in the perf window), so the server
    rebuilt the map without pause. An answer now holds at least MAP_FRESH[part], and three times what it took to build."""
    return max(MAP_FRESH.get(part, 60.0), 3 * build_s)


@router.get("/api/route", response_class=HTMLResponse)
async def route_estimate(request: Request, replicant: str, star: str, user: str = Depends(current_user)):
    try:
        body = await request.app.state.api.get(f"/replicants/{replicant}/stars/{star}")
        err = None
    except ApiError as e:
        body, err = {}, e.message
    return partial(request, "partials/route_estimate.html", s=(body or {}).get("star") or {}, err=err,
                   replicant=replicant, star=star)


@router.post("/map/refresh", response_class=HTMLResponse)
async def map_refresh(request: Request, user: str = Depends(current_user)):
    try:
        from .census import fetch_catalogue, full_catalogue
        stars = await full_catalogue(request.app.state.db, await fetch_catalogue(request.app.state.api))
        await request.app.state.db.kv_set("stars", stars)
        src = stars["sources"]
        bits = [f"{src['catalogue']} from the game's catalog"
                + (f" (it says {src['catalogue_total']})" if src.get("catalogue_total") not in (None, src["catalogue"]) else "")]
        if src["census"]:
            bits.append(f"{src['census']} from censuses")
        if src["observatory"]:
            bits.append(f"{src['observatory']} found by your observatories")
        if src["observatory_unplaced"]:
            bits.append(f"{len(src['observatory_unplaced'])} found without a position yet")
        return HTMLResponse(f"Catalog refreshed: {', '.join(bits)}. Reload the map.")
    except ApiError as e:
        return HTMLResponse(f"Refresh failed: {html.escape(e.message)} (the catalog allows 1 request/minute)")


__all__ = [n for n in dir() if not n.startswith("__")]   # everything, underscore helpers too
