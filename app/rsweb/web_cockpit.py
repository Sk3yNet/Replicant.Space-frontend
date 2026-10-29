"""Devices › Cockpit: fly the `swoop` debris run by hand (live spec 2026-10-29: POST /devices/{code}
{"command": "swoop", "action": boost|dodge|dive|roll|brake|null}). The debris event isn't in the spec, so the page
shows every event about the vessel as it arrives and marks event types the app has never stored before."""
from __future__ import annotations

import asyncio
import json
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from . import hubroute
from .targets import system_targets
from .web import call_action, current_user, f_human, load_state, page, star_of, templates

router = APIRouter()

ACTIONS = ("boost", "dodge", "dive", "roll", "brake")
RECENT = 40
NEED_KV = "cockpit:need"   # {device code: the spot the game says the run starts from}
# live 2026-10-29: 400 "Vessel must be deployed at ETHIREL-2-4" (a moon; vessels have no deploy command, so it means be there)
NEED_RE = re.compile(r"must be (?:deployed |located |stationed )?(?:at|in) ([A-Z0-9]+(?:-[A-Z0-9]+)*)")


def needed_spot(error: str | None) -> str | None:
    m = NEED_RE.search(error or "")
    return m.group(1) if m else None


def pilots(st: dict) -> list[dict]:
    """Devices that can swoop first, then every replicant's vessel (the command may only show once the run is on)."""
    hosts = {r.get("hosted_device_code"): r.get("name") or c for c, r in st["replicants"].items()
             if r.get("hosted_device_code")}
    out = []
    for d in st["devices"]:
        code = d.get("device_code")
        can = "swoop" in (d.get("available_commands") or [])
        if not code or not (can or code in hosts):
            continue
        out.append({"code": code, "type": f_human(d.get("device_type") or "device"), "location": d.get("location"),
                    "status": d.get("status"), "pilot": hosts.get(code), "swoop": can})
    return sorted(out, key=lambda p: (not p["swoop"], (p["pilot"] or "~").lower(), p["code"]))


def related(ev: dict, code: str) -> bool:
    """An event about this vessel, or one that names it anywhere in its payload."""
    if ev.get("device_code") == code:
        return True
    p = ev.get("payload")
    return code in (p if isinstance(p, str) else json.dumps(p or {}, default=str))


def slim(ev: dict, code: str) -> dict:
    p = ev.get("payload")
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except ValueError:
            pass
    return {"id": ev.get("id"), "event": ev.get("event"), "category": ev.get("category"),
            "device_code": ev.get("device_code"), "location": ev.get("location"), "created_at": ev.get("created_at"),
            "payload": p, "mine": related({**ev, "payload": p}, code)}


async def _known_types(db) -> list[str]:
    return [r["event"] for r in await db.fetchall("SELECT DISTINCT event FROM events")]


@router.get("/cockpit", response_class=HTMLResponse)
async def cockpit_pick(request: Request, user: str = Depends(current_user)):
    return await page(request, user, "cockpit_pick.html", "cockpit", pilots=pilots(await load_state(request)))


@router.get("/cockpit/{code}", response_class=HTMLResponse)
async def cockpit(request: Request, code: str, user: str = Depends(current_user)):
    st = await load_state(request)
    me = next((p for p in pilots(st) if p["code"] == code), None)
    dev = next((d for d in st["devices"] if d.get("device_code") == code), None)
    if not dev:
        raise HTTPException(404, "No such device")
    db = request.app.state.db
    rows = await db.fetchall("SELECT * FROM events WHERE device_code=? OR payload LIKE ? ORDER BY seq DESC LIMIT ?",
                             (code, f"%{code}%", RECENT))
    need = (await db.kv_get(NEED_KV, {}) or {}).get(code)
    star = star_of(need or dev.get("location") or "")
    spots = [t["code"] for t in (await system_targets(db, star))["targets"] if t["category"] == "moon"] if star else []
    boot = {"spots": sorted(set(spots) | ({need} if need else set())), "code": code, "actions": ACTIONS, "known": await _known_types(db), "location": dev.get("location"),
            "status": dev.get("status"), "need": need,
            "recent": [slim(dict(r), code) for r in reversed(rows)]}
    return templates.TemplateResponse(request, "cockpit.html", {
        "request": request, "dev": dev, "me": me or {"code": code, "type": f_human(dev.get("device_type")),
                                                     "pilot": None, "swoop": False},
        "boot": boot})


@router.post("/cockpit/{code}/swoop")
async def cockpit_swoop(request: Request, code: str, user: str = Depends(current_user)):
    """One swoop command; the page shows the game's answer and the round trip."""
    try:
        body = await request.json()
    except ValueError:
        body = {}
    action = (body or {}).get("action") or None
    if action is not None and action not in ACTIONS:
        return JSONResponse({"ok": False, "error": f"Unknown action {action}"}, 400)
    cmd = {"command": "swoop"} | ({"action": action} if action else {})
    t0 = time.monotonic()
    out = await call_action(request, user, "POST", f"/devices/{code}", cmd, f"swoop {action or 'start'} {code}")
    ms = round((time.monotonic() - t0) * 1000)
    need = needed_spot(out["error"])
    if need:
        db = request.app.state.db
        await db.kv_set(NEED_KV, {**(await db.kv_get(NEED_KV, {}) or {}), code: need})
    return JSONResponse({"ok": out["ok"], "status": out["status"], "error": out["error"], "response": out["response"],
                         "action": action, "ms": ms, "need": need})


@router.post("/cockpit/{code}/fly")
async def cockpit_fly(request: Request, code: str, user: str = Depends(current_user)):
    """Fly to the run's spot (a moon such as ETHIREL-2-4), planned like the job runner's trips (surge hops, hubs);
    dry_run previews the ETA. A refused plan goes the game's own way."""
    try:
        body = await request.json()
    except ValueError:
        body = {}
    dest = str((body or {}).get("destination") or "").strip().upper()
    if not dest:
        return JSONResponse({"ok": False, "error": "Pick a destination"}, 400)
    trip = {"command": "travel", "destination": dest} | ({"dry_run": True} if body.get("dry_run") else {})
    path, label = f"/devices/{code}", f"travel on {code} to {dest} (cockpit)"
    via = await request.app.state.worker.automations.trip_via(code, dest)
    out = await call_action(request, user, "POST", path, {**trip, "via": via}, f"{label} via {', '.join(via)}") if via else None
    if not out or not out["ok"]:
        via, out = [], await call_action(request, user, "POST", path, trip, label)
    return JSONResponse({"ok": out["ok"], "status": out["status"], "error": out["error"], "response": out["response"],
                         "via": via, "destination": dest, "dry_run": bool(body.get("dry_run")),
                         "seconds": hubroute.total_seconds(out["response"]) if out["ok"] else None})
