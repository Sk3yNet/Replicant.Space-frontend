"""Fleets › Logistics: the logistics service's request queue, its logistics devices, and the trade approvals it fulfills (see logistics.py)."""
from __future__ import annotations

import html

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from .db import now_iso
from .web import current_user, load_state, page
from . import balance as bal
from . import cargo
from . import logistics as lg

router = APIRouter()


def _msg(text: str, ok: bool = True) -> HTMLResponse:
    return HTMLResponse(f'<div class="result {"ok" if ok else "err"}">{html.escape(text)}</div>',
                        headers={"HX-Refresh": "true"} if ok else {})


@router.get("/fleets/logistics", response_class=HTMLResponse)
async def logistics_page(request: Request, user: str = Depends(current_user)):
    db = request.app.state.db
    st = await db.kv_get(lg.KV_STATE, {}) or {}
    eng = request.app.state.worker.automations
    jobs = [j for j in await eng.jobs() if j.get("rule") == "logistics"][-15:]
    on_way = {(j.get("meta") or {}).get("logistics", {}).get("req"): j for j in jobs if j.get("status") in ("running", "waiting")}
    why = {u["req"]: u["why"] for u in st.get("unmet") or []}
    ok = await db.kv_get(lg.KV_TRADES, {}) or {}
    fleets = lg.logistics_fleets(await eng.fleets())
    transit = await cargo.in_transit(db, (await load_state(request))["devices"], await eng.jobs())
    return await page(request, user, "logistics.html", "logistics", st=st, jobs=list(reversed(jobs)), on_way=on_way, why=why,
                      tiers=lg.TIERS, categories=lg.CATEGORIES, fleets=fleets, approvals=ok, transit=transit,
                      hub_balance=await db.kv_get("hub_balance", {}) or {}, carrier_classes=bal.CLASSES,
                      hub_leeway=await db.kv_get(bal.KV_LEEWAY, bal.LEEWAY_PCT), slack=bal.slack,
                      rescue_pause=lg.rescue_pauses(await db.kv_get(lg.KV_RESCUE_PAUSE, {})),
                      all_fleets=dict(sorted(((f["id"], f.get("name") or f["id"]) for f in await eng.fleets() if f.get("id")),
                                             key=lambda x: x[1].lower())))


PAUSE_HOURS = {"6": 6, "24": 24, "72": 72, "168": 168}   # the choices offered; "" holds until resumed


@router.post("/fleets/logistics/rescue-pause", response_class=HTMLResponse)
async def logistics_rescue_pause(request: Request, fleet: str = Form(""), hours: str = Form(""), resume: str = Form(""),
                                 user: str = Depends(current_user)):
    """Pause rescues for a while (owner 2026-10-26): all of them (fleet "") or one fleet's devices; resume ends it now."""
    from datetime import datetime, timedelta, timezone
    db = request.app.state.db
    p = lg.rescue_pauses(await db.kv_get(lg.KV_RESCUE_PAUSE, {}))
    if hours and hours not in PAUSE_HOURS:
        return _msg(f"unknown pause length {hours}", ok=False)
    until = ((datetime.now(timezone.utc) + timedelta(hours=PAUSE_HOURS[hours])).isoformat(timespec="seconds")
             if hours else None)
    entry = {"until": until, "at": now_iso()}
    if fleet:
        if resume:
            p["fleets"].pop(fleet, None)
        else:
            p["fleets"][fleet] = entry
    else:
        p["all"] = None if resume else entry
    await db.kv_set(lg.KV_RESCUE_PAUSE, p)
    name = next((f.get("name") for f in await request.app.state.worker.automations.fleets() if f.get("id") == fleet), fleet)
    what = f"{name}'s rescues" if fleet else "all rescues"
    return _msg(f"{what} resumed" if resume else f"{what} paused " + (f"for {PAUSE_HOURS[hours]} h" if hours else "until resumed")
                + " (from the next logistics pass; a rescue already under way goes on)")


@router.post("/fleets/logistics/leeway", response_class=HTMLResponse)
async def logistics_leeway(request: Request, pct: str = Form(...), user: str = Depends(current_user)):
    """How far a hub may be over or under its carrier share before balancing moves one (balance.py)."""
    try:
        v = float(pct)
    except ValueError:
        return _msg(f"not a number: {pct}", ok=False)
    if not 0 <= v <= 100:
        return _msg("the leeway is 0 to 100 %", ok=False)
    await request.app.state.db.kv_set(bal.KV_LEEWAY, v)
    return _msg(f"leeway {v:g} % saved")


@router.post("/fleets/logistics/prefs", response_class=HTMLResponse)
async def logistics_prefs(request: Request, req: str = Form(...), tier: str = Form(""), skip: str = Form(""),
                     confirm: str = Form(""), user: str = Depends(current_user)):
    """Bump a request to another tier ("" = its own), skip it / take it back, or confirm a rescue of more than a day."""
    db = request.app.state.db
    p = await db.kv_get(lg.KV_PREFS, {}) or {}
    bump, skips, oks = dict(p.get("bump") or {}), set(p.get("skip") or []), dict(p.get("confirm") or {})
    if tier.strip().isdigit() and int(tier) in lg.TIERS:   # every tier, evacuation (0), rescue (5) and shop listing (6) too
        bump[req] = int(tier)
    elif tier == "reset":
        bump.pop(req, None)
    elif tier.strip():
        return _msg(f"unknown priority {tier}", ok=False)
    if skip == "on":
        skips.add(req)
    elif skip == "off":
        skips.discard(req)
    if confirm == "on":
        from datetime import datetime, timezone
        oks[req] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    elif confirm == "off":
        oks.pop(req, None)
    await db.kv_set(lg.KV_PREFS, {"bump": bump, "skip": sorted(skips), "confirm": oks})
    # shown at once: the page reloads from the last pass's requests, which would show the old priority until the next pass
    st = await db.kv_get(lg.KV_STATE, {}) or {}
    for r in st.get("requests") or []:
        if r.get("id") == req:
            r["tier"] = bump.get(req, r.get("base_tier", r.get("tier")))
            r["skipped"] = req in skips
            if confirm == "on":
                r["confirm"] = False
            st["requests"].sort(key=lambda x: (x.get("tier", 9), not x.get("waiting"), str(x.get("id"))))
            await db.kv_set(lg.KV_STATE, st)
            break
    return _msg("saved — the next logistics pass (within two minutes) works to it")


@router.post("/fleets/logistics/run", response_class=HTMLResponse)
async def logistics_run(request: Request, user: str = Depends(current_user)):
    out = await request.app.state.worker.automations.logistics_pass(force=True)
    return _msg("; ".join(out) if out else "logistics pass done: nothing to send right now")


@router.post("/shop/approve", response_class=HTMLResponse)
async def trade_approve(request: Request, controller: str = Form(...), trade_code: str = Form(...), on: str = Form(""),
                        user: str = Depends(current_user)):
    """Approve one trade: the logistics service delivers its price and executes it, once. Shop listings nobody approved are
    never fulfilled."""
    db = request.app.state.db
    ok = await db.kv_get(lg.KV_TRADES, {}) or {}
    box = ok.setdefault("trades", {})
    key = f"{controller}:{trade_code}"
    if on == "on":
        box[key] = now_iso()
    else:
        box.pop(key, None)
    await db.kv_set(lg.KV_TRADES, ok)
    return HTMLResponse(f'<span class="muted small">{"approved" if on == "on" else "approval withdrawn"}</span>')
