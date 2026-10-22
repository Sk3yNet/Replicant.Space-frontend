"""Economy › Lockers: equipment lockers are NPC devices that hand out a set loadout, once per account, to a replicant at
their location (docs: equipment lockers). SOL-OORT: a slingshot to a fresh vessel in a private starter region (for
accounts from before 2.5.0); TWAFFY-OORT: a slingshot to a vessel in the Delta region with drones, for 50 compute cores
deployed there. Scan for one where a replicant is, then retrieve."""
from __future__ import annotations

import html

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from .web import current_user, load_state, page, run_action

router = APIRouter()
KNOWN = [{"location": "SOL-OORT", "gives": "an FTL slingshot pair and a heaven vessel in a private starter cluster",
          "cost": "free (accounts from before 2.5.0)"},
         {"location": "TWAFFY-OORT", "gives": "an FTL slingshot pair and a heaven vessel in the Delta region, with drones",
          "cost": "50 compute cores deployed at TWAFFY-OORT (used up)", "price": {"compute_core": 50}}]


def in_system(star: str) -> list[dict]:
    """The known lockers in a system (the System page links them here)."""
    return [k for k in KNOWN if k["location"].split("-")[0] == star]


@router.get("/lockers", response_class=HTMLResponse)
async def lockers_page(request: Request, user: str = Depends(current_user)):
    st = await load_state(request)
    reps = [(c, r.get("name") or c, r.get("location") or r.get("current_location")) for c, r in st["replicants"].items()]
    known = []
    for k in KNOWN:   # how much of the price is there, or on its way (pinned to the locker's location)
        from .loadouts import at_tag
        price = []
        for t, n in (k.get("price") or {}).items():
            there = sum(1 for d in st["devices"] if d.get("device_type") == t and d.get("location") == k["location"]
                        and not d.get("stowed_in_device_code"))
            coming = sum(1 for d in st["devices"] if d.get("device_type") == t and at_tag(k["location"]) in (d.get("tags") or [])
                         and d.get("location") != k["location"])
            price.append({"type": t, "need": n, "there": there, "coming": coming})
        known.append({**k, "progress": price, "ready": bool(price) and all(x["there"] >= x["need"] for x in price)})
    return await page(request, user, "lockers.html", "lockers", reps=reps, known=known)


@router.post("/lockers/deliver", response_class=HTMLResponse)
async def lockers_deliver(request: Request, location: str = Form(...), user: str = Depends(current_user)):
    """Deliver a locker's price: spare devices of each kind are pinned to its location (the loadout pass takes them
    there), the rest printed with those tags. Retrieving stays yours: send a replicant there, scan and retrieve."""
    from .loadouts import at_tag
    k = next((x for x in KNOWN if x["location"] == location), None)
    if not k or not k.get("price"):
        return HTMLResponse('<span class="result err">No price known for that locker.</span>')
    eng = request.app.state.worker.automations
    devices = await eng.devices()
    busy = eng.busy_devices(await eng.jobs())
    out = []
    load: dict[str, float] = {}   # shared, so the locker's types balance across the autofactories
    for t, n in k["price"].items():
        have = [d for d in devices if d.get("device_type") == t and (at_tag(location) in (d.get("tags") or [])
                or (d.get("location") == location and not d.get("stowed_in_device_code")))]
        short = n - len(have)
        if short <= 0:
            out.append(f"{n} {t.replace('_', ' ')} already there or on the way")
            continue
        pinned, printed, note = await eng.pin_or_print(t, short, location, devices, busy, f"locker {location}", have,
                                                     load=load)
        out.append(f"{t.replace('_', ' ')}: {len(pinned)} spare(s) sent, {printed} queued to print" + (f" ({note})" if note else ""))
    return HTMLResponse(f'<span class="result">{html.escape("; ".join(out))}</span>')


@router.get("/lockers/scan/{rep}", response_class=HTMLResponse)
async def lockers_scan(request: Request, rep: str, user: str = Depends(current_user)):
    from .api import ApiError
    try:
        # every page, and an unfiltered scan if the filtered one finds nothing (2026-10-22: the TWAFFY-OORT locker had to be
        # retrieved by hand)
        devs = await request.app.state.api.paged(f"/replicants/{rep}/scan/devices", "devices", background=False,
                                                 device_type="equipment_locker")
        if not devs:
            devs = await request.app.state.api.paged(f"/replicants/{rep}/scan/devices", "devices", background=False)
    except ApiError as e:
        return HTMLResponse(f'<span class="result err">{html.escape(e.message)}</span>')
    devs = [d for d in devs if "locker" in str(d.get("device_type") or "")]
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


def reps_at(st: dict, location: str) -> list[str]:
    """Your replicants at that exact location: their own record, or the device hosting them."""
    out = [c for c, r in st["replicants"].items() if (r.get("location") or r.get("current_location")) == location]
    out += [h["replicant_code"] for d in st["devices"] if d.get("location") == location
            for h in [d.get("hosting_replicant") or {}] if h.get("replicant_code") and h["replicant_code"] not in out]
    return out


@router.post("/lockers/retrieve_at", response_class=HTMLResponse)
async def lockers_retrieve_at(request: Request, location: str = Form(...), user: str = Depends(current_user)):
    """The Known lockers row's Retrieve, once the price is there: scan from a replicant at the locker, then retrieve."""
    from .api import ApiError
    st = await load_state(request)
    reps = reps_at(st, location)
    if not reps:
        return HTMLResponse(f'<span class="result err">No replicant of yours is at {html.escape(location)}: the locker '
                            'only hands out to a replicant at its location.</span>')
    try:
        devs = await request.app.state.api.paged(f"/replicants/{reps[0]}/scan/devices", "devices", background=False)
    except ApiError as e:
        return HTMLResponse(f'<span class="result err">{html.escape(e.message)}</span>')
    lk = next((d for d in devs if "locker" in str(d.get("device_type") or "") and d.get("location") == location), None) \
        or next((d for d in devs if "locker" in str(d.get("device_type") or "")), None)
    if not lk:
        return HTMLResponse(f'<span class="result err">The scan from {html.escape(reps[0])} found no locker. Retrieve '
                            'by its code instead (the Retrieve box below).</span>')
    code = str(lk["device_code"])
    return await run_action(request, user, "POST", f"/devices/{code}/retrieve", None, f"retrieve from locker {code}")


@router.post("/lockers/retrieve", response_class=HTMLResponse)
async def lockers_retrieve_code(request: Request, code: str = Form(...), user: str = Depends(current_user)):
    """Retrieve by the locker's device code (docs: POST /devices/{locker_code}/retrieve), when you know it."""
    code = code.strip().upper()
    if not code.isalnum():
        return HTMLResponse('<span class="result err">That is not a device code.</span>')
    return await run_action(request, user, "POST", f"/devices/{code}/retrieve", None, f"retrieve from locker {code}")
