"""Your trade shop: an ami_trade_controller with the `trade` directive, and the trades it offers.

Game API (docs): set_directive trade {name, description?, announcement?} (re-send to update); with an active FTL relay
in the system the shop is listed in the galactic directory, otherwise it trades locally only.
  GET    /devices/{ctrl}/trades                → {trades: [{name, trade_code, current_stock, initial_stock, criteria, rewards, created_at}]}
  POST   /devices/{ctrl}/trades                → 201; {name, stock, criteria: {resources, devices}, rewards: {resources, devices}}
         rewards are escrowed: you must hold them at the shop's location (for the whole stock)
  DELETE /devices/{ctrl}/trades/{trade_code}   → 204; escrow returned
  POST   /devices/{ctrl}/trades/{trade_code}   → buy (no body)
"""
from __future__ import annotations

from typing import Any

from .shapes import as_amounts

RESOURCES = ["structural", "conductive", "silicates", "carbon", "volatiles", "rares"]


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def is_shop(d: dict) -> bool:
    return "trade_controller" in (d.get("device_type") or "") or "trade" in (d.get("available_directives") or [])


def relay_in(star: str, devices: list[dict]) -> dict | None:
    """An *active* FTL relay or working hub in the system (docs: "If the system has an active FTL relay, the controller
    broadcasts a directory entry")."""
    from . import relaynet
    for d in devices:
        t = d.get("device_type") or ""
        if star_of(d.get("location")) == star and ("relay" in t or "system_hub" in t) and relaynet.reach(d):
            return d
    return None


def shop_config(d: dict) -> dict:
    dv = d.get("ami_directive") if isinstance(d.get("ami_directive"), dict) else {}
    if dv.get("name") != "trade":
        return {}
    return dict(dv.get("config") or dv.get("configuration") or {})


def shops(devices: list[dict], inventory: dict[str, Any], cache: dict[str, dict]) -> list[dict]:
    out = []
    for d in devices:
        if not is_shop(d):
            continue
        loc = d.get("location")
        relay = relay_in(star_of(loc), devices)
        stock_here = as_amounts((inventory or {}).get(loc) or {})
        devs_here: dict[str, int] = {}
        for x in devices:
            if x.get("location") == loc and x.get("device_code") != d["device_code"] and not is_shop(x):
                devs_here[x.get("device_type") or "?"] = devs_here.get(x.get("device_type") or "?", 0) + 1
        c = cache.get(d["device_code"]) or {}
        out.append({"code": d["device_code"], "location": loc, "star": star_of(loc), "status": d.get("status"),
                    "config": shop_config(d), "configured": bool(shop_config(d)),
                    "directive_status": d.get("ami_directive_status"), "relay": relay["device_code"] if relay else None,
                    "listed": bool(relay and shop_config(d)), "inventory": stock_here, "devices_here": devs_here,
                    "trades": c.get("trades") or [], "trades_at": c.get("at"), "trades_error": c.get("error")})
    return out


def parse_side(form: dict, prefix: str) -> dict:
    """Form fields <prefix>_<resource> and <prefix>_dev_type_<i> / <prefix>_dev_n_<i> → {resources, devices}."""
    res = {}
    for r in RESOURCES:
        try:
            v = int(float(form.get(f"{prefix}_{r}") or 0))
        except ValueError:
            v = 0
        if v > 0:
            res[r] = v
    devs: dict[str, int] = {}
    for i in range(4):
        t = (form.get(f"{prefix}_dev_type_{i}") or "").strip()
        try:
            n = int(float(form.get(f"{prefix}_dev_n_{i}") or 0))
        except ValueError:
            n = 0
        if t and n > 0:
            devs[t] = devs.get(t, 0) + n
    return {"resources": res, "devices": devs}


def check_escrow(rewards: dict, stock: int, shop: dict) -> list[str]:
    """Problems with putting `stock` × rewards in escrow at the shop's location (empty = fine)."""
    out = []
    for r, q in (rewards.get("resources") or {}).items():
        have = shop["inventory"].get(r, 0.0)
        if q * stock > have:
            out.append(f"{q * stock:g} {r} needed in escrow ({q} × {stock}), {have:g} at {shop['location']}")
    for t, n in (rewards.get("devices") or {}).items():
        have = shop["devices_here"].get(t, 0)
        if n * stock > have:
            out.append(f"{n * stock} × {t} needed in escrow, {have} at {shop['location']}")
    return out


def describe_side(side: dict | None) -> str:
    side = side or {}
    parts = [f"{q} {r}" for r, q in (side.get("resources") or {}).items()]
    devs = side.get("devices") or {}
    if isinstance(devs, dict):
        parts += [f"{n}× {t.replace('_', ' ')}" for t, n in devs.items()]
    elif isinstance(devs, list):
        parts += [str(x.get("device_type") if isinstance(x, dict) else x).replace("_", " ") for x in devs]
    return ", ".join(parts) or "nothing"


GONE_KV = "shops_gone"   # {controller: {"at": iso, "location": where a trade there failed}}


async def mark_gone(db, controller: str, location: str | None, at: str) -> None:
    """A trade at this shop failed (it may have moved, as Bill's does about daily): its known location isn't offered
    again until a newer system scan or directory read shows the shop."""
    gone = await db.kv_get(GONE_KV, {}) or {}
    gone[controller] = {"at": at, "location": location}
    await db.kv_set(GONE_KV, gone)


async def known_traders(db) -> dict[str, dict]:
    """Every shop you know of: the trader directory (kv "traders_cache", Fleets › Refresh traders) and every shop a
    stored system scan lists (scan "shops") — the newest sighting of each shop wins, so a re-scan or a refresh moves it
    (seen live 2026-10-11: Bill's Skunkworks at NOSOXA-OORT, which moves about daily; other shops may stay put). A shop
    whose trade failed (mark_gone) is left out until it's seen again after that."""
    import json
    seen: dict[str, dict] = {}

    def offer(code: str, entry: dict, at: str) -> None:
        if code and at >= (seen.get(code) or {}).get("seen_at", ""):
            seen[code] = {**entry, "seen_at": at}
    for r in await db.fetchall("SELECT star, data, updated_at FROM systems"):
        try:
            scan = json.loads(r["data"] or "{}")
        except ValueError:
            continue
        for s in scan.get("shops") or []:
            if isinstance(s, dict):
                offer(s.get("controller_code"), {
                    "controller_code": s.get("controller_code"), "location": s.get("location"),
                    "star": star_of(s.get("location")) or r["star"], "shop_name": s.get("shop_name"),
                    "owner_name": s.get("owner_name"), "owner_replicant_code": s.get("owner_replicant_code"),
                    "trades": s.get("trades") or [], "from_scan": True}, r["updated_at"] or "")
    read = await db.kv_updated("traders_cache") or ""
    for code, t in (await db.kv_get("traders_cache", {}) or {}).items():
        offer(code, t, t.get("read_at") or read)
    gone = await db.kv_get(GONE_KV, {}) or {}
    return {c: t for c, t in seen.items() if t["seen_at"] > (gone.get(c) or {}).get("at", "")}


async def refresh_directory(db, api, replicants: list[str]) -> tuple[int, str | None]:
    """Every replicant's trader directory (each lists the traders near it) and each trader's trades, at most 20 traders,
    into kv "traders_cache". Returns (traders read, the last error)."""
    from datetime import datetime, timezone
    from .api import ApiError
    traders, err = {}, None
    for rep in replicants:
        try:
            for t in ((await api.get(f"/replicants/{rep}/traders")) or {}).get("traders") or []:
                if t.get("controller_code"):
                    traders.setdefault(t["controller_code"], t)
        except ApiError as e:
            err = e.message
    if not traders:
        return 0, err
    out, now = {}, datetime.now(timezone.utc).isoformat(timespec="seconds")
    for t in list(traders.values())[:20]:
        code = t.get("controller_code")
        try:
            trades = ((await api.get(f"/devices/{code}/trades")) or {}).get("trades") or []
        except ApiError:
            trades = []
        out[code] = {**t, "trades": trades, "read_at": now}
    await db.kv_set("traders_cache", out)
    return len(out), err
