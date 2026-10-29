"""Devices › Cockpit: fly the `swoop` debris run by hand (live spec 2026-10-29: POST /devices/{code}
{"command": "swoop", "action": boost|dodge|dive|roll|brake|null}). The debris event isn't in the spec, so the page
shows every event about the vessel as it arrives and marks event types the app has never stored before."""
from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .web import call_action, current_user, f_human, load_state, page, templates

router = APIRouter()

ACTIONS = ("boost", "dodge", "dive", "roll", "brake")
RECENT = 40


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
    boot = {"code": code, "actions": ACTIONS, "known": await _known_types(db),
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
    return JSONResponse({"ok": out["ok"], "status": out["status"], "error": out["error"], "response": out["response"],
                         "action": action, "ms": round((time.monotonic() - t0) * 1000)})


@router.get("/cockpit/{code}/stream")
async def cockpit_stream(request: Request, code: str, user: str = Depends(current_user)):
    """Every game event as JSON (the page decides what to draw), the moment the app receives it."""
    st = request.app.state
    q = st.hub.subscribe()

    async def gen():
        try:
            yield "retry: 2000\n\n"
            while not await request.is_disconnected():
                try:
                    kind, data = await asyncio.wait_for(q.get(), timeout=10)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if kind == "event" and isinstance(data, dict):
                    yield f"event: game\ndata: {json.dumps(slim(data, code), default=str)}\n\n"
        finally:
            st.hub.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
