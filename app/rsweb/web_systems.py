"""Systems, mining prospects, the star census and system pages.

Routes split out of web.py by page area; they register on the shared router in the original order."""
from __future__ import annotations

from .web_core import *  # noqa: F401,F403


# --- systems ----------------------------------------------------------------------------
# --- stars: census and unexplored stars -------------------------------------------------------
@router.get("/stars", response_class=HTMLResponse)
async def stars_page(request: Request, ref: str = "", user: str = Depends(current_user)):
    from . import census
    db = request.app.state.db
    st = await load_state(request)
    cat = await db.kv_get("stars", {}) or {}
    cstars = await db.kv_get("census_stars", {}) or {}
    pos = {s.get("designation"): s.get("position") for s in cat.get("stars") or [] if isinstance(s, dict)}
    here = {star_of(d.get("location")) for d in st["devices"] if d.get("location")}
    explored = {r["star"] for r in await db.fetchall("SELECT star FROM systems")} | here
    reps = {c: star_of(r.get("location") or r.get("current_location")) for c, r in st["replicants"].items()}
    ref = (ref or next((s for s in reps.values() if s in pos), "") or next(iter(sorted(here & set(pos))), "")).upper()
    rows = census.unexplored(cat, explored, pos.get(ref), cstars, limit=50)
    done = await db.kv_get("census", {}) or {}
    vessels = sorted((d for d in st["devices"] if census.can_census(d) and d.get("location")),
                     key=lambda d: (star_of(d.get("location")) in done, d["device_code"]))
    return await page(request, user, "stars.html", "stars", rows=rows, ref=ref, refs=sorted(set(reps.values()) | here),
                      done=done, vessels=vessels, cstars=len(cstars), catalogue=len(cat.get("stars") or []),
                      sec_per_ly=census.seconds_per_ly(cstars), reps=reps,
                      rep_names={c: r.get("name") or c for c, r in st["replicants"].items()})


@router.post("/stars/census", response_class=HTMLResponse)
async def stars_census(request: Request, device: str = Form(...), user: str = Depends(current_user)):
    st = await load_state(request)
    d = next((x for x in st["devices"] if x.get("device_code") == device), None)
    if not d or not d.get("location"):
        return HTMLResponse('<div class="result err">That vessel isn\'t in a system right now.</div>')
    eng = request.app.state.worker.automations
    async with eng.lock:
        found, err = await eng.run_census(device, star_of(d["location"]), manual=True)
    if err:
        return HTMLResponse(f'<div class="result err">Census failed: {html.escape(err)}</div>')
    new = [s["designation"] for s in found if s.get("explored") is False]
    return HTMLResponse(f'<div class="result ok">{len(found)} stars around {html.escape(star_of(d["location"]))}'
                        + (f'; unexplored: {html.escape(", ".join(new))}' if new else "; all explored") + "</div>",
                        headers={"HX-Refresh": "true"})


@router.get("/systems/prospects", response_class=HTMLResponse)
async def systems_prospects(request: Request, origin: str = "", user: str = Depends(current_user)):
    """Mining prospects: every scanned system scored for a mining fleet (prospects.py), distances from `origin`
    (default: the system with most of your devices)."""
    from . import prospects
    st = await load_state(request)
    count = Counter(star_of(d.get("location")) for d in st["devices"] if d.get("location"))
    origin = (origin or (count.most_common(1)[0][0] if count else "")).upper()
    p = await prospects.rank(request.app.state.db, request.app.state.worker.automations, st["devices"], origin)
    fleets = await request.app.state.worker.automations.fleets()
    homes = sorted({f["home"] for f in fleets if f.get("home")} | set(count))
    fleets_at: dict[str, list[str]] = defaultdict(list)   # your fleets homed in each system (the ⚑ mark)
    for f in fleets:
        if f.get("home"):
            fleets_at[f["home"]].append(f.get("name") or f["id"])
    # stationed homes are scored for review: listed among the open systems by that score (still never a mission target)
    rank_of = {"ok": 0, "stationed": 0, "unscanned": 1, "warded": 3}
    p["rows"] = sorted(p["rows"], key=lambda r: (rank_of.get(r["status"], 2), -((r["score"] if r["score"] is not None
                                                 else r.get("review_score")) or 0),
                                                 r["distance"] if r["distance"] is not None else 1e9))
    from . import ward_orders as wo
    from . import wardhub as wh
    from . import wards as wds
    foreign = wds.foreign(await request.app.state.db.kv_get("stars", {}) or {}, st["devices"])
    hubs = {star_of(d.get("location")) for d in st["devices"] if wh.is_hub(d) and wh.deployed(d)}
    orders = await request.app.state.db.kv_get(wo.KV, {}) or {}
    ward = {}
    for r in p["rows"]:
        x = r["star"]
        here = wo.ward_here(st["devices"], x)
        ward[x] = {"foreign": x in foreign, "hub": x in hubs, "order": orders.get(x),
                   "mine": here["device_code"] if here else None, "active": bool(here and wh.active(here))}
    return await page(request, user, "prospects.html", "prospects", p=p, origin=origin, origins=homes, ward=ward, fleets_at=fleets_at,
                      w=wh.wards(st["devices"]))


@router.post("/systems/{star}/ward", response_class=HTMLResponse)
async def systems_ward(request: Request, star: str, action: str = Form(...), kind: str = Form("ward"),
                       bg: str = Form(""), user: str = Depends(current_user)):
    """Prospects / Systems: add a ward, relay or beacon in a system (activate / send a spare / print one) or remove
    yours (deactivate). `bg`: the Systems page checkboxes, answered with JSON {msg, on} (on: the box stays ticked,
    an order or a working one) and no page refresh."""
    eng = request.app.state.worker.automations
    async with eng.lock:
        msg = await (eng.order_outpost(kind, star, user) if action == "add" else eng.remove_outpost(kind, star, user))
    if bg:
        from . import outposts
        from . import ward_orders as wo
        from . import wardhub as wh
        star = star.upper()
        ordered = kind in wo.KINDS and star in (await request.app.state.db.kv_get(wo.kv_key(kind), {}) or {})
        working = action == "add" and any(wh.active(d) for d in
                                          (outposts.presence(await eng.devices()).get(star) or {}).get(kind) or [])
        return JSONResponse({"msg": msg, "on": ordered or working})
    return HTMLResponse(f'<div class="result small">{html.escape(msg)}</div>', headers={"HX-Refresh": "true"})


@router.get("/systems", response_class=HTMLResponse)
async def systems(request: Request, origin: str = "", user: str = Depends(current_user)):
    """Systems with your presence or a cached scan, nearest first from `origin` (default: the system with most of
    your devices; the same choices as Mining prospects)."""
    from .prospects import _dist
    st = await load_state(request)
    known = {r["star"]: r for r in await request.app.state.db.fetchall("SELECT star, updated_at FROM systems")}
    tag_rows = await request.app.state.db.fetchall(
        "SELECT star, CASE WHEN json_valid(data) THEN json_extract(data, '$.system_tags') END AS tags FROM systems")
    system_tags = scan_tags(tag_rows)
    presence: dict[str, dict] = defaultdict(lambda: {"devices": 0, "homes": [], "targets": [], "locations": 0, "resources": 0})
    for d in st["devices"]:
        presence[star_of(d.get("location"))]["devices"] += 1
    fleets = await request.app.state.worker.automations.fleets()
    for f in fleets:   # fleets homed here, and missions headed here
        name = f.get("name") or f["id"]
        if f.get("home"):
            presence[f["home"]]["homes"].append(name)
        m = f.get("mission") or {}
        if m.get("status") in ("running", "stalled"):
            for t in dict.fromkeys(star_of(x) for x in m.get("targets") or [] if x):
                if name not in presence[t]["targets"]:
                    presence[t]["targets"].append(name)
    for loc, info in (st["locations"] or {}).items():
        p = presence[star_of(loc)]
        p["locations"] += 1
        p["resources"] += (info or {}).get("resources") or 0
    for s in known:
        presence[s]
    count = Counter(star_of(d.get("location")) for d in st["devices"] if d.get("location"))
    origin = (origin or (count.most_common(1)[0][0] if count else "")).upper()
    origins = sorted({f["home"] for f in fleets if f.get("home")} | set(count))
    db = request.app.state.db
    cat = await db.kv_get("stars", {}) or {}
    pos = {x.get("designation"): x.get("position") for x in cat.get("stars") or [] if isinstance(x, dict)}
    region = {x.get("designation"): x.get("region") for x in cat.get("stars") or [] if isinstance(x, dict)}
    for s, p in presence.items():
        dist = _dist(pos.get(origin), pos.get(s))
        p["distance"] = None if dist is None else round(dist, 1)
        p["region"] = region.get(s) or ""
    rows = sorted(presence.items(), key=lambda kv: (kv[1]["distance"] is None, kv[1]["distance"] or 0,
                                                    -kv[1]["devices"], kv[0]))
    for s, p in rows:
        if s:
            r = await system_resources(db, s)
            p["mineable"], p["salvage"], p["unknown_sites"] = r["mineable"], r["salvageable"], r["unknown_sites"]
            p["top"] = sorted(((k, t["sites"] + t["salvage"]) for k, t in r["totals"].items() if t["sites"] + t["salvage"]),
                              key=lambda kv: -kv[1])[:3]
    from . import outposts
    pres = outposts.presence(st["devices"])
    for s, p in rows:
        p["outposts"] = pres.get(s) or {k: [] for k in outposts.KINDS}
    out_rows = [(s, p) for s, p in rows if s]
    from . import ward_orders as wo
    from . import wardhub as wh
    orders = {k: await db.kv_get(wo.kv_key(k), {}) or {} for k in wo.KINDS}
    cov = await request.app.state.worker.automations.civ_coverage()
    beacon_at = {d.get("location") for d in st["devices"] if d.get("device_type") == "ftl_beacon" and wh.active(d)}
    for s, p in out_rows:
        p["civ"] = [{"location": x, "beacon": x in beacon_at} for x in wo.civ_places(cov, s)]   # local civilization bodies
        p["orders"] = {k: orders[k].get(s) for k in wo.KINDS}
        p["working"] = {k: any(wh.active(d) for d in p["outposts"][k]) for k in wo.KINDS}
        p["civ_gap"] = [c["location"] for c in p["civ"] if not c["beacon"]] if p["civ"] else []
    return await page(request, user, "systems.html", "systems", rows=out_rows, known=known, outpost_kinds=outposts.LABELS, origin=origin, origins=origins,
                      system_tags=system_tags, regions=sorted({p["region"] for _, p in out_rows if p["region"]}))


@router.post("/systems/{star}/surveyed", response_class=HTMLResponse)
async def system_surveyed(request: Request, star: str, body: str = Form(...), on: str = Form(""),
                          user: str = Depends(current_user)):
    """Mark one planet or belt surveyed (or not) by hand: auto-survey skips surveyed bodies. Once every one is
    marked the system counts as fully surveyed and the page shows that instead of the boxes."""
    db = request.app.state.db
    star, body = star.upper(), body.strip().upper()
    if star_of(body) != star:
        return HTMLResponse("", status_code=400)
    marks = await db.kv_get("surveyed", {}) or {}
    if on:
        marks[body] = marks.get(body) or now_iso()
    else:
        marks.pop(body, None)
        done = await db.kv_get("ami_surveyed", {}) or {}
        if done.pop(star, None):
            await db.kv_set("ami_surveyed", done)
    await db.kv_set("surveyed", marks)
    row = await db.fetchone("SELECT data FROM systems WHERE star=?", (star,))
    _, full = await survey_state(db, star, json.loads(row["data"]) if row else {})
    if full:
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    return HTMLResponse(f'<span class="small {"lv-done" if on else "muted"}">{"✓" if on else ""}</span>')


@router.get("/systems/{star}", response_class=HTMLResponse)
async def system_view(request: Request, star: str, refresh: int = 0, user: str = Depends(current_user)):
    star = star.upper()
    db = request.app.state.db
    err = None
    row = await db.fetchone("SELECT data, updated_at FROM systems WHERE star=?", (star,))
    if refresh or not row:
        try:
            await request.app.state.worker.refresh_system(star)
            row = await db.fetchone("SELECT data, updated_at FROM systems WHERE star=?", (star,))
        except ApiError as e:
            err = e.message
    scan = json.loads(row["data"]) if row else {}
    st = await load_state(request)
    sys_t = await system_targets(db, star)
    res = await system_resources(db, star)
    view = await system_view_model(request, star, scan, st, sys_t, res)
    reps = [r for r in st["replicants"].values() if star_of(r.get("location") or r.get("current_location")) == star]
    game_locs = {k: v for k, v in (st["locations"] or {}).items() if star_of(k) == star}
    qty = {x["code"]: x for x in res["sites_shown"] + res["salvage_shown"]}
    here = devices_in_system(st["devices"], star)
    surveyed, full = await survey_state(db, star, scan)
    viability = [v for v in await request.app.state.worker.automations.viability_report() if v["star"] == star]
    belts, resources, bodies = await system_layout(db, star, scan, st, sys_t, res, viability)
    from . import others as oth
    osnap = oth.normalize(await db.kv_get(oth.KV, {}))["stars"].get(star)
    from .web_lockers import in_system as lockers_in
    region = next((x.get("region") for x in (await db.kv_get("stars", {}) or {}).get("stars") or []
                   if isinstance(x, dict) and x.get("designation") == star), None)
    return await page(request, user, "system.html", "systems", star=star, scan=scan, view=view, err=err, region=region,
                      others=oth.summary(osnap, await db.kv_get("replicant_profiles", {}) or {}), others_snap=osnap,
                      other_drones=oth.drone_totals(osnap, await db.kv_get("replicant_profiles", {}) or {}),
                      others_warded=star in oth.warded_by_others(await db.kv_get("stars", {}) or {}, st["devices"]),
                      surveyed=surveyed, fully_surveyed=full,
                      updated=row["updated_at"] if row else None, reps=reps, res=res, sys_t=sys_t, here=here,
                      CATEGORY_LABEL=CATEGORY_LABEL, game_locs=game_locs, qty=qty,
                      belts=belts, resources=resources, bodies=bodies, lockers=lockers_in(star, (oth.normalize(await db.kv_get(oth.KV, {})) or {}).get("lockers")))


@router.post("/systems/{star}/scan-others", response_class=HTMLResponse)
async def system_scan_others(request: Request, star: str, user: str = Depends(current_user)):
    """Scan the other devices in the system from one of our replicants there (GET /replicants/{code}/scan/devices)."""
    from . import others as oth, traffic as tr
    star = star.upper()
    st = await load_state(request)
    rep = next((c for c, r in st["replicants"].items() if star_of(r.get("location") or r.get("current_location")) == star), None)
    if not rep:
        return HTMLResponse(f'<div class="result err">None of your replicants is in {html.escape(star)}: only a replicant in the system can scan it.</div>')
    try:
        await oth.scan_into(request.app.state.db, request.app.state.api, rep,
                            tr.my_codes(st["account"], st["replicants"], st["devices"]))
    except ApiError as e:
        return HTMLResponse(f'<div class="result err">Scan failed: {html.escape(e.message)}</div>')
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/systems/{star}/resources/refresh", response_class=HTMLResponse)
async def system_resources_refresh(request: Request, star: str, user: str = Depends(current_user)):
    """Re-read the system's belts (open mining sites) and the bodies its salvage sits on (at most 15 GETs).
    Belts come from the stored system scan; without one, GET /locations/<STAR> (same body as a scan) supplies them,
    plus any belt our devices or events have been at. Salvage codes (X-1-SAL-2) aren't locations: read the body (X-1)."""
    from .salvage import body_of
    star = star.upper()
    db, api = request.app.state.db, request.app.state.api
    row = await db.fetchone("SELECT data FROM systems WHERE star=?", (star,))
    scan = json.loads(row["data"]) if row else {}
    belts = [b.get("designation") for b in ((scan.get("asteroid_belt") or {}).get("belts")) or [] if b.get("designation")]
    failed: list[str] = []
    if not belts:
        try:
            sysd = await api.get(f"/locations/{star}")
            if isinstance(sysd, dict) and (sysd.get("asteroid_belt") or sysd.get("planets")):
                belts = [b.get("designation") for b in ((sysd.get("asteroid_belt") or {}).get("belts")) or [] if b.get("designation")]
                if not row:
                    await db.execute("INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                                     (star, json.dumps(sysd), now_iso()))
        except ApiError as e:
            failed.append(f"{star}: {e.message}")
    st = await load_state(request)
    pat = re.compile(rf"\b{re.escape(star)}-BELT-\d+\b")
    for d in st["devices"]:
        belts += pat.findall(d.get("location") or "")
    for r in await db.fetchall("SELECT DISTINCT location FROM events WHERE location LIKE ?", (f"{star}-BELT-%",)):
        belts += pat.findall(r["location"] or "")
    belts = list(dict.fromkeys(b.upper() for b in belts if "-SITE-" not in b))
    res = await system_resources(db, star)
    bodies = list(dict.fromkeys(body_of(x["code"]) for x in res["salvage"] if not x["depleted"]))
    read_belts, read_bodies, open_sites = 0, 0, 0
    for code in (belts + [c for c in bodies if c not in belts])[:15]:
        try:
            data = await api.get(f"/locations/{code}")
        except ApiError as e:
            failed.append(f"{code}: {e.message}")
            continue
        if isinstance(data, dict) and data:
            await locread.save(db, code, data)   # stamped: the engine's belt counts take this copy
            if code in belts:
                read_belts += 1
                open_sites += len(data.get("resource_sites") or [])
            else:
                read_bodies += 1
    bits = []
    if read_belts:
        bits.append(f"read {read_belts} belt(s): {open_sites} open mining site(s)")
    elif not belts:
        bits.append("no belts known for this system — run a system scan here first")
    if read_bodies:
        bits.append(f"read {read_bodies} salvage body/bodies")
    msg = "; ".join(bits)
    if read_belts and not open_sites:
        msg += (". Belts only list sites that are open: a survey drone's <code>search</code> at the belt (or an AMI survey "
                "controller on <code>belt_search</code>) opens one, and it stays open while the drone tracks it.")
    err = f'<div class="lv-alert small">Could not read: {html.escape("; ".join(failed[:4]))}</div>' if failed else ""
    reload = '<script>setTimeout(function(){location.reload()}, 2500)</script>' if (read_belts or read_bodies) else ""
    return HTMLResponse(f'<div class="small">{msg}</div>{err}{reload}')


@router.get("/locations/{code}", response_class=HTMLResponse)
async def location_detail(request: Request, code: str, user: str = Depends(current_user)):
    try:
        data = await request.app.state.api.get(f"/locations/{code}")
        err = None
        await locread.save(request.app.state.db, code, data)   # stamped for the engine and Terraforming
    except ApiError as e:
        data, err = {}, e.message
    st = await load_state(request)
    devices = [d for d in st["devices"] if d.get("location") == code]
    tmpl = "partials/location.html" if request.headers.get("hx-request") else "location_page.html"
    if tmpl.startswith("partials"):
        return partial(request, tmpl, code=code, data=data, err=err, devices=devices)
    return await page(request, user, tmpl, "systems", code=code, data=data, err=err, devices=devices)


__all__ = [n for n in dir() if not n.startswith("__")]   # everything, underscore helpers too
