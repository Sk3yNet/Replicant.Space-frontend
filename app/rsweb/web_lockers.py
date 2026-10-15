"""Economy › Lockers: equipment lockers are NPC devices that hand out a set loadout, once per account, to a replicant at
their location (docs: equipment lockers). SOL-OORT: a slingshot to a fresh vessel in a private starter region (for
accounts from before 2.5.0); TWAFFY-OORT: a slingshot to a vessel in the Delta region with drones, for 50 compute cores
deployed there. Scan for one where a replicant is, then retrieve."""
from __future__ import annotations

import html

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .web import current_user, load_state, page, run_action

router = APIRouter()
KNOWN = [{"location": "SOL-OORT", "gives": "an FTL slingshot pair and a heaven vessel in a private starter cluster",
          "cost": "free (accounts from before 2.5.0)"},
         {"location": "TWAFFY-OORT", "gives": "an FTL slingshot pair and a heaven vessel in the Delta region, with drones",
          "cost": "50 compute cores deployed at TWAFFY-OORT (used up)"}]


@router.get("/lockers", response_class=HTMLResponse)
async def lockers_page(request: Request, user: str = Depends(current_user)):
    st = await load_state(request)
    reps = [(c, r.get("name") or c, r.get("location") or r.get("current_location")) for c, r in st["replicants"].items()]
    return await page(request, user, "lockers.html", "lockers", reps=reps, known=KNOWN)


@router.get("/lockers/scan/{rep}", response_class=HTMLResponse)
async def lockers_scan(request: Request, rep: str, user: str = Depends(current_user)):
    from .api import ApiError
    try:
        b = await request.app.state.api.get(f"/replicants/{rep}/scan/devices", device_type="equipment_locker") or {}
    except ApiError as e:
        return HTMLResponse(f'<span class="result err">{html.escape(e.message)}</span>')
    devs = b.get("devices") or []
    if not devs:
        return HTMLResponse('<span class="muted small">No equipment locker where this replicant is.</span>')
    out = []
    for d in devs:
        code = html.escape(str(d.get("device_code")))
        out.append(f'<div class="small"><span class="mono">{code}</span> at {html.escape(str(d.get("location")))} '
                   f'(owner {html.escape(str(d.get("owner_name") or d.get("owner_replicant_code") or "?"))}) '
                   f'<button class="small" hx-post="/lockers/{code}/retrieve" hx-target="next .lk-out" '
                   f'hx-confirm="Retrieve from locker {code}? Each locker works once per account; some take a payment '
                   f'deployed at their location.">Retrieve</button><span class="lk-out"></span></div>')
    return HTMLResponse("".join(out))


@router.post("/lockers/{code}/retrieve", response_class=HTMLResponse)
async def lockers_retrieve(request: Request, code: str, user: str = Depends(current_user)):
    return await run_action(request, user, "POST", f"/devices/{code}/retrieve", None, f"retrieve from locker {code}")
