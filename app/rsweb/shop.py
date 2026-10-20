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

import re
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


GONE_KV = "shops_gone"   # {controller: {"at": iso, "location": where it was, "manual": you removed it}}
KNOWN_KV = "shops_known"  # {controller: the newest sighting}: a shop is kept until you remove it (or Bill moves)


def is_bills(t: dict) -> bool:
    from .trail import BILL_CODE
    return t.get("owner_replicant_code") == BILL_CODE or str(t.get("owner_name") or "").strip().lower() == "bill"


async def mark_gone(db, controller: str, location: str | None, at: str, manual: bool = False) -> None:
    """The shop counts as closed until it's seen again after `at`: Bill's after his BobNet post that he moved, any
    other only when you remove it (manual). Neither a system scan (`shops: []` with a shop there, ROSALIATE-OORT
    2026-10-20) nor a device scan nor a failed trade proves a shop is gone (owner, 2026-10-20)."""
    gone = await db.kv_get(GONE_KV, {}) or {}
    gone[controller] = {"at": at, "location": location, **({"manual": True} if manual else {})}
    await db.kv_set(GONE_KV, gone)


POSTED_KV = "shops_posted"   # {controller or "post:<owner>": the shop as its owner's BobNet post placed it}
OPEN_RE = re.compile(r"([^:!.]+?)\s+is open for shoppers", re.I)
SPOT_RE = re.compile(r"\bfind me (?:in|at) the (oort|kuiper)\b", re.I)


def open_post(payload: dict, stars: set[str]) -> dict | None:
    """A shop owner announces where the shop opened on BobNet (seen 2026-10-20, #trade, Dash @ MAZARAR: "Rock Droppers
    is open for shoppers! … Find me in the Oort."): {"shop_name", "star", "location", "owner_name",
    "owner_replicant_code", "text"}, else None. The star is the poster's (`current_star`), else a known star the post
    names; "the Oort" / "the Kuiper" places it at STAR-OORT / STAR-KUIPER, otherwise only the star is known."""
    text = str(payload.get("message") or "")
    m = OPEN_RE.search(text)
    if not m:
        return None
    star = str(payload.get("current_star") or "").upper()
    if not star:
        named = [w for w in re.findall(r"[A-Z][A-Z0-9]{2,}", text) if w in stars]
        star = named[0] if named else ""
    if not star:
        return None
    spot = SPOT_RE.search(text)
    return {"shop_name": m.group(1).strip(), "star": star, "location": f"{star}-{spot.group(1).upper()}" if spot else star,
            "owner_name": payload.get("replicant_name"), "owner_replicant_code": payload.get("replicant_code"),
            "text": text}


async def shop_opened(db, post: dict, at: str) -> str:
    """Move the poster's shop (same owner or shop name) to where the post says it opened; a shop you don't know yet is
    listed under "post:<owner>" until a scan or the directory shows its controller. Returns the shop's key."""
    def norm(x: Any) -> str:
        return str(x or "").strip().lower()
    known = await known_traders(db)
    mine = [c for c, t in known.items() if not c.startswith("post:") and (
        (post["owner_replicant_code"] and t.get("owner_replicant_code") == post["owner_replicant_code"])
        or (norm(t.get("shop_name")) and norm(t.get("shop_name")) == norm(post["shop_name"]))
        or (norm(t.get("owner_name")) and norm(t.get("owner_name")) == norm(post["owner_name"])))]
    posted = await db.kv_get(POSTED_KV, {}) or {}
    key = max(mine, key=lambda c: known[c]["seen_at"]) if mine else f"post:{post['owner_replicant_code'] or norm(post['owner_name'])}"
    old = {k: v for k, v in (known.get(key) or {}).items() if k not in ("seen_at", "from_scan")}
    posted[key] = {**old, "controller_code": old.get("controller_code") or (None if key.startswith("post:") else key),
                   "shop_name": old.get("shop_name") or post["shop_name"], "location": post["location"],
                   "star": post["star"], "owner_name": old.get("owner_name") or post["owner_name"],
                   "owner_replicant_code": old.get("owner_replicant_code") or post["owner_replicant_code"],
                   "trades": old.get("trades") or [], "from_post": post["text"][:200], "read_at": at}
    await db.kv_set(POSTED_KV, posted)
    return key


async def known_traders(db) -> dict[str, dict]:
    """Every shop you know of: the trader directory (kv "traders_cache", Fleets › Refresh traders), every shop a
    stored system scan lists (scan "shops") and every shop seen before (kv "shops_known") — the newest sighting of each
    shop wins, so a re-scan or a refresh moves it. A shop stays listed until you remove it (owner, 2026-10-20: a scan
    that doesn't show it, a directory that no longer lists it or a failed trade prove nothing). Bill is the
    exception: after his BobNet post that he moved his shop is closed until it's seen again (mark_gone)."""
    import json
    stored = await db.kv_get(KNOWN_KV, {}) or {}
    seen: dict[str, dict] = {c: t for c, t in stored.items() if isinstance(t, dict) and t.get("seen_at")}

    def offer(code: str, entry: dict, at: str) -> None:
        if code and at >= (seen.get(code) or {}).get("seen_at", ""):   # what this sighting leaves out stays known
            old = {k: v for k, v in (seen.get(code) or {}).items() if not k.startswith("from_")}
            seen[code] = {**old, **{k: v for k, v in entry.items() if v is not None}, "seen_at": at}
    for r in await db.fetchall("SELECT star, data, updated_at FROM systems"):
        try:
            scan = json.loads(r["data"] or "{}")
        except ValueError:
            continue
        if not isinstance(scan, dict) or not isinstance(scan.get("shops"), list):
            continue
        for s in scan["shops"]:
            if isinstance(s, dict):
                offer(s.get("controller_code"), {
                    "controller_code": s.get("controller_code"), "location": s.get("location"),
                    "star": star_of(s.get("location")) or r["star"], "shop_name": s.get("shop_name"),
                    "owner_name": s.get("owner_name"), "owner_replicant_code": s.get("owner_replicant_code"),
                    "trades": s.get("trades") or [], "from_scan": True}, r["updated_at"] or "")
    read = await db.kv_updated("traders_cache") or ""
    for code, t in (await db.kv_get("traders_cache", {}) or {}).items():
        offer(code, t, t.get("read_at") or read)
    for code, t in (await db.kv_get(POSTED_KV, {}) or {}).items():
        offer(code, t, t.get("read_at") or "")
    owners = {t.get("owner_replicant_code") for c, t in seen.items() if not c.startswith("post:")} - {None, ""}
    for code in [c for c, t in seen.items() if c.startswith("post:") and t.get("owner_replicant_code") in owners]:
        del seen[code]   # its controller has been seen since: the placeholder from the post is done
    if seen != stored:
        await db.kv_set(KNOWN_KV, seen)
    gone = await db.kv_get(GONE_KV, {}) or {}

    def closed(c: str, t: dict) -> bool:
        g = gone.get(c) or {}
        return bool(g) and (is_bills(t) or g.get("manual")) and t["seen_at"] <= g.get("at", "")
    return {c: t for c, t in seen.items() if not closed(c, t)}


async def note_scanned(db, api, star: str, devices: list[dict], mine: set[str]) -> list[str]:
    """Other players' trade controllers in a device scan (system scans don't list shops) go to kv "shops_known" as
    a fresh sighting, with their trades when the game gives them. Returns the controllers' codes."""
    from datetime import datetime, timezone
    from .api import ApiError
    found = [d for d in devices if isinstance(d, dict) and d.get("device_code") and is_shop(d)
             and d.get("owner_replicant_code") not in mine]
    if not found:
        return []
    known = await db.kv_get(KNOWN_KV, {}) or {}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for d in found:
        code = d["device_code"]
        try:
            trades = ((await api.get(f"/devices/{code}/trades")) or {}).get("trades")
        except ApiError:
            trades = None
        new = {"controller_code": code, "location": d.get("location"), "star": star_of(d.get("location")) or star,
               "owner_name": d.get("owner_name"), "owner_replicant_code": d.get("owner_replicant_code"),
               "trades": trades, "from_device_scan": True}
        known[code] = {**(known.get(code) or {}), **{k: v for k, v in new.items() if v is not None}, "seen_at": now}
        known[code].setdefault("trades", [])
    await db.kv_set(KNOWN_KV, known)
    return [d["device_code"] for d in found]


LAST_KV = "traders_last"   # the last directory read: {at, asked, found, errors: [{replicant, error}]}


async def refresh_directory(db, api, replicants: list[str]) -> tuple[int, str | None]:
    """Every replicant's trader directory and each trader's trades, at most 20 traders, into kv "traders_cache", added
    to what it held: a shop the directory doesn't list now is out of reach, not gone. The game lists the traders in the
    replicant's own system, and others only where the shop's owner has an FTL relay when the replicant's system has one
    of yours (docs, GET /replicants/{code}/traders). What the read found goes to kv "traders_last".
    Returns (traders read, the last error)."""
    from datetime import datetime, timezone
    from .api import ApiError
    traders, err, errors = {}, None, []
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for rep in replicants:
        try:
            for t in ((await api.get(f"/replicants/{rep}/traders")) or {}).get("traders") or []:
                if t.get("controller_code"):
                    traders.setdefault(t["controller_code"], t)
        except ApiError as e:
            err = e.message
            errors.append({"replicant": rep, "error": e.message})
    await db.kv_set(LAST_KV, {"at": now, "asked": len(replicants), "found": len(traders), "errors": errors[:5],
                              "error_count": len(errors)})
    if not traders:
        return 0, err
    out = await db.kv_get("traders_cache", {}) or {}
    for t in list(traders.values())[:20]:
        code = t.get("controller_code")
        try:
            trades = ((await api.get(f"/devices/{code}/trades")) or {}).get("trades") or []
        except ApiError:
            trades = (out.get(code) or {}).get("trades") or []
        out[code] = {**t, "trades": trades, "read_at": now}
    await db.kv_set("traders_cache", out)
    return min(len(traders), 20), err
