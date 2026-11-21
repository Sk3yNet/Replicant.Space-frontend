"""Activity › Info beacons: your information beacons (description, broadcast, comments) and other players' beacons that a
scan found, with comments on any of them from a replicant at the beacon (game v4.1.0; see infobeacons.py)."""
from __future__ import annotations

import html
import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from . import infobeacons as ib
from .api import ApiError
from .web import call_action, current_user, load_state, page, partial

router = APIRouter()
READ_MAX = 8   # beacons read per system scan (each is one GET)


async def _store(db) -> dict:
    return await db.kv_get(ib.KV, {}) or {}


async def read_beacon(request: Request, code: str) -> tuple[dict, str | None]:
    """GET the beacon and remember the answer. Returns (device, error); on an error, the last copy read."""
    db = request.app.state.db
    try:
        dev = await request.app.state.api.get(f"/devices/{code}")
    except ApiError as e:
        return ((await _store(db)).get(code) or {}).get("device") or {}, e.message
    if isinstance(dev, dict) and dev:
        await db.kv_set(ib.KV, ib.remember(await _store(db), {"device_type": ib.TYPE, **dev}))
    return dev if isinstance(dev, dict) else {}, None


async def remember(db, dev: dict) -> None:
    """The device page's live answer for a beacon, kept for the Info beacons page."""
    if ib.is_beacon(dev):
        await db.kv_set(ib.KV, ib.remember(await _store(db), dev))


async def card_ctx(request: Request, code: str, dev: dict | None = None) -> dict:
    st = await load_state(request)
    mine = next((d for d in st["devices"] if d.get("device_code") == code), None)
    dev = dev or ((await _store(request.app.state.db)).get(code) or {}).get("device") or mine or {}
    loc = dev.get("location") or (mine or {}).get("location")
    here = [c for c, r in st["replicants"].items() if loc and (r.get("location") or r.get("current_location")) == loc]
    here += [h["replicant_code"] for d in st["devices"] if loc and d.get("location") == loc
             for h in [d.get("hosting_replicant") or {}] if h.get("replicant_code") and h["replicant_code"] not in here]
    return {"ib_code": code, "ib_dev": dev, "ib_mine": mine is not None, "ib_loc": loc, "ib_here": here,
            "ib_desc": ib.description(dev), "ib_comments": ib.comments(dev), "ib_comments_on": ib.comments_on(dev),
            "ib_active": ib.active(dev), "ib_max": ib.MAX_TEXT, "ib_max_comments": ib.MAX_COMMENTS,
            "ib_deployed": bool(loc) and not (dev.get("stowed_in_device_code") or dev.get("attached_to_device_code"))
            and str(dev.get("status") or "") not in ("stowed", "compacted"),
            "ib_cmds": dev.get("available_commands") or (mine or {}).get("available_commands") or []}


async def card(request: Request, code: str, outcome: dict | None = None, err: str | None = None) -> HTMLResponse:
    dev, rerr = await read_beacon(request, code)
    return partial(request, "partials/info_beacon.html", outcome=outcome, err=err, read_err=rerr,
                   **await card_ctx(request, code, dev))


@router.get("/info-beacons", response_class=HTMLResponse)
async def info_beacons_page(request: Request, user: str = Depends(current_user)):
    db = request.app.state.db
    st = await load_state(request)
    from . import others as oth
    seen = oth.normalize(await db.kv_get(oth.KV, {}))["info_beacons"]
    mine, theirs = ib.rows(st["devices"], await _store(db), seen)
    reps = [(c, r.get("name") or c, r.get("location") or r.get("current_location")) for c, r in st["replicants"].items()]
    rows = await db.fetchall("SELECT * FROM events WHERE event='device.commented' ORDER BY seq DESC LIMIT 30")
    recent = []
    for r in rows:
        try:
            p = json.loads(r["payload"]) if r["payload"] else {}
        except (TypeError, ValueError):
            p = {}
        recent.append({"at": r["created_at"], "code": r["device_code"],
                       "line": ib.commented_line({"device_code": r["device_code"], "location": r["location"], **p})})
    return await page(request, user, "info_beacons.html", "infobeacons", mine=mine, theirs=theirs, reps=reps,
                      recent=recent, max_text=ib.MAX_TEXT)


@router.get("/info-beacons/scan/{rep}", response_class=HTMLResponse)
async def info_beacons_scan(request: Request, rep: str, user: str = Depends(current_user)):
    """Look for information beacons in the replicant's system, then read each one (description and comments)."""
    api = request.app.state.api
    try:
        devs = await api.paged(f"/replicants/{rep}/scan/devices", "devices", background=False, device_type=ib.TYPE)
    except ApiError as e:
        return HTMLResponse(f'<span class="result err">{html.escape(e.message)}</span>')
    devs = [d for d in devs if isinstance(d, dict) and d.get("device_type") == ib.TYPE and d.get("device_code")]
    if not devs:
        return HTMLResponse('<span class="muted small">No information beacon in this replicant\'s system.</span>')
    db = request.app.state.db
    from . import others as oth
    s = oth.normalize(await db.kv_get(oth.KV, {}))
    st = await load_state(request)
    mine = set(st["replicants"])
    for d in devs:
        if d.get("owner_replicant_code") not in mine:
            s["info_beacons"][str(d["device_code"])] = {"location": d.get("location"), "owner": d.get("owner_replicant_code"),
                                                       "owner_name": d.get("owner_name"), "seen_at": ib.now()}
    await db.kv_set(oth.KV, s)
    out = []
    for i, d in enumerate(devs):
        code = str(d["device_code"])
        dev, err = await read_beacon(request, code) if i < READ_MAX else ({}, "not read (too many at once)")
        text = ib.description(dev)
        out.append(f'<div class="small" style="margin:4px 0"><a class="mono" href="/devices/{html.escape(code)}">{html.escape(code)}</a> '
                   f'at {html.escape(str(d.get("location") or "?"))} · {html.escape(str(d.get("owner_name") or d.get("owner_replicant_code") or "?"))}'
                   f' · {len(ib.comments(dev))} comment(s)'
                   + (f' <span class="muted">({html.escape(err)})</span>' if err else "")
                   + (f'<div class="ib-text">{html.escape(text)}</div>' if text else "") + "</div>")
    return HTMLResponse(f'<p class="small">{len(devs)} beacon(s) found; reload the page to see them in the list.</p>' + "".join(out))


@router.get("/info-beacons/{code}/card", response_class=HTMLResponse)
async def info_beacon_card(request: Request, code: str, user: str = Depends(current_user)):
    return await card(request, code)


@router.post("/info-beacons/{code}/settings", response_class=HTMLResponse)
async def info_beacon_settings(request: Request, code: str, description: str = Form(""), comments: str = Form(""),
                               user: str = Depends(current_user)):
    try:
        body = ib.settings_body(description, comments == "1")
    except ValueError as e:
        return await card(request, code, err=str(e))
    return await card(request, code, await call_action(request, user, "PATCH", f"/devices/{code}", body,
                                                       f"information beacon {code}: description"))


@router.post("/info-beacons/{code}/switch", response_class=HTMLResponse)
async def info_beacon_switch(request: Request, code: str, command: str = Form("activate"), user: str = Depends(current_user)):
    command = "deactivate" if command == "deactivate" else "activate"
    return await card(request, code, await call_action(request, user, "POST", f"/devices/{code}", {"command": command},
                                                       f"{command} information beacon {code}"))


@router.post("/info-beacons/{code}/comment", response_class=HTMLResponse)
async def info_beacon_comment(request: Request, code: str, message: str = Form(""), user: str = Depends(current_user)):
    try:
        body = ib.comment_body(message)
    except ValueError as e:
        return await card(request, code, err=str(e))
    return await card(request, code, await call_action(request, user, "POST", f"/devices/{code}", body,
                                                       f"comment on information beacon {code}"))


@router.post("/info-beacons/{code}/comments/{index}/delete", response_class=HTMLResponse)
async def info_beacon_delete_comment(request: Request, code: str, index: int, user: str = Depends(current_user)):
    return await card(request, code, await call_action(request, user, "DELETE", f"/devices/{code}/comments/{index}", None,
                                                       f"delete comment {index} on information beacon {code}"))
