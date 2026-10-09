"""Map › Trail: follow another replicant (Bill) through the audit logs of their public FTL beacons — see trail.py."""
from __future__ import annotations

import html

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from .api import ApiError
from .db import now_iso
from .web import current_user, f_duration, load_state, page, run_action, star_of
from . import trail as tl

router = APIRouter()


async def _state(db) -> dict:
    return tl.normalize(await db.kv_get(tl.KV, {}) or {})


def _positions(cat: dict) -> dict:
    return {s.get("designation"): s.get("position") for s in (cat or {}).get("stars") or []
            if isinstance(s, dict) and s.get("designation") and s.get("position")}


def _msg(text: str, ok: bool = True) -> HTMLResponse:
    return HTMLResponse(f'<div class="result {"ok" if ok else "err"}">{html.escape(text)}</div>',
                        headers={"HX-Refresh": "true"} if ok else {})


@router.get("/trail", response_class=HTMLResponse)
async def trail_page(request: Request, user: str = Depends(current_user)):
    db = request.app.state.db
    s = await _state(db)
    st = await load_state(request)
    pos = _positions(await db.kv_get("stars", {}) or {})
    legs = tl.legs(s["audit"], s["beacons"], s.get("target_code"), pos)
    last = tl.last_seen(s["audit"], s["beacons"], s.get("target_code"))
    reps = [{"code": c, "name": r.get("name") or c, "star": star_of(r.get("location") or r.get("current_location"))}
            for c, r in st["replicants"].items()]
    scanner = next((r for r in reps if r["code"] == s.get("scanner")), None)
    return await page(request, user, "trail.html", "trail", s=s, legs=legs, last=last, reps=reps, n_pos=len(pos),
                      known_stars=sorted(pos), scanner=scanner)


@router.post("/trail/target", response_class=HTMLResponse)
async def trail_target(request: Request, name: str = Form(...), user: str = Depends(current_user)):
    """Look the replicant up in the public directory (GET /replicants?name=…)."""
    db, api = request.app.state.db, request.app.state.api
    name = name.strip()
    try:
        found = ((await api.get("/replicants", name=name, limit=20)) or {}).get("replicants") or []
    except ApiError as e:
        return _msg(f"Directory lookup failed: {e.message}", ok=False)
    exact = [r for r in found if str(r.get("name") or "").lower() == name.lower()]
    pick = (exact or found or [None])[0]
    if not pick:
        return _msg(f"No replicant called {name} in the directory.", ok=False)
    s = await _state(db)
    if s.get("target_code") != pick.get("replicant_code"):   # someone else: their beacon logs start afresh
        s["audit"], s["read_at"] = {}, {}
    s.update({"target_name": pick.get("name") or name, "target_code": pick.get("replicant_code"),
              "target_last_location": pick.get("last_location"), "target_npc": pick.get("is_npc")})
    s["matches"] = [{"name": r.get("name"), "code": r.get("replicant_code"), "last": r.get("last_location")} for r in found[:10]]
    await db.kv_set(tl.KV, s)
    return _msg(f"Following {s['target_name']} ({s['target_code']}).")


@router.post("/trail/scan", response_class=HTMLResponse)
async def trail_scan(request: Request, replicant: str = Form(...), user: str = Depends(current_user)):
    """Find the target's beacons among the other devices in a replicant's current system (GET /scan/devices)."""
    db, api = request.app.state.db, request.app.state.api
    s = await _state(db)
    s["scanner"] = replicant          # remembered: the vessel that scanned is the one the travel buttons send
    try:
        found = await tl.scan_for_beacons(api, replicant, s)
    except ApiError as e:
        return _msg(f"Scan failed: {e.message}", ok=False)
    await db.kv_set(tl.KV, s)
    if not found:
        return _msg(f"No beacon of {s['target_name']} in that replicant's system.", ok=False)
    return _msg(f"Found {len(found)} beacon(s): " + ", ".join(f"{b['code']} at {b['location']}" for b in found))


@router.post("/trail/beacon", response_class=HTMLResponse)
async def trail_beacon(request: Request, code: str = Form(...), star: str = Form(""), remove: str = Form(""),
                       user: str = Depends(current_user)):
    db = request.app.state.db
    s = await _state(db)
    code = code.strip().upper()
    if remove:
        s["beacons"].pop(code, None)
        s["audit"].pop(code, None)
        await db.kv_set(tl.KV, s)
        return _msg(f"Removed {code}.")
    s["last_beacon"] = {"code": code, "star": star.strip().upper()}
    s["beacons"][code] = {**s["beacons"].get(code, {}), "code": code, "star": star.strip().upper() or None,
                          "found_at": now_iso()}
    await db.kv_set(tl.KV, s)
    return await trail_read(request, beacon=code, user=user)


@router.post("/trail/read", response_class=HTMLResponse)
async def trail_read(request: Request, beacon: str = Form(""), user: str = Depends(current_user)):
    db, api = request.app.state.db, request.app.state.api
    s = await _state(db)
    if not s["beacons"]:
        return _msg("No beacons to read yet — scan a system or add a beacon code.", ok=False)
    new, errs = await tl.read_beacons(api, s, beacon or None)
    await db.kv_set(tl.KV, tl.merge_read(await db.kv_get(tl.KV, {}) or {}, s))
    if errs and not new:
        return _msg("; ".join(errs), ok=False)
    n = sum(len(s["audit"].get(c) or []) for c in ([beacon] if beacon else s["beacons"]))
    return _msg(f"Read {'1 beacon' if beacon else str(len(s['beacons'])) + ' beacon(s)'}: {n} entr{'y' if n == 1 else 'ies'}, "
                f"{len(new)} new" + (f" ({'; '.join(errs)})" if errs else ""))


@router.post("/trail/stars", response_class=HTMLResponse)
async def trail_stars(request: Request, replicant: str = Form(...), user: str = Depends(current_user)):
    """Stars around a replicant (GET /replicants/{code}/stars, paged) onto the map, so the arrows have stars to match."""
    from .census import record
    db, api = request.app.state.db, request.app.state.api
    st = await load_state(request)
    r = st["replicants"].get(replicant) or {}
    s = await _state(db)
    s["stars_replicant"] = replicant
    await db.kv_set(tl.KV, s)
    pages = []
    try:
        for n in range(1, 9):
            body = await api.get(f"/replicants/{replicant}/stars", per_page=50, page=n) or {}
            pages.append(body)
            if n >= int(body.get("total_pages") or 1):
                break
    except ApiError as e:
        if not pages:
            return _msg(f"Nearest stars failed: {e.message}", ok=False)
    found = await record(db, star_of(r.get("location") or r.get("current_location")) or "?", replicant, pages)
    placed = [x for x in found if x.get("position")]
    return _msg(f"{len(found)} stars around {r.get('name') or replicant} ({len(placed)} with positions) added to the map.")


@router.post("/trail/reset", response_class=HTMLResponse)
async def trail_reset(request: Request, user: str = Depends(current_user)):
    """Clear the beacon trail (the target relocated): beacons, their logs and the follow — keeps who is followed."""
    db = request.app.state.db
    s = await _state(db)
    fo = s.get("follow") or {}
    if fo.get("active") and fo.get("job"):
        await request.app.state.worker.automations.cancel(fo["job"])
    for k in ("beacons", "audit", "read_at"):
        s[k] = {}
    s["seen"] = []
    for k in ("last_beacon", "follow", "polled_at"):
        s.pop(k, None)
    await db.kv_set(tl.KV, s)
    request.app.state.hub.publish("state", "trail")
    return _msg(f"Trail cleared: still following {s['target_name']}. Scan for their beacons again.")


@router.post("/trail/follow", response_class=HTMLResponse)
async def trail_follow(request: Request, action: str = Form("start"), user: str = Depends(current_user)):
    """Follow the trail to the end with the replicant that scanned (engine: trail_follow_pass): fly to where the
    latest departure points, read the beacons there, keep going until an arrival has no departure after it."""
    db = request.app.state.db
    s = await _state(db)
    if action == "stop":
        fo = s.get("follow") or {}
        if fo.get("active"):
            fo["active"], fo["result"] = False, f"stopped by {user}"
            fo.setdefault("log", []).append({"at": now_iso(), "text": f"stopped by {user}"})
            if fo.get("job"):
                await request.app.state.worker.automations.cancel(fo["job"])
            s["follow"] = fo
            await db.kv_set(tl.KV, s)
        return _msg("Stopped following.")
    rep = s.get("scanner")
    if not rep:
        return _msg("Scan for beacons with a replicant first: that's the one that follows the trail.", ok=False)
    pos = _positions(await db.kv_get("stars", {}) or {})
    last = tl.last_seen(s["audit"], s["beacons"], s.get("target_code"))
    legs = tl.legs(s["audit"], s["beacons"], s.get("target_code"), pos)
    if last and last.get("travel_type") == "arrival":
        first, alts, why = last["star"], [], f"{s['target_name']}'s latest move is an arrival at {last['star']}"
    elif legs and legs[0]["candidates"]:
        first, alts = legs[0]["candidates"][0]["star"], [c["star"] for c in legs[0]["candidates"][1:4]]
        why = f"the latest departure (from {legs[0]['from']}) points at {first}"
    else:
        return _msg("Nothing to follow yet: read a beacon with a departure of theirs first (and load the stars around it).", ok=False)
    s["follow"] = {"active": True, "replicant": rep, "state": "travel", "star": first, "alts": alts, "job": None,
                   "visited": [], "started_at": now_iso(), "by": user,
                   "log": [{"at": now_iso(), "text": f"following {s['target_name']}: {why}"}]}
    await db.kv_set(tl.KV, s)
    return _msg(f"Following {s['target_name']}: first stop {first}.")


ETA_TTL = 1800   # seconds an estimate is reused (per replicant, its system and the target)


@router.get("/trail/eta", response_class=HTMLResponse)
async def trail_eta(request: Request, star: str, user: str = Depends(current_user)):
    """How long the replicant that scanned would take to reach `star` (GET /replicants/{code}/stars/{star}:
    estimated_travel_time), cached for half an hour so the page's lazy loads don't cost a request each time."""
    db = request.app.state.db
    s = await _state(db)
    rep = s.get("scanner")
    if not rep:
        return HTMLResponse("")
    r = (await load_state(request))["replicants"].get(rep) or {}
    here = star_of(r.get("location") or r.get("current_location"))
    star = star.strip().upper()
    if star == here:
        return HTMLResponse('<span class="muted small">here</span>')
    cache = await db.kv_get("trail_eta", {}) or {}
    key = f"{rep}|{here}|{star}"
    hit = cache.get(key)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).timestamp()
    if not hit or now - float(hit.get("t") or 0) > ETA_TTL:
        try:
            body = await request.app.state.api.get(f"/replicants/{rep}/stars/{star}", background=True) or {}
            st = body.get("star") or body
            hit = {"t": now, "secs": st.get("estimated_travel_time"), "ly": st.get("distance_from_replicant")}
        except ApiError as e:
            return HTMLResponse(f'<span class="muted small" title="{html.escape(e.message)}">?</span>')
        cache = {k: v for k, v in cache.items() if now - float(v.get("t") or 0) <= ETA_TTL}   # drop stale ones
        cache[key] = hit
        await db.kv_set("trail_eta", cache)
    if hit.get("secs") is None:
        return HTMLResponse('<span class="muted small">?</span>')
    arrive = datetime.fromtimestamp(now + float(hit["secs"]), timezone.utc).isoformat(timespec="minutes")
    ly = f" · {float(hit['ly']):.1f} ly from {html.escape(here)}" if hit.get("ly") is not None else ""
    return HTMLResponse(f'<span class="small" title="{html.escape(r.get("name") or rep)}: estimated by the game{ly}; '
                        f'arrives about {arrive} UTC">≈ {f_duration(hit["secs"])}</span>')


@router.post("/trail/travel", response_class=HTMLResponse)
async def trail_travel(request: Request, star: str = Form(...), replicant: str = Form(""),
                       user: str = Depends(current_user)):
    """Send the replicant that scanned for the beacons (its vessel) to a candidate system."""
    s = await _state(request.app.state.db)
    rep, star = (replicant or s.get("scanner") or "").strip(), star.strip().upper()
    if not rep:
        return _msg("Scan for beacons with a replicant first: that's the one the travel buttons send.", ok=False)
    return await run_action(request, user, "POST", f"/replicants/{rep}/travel", {"destination": star},
                            f"{rep} → {star} (following {s['target_name']})")
