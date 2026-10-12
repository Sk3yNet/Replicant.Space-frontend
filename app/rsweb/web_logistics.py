"""Fleets › Logistics: the logistics service's request queue, its logistics devices, and the trade approvals it fulfills (see logistics.py)."""
from __future__ import annotations

import html

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse

from .db import now_iso
from .web import current_user, page
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
    return await page(request, user, "logistics.html", "logistics", st=st, jobs=list(reversed(jobs)), on_way=on_way, why=why,
                      tiers=lg.TIERS, fleets=fleets, approvals=ok)


@router.post("/fleets/logistics/prefs", response_class=HTMLResponse)
async def logistics_prefs(request: Request, req: str = Form(...), tier: str = Form(""), skip: str = Form(""),
                     user: str = Depends(current_user)):
    """Bump a request to another tier ("" = its own), or skip it / take it back."""
    db = request.app.state.db
    p = await db.kv_get(lg.KV_PREFS, {}) or {}
    bump, skips = dict(p.get("bump") or {}), set(p.get("skip") or [])
    if tier.strip() in ("1", "2", "3", "4"):
        bump[req] = int(tier)
    elif tier == "reset":
        bump.pop(req, None)
    if skip == "on":
        skips.add(req)
    elif skip == "off":
        skips.discard(req)
    await db.kv_set(lg.KV_PREFS, {"bump": bump, "skip": sorted(skips)})
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
