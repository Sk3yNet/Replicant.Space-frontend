"""Map › Wards & hubs: our system wards against the 25 cap (activate / deactivate, evicted miners) and our system hubs'
shield and upkeep — see wardhub.py."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .web import call_action, current_user, load_state, page, render_action, star_of
from . import wardhub as wh

router = APIRouter()


async def overview(db, st: dict) -> dict:
    return {"hubs": wh.hubs(st["devices"], await wh.hub_events(db), wh.stock_by_star(st["inventory"])),
            "wards": wh.wards(st["devices"])}


@router.get("/wards", response_class=HTMLResponse)
async def wards_page(request: Request, user: str = Depends(current_user)):
    db = request.app.state.db
    st = await load_state(request)
    ov = await overview(db, st)
    fleets = await request.app.state.worker.automations.fleets()
    stationed = {f.get("home"): f for f in fleets if f.get("station") and f.get("home")}
    return await page(request, user, "wards.html", "wards", hubs=ov["hubs"], w=ov["wards"], stationed=stationed,
                      evictions=(await db.kv_get(wh.EVICTIONS_KV, []) or [])[-30:][::-1])


@router.post("/wards/{code}/{command}", response_class=HTMLResponse)
async def ward_command(request: Request, code: str, command: str, user: str = Depends(current_user)):
    """activate / deactivate one of our wards (activate refuses past the cap, and refuses a system with a hub of ours)."""
    if command not in ("activate", "deactivate"):
        return HTMLResponse("", status_code=404)
    st = await load_state(request)
    d = next((x for x in st["devices"] if x.get("device_code") == code), None)
    if not d or not (wh.is_ward(d) or wh.is_hub(d)):
        return HTMLResponse('<div class="result err">No ward or hub of yours with that code.</div>')
    if command == "activate" and wh.is_ward(d):
        w = wh.wards(st["devices"])
        if w["active"] >= wh.WARD_CAP:
            return HTMLResponse(f'<div class="result err">{w["active"]} wards are already active — the game allows '
                                f'{wh.WARD_CAP} per account. Deactivate one first.</div>')
        if star_of(d.get("location")) in {star_of(x.get("location")) for x in st["devices"] if wh.is_hub(x) and wh.deployed(x)}:
            return HTMLResponse('<div class="result err">You have a system hub in that system, and wards and hubs don\'t '
                                'go together. Move the ward or the hub.</div>')
    out = await call_action(request, user, "POST", f"/devices/{code}", {"command": command},
                            f"{command} {d.get('device_type')} {code} at {d.get('location')}")
    note = None
    ev = wh.evicted(out.get("response")) if out.get("ok") else []
    if ev:
        note = f"Evicted miners: {wh.eviction_text(ev)}"
    return render_action(request, out, note=note)
