"""Missing blueprints (Economy › Missing blueprints, owner 2026-11-13): a running list of device types the account has
no blueprint for, where each one has been seen, and which devices hold it as a component: priority targets for trade
and research.

/blueprints lists unlocked blueprints only, so any device type outside it is missing. Kept in kv "missing_bps":

    types        {type: {first_seen, last_seen, seen: {key: {kind, at, code?, where?, owner?, owner_name?, note?}}}}
                 sightings, kept after their source forgets them (scans go after a week, location reads get replaced).
                 kind: scan (another player's device in a device scan), traffic (a beacon saw one come or go),
                 place (a device at a location read that isn't ours), shop (a shop's listing pays in it),
                 sim (a simulated device), added (put on the list by hand). At most SEEN_MAX per type.
    hidden       {type: iso} types taken off the list by hand (story devices nobody prints, say)
    refreshed_at when the sources were last read

Worked out on every read (not stored): which blueprints need the type as a component (at any depth), whether the
game refused it as locked lately (kv locked_prints), our own devices of the type, and our own devices whose blueprint
lists it as a component. Decommissioning a device unlocks its own blueprint (terraform monitor, 2026-10-19), and since
v3.6.1 decommissioning a device built from components unlocks every component blueprint: both are a route to the
blueprint now. Nothing here acts on its own: the list only points.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

KV = "missing_bps"
SEEN_MAX = 20
REFRESH_MIN = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def normalize(s: Any) -> dict:
    s = s if isinstance(s, dict) else {}
    s.setdefault("types", {})
    s.setdefault("hidden", {})
    return s


def _side_devices(side: Any) -> dict[str, int]:
    """A trade side's devices, {type: n} or [{device_type, quantity}]."""
    devs = (side or {}).get("devices") if isinstance(side, dict) else None
    if isinstance(devs, dict):
        return {str(t): int(n or 1) for t, n in devs.items()}
    out: dict[str, int] = {}
    for x in devs or []:
        if isinstance(x, dict) and x.get("device_type"):
            out[x["device_type"]] = out.get(x["device_type"], 0) + int(x.get("quantity") or x.get("count") or 1)
    return out


def sightings(known: set[str], mine_devices: set[str], mine_reps: set[str], others: dict, traffic: dict,
              places: dict[str, Any], traders: dict, sim_devices: list[dict], profiles: dict) -> list[tuple[str, str, dict]]:
    """(type, key, sighting) for every device type outside `known` the sources show, ours left out."""
    out: list[tuple[str, str, dict]] = []

    def name(code: str | None) -> str | None:
        return ((profiles or {}).get(code) or {}).get("name") if code else None

    for star, snap in ((others or {}).get("stars") or {}).items():
        for d in (snap or {}).get("raw") or (snap or {}).get("devices") or []:
            t = d.get("device_type") or d.get("type")
            code = d.get("device_code") or d.get("code")
            owner = d.get("owner_replicant_code") or d.get("owner")
            if not t or t in known or code in mine_devices or (owner and owner in mine_reps):
                continue
            out.append((t, f"scan:{code}", {"kind": "scan", "at": snap.get("scanned_at"), "code": code,
                                            "where": d.get("location") or star, "owner": owner,
                                            "owner_name": d.get("owner_name") or name(owner)}))
    for e in (traffic or {}).get("entries") or []:
        t, code, rep = e.get("device_type"), e.get("device_code"), e.get("replicant_code")
        if not t or t in known or code in mine_devices or (rep and rep in mine_reps):
            continue
        out.append((t, f"traffic:{code}", {"kind": "traffic", "at": e.get("logged_at"), "code": code,
                                           "where": e.get("location") or e.get("star"), "owner": rep,
                                           "owner_name": name(rep), "note": e.get("travel_type")}))
    for place, data in (places or {}).items():
        if star_of(place).startswith("VIRT"):
            continue
        for d in (data or {}).get("devices") or [] if isinstance(data, dict) else []:
            t, code = d.get("device_type"), d.get("device_code")
            if not t or t in known or code in mine_devices:
                continue
            out.append((t, f"place:{code}", {"kind": "place", "at": (data or {}).get("_read_at"), "code": code,
                                             "where": place, "note": d.get("status")}))
    for ctrl, sh in (traders or {}).items():
        for tr in (sh or {}).get("trades") or []:
            for t, n in _side_devices(tr.get("rewards")).items():
                if t in known:
                    continue
                out.append((t, f"shop:{ctrl}:{tr.get('trade_code') or tr.get('name')}",
                            {"kind": "shop", "at": sh.get("read_at") or sh.get("seen_at"), "code": ctrl,
                             "where": sh.get("location") or sh.get("star"), "owner": sh.get("owner_replicant_code"),
                             "owner_name": sh.get("owner_name") or sh.get("name"),
                             "note": f"{tr.get('name') or 'trade'}: {n} for {_price(tr.get('criteria'))}"}))
    for d in sim_devices or []:
        t = d.get("device_type")
        if t and t not in known:
            out.append((t, f"sim:{t}", {"kind": "sim", "where": d.get("location")}))
    return out


def _price(side: Any) -> str:
    if not isinstance(side, dict):
        return "?"
    res = side.get("resources") or {}
    parts = [f"{q} {r}" for r, q in (res.items() if isinstance(res, dict) else [])]
    parts += [f"{n} {t.replace('_', ' ')}" for t, n in _side_devices(side).items()]
    return ", ".join(parts) or "nothing"


def merge(s: dict, found: list[tuple[str, str, dict]], known: set[str], now: str | None = None) -> int:
    """Add sightings to the kept list; types that have a blueprint now drop off. Returns how many types are new."""
    now = now or _now()
    new = 0
    for t in [t for t in s["types"] if t in known]:
        s["types"].pop(t)
    for t, key, x in found:
        e = s["types"].get(t)
        if e is None:
            e = s["types"][t] = {"first_seen": now, "seen": {}}
            new += 1
        e["last_seen"] = now
        e["seen"][key] = {k: v for k, v in {**x, "at": x.get("at") or now}.items() if v not in (None, "")}
        if len(e["seen"]) > SEEN_MAX:
            keep = sorted(e["seen"].items(), key=lambda kv: (kv[1].get("kind") == "added", str(kv[1].get("at") or "")),
                          reverse=True)[:SEEN_MAX]
            e["seen"] = dict(keep)
    return new


def needed_by(bps: dict[str, dict]) -> dict[str, dict[str, set[str]]]:
    """Missing type → {"direct": blueprints listing it as a component, "all": every blueprint needing it at any depth}."""
    from .components import components
    direct: dict[str, set[str]] = {}
    for t, b in bps.items():
        for c in components(b):
            if c not in bps:
                direct.setdefault(c, set()).add(t)
    parents: dict[str, set[str]] = {}
    for t, b in bps.items():
        for c in components(b):
            parents.setdefault(c, set()).add(t)
    out = {}
    for m, ds in direct.items():
        seen, todo = set(ds), list(ds)
        while todo:
            for p in parents.get(todo.pop(), ()):
                if p not in seen:
                    seen.add(p)
                    todo.append(p)
        out[m] = {"direct": ds, "all": seen}
    return out


def rows(s: dict, bps: dict[str, dict], devices: list[dict], locked: dict | None = None,
         show_hidden: bool = False) -> list[dict]:
    """The list, best target first."""
    from .components import components
    need = needed_by(bps)
    locked = {t: at for t, at in (locked or {}).items() if t not in bps} if isinstance(locked, dict) else {}
    types = set(s["types"]) | set(need) | set(locked)
    own: dict[str, list[dict]] = {}
    for d in devices or []:
        if d.get("device_type"):
            own.setdefault(d["device_type"], []).append(d)
    out = []
    for t in types:
        if t in bps:
            continue
        hidden = t in s["hidden"]
        if hidden and not show_hidden:
            continue
        e = s["types"].get(t) or {}
        seen = list((e.get("seen") or {}).values())
        nb = need.get(t) or {"direct": set(), "all": set()}
        mine = own.get(t) or []
        holders = [d for p in sorted(nb["direct"]) for d in own.get(p) or [] if t in components(bps.get(p))]
        shops = [x for x in seen if x.get("kind") == "shop"]
        players = sorted({x.get("owner_name") or x.get("owner") for x in seen
                          if x.get("kind") in ("scan", "traffic") and (x.get("owner_name") or x.get("owner"))})
        if mine:
            route = "decommission"
        elif holders:
            route = "decommission a holder"
        elif shops:
            route = "buy"
        elif players or any(x.get("kind") == "place" for x in seen):
            route = "trade"
        else:
            route = "research"
        score = 10 * len(nb["all"]) + (15 if t in locked else 0) + 4 * min(len(players), 5) + 6 * len(shops) \
            + (8 if route.startswith("decommission") else 0) + min(len(seen), 5)
        out.append({"type": t, "hidden": hidden, "score": score, "route": route,
                    "needed_by": sorted(nb["direct"]), "blocks": sorted(nb["all"]),
                    "refused_at": locked.get(t), "own": mine, "holders": holders, "shops": shops,
                    "players": players, "seen": sorted(seen, key=lambda x: str(x.get("at") or ""), reverse=True),
                    "first_seen": e.get("first_seen"), "last_seen": e.get("last_seen")})
    out.sort(key=lambda r: (-r["score"], r["type"]))
    return out


async def refresh(db, max_age_min: float = 0) -> dict:
    """Read every source into kv missing_bps (skipped when it was read less than `max_age_min` ago). Returns the state."""
    from .shapes import normalize_blueprints
    from .traffic import my_codes
    s = normalize(await db.kv_get(KV, {}))
    if max_age_min and s.get("refreshed_at"):
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(s["refreshed_at"])
            if age.total_seconds() < max_age_min * 60:
                return s
        except ValueError:
            pass
    bps = {b["device_type"]: b for b in normalize_blueprints(await db.kv_get("blueprints", []))}
    if not bps:   # never synced: everything would look missing
        return s
    devices = await db.kv_get("devices", []) or []
    mine_devices = {d.get("device_code") for d in devices}
    mine_reps = my_codes(await db.kv_get("account", {}) or {}, await db.kv_get("replicants", {}) or {}, devices)
    places = {}
    for r in await db.fetchall("SELECT key, value, updated_at FROM kv WHERE key LIKE 'loc:%'"):
        try:
            v = json.loads(r["value"]) if isinstance(r["value"], str) else r["value"]
        except (ValueError, TypeError):
            continue
        if isinstance(v, dict) and v.get("devices"):
            places[r["key"][4:]] = {"devices": v["devices"], "_read_at": r["updated_at"]}
    found = sightings(set(bps), mine_devices, mine_reps, await db.kv_get("others", {}) or {},
                      await db.kv_get("traffic", {}) or {}, places, await db.kv_get("traders_cache", {}) or {},
                      await db.kv_get("sim:devices", []) or [], await db.kv_get("replicant_profiles", {}) or {})
    merge(s, found, set(bps))
    s["refreshed_at"] = _now()
    await db.kv_set(KV, s)
    return s


async def add(db, device_type: str, note: str = "") -> None:
    s = normalize(await db.kv_get(KV, {}))
    t = device_type.strip().lower().replace(" ", "_")
    if t:
        merge(s, [(t, "added", {"kind": "added", "note": note.strip()[:200]})], set())
        s["hidden"].pop(t, None)
        await db.kv_set(KV, s)


async def hide(db, device_type: str, hidden: bool = True) -> None:
    s = normalize(await db.kv_get(KV, {}))
    if hidden:
        s["hidden"][device_type] = _now()
    else:
        s["hidden"].pop(device_type, None)
    await db.kv_set(KV, s)
