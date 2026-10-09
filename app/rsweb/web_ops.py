"""Pages for traffic & civilization contact, asteroid defense, maintenance and your trade shop."""
from __future__ import annotations

import html

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

from .api import ApiError
from .db import now_iso
from .shapes import as_amounts, normalize_blueprints, normalize_inventory
from .web import call_action, current_user, load_state, page, partial, render_action, star_of
from . import traffic as tr

router = APIRouter()


def _eng(request: Request):
    return request.app.state.worker.automations


def _lines(lines: list[str], ok: bool = True) -> HTMLResponse:
    if not lines:
        lines = ["Nothing to do."]
    body = "".join(f"<div>{html.escape(x)}</div>" for x in lines)
    return HTMLResponse(f'<div class="result {"ok" if ok else "err"} small">{body}</div>')


# =====================================================================================
# traffic & civilization contact
# =====================================================================================
@router.get("/traffic", response_class=HTMLResponse)
async def traffic_page(request: Request, star: str = "", others: int = 0, user: str = Depends(current_user)):
    db = request.app.state.db
    st = await load_state(request)
    state = await db.kv_get("traffic", {}) or {}
    profiles = await db.kv_get("replicant_profiles", {}) or {}
    mine = tr.my_codes(st["account"], st["replicants"], st["devices"])
    entries = state.get("entries") or []
    star = star.strip().upper()
    s = await _eng(request).settings()
    cov = await _eng(request).civ_coverage()
    stowed = await db.kv_get("stowed_map", {}) or {}
    return await page(request, user, "traffic.html", "traffic",
                      coverage=cov, entries=tr.filtered(entries, mine, star, bool(others)),
                      summary=tr.summary(entries, mine, profiles), profiles=profiles, mine=mine, star=star, others=others,
                      beacons=tr.beacons(st["devices"]), state=state, total=len(entries),
                      stars=sorted({e.get("star") for e in entries if e.get("star")}),
                      rules=s["rules"], placements={r["location"]: tr.placement(r["location"], st["devices"], st["replicants"], stowed)
                                                    for r in cov if not r["beacon"]},
                      redundant=tr.redundant_beacons(st["devices"], cov))


@router.post("/traffic/refresh", response_class=HTMLResponse)
async def traffic_refresh(request: Request, user: str = Depends(current_user)):
    out = await _eng(request).sync_traffic()
    if out["errors"] and not out["beacons"] - len(out["errors"]):
        return _lines([f"{b}: {e}" for b, e in out["errors"].items()], ok=False)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/traffic/beacon", response_class=HTMLResponse)
async def traffic_place_beacon(request: Request, user: str = Depends(current_user)):
    loc = ((await request.form()).get("location") or "").strip().upper()
    eng = _eng(request)
    async with eng.lock:
        lines = await eng.civ_beacon_pass(only_loc=loc, manual=True)
    return _lines(lines or [f"{loc}: already has a beacon"])


@router.post("/traffic/spare-redundant", response_class=HTMLResponse)
async def traffic_spare_redundant(request: Request, user: str = Depends(current_user)):
    eng = _eng(request)
    async with eng.lock:
        lines = await eng.spare_redundant_beacons(manual=True)
    return _lines(lines or ["No redundant beacons."])


# =====================================================================================
# asteroid defense
# =====================================================================================
@router.get("/defence", response_class=HTMLResponse)
async def defence_page(request: Request, user: str = Depends(current_user)):
    eng = _eng(request)
    s = await eng.settings()
    objs = await eng.defence_objects()
    return await page(request, user, "defence.html", "defence", report=await eng.defence_report(), objs=objs,
                      cfg=s["rules"].get("asteroid_defence") or {}, read_at=max((o.get("read_at") or "" for o in objs.values()), default=""))


@router.post("/defence/read", response_class=HTMLResponse)
async def defence_read(request: Request, user: str = Depends(current_user)):
    eng = _eng(request)
    async with eng.lock:
        await eng.sync_objects()
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/defence/track", response_class=HTMLResponse)
async def defence_track(request: Request, user: str = Depends(current_user)):
    des = ((await request.form()).get("designation") or "").strip().upper()
    if "-OBJ-" not in des:
        return _lines([f"'{des}' doesn't look like an object code (e.g. DELTA-OBJ-3)"], ok=False)
    eng = _eng(request)
    objs = await request.app.state.db.kv_get("objects", {}) or {}
    objs.setdefault(des, {"designation": des, "status": "active", "history": [], "detected_at": now_iso(), "source": "added by hand"})
    await request.app.state.db.kv_set("objects", objs)
    async with eng.lock:
        await eng.sync_objects(only=des)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/defence/{des}/act", response_class=HTMLResponse)
async def defence_act(request: Request, des: str, user: str = Depends(current_user)):
    eng = _eng(request)
    async with eng.lock:
        out = await eng.sync_objects(manual=True, only=des.upper())
    return _lines([x for x in out["lines"] if x.startswith(("defence", des.upper()))] or out["lines"])


# =====================================================================================
# maintenance
# =====================================================================================
@router.get("/maintenance", response_class=HTMLResponse)
async def maintenance_page(request: Request, user: str = Depends(current_user)):
    from . import upkeep as up
    eng = _eng(request)
    s = await eng.settings()
    cfg = s["rules"].get("maintenance") or {}
    st = await load_state(request)
    return await page(request, user, "maintenance.html", "maintenance", report=up.report(st["devices"], float(cfg.get("threshold") or 80)),
                      cfg=cfg)


@router.post("/maintenance/run", response_class=HTMLResponse)
async def maintenance_run(request: Request, user: str = Depends(current_user)):
    star = ((await request.form()).get("star") or "").strip().upper() or None
    eng = _eng(request)
    async with eng.lock:
        lines = await eng.maintenance_pass(manual=True, star=star)
    return _lines(lines)


@router.post("/maintenance/repair", response_class=HTMLResponse)
async def maintenance_repair(request: Request, user: str = Depends(current_user)):
    """Ask a maintenance drone in the device's system to repair it now."""
    form = await request.form()
    target, drone = (form.get("target") or "").strip().upper(), (form.get("drone") or "").strip().upper()
    out = await call_action(request, user, "POST", f"/devices/{drone}", {"command": "repair", "target": target},
                            f"{drone} repair {target}")
    return render_action(request, out)


# =====================================================================================
# trade shop
# =====================================================================================
async def _shops(request: Request) -> list[dict]:
    from . import shop as sh
    st = await load_state(request)
    inv = {i.get("location"): i.get("items") or {} for i in st["inventory"]}
    return sh.shops(st["devices"], inv, await request.app.state.db.kv_get("shop_trades", {}) or {})


async def _read_trades(request: Request, code: str) -> None:
    db = request.app.state.db
    cache = await db.kv_get("shop_trades", {}) or {}
    try:
        body = await request.app.state.api.get(f"/devices/{code}/trades")
        cache[code] = {"trades": (body or {}).get("trades") or [], "at": now_iso()}
    except ApiError as e:
        cache[code] = {**(cache.get(code) or {}), "error": e.message, "at": now_iso()}
    await db.kv_set("shop_trades", cache)


@router.get("/shop", response_class=HTMLResponse)
async def shop_page(request: Request, user: str = Depends(current_user)):
    from . import shop as sh
    traders = await request.app.state.db.kv_get("traders_cache", {}) or {}
    st = await load_state(request)
    types = sorted({d.get("device_type") for d in st["devices"] if d.get("device_type")})
    from datetime import datetime, timezone
    for s in await _shops(request):   # read a shop's trades when we have none or they're over 10 minutes old
        at = s.get("trades_at")
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(at)).total_seconds() if at else None
        if age is None or age > 600:
            await _read_trades(request, s["code"])
    return await page(request, user, "shop.html", "shop", shops=await _shops(request), traders=traders,
                      resources=sh.RESOURCES, describe=sh.describe_side, device_types=types,
                      mine=set(st["replicants"]))


@router.post("/shop/{code}/refresh", response_class=HTMLResponse)
async def shop_refresh(request: Request, code: str, user: str = Depends(current_user)):
    await _read_trades(request, code.upper())
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/shop/{code}/configure", response_class=HTMLResponse)
async def shop_configure(request: Request, code: str, user: str = Depends(current_user)):
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        return _lines(["A shop needs a name."], ok=False)
    cfg = {"name": name}
    for k in ("description", "announcement"):
        if (form.get(k) or "").strip():
            cfg[k] = form.get(k).strip()
    out = await call_action(request, user, "POST", f"/devices/{code}",
                            {"command": "set_directive", "directive": "trade", "configuration": cfg}, f"shop {code}: {name}")
    if out["ok"]:
        try:
            await request.app.state.worker.sync_devices()
        except Exception:  # noqa: BLE001 — the next poll catches up
            pass
    return render_action(request, out, "Shop configured. Re-send to change the name, description or announcement." if out["ok"] else None)


@router.post("/shop/{code}/trades", response_class=HTMLResponse)
async def shop_add_trade(request: Request, code: str, user: str = Depends(current_user)):
    from . import shop as sh
    form = dict(await request.form())
    name = (form.get("name") or "").strip()
    try:
        stock = max(1, int(form.get("stock") or 1))
    except ValueError:
        stock = 1
    criteria, rewards = sh.parse_side(form, "cr"), sh.parse_side(form, "rw")
    if not name or not (criteria["resources"] or criteria["devices"]) or not (rewards["resources"] or rewards["devices"]):
        return _lines(["A trade needs a name, something the buyer pays and something they get."], ok=False)
    shop = next((s for s in await _shops(request) if s["code"] == code.upper()), None)
    problems = sh.check_escrow(rewards, stock, shop) if shop else []
    if problems and not form.get("anyway"):
        return _lines(["Not enough to put in escrow at the shop:"] + problems
                      + ["Tick 'try anyway' to send it regardless (the game decides)."], ok=False)
    out = await call_action(request, user, "POST", f"/devices/{code}/trades",
                            {"name": name, "stock": stock, "criteria": criteria, "rewards": rewards}, f"shop {code}: new trade {name}")
    if out["ok"]:
        await _read_trades(request, code.upper())
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    return render_action(request, out)


@router.post("/shop/{code}/trades/{trade}/delete", response_class=HTMLResponse)
async def shop_delete_trade(request: Request, code: str, trade: str, user: str = Depends(current_user)):
    out = await call_action(request, user, "DELETE", f"/devices/{code}/trades/{trade}", None, f"shop {code}: remove {trade}")
    if out["ok"]:
        await _read_trades(request, code.upper())
        return HTMLResponse("", headers={"HX-Refresh": "true"})
    return render_action(request, out)


@router.post("/shop/buy", response_class=HTMLResponse)
async def shop_buy(request: Request, user: str = Depends(current_user)):
    form = await request.form()
    ctrl, trade = (form.get("controller") or "").strip().upper(), (form.get("trade_code") or "").strip()
    out = await call_action(request, user, "POST", f"/devices/{ctrl}/trades/{trade}", None, f"buy {trade} at {ctrl}")
    note = None
    if not out["ok"]:
        note = "Trades are paid from what you hold at the shop's location — bring the payment there first."
    return render_action(request, out, note)
