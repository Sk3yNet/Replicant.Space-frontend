"""Devices › Cockpit: fly the `swoop` debris run by hand (POST /devices/{code} {"command": "swoop", "action": …}).

Live run 2026-10-29 (Percival_Deltaward, heaven vessel 5DF3161B at ETHIREL-2-4):
  start            → {status: swoop_starting, rounds: 5, warmup_seconds: 20, available_actions: [...]}; event swoop.started
  move in warmup   → {status: warmup, warmup_actions: n} (practice, nothing counts)
  each round       → event swoop.round {round, debris_status: flanking|clear|incoming, description, duration_seconds}
                     with device_code null (only replicant_code names the pilot)
  first move       → {status: active, round, correct: bool}; later moves that round → {round, waiting: true}
  end              → event swoop.complete {rounds_pass, rounds_fail, total_rounds, score, vessel_damage}
  after the end    → 400 "No active swoop session"
Which move beats which debris status isn't documented: `learned` pairs every answered move with its round's status."""
from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .web import call_action, current_user, f_human, load_state, page, templates

router = APIRouter()

ACTIONS = ("boost", "dodge", "dive", "roll", "brake")
RECENT = 40


def pilots(st: dict) -> list[dict]:
    """Devices that can swoop first, then every replicant's vessel (the command may only show once the run is on)."""
    hosts = {r.get("hosted_device_code"): (c, r.get("name") or c) for c, r in st["replicants"].items()
             if r.get("hosted_device_code")}
    out = []
    for d in st["devices"]:
        code = d.get("device_code")
        can = "swoop" in (d.get("available_commands") or [])
        if not code or not (can or code in hosts):
            continue
        rep = hosts.get(code) or ((d.get("hosting_replicant") or {}).get("replicant_code"),
                                  (d.get("hosting_replicant") or {}).get("name"))
        out.append({"code": code, "type": f_human(d.get("device_type") or "device"), "location": d.get("location"),
                    "status": d.get("status"), "pilot": rep[1], "rep": rep[0], "swoop": can})
    return sorted(out, key=lambda p: (not p["swoop"], (p["pilot"] or "~").lower(), p["code"]))


def related(ev: dict, code: str, rep: str | None = None) -> bool:
    """An event about this vessel or its pilot (swoop.round names only the replicant), or one naming the vessel."""
    if ev.get("device_code") == code or (rep and ev.get("replicant_code") == rep and ev.get("category") == "swoop"):
        return True
    p = ev.get("payload")
    return code in (p if isinstance(p, str) else json.dumps(p or {}, default=str))


def slim(ev: dict, code: str, rep: str | None = None) -> dict:
    p = ev.get("payload")
    if isinstance(p, str):
        try:
            p = json.loads(p)
        except ValueError:
            pass
    return {"id": ev.get("id"), "event": ev.get("event"), "category": ev.get("category"),
            "device_code": ev.get("device_code"), "replicant_code": ev.get("replicant_code"), "location": ev.get("location"),
            "created_at": ev.get("created_at"), "payload": p, "mine": related({**ev, "payload": p}, code, rep)}


def _ts(s) -> float | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


async def learned(db) -> dict:
    """{debris status: {action: {"right": n, "wrong": n}}} from every answered swoop move and the round it answered
    (the latest swoop.round with that number up to 60 s before the move)."""
    rounds = []
    for r in await db.fetchall("SELECT created_at, payload FROM events WHERE event='swoop.round'"):
        try:
            p = json.loads(r["payload"] or "{}")
        except ValueError:
            continue
        t = _ts(r["created_at"])
        if t is not None and p.get("debris_status"):
            rounds.append((t, p.get("round"), p["debris_status"]))
    out: dict[str, dict] = {}
    for a in await db.fetchall("SELECT at, body, response FROM actions WHERE body LIKE '%swoop%' AND response LIKE '%correct%'"):
        try:
            body, resp = json.loads(a["body"] or "{}"), json.loads(a["response"] or "{}")
        except ValueError:
            continue
        t, act = _ts(a["at"]), body.get("action")
        if t is None or act not in ACTIONS or not isinstance(resp, dict) or not isinstance(resp.get("correct"), bool):
            continue
        match = max((x for x in rounds if x[1] == resp.get("round") and t - 60 <= x[0] <= t + 2), default=None)
        if match:
            cell = out.setdefault(match[2], {}).setdefault(act, {"right": 0, "wrong": 0})
            cell["right" if resp["correct"] else "wrong"] += 1
    return out


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
    rep = (me or {}).get("rep")
    rows = await db.fetchall("SELECT * FROM events WHERE device_code=? OR (replicant_code=? AND category='swoop') "
                             "OR payload LIKE ? ORDER BY seq DESC LIMIT ?", (code, rep or "-", f"%{code}%", RECENT))
    boot = {"code": code, "rep": rep, "actions": ACTIONS, "known": await _known_types(db), "learned": await learned(db),
            "recent": [slim(dict(r), code, rep) for r in reversed(rows)]}
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
    resp = out["response"]
    graded = isinstance(resp, dict) and isinstance(resp.get("correct"), bool)
    return JSONResponse({"ok": out["ok"], "status": out["status"], "error": out["error"], "response": resp,
                         "action": action, "ms": ms, "learned": await learned(request.app.state.db) if graded else None})


@router.get("/cockpit/{code}/stream")
async def cockpit_stream(request: Request, code: str, rep: str = "", user: str = Depends(current_user)):
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
                    yield f"event: game\ndata: {json.dumps(slim(data, code, rep or None), default=str)}\n\n"
        finally:
            st.hub.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
