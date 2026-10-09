"""In-game events (contracts): discovered at a body, fulfilled by delivering what the criteria ask for.

From live data:
  event.discovered {designation, location, title, description, category, event_type, tier,
                    criteria: [{name, resources: {res: n}, devices: [...]}], rewards: {...}}
  event.completed  {designation, location, event_type, tier, consumed: {resources: {...}}, rewards: {...}}
A replicant must be present (at the event's location) to fulfil it; the consumed resources match the criteria.

This module builds the tracker: open events, progress against stock at the location / in the system,
what's missing, and whether a replicant is there. Fulfil: POST /v1/locations/{location}/events/{designation}
(no body) with the materials at the location and a replicant present — confirmed by the player.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from .shapes import as_amounts

DEFAULT_FULFIL = "POST /locations/{location}/events/{designation}"   # confirmed by the player
CLOSED = ("event.completed", "event.expired", "event.failed", "event.cancelled")
DONE_KV = "contracts_done"   # {designation: when}: the game said "already completed by this account"


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def _devices_needed(c: dict) -> dict[str, int]:
    d = c.get("devices") or []
    if isinstance(d, dict):
        return {k: int(v or 0) for k, v in d.items()}
    out: dict[str, int] = defaultdict(int)
    for x in d:
        if isinstance(x, str):
            out[x] += 1
        elif isinstance(x, dict) and x.get("device_type"):
            out[x["device_type"]] += int(x.get("quantity") or x.get("count") or 1)
    return dict(out)


async def load(db) -> dict[str, dict]:
    """designation -> event record, newest info wins; closed ones carry status/rewards/consumed."""
    rows = await db.fetchall("SELECT event, payload, created_at FROM events WHERE event LIKE 'event.%' ORDER BY seq")
    out: dict[str, dict] = {}
    for r in rows:
        p = json.loads(r["payload"] or "{}")
        des = p.get("designation")
        if not des:
            continue
        e = out.setdefault(des, {"designation": des, "status": "open"})
        if r["event"] == "event.discovered":
            e.update({k: v for k, v in p.items() if v is not None})
            e["discovered_at"] = r["created_at"]
        elif r["event"] in CLOSED:
            e.update({k: v for k, v in p.items() if v is not None and k not in ("criteria",)})
            e["status"] = r["event"].split(".")[1]
            e["closed_at"] = r["created_at"]
        else:  # event.updated / progress etc.: keep whatever it says
            e.update({k: v for k, v in p.items() if v is not None})
    for des, at in (await db.kv_get(DONE_KV, {}) or {}).items():
        e = out.get(des)
        if e and e.get("status") == "open":
            e["status"], e["closed_at"] = "completed", at
    for e in out.values():
        e.setdefault("location", "")
        e["star"] = star_of(e.get("location"))
        e.setdefault("title", (e.get("event_type") or e["designation"]).replace("_", " ").title())
    return out


def progress(e: dict, inventory: dict[str, dict], devices: list[dict], replicants: dict) -> dict:
    """Per criterion: needed vs what's at the location / in the system; replicants present."""
    loc, star = e.get("location"), e.get("star")
    at_loc = as_amounts(inventory.get(loc) or {})
    in_sys: dict[str, float] = defaultdict(float)
    piles: dict[str, dict] = {}
    for l, items in inventory.items():
        if star_of(l) == star:
            amounts = as_amounts(items)
            piles[l] = amounts
            for r, q in amounts.items():
                in_sys[r] += q
    crits = []
    for c in e.get("criteria") or [{"name": "default", "resources": {}, "devices": []}]:
        need = {r: float(v) for r, v in as_amounts(c.get("resources") or {}).items() if v}
        rows = []
        for r, n in sorted(need.items()):
            here, sysq = at_loc.get(r, 0.0), in_sys.get(r, 0.0)
            rows.append({"resource": r, "need": n, "at_location": here, "in_system": sysq,
                         "short_here": max(0.0, n - here), "short_system": max(0.0, n - sysq)})
        dneed = _devices_needed(c)
        drows = []
        for t, n in sorted(dneed.items()):
            here = sum(1 for d in devices if d.get("device_type") == t and d.get("location") == loc)
            sysn = sum(1 for d in devices if d.get("device_type") == t and star_of(d.get("location")) == star)
            drows.append({"device_type": t, "need": n, "at_location": here, "in_system": sysn,
                          "short_here": max(0, n - here), "short_system": max(0, n - sysn)})
        crits.append({"name": c.get("name") or "default", "resources": rows, "devices": drows,
                      "ready_here": all(x["short_here"] == 0 for x in rows + drows),
                      "ready_system": all(x["short_system"] == 0 for x in rows + drows)})
    present, nearby = [], []
    for code, r in (replicants or {}).items():
        rloc = r.get("location") or r.get("current_location")
        name = r.get("name") or code
        if rloc == loc:
            present.append({"code": code, "name": name})
        elif star_of(rloc) == star:
            nearby.append({"code": code, "name": name, "location": rloc})
    best = next((c for c in crits if c["ready_here"]), None) or next((c for c in crits if c["ready_system"]), None) or (crits[0] if crits else None)
    state = ("ready" if best and best["ready_here"] and present else
             "needs replicant" if best and best["ready_here"] else
             "deliver" if best and best["ready_system"] else "gather")
    return {"criteria": crits, "best": best, "present": present, "nearby": nearby, "state": state, "piles": piles}


def delivery_plan(e: dict, prog: dict, devices: list[dict]) -> dict:
    """How to get the best criterion's resources to the event location: one AMI transport `delivery` per
    source pile (biggest piles first), until the shortfall is covered."""
    best = prog.get("best") or {}
    loc, star = e.get("location"), e.get("star")
    short = {x["resource"]: x["short_here"] for x in best.get("resources") or [] if x["short_here"] > 0}
    ctrl = next((d for d in devices if star_of(d.get("location")) == star and "transport" in (d.get("device_type") or "")
                 and "controller" in (d.get("device_type") or "") and "ferry" not in (d.get("tags") or [])), None)
    legs = []
    remaining = dict(short)
    for pile, items in sorted(prog.get("piles", {}).items(), key=lambda kv: -sum(kv[1].values())):
        if pile == loc or not remaining:
            continue
        take = {r: min(q, items.get(r, 0.0)) for r, q in remaining.items() if items.get(r, 0.0) > 0}
        if not take:
            continue
        legs.append({"collect": pile, "deliver": loc, "requirement": {r: int(q + 0.999) for r, q in take.items()}})
        for r, q in take.items():
            remaining[r] -= q
            if remaining[r] <= 0:
                remaining.pop(r)
    return {"controller": ctrl, "legs": legs, "missing": {r: q for r, q in remaining.items() if q > 0}, "short": short}


# --- approval before anything fulfils a contract on its own ----------------------------------------------------------
# Auto-fulfil (the Work on contracts rule, trade fleets' auto-fulfil contracts) only takes a contract whose species you
# have approved on the Contracts page (off until you tick it). The species comes from the system scan of the contract's
# body; without one, the body itself is what you approve.
APPROVALS_KV = "contract_approvals"


async def life_map(db) -> dict[str, dict]:
    from .traffic import inhabited
    systems = {}
    for r in await db.fetchall("SELECT star, data FROM systems"):
        try:
            systems[r["star"]] = json.loads(r["data"] or "{}")
        except ValueError:
            continue
    return inhabited(systems)


def species_key(e: dict, life: dict[str, dict]) -> tuple[str, str]:
    """(approval key, label) for a contract: its species, else its body."""
    sp = (life.get(e.get("location") or "") or {}).get("species") or e.get("species")
    if sp:
        return f"species:{str(sp).lower()}", str(sp)
    return f"body:{e.get('location')}", f"the inhabitants of {e.get('location')}"


async def approved(db, e: dict, life: dict[str, dict] | None = None, approvals: dict | None = None) -> bool:
    life = life if life is not None else await life_map(db)
    approvals = approvals if approvals is not None else (await db.kv_get(APPROVALS_KV, {}) or {})
    return species_key(e, life)[0] in approvals
