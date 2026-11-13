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
    return await run_action(request, user, "POST", f"/locations/{code.upper()}/contribute", {"devices": codes},
                            f"contribute {len(codes)} device(s) to {code.upper()}")


@router.get("/megastructures", response_class=HTMLResponse)
async def megastructures(request: Request, user: str = Depends(current_user)):
    """The all-time megastructure contribution leaderboard (GET /leaderboards/megastructure)."""
    try:
        board, err = (await request.app.state.api.get("/leaderboards/megastructure") or {}).get("leaderboard") or [], None
    except ApiError as e:
        board, err = [], e.message
    return await page(request, user, "megastructures.html", "systems", board=board, err=err)


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
    return await page(request, user, "relays.html", "relays", nets=rn["networks"], nodes=rn["nodes"], bridges=br,
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


@router.get("/map/relays/{code}/network", response_class=HTMLResponse)
async def map_relay_network(request: Request, code: str, user: str = Depends(current_user)):
    """A relay's / hub's live links (GET /devices/{code}/network: connections, range_ly, status) next to the links the
    app expects from distances: expected but missing, and linked though not expected."""
    from . import relaynet
    code = code.strip().upper()
    try:
        body = await request.app.state.api.get(f"/devices/{code}/network") or {}
    except ApiError as e:
        return HTMLResponse(f'<span class="lv-alert small">{html.escape(e.message)}</span>')
    st = await load_state(request)
    pos = await relaynet.star_pos(request.app.state.db)
    rn = relaynet.networks(st["devices"], pos, await _bp_map(request))
    me = star_of(next((d.get("location") for d in st["devices"] if d.get("device_code") == code), "") or "")
    expect = {l["b"] if l["a"] == me else l["a"] for l in rn["links"] if me in (l["a"], l["b"])}
    conns = [c for c in body.get("connections") or [] if isinstance(c, dict)]
    live = {c.get("star") for c in conns if c.get("star")}
    return partial(request, "partials/relay_network.html", body=body, conns=conns, missing=sorted(expect - live),
                   extra=sorted(live - expect))


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
    key = f"{part}?{sim_view(request) or ''}"
    if request.app.state.worker.automations.focused() and key in _MAP_LAST:
        return JSONResponse(_MAP_LAST[key])   # a swoop run is on: a map redraw took up to 21 s of the event loop
    _MAP_LAST[key] = out = await map_payload(request, part)
    return JSONResponse(out)


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
