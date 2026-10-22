"""Fleets: the landing page (status, templates), Fleet details (cards, next pass, tags check, spare & on the way), and
every fleet edit, mission and loadout route. Moved out of web.py (2026-10-15) so edits here don't page through it."""
from __future__ import annotations

import html
import json
import re
from collections import Counter
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from . import automations as auto
from . import fleets as fl
from . import loadouts as lo
from . import pagecache, printqueue
from .db import now_iso
from .shapes import normalize_blueprints
from .web import current_user, load_state, loadout_ctx, page, partial, star_of, start_chain

router = APIRouter()

def fleet_order(f: dict) -> tuple[str, str]:
    """Fleets show alphabetically by name."""
    return ((f.get("name") or f.get("id") or "").casefold(), f.get("id") or "")


async def fleets_ctx(request: Request) -> dict:
    eng = request.app.state.worker.automations
    st = await load_state(request)
    items = sorted(await eng.fleets(), key=fleet_order)
    bps = normalize_blueprints(await request.app.state.db.kv_get("blueprints", []))
    types = sorted({b["device_type"] for b in bps} | {d.get("device_type") for d in st["devices"] if d.get("device_type")})
    jobs = {j["id"]: j for j in await eng.jobs()}
    bp_by = {b["device_type"]: b for b in bps}
    for f in items:
        f["roster"] = fl.roster(f, st["devices"])
        f["points"] = fl.attach_points(f, st["devices"], bp_by, f["roster"]["rows"])
        m = f.get("mission") or {}
        f["job"] = jobs.get(m.get("job"))
    # vessels hosting a replicant can join too (a replicant riding with the fleet keeps it commandable where no relay
    # reaches); they're marked so it's clear the replicant goes wherever the fleet goes
    hosted = {r.get("hosted_device_code"): (r.get("name") or c) for c, r in st["replicants"].items() if r.get("hosted_device_code")}
    # every device that could join a fleet, marked with the fleet it's in now (joining moves it out of that one);
    # fleetless first, then by fleet, system and type
    names = {x["id"]: x.get("name") or x["id"] for x in items}
    free = [{**d, "_hosts": hosted.get(d.get("device_code")), "_fleet": fl.fleet_of(d),
             "_fleet_name": names.get(fl.fleet_of(d) or "", fl.fleet_of(d))} for d in st["devices"]]
    stars_seen = sorted({star_of(d.get("location")) for d in st["devices"] if d.get("location")})
    from .shop import known_traders
    traders = await known_traders(request.app.state.db)
    profiles = {t: fl.type_profile(t, bp_by, st["devices"]) for t in types}
    lctx = await loadout_ctx(request)
    homes = sorted(fl.worked_systems(items))
    from .census import destination_systems
    cat = await request.app.state.db.kv_get("stars", {}) or {}
    scanned = {r["star"] for r in await request.app.state.db.fetchall("SELECT star FROM systems")}
    yours = {star_of(d.get("location")) for d in st["devices"] if d.get("location")}
    from . import prospects as _pr
    ranks: dict[str, dict] = {}   # origin -> {star: prospect}
    dests: dict[str, list[dict]] = {}   # home -> destination list (fleets sharing a home share it)
    for f in items:
        if f.get("home") not in dests:
            async def dl(h=f.get("home")) -> list[dict]:
                return destination_systems(cat, scanned | yours, yours, h, limit=300)
            dests[f.get("home")] = [o for o in await pagecache.get(request, f"dests:{f.get('home')}", dl) if o["value"] not in homes]
        f["target_options"] = [dict(o) for o in dests[f.get("home")]]
        if f.get("role") == "mining":   # mining targets: best prospects first, with their score and why
            o_star = f.get("home") or ""
            if o_star not in ranks:
                async def rk(o=o_star) -> dict:
                    return {r["star"]: r for r in (await _pr.rank(request.app.state.db, eng, st["devices"], o))["rows"]}
                ranks[o_star] = await pagecache.get(request, f"ranks:{o_star}", rk)
            pr = ranks[o_star]
            for o in f["target_options"]:
                r = pr.get(o["value"])
                if r and r["score"] is not None:
                    o["group"], o["label"] = f"prospect {r['score']}", " · ".join(r["reasons"][:3])
                elif r and r["status"] == "warded":
                    o["group"] = "warded — can't mine"
            def by_prospect(o: dict, pr: dict = pr) -> tuple:
                s = (pr.get(o["value"]) or {}).get("score")
                return (s is None, -(s or 0), o.get("distance") is None, o.get("distance") or 0)
            f["target_options"].sort(key=by_prospect)
    stars_all = sorted(set(stars_seen) | {f["home"] for f in items if f.get("home")})
    reps = {c: (r.get("name") or c) for c, r in st["replicants"].items()}
    for f in items:
        own = fl.ownership(f, st["devices"])
        f["owner_move"], f["owner_hosts"] = own["move"], own["hosts"]
        f["owners"] = Counter(reps.get(d.get("replicant_code"), d.get("replicant_code") or "?") for d in fl.members(f, st["devices"]))
    boots = [f for f in items if f.get("role") == "bootstrap" and f.get("boot")]
    if boots:
        import copy
        from . import bootstrap as bt
        w = await eng.bootstrap_world(st["devices"], items)
        for f in boots:
            f["bview"] = bt.evaluate(copy.deepcopy(f), w)
            f["bsettings"] = bt.settings(f["boot"])
            f["bkids"] = [g for g in items if g.get("parent") == f["id"]]
    for f in items:
        f["report"] = lctx["plan"]["report"].get(f["id"])
        f["orders"] = [o for o in lctx.get("orders") or [] if o.get("fleet") == f["id"] and not o.get("device_code")]
        f["route"] = next((r for r in lctx["plan"].get("routes") or [] if r.get("fleet_id") == f["id"]), None)
        f["sends_to"] = fl.materials_target(f, items)
        f["takes_from"] = [x for x in items if (fl.materials_target(x, items) or {}).get("id") == f["id"]]
    from . import gameevents as gev
    from .shapes import normalize_inventory
    inv = {i.get("location"): i.get("items") or {} for i in normalize_inventory(await request.app.state.db.kv_get("inventory", []))}
    contracts = []
    for e in (await gev.load(request.app.state.db)).values():
        if e.get("status") != "open" or not e.get("location"):
            continue
        prog = gev.progress(e, inv, st["devices"], st["replicants"])
        price = {x["resource"]: x["need"] for x in (prog.get("best") or {}).get("resources") or []}
        dev_short = {x["device_type"]: x["short_here"] for x in (prog.get("best") or {}).get("devices") or [] if x["short_here"]}
        contracts.append({"designation": e["designation"], "location": e["location"], "title": e.get("title"), "price": price,
                          "rewards": (e.get("rewards") or {}).get("resources") or {} if isinstance(e.get("rewards"), dict) else {},
                          "short": fl.site_short(price, inv.get(e["location"]) or {}), "devices_short": dev_short})
    from . import prospecting as _pr
    runs = await request.app.state.db.kv_get("observatory_runs", {}) or {}
    obs_status: dict[str, list[str]] = {}
    for d in st["devices"]:
        if d.get("device_type") == "galactic_observatory" and fl.fleet_of(d) and runs.get(d["device_code"]):
            r = runs[d["device_code"]]
            obs_status.setdefault(fl.fleet_of(d), []).append(
                f"{d['device_code']}: {_pr.summary(r)}" + (f" — {r['note']}" if r.get("note") else ""))
    obs_fleets = {fl.fleet_of(d) for d in st["devices"] if d.get("device_type") == "galactic_observatory" and fl.fleet_of(d)}
    obs_fleets |= {f["id"] for f in items if (fl.station_wants(f) or {}).get("galactic_observatory")}   # or its loadout wants one
    from . import bootstrap as _bt
    return {**lctx, "plans_age": pagecache.age_minutes(request), "obs_status": obs_status, "obs_fleets": obs_fleets, "contracts": sorted(contracts, key=lambda c: c["location"]), "rep_names": reps, "profiles": profiles, "home_systems": homes, "templates": lctx["cfg"]["phases"], "fleets": items,
            "types": types, "free": sorted(free, key=lambda d: (bool(d["_fleet"]), (d["_fleet_name"] or "").lower(), star_of(d.get("location")),
                                                 d.get("device_type") or "", d.get("device_code") or "")),
            "stars": stars_all, "roles": fl.ROLE_CHOICES, "phases": fl.PHASES, "traders": traders,
            "boot_reps": [(c, r.get("name") or c) for c, r in st["replicants"].items() if r.get("hosted_device_code")],
            "stages": _bt.STAGES, "labels": _bt.LABEL}


@router.get("/fleets", response_class=HTMLResponse)
async def fleets_page(request: Request, user: str = Depends(current_user)):
    """Landing: fleet status (loaded after the page, see /fleets/overview) and the templates. Nothing slow here: the
    loadout plan, prospect ranks, contracts and the per-fleet cards are on /fleets/details."""
    eng = request.app.state.worker.automations
    db = request.app.state.db
    st = await load_state(request)
    items = sorted(await eng.fleets(), key=fleet_order)
    cfg = await eng.loadout_cfg()
    bps = normalize_blueprints(await db.kv_get("blueprints", []))
    types = sorted({b["device_type"] for b in bps} | {d.get("device_type") for d in st["devices"] if d.get("device_type")}
                   - {"heaven_vessel"})
    stars = sorted({star_of(d.get("location")) for d in st["devices"] if d.get("location")} | {f["home"] for f in items if f.get("home")})
    return await page(request, user, "fleets.html", "fleets", fleets=items, cfg=cfg, templates=cfg["phases"], types=types,
                      stars=stars, roles=fl.ROLES, last=await db.kv_get("loadouts_last", {}) or {},
                      strays=sorted((await db.kv_get("fleet_strays", {}) or {}).values(), key=lambda x: (x.get("name") or "", x["code"])),
                      home_moves=await db.kv_get("home_moves", {}) or {}, stray_reason=fl.stray_reason,
                      depot=lo.spare_depot(cfg, st["devices"]),
                      all_tags=sorted({t for d in st["devices"] for t in (d.get("tags") or [])}),
                      boot_reps=[(c, r.get("name") or c) for c, r in st["replicants"].items() if r.get("hosted_device_code")])


@router.get("/fleets/overview", response_class=HTMLResponse)
async def fleets_overview(request: Request, user: str = Depends(current_user)):
    """The landing page's status table: each fleet against the loadout plan (computed here, after the page shows)."""
    eng = request.app.state.worker.automations
    st = await load_state(request)
    items = sorted(await eng.fleets(), key=fleet_order)
    cfg = await eng.loadout_cfg()
    p = await pagecache.get(request, "loadout_plan", eng.loadout_plan)
    for f in items:
        f["report"] = p["report"].get(f["id"])
        f["route"] = next((r for r in p.get("routes") or [] if r.get("fleet_id") == f["id"]), None)
        f["sends_to"] = fl.materials_target(f, items)
        f["takes_from"] = [x for x in items if (fl.materials_target(x, items) or {}).get("id") == f["id"]]
    return partial(request, "partials/fleets_overview.html", fleets=items, templates=cfg["phases"],
                   present=Counter(lo.star_of(d.get("location")) for d in st["devices"]), plans_age=pagecache.age_minutes(request))


@router.post("/fleets/recompute", response_class=HTMLResponse)
async def fleets_recompute(request: Request, user: str = Depends(current_user)):
    """Recompute the kept plans (pagecache): the POST itself clears them (main.py); the page reloads to show fresh ones."""
    pagecache.clear(request.app)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.get("/fleets/details", response_class=HTMLResponse)
async def fleets_details(request: Request, user: str = Depends(current_user)):
    return await page(request, user, "fleet_details.html", "fleet_details", **await fleets_ctx(request))


def _fleet_locked(fn):
    """Run a fleet-editing route under the engine's lock: it reads, changes and saves the whole fleet list, as the
    tick does, and an edit between the tick's read and save would be lost (or a job orphaned)."""
    import functools

    @functools.wraps(fn)
    async def run(request: Request, *a, **kw):
        async with request.app.state.worker.automations.lock:
            return await fn(request, *a, **kw)
    return run


async def _fleets(request: Request) -> tuple[Any, list[dict]]:
    eng = request.app.state.worker.automations
    return eng, await eng.fleets()


@router.post("/fleets", response_class=HTMLResponse)
@_fleet_locked
async def fleets_create(request: Request, name: str = Form(...), role: str = Form("mining"), home: str = Form(""),
                        station: str = Form(""), template: str = Form(""), user: str = Depends(current_user)):
    eng, items = await _fleets(request)
    fid = fl.fleet_id_for(name, items)
    items.append({"id": fid, "name": name.strip(), "role": role if role in fl.ROLES else "mining", "home": home.strip().upper(),
                  "wants": {}, "station": station == "on", "materials": "", "template": template or None})
    await eng.save_fleets(items)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/fleets/bootstrap", response_class=HTMLResponse)
@_fleet_locked
async def fleets_bootstrap_new(request: Request, name: str = Form(...), replicant: str = Form(...),
                               user: str = Depends(current_user)):
    """A bootstrap fleet (bootstrap.py): the replicant's heaven vessel and what's aboard it, from its home system to an
    autofactory hub with mining outposts. Only uses what it starts with and prints."""
    from . import bootstrap as bt
    eng, items = await _fleets(request)
    st = await load_state(request)
    r = st["replicants"].get(replicant) or {}
    vessel = r.get("hosted_device_code")
    if not vessel:
        return HTMLResponse('<div class="result err">That replicant isn\'t in a vessel.</div>')
    if any((x.get("boot") or {}).get("vessel") == vessel for x in items):
        return HTMLResponse('<div class="result err">That vessel already runs a bootstrap.</div>')
    by = {d.get("device_code"): d for d in st["devices"]}
    v = by.get(vessel) or {}
    if v and v.get("device_type") != "heaven_vessel":   # its matrix deployed on its own: the heaven vessel beside it
        hv = next((d for d in st["devices"] if d.get("device_type") == "heaven_vessel" and d.get("location") == v.get("location")
                   and d.get("replicant_code") == replicant), None)
        if hv:
            vessel, v = hv["device_code"], hv
    home = star_of(v.get("location") or r.get("location") or r.get("current_location"))
    fid = fl.fleet_id_for(name, items)
    items.append({"id": fid, "name": name.strip(), "role": "bootstrap", "home": home, "wants": {}, "station": False,
                  "materials": "", "template": None, "boot": bt.new_state(vessel, replicant, home)})
    await eng.save_fleets(items)
    stowed = (await request.app.state.db.kv_get("stowed_map", {}) or {}).get(vessel) or []
    aboard = sorted(set(stowed) | {d["device_code"] for d in st["devices"] if d.get("stowed_in_device_code") == vessel}
                    | ({r["hosted_device_code"]} - {vessel}))   # the replicant's matrix, wherever it is
    steps = [eng._tag_step(c, [bt.family_tag(fid), fl.fleet_tag(fid)]) for c in [vessel] + aboard]
    await eng.create_job("fleets", f"{name}: bootstrap starts in {home}", vessel, steps,
                         {"devices": [vessel] + aboard, "bootstrap": fid}, force=True)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/fleets/{fid}/to-bootstrap", response_class=HTMLResponse)
@_fleet_locked
async def fleets_to_bootstrap(request: Request, fid: str, user: str = Depends(current_user)):
    eng, items = await _fleets(request)
    f = next((x for x in items if x["id"] == fid), None)
    if not f or f.get("role") == "bootstrap":
        return HTMLResponse('<div class="result err">No such fleet, or it already is a bootstrap.</div>')
    return await _to_bootstrap(request, eng, items, f, user)


async def _to_bootstrap(request: Request, eng: Any, items: list[dict], f: dict, user: str) -> HTMLResponse:
    """Convert a fleet into a bootstrap (keeping its id, so its fleet: tag still names its members): it needs a member
    vessel hosting a replicant; every member is tagged boot:<id>, and it starts from where that vessel is. Picking the
    bootstrap role in a fleet's Save row does this too."""
    from . import bootstrap as bt
    fid = f["id"]
    err = {"HX-Retarget": f"#fp-{fid}", "HX-Reswap": "innerHTML"}   # the Save row swaps nothing: show it under it
    st = await load_state(request)
    mine = fl.members(f, st["devices"])
    codes = {d.get("device_code") for d in mine}
    rep = next(((c, r) for c, r in st["replicants"].items() if r.get("hosted_device_code") in codes), None)
    # the vessel: the replicant's host, or (its matrix deployed on its own) a member heaven vessel, nearest it first
    host = next((d for d in mine if rep and d.get("device_code") == rep[1]["hosted_device_code"]), {})
    vessels = sorted((d for d in mine if d.get("device_type") == "heaven_vessel"),
                     key=lambda d: (d.get("location") != host.get("location"), d.get("device_code")))
    vessel = host.get("device_code") if host.get("device_type") == "heaven_vessel" else (vessels[0]["device_code"] if vessels else None)
    if not rep or not vessel:
        return HTMLResponse('<div class="result err">A bootstrap needs a heaven vessel and a replicant among the '
                            'fleet\'s members — add them under Add / remove devices first.</div>', headers=err)
    code, r = rep
    if any((x.get("boot") or {}).get("vessel") == vessel for x in items):
        return HTMLResponse('<div class="result err">That vessel already runs a bootstrap.</div>', headers=err)
    m = f.get("mission") or {}
    if m.get("status") in fl.AWAY:   # stopped where it is: the bootstrap carries on from there
        job = await eng._get(m["job"]) if m.get("job") else None
        if job and job["status"] in ("running", "waiting"):
            job["status"], job["finished_at"] = "cancelled", now_iso()
            await eng._update(job)
    v = next((d for d in st["devices"] if d.get("device_code") == vessel), {})
    home = star_of(v.get("location") or r.get("location") or r.get("current_location")) or f.get("home")
    f.update({"role": "bootstrap", "home": home, "station": False, "materials": "", "template": None, "wants": {},
              "boot": bt.new_state(vessel, code, home)})
    f.pop("mission", None)
    for x in items:   # nobody sends materials to it any more
        if x.get("materials") == fid:
            x["materials"] = ""
    await eng.save_fleets(items)
    steps = [eng._tag_step(c, [bt.family_tag(fid)]) for c in sorted(codes)]
    await eng.create_job("fleets", f"{f['name']}: converted to a bootstrap in {home}", vessel, steps,
                         {"devices": sorted(codes), "bootstrap": fid}, force=True)
    await eng.log("fleets", f"{f['name']} converted to a bootstrap by {user}: {len(codes)} device(s), starting in {home}")
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/fleets/{fid}/bootstrap", response_class=HTMLResponse)
async def fleets_bootstrap_control(request: Request, fid: str, user: str = Depends(current_user)):
    """Bootstrap controls: decide (your OK on the hub, an outpost, a waypoint, a replacement), pause / resume,
    settings, run a pass now, end."""
    form = await request.form()
    eng = request.app.state.worker.automations
    action = form.get("action") or ""
    if action == "decide":
        async with eng.lock:
            msg = await eng.bootstrap_decide(fid, form.get("pick") or "", form.get("kind") or "")
        return HTMLResponse(f'<div class="result ok">{html.escape(msg)}</div>', headers={"HX-Refresh": "true"})
    async with eng.lock:
        items = await eng.fleets()
        f = next((x for x in items if x["id"] == fid and x.get("role") == "bootstrap"), None)
        if not f:
            return HTMLResponse("", status_code=404)
        b = f["boot"]
        if action in ("pause", "resume"):
            b["paused"] = action == "pause"
        elif action == "settings":
            st = b.setdefault("settings", {})
            home = (form.get("home") or "").strip().upper()
            if home and home != b.get("home"):
                if b["stage"] not in ("home", "survey", "hub_choice"):
                    return HTMLResponse('<div class="result err">The hub is chosen: home moved there already.</div>')
                # a new place to start (owner 2026-10-22: it stopped in a system with no belt): compound there first
                b["home"] = f["home"] = home
                b.update(stage="home", decision=None, candidates=[])
                await eng.log("fleets", f"{f['name']}: bootstrap home set to {home} by {user}")
            for k in ("radius", "ward_hours", "min_score", "hub_miners"):
                v = (form.get(k) or "").strip()
                if v:
                    try:
                        st[k] = max(0.0, float(v))
                    except ValueError:
                        return HTMLResponse(f'<div class="result err">{k} must be a number</div>')
        elif action == "normal":
            # back to an ordinary stationed fleet, homed where most of its devices are (the vessel aside), loadout = them
            from . import bootstrap as bt
            st = await load_state(request)
            mine = [d for d in bt.members(f, st["devices"])]
            others = [d for d in mine if d.get("device_code") != b.get("vessel")]
            homes = Counter(star_of(d.get("location")) for d in others if d.get("location"))
            home = homes.most_common(1)[0][0] if homes else (f.get("home") or b.get("home"))
            for k in ("boot",):
                f.pop(k, None)
            role = form.get("role") if form.get("role") in fl.ROLES else "mining"
            f.update({"role": role, "home": home, "station": True, "template": None,
                      "wants": dict(Counter(d.get("device_type") for d in others if d.get("device_type")))})
            for x in items:
                if x.get("parent") == fid:
                    x.pop("parent", None)
            steps = [eng._tag_step(d["device_code"], [fl.fleet_tag(fid)], [bt.family_tag(fid)]) for d in mine]
            await eng.save_fleets(items)
            await eng.create_job("fleets", f"{f['name']}: back to a normal fleet in {home}", None, steps,
                                 {"devices": [d["device_code"] for d in mine]}, force=True)
            await eng.log("fleets", f"{f['name']} is a normal stationed fleet again, home {home} ({len(mine)} device(s))")
            return HTMLResponse("", headers={"HX-Refresh": "true"})
        elif action == "end":
            items = [x for x in items if x["id"] != fid]
            for x in items:
                if x.get("parent") == fid:
                    x.pop("parent", None)
        await eng.save_fleets(items)
    if action == "run":
        async with eng.lock:
            lines = await eng.bootstrap_pass(only=fid)
        return HTMLResponse(f'<div class="result ok small">{html.escape("; ".join(lines) or "nothing to do right now")}</div>',
                            headers={"HX-Refresh": "true"})
    return HTMLResponse("", headers={"HX-Refresh": "true"})


async def rename_fleet(request: Request, f: dict, items: list[dict], name: str) -> None:
    """A renamed fleet gets the id (and fleet: tag) of its new name. Everything holding the old id follows: other
    fleets' materials, queued print orders, running jobs, the stored device list (devices show the new tag at once,
    marked for retagging) — and the engine's rename pass changes the tags in the game (fleet_rename_pass)."""
    old = f["id"]
    new = fl.fleet_id_for(name, items, keep=old)
    if new == old or fl.fleet_tag(new) == fl.fleet_tag(old):
        return
    db, eng = request.app.state.db, request.app.state.worker.automations
    f["id"] = new
    for x in items:
        if x.get("materials") == old:
            x["materials"] = new
    renames = await db.kv_get(fl.RENAMES_KV, {}) or {}
    renames = {k: (new if v == old else v) for k, v in renames.items()}
    renames[old] = new
    renames.pop(new, None)
    await db.kv_set(fl.RENAMES_KV, renames)
    orders = await db.kv_get("loadout_orders", []) or []
    for o in orders:
        if o.get("fleet") == old:
            o["fleet"] = new
    await db.kv_set("loadout_orders", orders)
    jobs = await db.kv_get("automation_jobs", []) or []
    for j in jobs:
        if (j.get("meta") or {}).get("fleet") == old:
            j["meta"]["fleet"] = new
    await db.kv_set("automation_jobs", jobs)
    devices = await db.kv_get("devices", []) or []
    n = fl.apply_renames(devices, renames)
    await db.kv_set("devices", devices)
    await eng.log("fleets", f"fleet {old} renamed to {name}: its tag becomes {fl.fleet_tag(new)} ({n} device(s) to retag)")


@router.post("/fleets/{fid}/edit", response_class=HTMLResponse)
@_fleet_locked
async def fleets_edit(request: Request, fid: str, user: str = Depends(current_user)):
    form = await request.form()
    eng, items = await _fleets(request)
    f = next((x for x in items if x["id"] == fid), None)
    if not f:
        return HTMLResponse("", status_code=404)
    if form.get("delete") == "1":
        items = [x for x in items if x["id"] != fid]
        # its devices drop the fleet's tag (and any to:<fleet> sending one to it): they're fleetless, free to be used again
        tag, to_fleet = fl.fleet_tag(fid), "to:" + re.sub(r"[^a-z0-9\-_:.]", "", fid.lower())[:29]
        steps = [auto.step(f"{d['device_code']} leaves {f['name']} (fleet deleted)", f"/devices/{d['device_code']}",
                           {"configuration": {"remove_tags": [t for t in (tag, to_fleet) if t in (d.get("tags") or [])]}},
                           method="PATCH")
                 for d in (await load_state(request))["devices"] if {tag, to_fleet} & set(d.get("tags") or [])]
        if steps:
            async with eng.lock:
                await eng.create_job("fleets", f"fleet {f['name']} deleted: {len(steps)} device(s) untagged", None, steps,
                                     {"devices": []}, force=True)
    else:
        new_name = (form.get("name") or f["name"]).strip()
        if new_name != f["name"]:
            await rename_fleet(request, f, items, new_name)
        f["name"] = new_name
        if form.get("role") == "bootstrap" and f.get("role") != "bootstrap":
            return await _to_bootstrap(request, eng, items, f, user)
        old_home = f.get("home")
        f["home"] = (form.get("home") or f["home"]).upper()
        f["role"] = form.get("role") if form.get("role") in fl.ROLES else f["role"]
        if old_home and f["home"] != old_home and f.get("station"):   # a stationed fleet's new home: move together if it can
            how = await eng.start_relocation(f, old_home, f["home"])
            await eng.log("fleets", f"{f['name']}: new home {f['home']} (was {old_home}) — {how}")
        wants = None
        if "template" in form:
            f["template"] = form.get("template") or None
            if f["template"]:
                fl.resolve_template(f, await eng.loadout_cfg())
        if f.get("template"):
            pass   # the loadout comes from the template
        elif form.get("lines"):   # the loadout editor: parallel type / qty lists, blank or 0 lines ignored
            wants, stock = {}, {}
            stocks = form.getlist("stock")
            for i, (t, q) in enumerate(zip(form.getlist("type"), form.getlist("qty"))):
                t = (t or "").strip()
                try:
                    q = int(str(q).strip() or 0)
                except ValueError:
                    q = 0
                try:   # keep in stock (stock.py): printed as spares at home beyond the loadout
                    k = int(str(stocks[i] if i < len(stocks) else "").strip() or 0)
                except ValueError:
                    k = 0
                if t and q > 0:
                    wants[t] = wants.get(t, 0) + q
                if t and k > 0:
                    stock[t] = stock.get(t, 0) + k
            if stocks:
                f["stock"] = stock
        elif any(k.startswith("want:") for k in form.keys()):   # older bulk form / API use
            wants = {}
            for k, v in form.multi_items():
                if k.startswith("want:") and k[5:] and str(v).strip():
                    try:
                        if int(v) > 0:
                            wants[k[5:]] = int(v)
                    except ValueError:
                        pass
        if wants is not None:
            f["wants"] = wants
            gone = {t for t in form.getlist("release") if t and t not in wants}
            if gone:
                st = await load_state(request)
                tag = fl.fleet_tag(fid)
                steps = [auto.step(f"{d['device_code']} leaves {fid}", f"/devices/{d['device_code']}",
                                   {"configuration": {"remove_tags": [tag]}}, method="PATCH")
                         for d in fl.members(f, st["devices"]) if d.get("device_type") in gone]
                if steps:
                    async with eng.lock:
                        await eng.create_job("fleets", f"fleet {fid}: {', '.join(sorted(gone))} removed ({len(steps)} device(s) released)",
                                             None, steps, {"devices": []}, force=True)
        if form.get("autosave") and form.get("delete") != "1":
            await eng.save_fleets(items)
            n = sum((f.get("wants") or {}).values())
            return HTMLResponse(f'<span class="muted">Loadout saved · {len(f.get("wants") or {})} type(s), {n} device(s)</span>')
    await eng.save_fleets(items)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/fleets/{fid}/station", response_class=HTMLResponse)
@_fleet_locked
async def fleets_station(request: Request, fid: str, user: str = Depends(current_user)):
    """Stationed (kept at its loadout in its home system) and materials: '' none, 'self' takes materials in, or the id of
    the fleet this one's home system ferries its stockpile to."""
    form = await request.form()
    eng, items = await _fleets(request)
    f = next((x for x in items if x["id"] == fid), None)
    if not f:
        return HTMLResponse("", status_code=404)
    f["station"] = form.get("station") == "on"
    m = (form.get("materials") or "").strip()
    f["materials"] = m if m in ("self", "supply") or any(x["id"] == m and x["id"] != fid for x in items) else ""
    if "supplied_from" in form:   # a hub whose autofactories and carriers may supply this fleet at any distance
        sup = (form.get("supplied_from") or "").strip()
        f["supplied_from"] = sup if any(x["id"] == sup and x["id"] != fid for x in items) else ""
    await eng.save_fleets(items)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/fleets/{fid}/owner", response_class=HTMLResponse)
@_fleet_locked
async def fleets_owner(request: Request, fid: str, owner: str = Form(""), keep_owner: str = Form(""), now: str = Form(""),
                       user: str = Depends(current_user)):
    """The replicant that should own every device in the fleet; `keep_owner` re-checks it every few minutes; `now`
    transfers the members it doesn't own yet straight away."""
    eng, items = await _fleets(request)
    f = next((x for x in items if x["id"] == fid), None)
    if not f:
        return HTMLResponse("", status_code=404)
    reps = (await load_state(request))["replicants"]
    f["owner"] = owner if owner in reps else ""
    f["keep_owner"] = keep_owner == "on" and bool(f["owner"])
    await eng.save_fleets(items)
    if now and f["owner"]:
        async with eng.lock:
            lines = await eng.fleet_owners(force=True, only=fid)
        msg = "<br>".join(html.escape(x) for x in lines) or "Every member already belongs to that replicant."
        return HTMLResponse(f'<div class="result ok">{msg}</div>', headers={"HX-Trigger": "fleet-owner"})
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/fleets/{fid}/want", response_class=HTMLResponse)
@_fleet_locked
async def fleets_want(request: Request, fid: str, user: str = Depends(current_user)):
    """Set one loadout line. A new type defaults to 1; qty 0 removes the type completely — the line goes
    and any member devices of that type leave the fleet (their fleet tag is removed)."""
    form = await request.form()
    eng, items = await _fleets(request)
    f = next((x for x in items if x["id"] == fid), None)
    t = (form.get("type") or "").strip()
    if not f or not t:
        return HTMLResponse("", status_code=404 if not f else 400)
    raw = str(form.get("qty") if form.get("qty") is not None else "1").strip()
    try:
        qty = int(raw or 0)
    except ValueError:
        qty = 1
    wants = dict(f.get("wants") or {})
    if qty > 0:
        wants[t] = qty
    else:
        wants.pop(t, None)
        st = await load_state(request)
        tag = fl.fleet_tag(fid)
        steps = [auto.step(f"{d['device_code']} leaves {fid}", f"/devices/{d['device_code']}",
                           {"configuration": {"remove_tags": [tag]}}, method="PATCH")
                 for d in fl.members(f, st["devices"]) if d.get("device_type") == t]
        if steps:
            async with eng.lock:
                await eng.create_job("fleets", f"fleet {fid}: {t} removed ({len(steps)} device(s) released)",
                                     None, steps, {"devices": []}, force=True)
    f["wants"] = wants
    await eng.save_fleets(items)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/fleets/{fid}/members", response_class=HTMLResponse)
async def fleets_members(request: Request, fid: str, user: str = Depends(current_user)):
    """Add (tag) or remove (untag) devices. Fleet devices drop their home:/spare/to: tags — the fleet owns them."""
    form = await request.form()
    tag = fl.fleet_tag(fid)
    st = await load_state(request)
    by = {d.get("device_code"): d for d in st["devices"]}
    eng = request.app.state.worker.automations
    steps = []
    for code in form.getlist("add"):
        d = by.get(code) or {}
        if tag in (d.get("tags") or []):
            continue   # already in this fleet (sending its tag in both add and remove is refused)
        rem = [t for t in d.get("tags") or [] if (t.startswith(("home:", "to:", "fleet:")) or t == "spare") and t != tag]
        steps.append(auto.step(f"{code} joins {fid}", f"/devices/{code}",
                               {"configuration": {"add_tags": [tag], **({"remove_tags": rem} if rem else {})}}, method="PATCH"))
    for code in form.getlist("remove"):
        steps.append(auto.step(f"{code} leaves {fid}", f"/devices/{code}", {"configuration": {"remove_tags": [tag]}}, method="PATCH"))
    if not steps and form.getlist("add"):
        return HTMLResponse('<div class="result">Those devices are already in this fleet.</div>')
    if not steps:
        return HTMLResponse('<div class="result err">Pick at least one device.</div>')
    async with eng.lock:
        await eng.create_job("fleets", f"fleet {fid}: {len(steps)} membership change(s)", None, steps, {"devices": []}, force=True)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


def _int(v, default: int, lo: int, hi: int) -> int:
    """A form number, clamped; the default when it isn't one."""
    try:
        return max(lo, min(hi, int(float(v))))
    except (TypeError, ValueError):
        return default


async def known_stars(request: Request) -> set[str]:
    """Every system the app knows: the catalog (census stars included), scanned systems, where devices are."""
    db = request.app.state.db
    cat = await db.kv_get("stars", {}) or {}
    out = {s.get("designation") for s in cat.get("stars") or [] if isinstance(s, dict) and s.get("designation")}
    out |= {r["star"] for r in await db.fetchall("SELECT star FROM systems")}
    out |= {star_of(d.get("location")) for d in (await load_state(request))["devices"] if d.get("location")}
    out |= {f.get("home") for f in await request.app.state.worker.automations.fleets() if f.get("home")}
    return out - {"", None}


@router.post("/fleets/{fid}/mission", response_class=HTMLResponse)
@_fleet_locked
async def fleets_mission(request: Request, fid: str, user: str = Depends(current_user)):
    form = await request.form()
    eng, items = await _fleets(request)
    f = next((x for x in items if x["id"] == fid), None)
    if not f:
        return HTMLResponse("", status_code=404)
    if (f.get("mission") or {}).get("status") == "running":
        return HTMLResponse('<div class="result err">This fleet is already on a mission — recall or stop it first.</div>')
    if f.get("role") == "logistics":
        return HTMLResponse('<div class="result err">A logistics fleet takes no missions: it runs the logistics service (Fleets › Logistics).</div>')
    targets = [t.strip().upper() for t in re.split(r"[,\s]+", form.get("targets") or "") if t.strip()]
    m = {"status": "running", "phase": None, "idx": 0, "targets": targets, "started_at": now_iso(), "log": [],
         "opts": {"deliver": form.get("deliver") == "on", "exhausted_minutes": _int(form.get("exhausted_minutes"), 30, 1, 24 * 60)}}
    if f["role"] == "trade":
        try:
            if form.get("contract"):
                m["contract"] = json.loads(form.get("contract"))
                m["targets"] = [star_of(m["contract"].get("location"))]
            else:
                m["trade"] = json.loads(form.get("trade") or "{}")
                m["targets"] = [m["trade"].get("star") or star_of(m["trade"].get("location"))]
        except ValueError:
            m["targets"] = []
        if not m["targets"] or not m["targets"][0]:
            return HTMLResponse('<div class="result err">Pick a contract or a trade.</div>')
    if not m["targets"] or not m["targets"][0]:
        return HTMLResponse('<div class="result err">Give the mission a target system.</div>')
    if f["role"] == "mining":
        m["targets"] = m["targets"][:1]
    # nothing leaves short: the whole loadout must be in its home system (aboard, there, or arriving) — or Launch anyway
    st_r = await load_state(request)
    ready = fl.readiness(f, st_r["devices"])
    if not ready["ready"]:
        if form.get("partial") != "on":
            return HTMLResponse(f'<div class="result err">{html.escape(fl.readiness_text(ready))}. Use <b>Fill from spares</b> or '
                                '<b>Print what\'s missing</b> (the loadout pass also fills a stationed fleet), or tick '
                                '<b>Launch anyway</b> to send it short.</div>')
        m["opts"]["partial"] = True
        eng._mlog(m, f"launched short on purpose: {fl.readiness_text(ready)}")
    # seen live 2026-10-06: a typo (LORALEL for LORALAEL) stalled a mission with "Unknown star designation"
    known = await known_stars(request)
    bad = [t for t in m["targets"] if f["role"] != "trade" and star_of(t) not in known]
    if bad and known:
        import difflib
        hints = []
        for t in bad:
            close = difflib.get_close_matches(star_of(t), sorted(known), n=3, cutoff=0.6)
            hints.append(f"{t}" + (f" (did you mean {' / '.join(close)}?)" if close else ""))
        return HTMLResponse(f'<div class="result err">Unknown system: {html.escape(", ".join(hints))}. Pick one from the list — '
                            'stars beyond the catalog need a census first (Map › Stars).</div>')
    if f["role"] in ("mining", "explore"):
        homes = fl.worked_systems(items)
        taken = [t for t in m["targets"] if star_of(t) in homes]
        if taken:
            return HTMLResponse(f'<div class="result err">{html.escape(", ".join(taken))}: '
                                f'{"has" if len(taken) == 1 else "have"} a stationed fleet, and only the stationed fleet works '
                                'its home system. Pick a system no fleet is stationed in.</div>')
    # another player's system ward: no mission there (a trade only visits the deal's site, so it may go)
    from . import wards
    st0 = await load_state(request)
    warded = sorted({star_of(t) for t in m["targets"] if t}
                    & wards.foreign(await request.app.state.db.kv_get("stars", {}) or {}, st0["devices"]))
    if warded and f["role"] != "trade":
        return HTMLResponse(f'<div class="result err">{html.escape(", ".join(warded))}: another player\'s system ward or hub is '
                            'there, so nothing of yours can mine it. Pick another system (Systems › Mining prospects).</div>')
    if m.get("contract"):   # devices the contract asks for: a trade run carries resources only
        from . import gameevents as gev
        e = (await gev.load(request.app.state.db)).get(m["contract"].get("designation") or "")
        if e:
            prog = gev.progress(e, {}, st0["devices"], st0["replicants"])
            miss = {x["device_type"]: x["short_here"] for x in (prog.get("best") or {}).get("devices") or [] if x["short_here"]}
            if miss:
                w = ("this contract also asks for devices at " + e.get("location", "?") + ": "
                     + ", ".join(f"{n}× {t}" for t, n in miss.items())
                     + " — the run waits at the site until they're there; they're delivered from spares automatically, "
                       "and printing any no spare covers needs your authorization on the Contracts page")
                eng._mlog(m, "warning: " + w)
                await eng.log("fleets", f"{f['name']}: {w}", "alert", notify=True)
    if warded and m.get("contract"):   # species interaction lock: other players can't complete location events there
        return HTMLResponse(f'<div class="result err">{html.escape(", ".join(warded))}: another player\'s system ward or hub is there, and '
                            "its species interaction lock stops anyone else completing the civilization's events. "
                            'Trades with traders are still fine.</div>')
    if warded:
        eng._mlog(m, f"warning: another player's ward or hub in {', '.join(warded)} — fine for a trade, but nothing can mine there")
    # out of relay range only a replicant riding with the fleet keeps it under command
    from . import outposts as _op
    member_codes = {d["device_code"] for d in fl.members(f, st0["devices"])}
    aboard = [r.get("name") or c for c, r in st0["replicants"].items() if r.get("hosted_device_code") in member_codes]
    dark = [t for t in dict.fromkeys(star_of(x) for x in m["targets"] if x) if not _op.deployed_in(st0["devices"], t, "relay")]
    if dark and not aboard:
        w = (f"no relay of yours in {', '.join(dark)} and no replicant rides with the fleet — devices there may be out of "
             "control range; add a vessel hosting a replicant to the fleet")
        eng._mlog(m, "warning: " + w)
        await eng.log("fleets", f"{f['name']}: {w}", "alert", notify=True)
    elif aboard:
        eng._mlog(m, f"replicant aboard: {', '.join(aboard)}")
    f["mission"] = m
    await eng.save_fleets(items)
    async with eng.lock:
        await eng.run_fleets()
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/fleets/{fid}/control", response_class=HTMLResponse)
@_fleet_locked
async def fleets_control(request: Request, fid: str, action: str = Form(...), user: str = Depends(current_user)):
    eng, items = await _fleets(request)
    f = next((x for x in items if x["id"] == fid), None)
    if f and action == "partial":   # Launch anyway: a mission waiting to leave goes short; a pending move goes now
        m = f.get("mission") or {}
        if m.get("status") == "running" and not m.get("departed"):
            m.setdefault("opts", {})["partial"] = True
            eng._mlog(m, f"sent short on purpose by {user}")
        if f.get("pending_home"):
            f["move_partial"] = True
        await eng.save_fleets(items)
        await eng.log("fleets", f"{f['name']}: sent short on purpose by {user}")
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    if f and action == "end" and not f.get("mission"):
        f["mission"] = {"status": "ended", "targets": [], "log": []}   # no mission yet: just gather everyone aboard
    m = (f or {}).get("mission") or {}
    if not f or not m:
        return HTMLResponse("", status_code=404)
    async with eng.lock:
        if m.get("job"):
            await eng.cancel(m["job"])
        if action == "end":
            m["status"], m["end_here"] = "running", True
            m.pop("end_started", None)
            eng._mlog(m, "end mission ordered: recall everything aboard the carriers and stay here")
        elif action == "recall":
            m["status"], m["phase"] = "running", ("work" if f["role"] != "trade" else "trade")
            if f["role"] == "explore":
                m["targets"] = (m.get("targets") or [])[:m.get("idx", 0) + 1]  # no further targets
            eng._mlog(m, "recall ordered")
        elif action == "resume" and m.get("end_here"):
            m["status"] = "running"
            m.pop("end_started", None)   # re-run the recall & board
            eng._mlog(m, "resumed (end mission & board)")
        elif action == "resume":
            m["status"] = "running"
            # re-run the phase that stalled
            ph = fl.PHASES[f["role"]]
            cur = m.get("phase")
            m["phase"] = ph[ph.index(cur) - 1] if cur in ph and ph.index(cur) > 0 else None
            if m["phase"] == "watch":
                m["recall_now"] = True   # re-run the recall itself, not the wait before it (seen live 2026-10-11: Surveyors)
            eng._mlog(m, "resumed")
        elif action == "stop":
            m["status"] = "stopped"
            eng._mlog(m, "stopped (devices stay where they are)")
        elif action == "station":
            m["status"] = "done"
            eng._mlog(m, "back to station: the loadout pass brings its devices home" if f.get("station")
                      else "mission cleared")
        m["job"] = None
        await eng.save_fleets(items)
        await eng.run_fleets()
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/fleets/{fid}/auto-deals", response_class=HTMLResponse)
@_fleet_locked
async def fleets_auto_deals(request: Request, fid: str, user: str = Depends(current_user)):
    """A trade fleet's 'auto-fulfil contracts' / 'auto-fulfil trades' checkboxes."""
    form = await request.form()
    eng, items = await _fleets(request)
    f = next((x for x in items if x["id"] == fid), None)
    if not f or f.get("role") != "trade":
        return HTMLResponse("", status_code=404)
    f["auto_contracts"] = form.get("auto_contracts") == "on"
    f["auto_trades"] = form.get("auto_trades") == "on"
    await eng.save_fleets(items)
    on = [x for x, k in (("contracts", "auto_contracts"), ("trades", "auto_trades")) if f[k]]
    return HTMLResponse(f'<span class="small {"lv-done" if on else "muted"}">'
                        + (f"Picks up {' and '.join(on)} on its own when it's free." if on else "Off.") + "</span>")


@router.post("/fleets/{fid}/path", response_class=HTMLResponse)
@_fleet_locked
async def fleets_path(request: Request, fid: str, user: str = Depends(current_user)):
    """A stationed mining fleet's path forward (pathing.py): heading, cone, auto-relocate, auto-prospect."""
    from . import pathing as pa
    form = await request.form()
    eng, items = await _fleets(request)
    f = next((x for x in items if x["id"] == fid), None)
    if not f or f.get("role") not in ("mining", "explore"):
        return HTMLResponse("", status_code=404)
    cat = await request.app.state.db.kv_get("stars", {}) or {}
    pos = {s.get("designation"): s.get("position") for s in cat.get("stars") or [] if isinstance(s, dict)}
    kind = form.get("heading_kind") or "keep"
    # the form shows the saved heading: the same choice sent back unchanged keeps the saved vector (it's fixed when set,
    # so re-saving the cone mustn't recompute "outward" from a home the fleet has since moved to)
    h = f.get("heading") or {}
    same = (h and kind == h.get("kind") and (kind != "star" or (form.get("heading_star") or "").strip().upper() == h.get("star"))
            and (kind != "custom" or [round(float(x), 4) for x in re.split(r"[,\s]+", form.get("heading_vec") or "") if x]
                 == [round(float(x), 4) for x in h.get("vector") or []]))
    if same or (kind == "none" and not h):
        kind = "keep"
    try:
        if kind == "none":
            f.pop("heading", None)
        elif kind == "outward":
            f["heading"] = {"vector": pa.heading_from("outward", pa.xyz(pos.get(f.get("home")))),
                            "label": f"outward from Sol through {f.get('home')}", "kind": "outward"}
        elif kind == "star":
            tgt = (form.get("heading_star") or "").strip().upper()
            f["heading"] = {"vector": pa.heading_from("star", pa.xyz(pos.get(f.get("home"))), pa.xyz(pos.get(tgt))),
                            "label": f"from {f.get('home')} toward {tgt}", "kind": "star", "star": tgt}
        elif kind == "custom":
            vec = [x for x in re.split(r"[,\s]+", form.get("heading_vec") or "") if x]
            f["heading"] = {"vector": pa.heading_from("custom", None, custom=vec), "label": "custom", "kind": "custom"}
    except ValueError as e:
        return HTMLResponse(f'<span class="lv-alert small">{html.escape(str(e))}</span>')
    f["cone"] = _int(form.get("cone"), pa.DEFAULT_CONE, 10, 180)
    if f.get("role") == "explore":   # advancing explorer: each finished system's farthest star ahead becomes home
        f["advance"] = form.get("advance") == "on"
        try:
            f["hop_ly"] = max(1.0, min(100.0, float(form.get("hop_ly") or 30)))
        except ValueError:
            f["hop_ly"] = 30.0
        if f["advance"]:
            f["station"] = True   # the loadout pass carries a stationed fleet to its new home
        await eng.save_fleets(items)
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    f["auto_relocate"] = form.get("auto_relocate") == "on"
    f["auto_prospect"] = form.get("auto_prospect") == "on"
    if not f["auto_relocate"]:
        for k in ("next_home", "next_home_at", "depleted_since"):
            f.pop(k, None)
    await eng.save_fleets(items)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/fleets/{fid}/next-home", response_class=HTMLResponse)
@_fleet_locked
async def fleets_next_home(request: Request, fid: str, action: str = Form(...), user: str = Depends(current_user)):
    """A planned move: cancel it (the fleet stays; it chooses again only after the home has been dry for a while
    longer) or carry it out now."""
    eng, items = await _fleets(request)
    f = next((x for x in items if x["id"] == fid), None)
    if not f or not f.get("next_home"):
        return HTMLResponse("", status_code=404)
    if action == "cancel":
        f["path_note"] = f"move to {f['next_home']} canceled by {user}"
        f.pop("next_home", None), f.pop("next_home_at", None)
        f["depleted_since"] = now_iso()   # wait out the dry period again before choosing
    elif action == "now":
        f["next_home_at"] = now_iso()
    await eng.save_fleets(items)
    if action == "now":
        await eng.pathing_pass(force=True)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/fleets/{fid}/auto-scout", response_class=HTMLResponse)
@_fleet_locked
async def fleets_auto_scout(request: Request, fid: str, user: str = Depends(current_user)):
    """An explore fleet's 'auto-scout prospects' checkbox and how many systems a run visits."""
    form = await request.form()
    eng, items = await _fleets(request)
    f = next((x for x in items if x["id"] == fid), None)
    if not f or f.get("role") != "explore":
        return HTMLResponse("", status_code=404)
    f["auto_scout"] = form.get("auto_scout") == "on"
    try:
        f["scout_count"] = max(1, min(10, int(form.get("scout_count") or 3)))
    except ValueError:
        f["scout_count"] = 3
    await eng.save_fleets(items)
    return HTMLResponse(f'<span class="small {"lv-done" if f["auto_scout"] else "muted"}">'
                        + ("On: scouts the nearest unsurveyed systems when it's free." if f["auto_scout"] else "Off.")
                        + "</span>")


@router.post("/fleets/traders", response_class=HTMLResponse)
async def fleets_traders(request: Request, user: str = Depends(current_user)):
    """Refresh the trader directory (every replicant's) and each trader's trades (one request per trader, at most 20)."""
    st = await load_state(request)
    api = request.app.state.api
    if not st["replicants"]:
        return HTMLResponse('<span class="lv-alert small">No replicant to ask.</span>')
    from .shop import refresh_directory
    n, err = await refresh_directory(request.app.state.db, api, list(st["replicants"]))
    if n:
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    asked = len(st["replicants"])
    if err:
        return HTMLResponse(f'<span class="lv-alert small">Asked {asked} replicants, no traders: {html.escape(err)}</span>')
    return HTMLResponse(f'<span class="muted small">Asked {asked} replicants: the game lists no traders for any of them '
                        '(only shops in their own systems, or where the owner has an FTL relay and you have one).</span>')


@router.post("/fleets/{fid}/fill", response_class=HTMLResponse)
async def fleets_fill(request: Request, fid: str, user: str = Depends(current_user)):
    """Fill this fleet's gaps from spares now (the same as the Fill unstationed fleets rule, for one fleet)."""
    eng = request.app.state.worker.automations
    async with eng.lock:
        lines = await eng.fill_fleets(force=True, only=fid)
    if not lines:
        return HTMLResponse('<div class="result ok">Nothing to fill (full loadout, on a mission, or already busy).</div>')
    return HTMLResponse('<div class="result ok">' + "<br>".join(html.escape(x) for x in lines) + "</div>")


# --- reset & reform -----------------------------------------------------------------------------------
async def reform_plan(request: Request, full: bool) -> dict:
    from . import reform
    eng = request.app.state.worker.automations
    st = await load_state(request)
    hosts = {r.get("hosted_device_code") for r in st["replicants"].values() if r.get("hosted_device_code")}
    return reform.plan(await eng.loadout_cfg(), st["devices"], await eng.fleets(), eng.busy_devices(await eng.jobs()),
                       hosts, full=full)


@router.get("/fleets/reform", response_class=HTMLResponse)
async def fleets_reform_page(request: Request, user: str = Depends(current_user)):
    return await page(request, user, "reform.html", "reform")


@router.post("/fleets/reform/preview", response_class=HTMLResponse)
async def fleets_reform_preview(request: Request, user: str = Depends(current_user)):
    form = await request.form()
    return partial(request, "partials/reform_plan.html", p=await reform_plan(request, form.get("full") == "on"))


@router.post("/fleets/reform/apply", response_class=HTMLResponse)
async def fleets_reform_apply(request: Request, user: str = Depends(current_user)):
    """Re-plan from the current state (it may have moved since the preview) and send it as one job."""
    from . import reform
    form = await request.form()
    eng = request.app.state.worker.automations
    async with eng.lock:
        p = await reform_plan(request, form.get("full") == "on")
        steps = reform.steps(p)
        if not steps:
            return HTMLResponse('<div class="result ok">Nothing to change: every tag and controller already matches.</div>')
        codes = sorted({r["code"] for r in p["retag"]} | {c for ds in p["releases"].values() for c in ds})
        job = await eng.create_job("reform", f"reset & reform: {len(p['retag'])} retag(s), "
                                             f"{sum(len(v) for v in p['releases'].values())} release(s)", None, steps,
                                   {"devices": codes}, force=True)
        await eng.log("reform", f"reset & reform started by {user}: {len(p['retag'])} device(s) retagged, "
                                f"{sum(len(v) for v in p['releases'].values())} drone(s) released"
                                + (" (full)" if p["full"] else ""))
    return HTMLResponse(f'<div class="result ok">Started job <a href="/automations">{html.escape(job["title"]) if job else "?"}</a>. '
                        "The next loadout pass and the fleets' own phases adopt released drones again.</div>")


@router.post("/fleets/{fid}/print", response_class=HTMLResponse)
@_fleet_locked
async def fleets_print(request: Request, fid: str, user: str = Depends(current_user)):
    """Queue the fleet's shortfall on an autofactory (home system first); prints come out already in the fleet."""
    eng, items = await _fleets(request)
    f = next((x for x in items if x["id"] == fid), None)
    st = await load_state(request)
    short = fl.short_list(f, st["devices"]) if f else {}
    if not short:
        return HTMLResponse('<div class="result ok">Nothing missing.</div>')
    facs = [d for d in st["devices"] if "enqueue_print" in (d.get("available_commands") or [])]
    facs.sort(key=lambda d: (star_of(d.get("location")) != f["home"], d.get("device_code")))
    if not facs:
        return HTMLResponse('<div class="result err">No autofactory to print on.</div>')
    # every autofactory in the chosen system shares the prints, evenly by print time
    peers = [d for d in facs if star_of(d.get("location")) == star_of(facs[0].get("location"))]
    bps = {b["device_type"]: b for b in normalize_blueprints(await request.app.state.db.kv_get("blueprints", []))}
    load = {d["device_code"]: printqueue.load_seconds(d, bps) for d in peers}
    steps = []
    for t, n in sorted(short.items(), key=lambda tn: -float((bps.get(tn[0]) or {}).get("print_time") or 0)):
        for fac, k in printqueue.split(peers, t, n, bps, load):
            steps.append(auto.step(f"print {k}× {t} on {fac['device_code']} for {f['name']}", f"/devices/{fac['device_code']}",
                                   {"command": "enqueue_print", "device_type": t, "quantity": k, "tags": [fl.fleet_tag(fid)]}))
    return await start_chain(request, user, f"fleet {f['name']}: print {fl.summarize(short)}", steps[0], steps[1:],
                             facs[0]["device_code"])
