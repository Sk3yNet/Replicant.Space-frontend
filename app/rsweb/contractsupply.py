"""Devices a contract asks for, delivered to its location.

Contracts (Contracts page) can ask for devices at the event's location as well as resources. A trade run carries
resources only, so the devices are supplied here, per contract (kv "contract_supply", {designation: entry}):

    active     delivering — set by "Deliver devices" on the Contracts page, or by a trade fleet's run on the contract
    assigned   {code: {type, at}}  spare / idle fleetless devices sent to it (tagged to:<star> at:<location> contract:<des>)
    printed    [codes]  devices that came out of its prints (tagged contract:<des> by the print)
    print_ok   {type: n}  prints you authorized (cumulative) — nothing is printed without it
    orders     [{type, factory, n, at}]  prints ordered

Each pass (engine stage contract_supply_pass, every few minutes) per active contract: what's at the location, on its way
(tagged contract:<des>) and printing counts as covered; the rest is short. Short → fleetless spare or idle devices
nearest first (within the loadouts supply range) are tagged for it, and the loadouts pass flies or carries them there
and to the exact location (the at: pin). Still short → only as many prints as you authorized, on the nearest autofactory
in range, tagged the same way. When the contract closes, leftover devices lose those tags and become spare.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timezone

from . import loadouts as lo
from .shapes import as_amounts

KV = "contract_supply"
ORDER_DAYS = 3           # a print not seen after this long no longer counts as on its way
ASSIGN_MINUTES = 30      # a device tagged for it whose tag hasn't shown in the device list after this long is retried


def contract_tag(des: str) -> str:
    return "contract:" + re.sub(r"[^a-z0-9\-_:.]", "", str(des).lower())[:23]


def _ts(s: str | None) -> float | None:
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def need_of(prog: dict) -> dict[str, int]:
    return {x["device_type"]: int(x["need"]) for x in (prog.get("best") or {}).get("devices") or [] if x.get("need")}


def free_for(d: dict, t: str, hosts: set[str], busy: set[str], protect: set[str], ignore: set[str],
             stationed: set[str]) -> bool:
    """A device that may be sent to a contract: of the type, fleetless, not on its way anywhere, not run by a
    controller, not hosting a replicant, idle; at a stationed fleet's home only when it is spare there."""
    tags = set(d.get("tags") or [])
    code = d.get("device_code")
    if (d.get("device_type") or "") != t or code in hosts or code in busy or code in protect or ignore & tags:
        return False
    if any(x.startswith(("fleet:", "contract:", "to:")) for x in tags) or lo.GATHER in tags:
        return False
    if lo.FERRY_TAG in tags or "taxi" in tags or d.get("taxi_mode") == "taxi" or d.get("controller_device_code"):
        return False
    if d.get("hosting_replicant") or d.get("location_stale") or not d.get("location") or d.get("in_control_range") is False:
        return False
    if not str(d.get("status") or "").startswith(("idle", "inactive", "stowed", "deployed", "monitoring", "stationary")):
        return False
    return lo.SPARE in tags or lo.star_of(d.get("location")) not in stationed


def plan(e: dict, prog: dict, entry: dict, devices: list[dict], *, hosts: set[str], busy: set[str],
         protect: set[str] = frozenset(), ignore: set[str] = frozenset(), stationed: set[str] = frozenset(),
         blueprints: list[dict] | None = None, inventory: dict[str, dict] | None = None,
         pos: dict[str, dict] | None = None, reach: float = 0.0, now: float | None = None,
         active: bool = False) -> dict:
    """Per device type: need, here, coming, printing, short — and (when active) devices to send and prints to order.
    Pure: the caller tags / prints and records it in the entry."""
    now = now or datetime.now(timezone.utc).timestamp()
    loc, star = e.get("location"), e.get("star") or lo.star_of(e.get("location"))
    tag = contract_tag(e["designation"])
    pos, inventory = pos or {}, inventory or {}
    bps = {b["device_type"]: b for b in blueprints or []}
    by = {d.get("device_code"): d for d in devices}
    need = need_of(prog)
    assigned = {c: a for c, a in (entry.get("assigned") or {}).items()
                if c in by and (tag in (by[c].get("tags") or []) or (_ts(a.get("at")) or 0) > now - ASSIGN_MINUTES * 60)}
    printed = set(entry.get("printed") or []) | {d["device_code"] for d in devices
                                                  if tag in (d.get("tags") or []) and d["device_code"] not in assigned}
    orders = [o for o in entry.get("orders") or [] if (_ts(o.get("at")) or 0) > now - ORDER_DAYS * 86400]
    ordered = Counter()
    for o in orders:
        ordered[o["type"]] += int(o.get("n") or 1)
    came = Counter((by.get(c) or {}).get("device_type") for c in printed if c in by)
    for c in entry.get("printed") or []:   # printed earlier and gone since (consumed, decommissioned): still came out
        if c not in by:
            came[(entry.get("printed_types") or {}).get(c)] += 1
    ours = {c for c in set(assigned) | printed if c in by}
    rows, send, prints, notes = [], [], [], []
    picked: set[str] = set()
    reserved: dict[str, Counter] = {}
    for t, n in sorted(need.items()):
        here = sum(1 for d in devices if d.get("device_type") == t and d.get("location") == loc)
        coming = sorted(c for c in ours if by[c].get("device_type") == t and by[c].get("location") != loc)
        printing = max(0, ordered[t] - came[t])
        short = max(0, n - here - len(coming) - printing)
        row = {"type": t, "need": n, "here": here, "coming": coming, "printing": printing, "sending": [],
               "print": 0, "short": short, "authorized": int((entry.get("print_ok") or {}).get(t) or 0),
               "ordered": ordered[t], "cost": as_amounts((bps.get(t) or {}).get("resources")), "why": ""}
        if active and short:
            cands = [d for d in devices if d["device_code"] not in picked
                     and free_for(d, t, hosts, busy, protect, ignore, stationed)
                     and (not reach or lo._dist(lo.star_of(d.get("location")), star, pos) <= reach
                          or lo._dist(lo.star_of(d.get("location")), star, pos) >= 1e9)]
            cands.sort(key=lambda d: (d.get("location") != loc, lo._dist(lo.star_of(d.get("location")), star, pos),
                                      lo.SPARE not in (d.get("tags") or []), d["device_code"]))
            for d in cands[:short]:
                picked.add(d["device_code"])
                row["sending"].append(d["device_code"])
                send.append(d)
            row["short"] = short = short - len(row["sending"])
        if active and short:
            allowed = max(0, row["authorized"] - ordered[t])
            k = min(short, allowed)
            if k and t not in bps:
                row["why"] = f"no blueprint for {t}"
            elif k:
                f, why = factory_for(t, k, devices, star, busy, bps, inventory, pos, reach, reserved)
                if f:
                    prints.append({"factory": f["device_code"], "factory_star": lo.star_of(f.get("location")),
                                   "device_type": t, "n": k, "star": star, "location": loc,
                                   "tags": [lo.to_tag(star), lo.at_tag(loc), tag], "note": why})
                    row["print"] = k
                    row["short"] = short = short - k
                else:
                    row["why"] = why
            if short and not row["why"]:
                row["why"] = "authorize printing below" if not allowed else "waiting for an autofactory"
        rows.append(row)
    ready = bool(need) and all(r["here"] >= r["need"] for r in rows)
    return {"rows": rows, "send": send, "prints": prints, "assigned": assigned, "printed": sorted(printed),
            "orders": orders, "ready": ready, "tag": tag, "notes": notes,
            "unresolved": sum(r["short"] for r in rows),
            "to_authorize": {r["type"]: r["short"] for r in rows if r["short"] and r["authorized"] <= r["ordered"]}}


def factory_for(t: str, n: int, devices: list[dict], star: str, busy: set[str], bps: dict, inventory: dict,
                pos: dict, reach: float, reserved: dict[str, Counter]) -> tuple[dict | None, str]:
    """The nearest autofactory in range with queue room (one whose stockpile covers the cost first)."""
    cost = as_amounts((bps.get(t) or {}).get("resources"))
    cands = []
    for f in devices:
        if not lo.is_factory(f) or f.get("device_code") in busy or f.get("location_stale") or not f.get("location") \
                or f.get("in_control_range") is False:
            continue
        cap = int((bps.get(f.get("device_type")) or {}).get("queue_size") or f.get("queue_capacity") or 10)
        used = len(f.get("print_queue") or []) + (1 if f.get("printing") or str(f.get("status") or "").startswith("printing") else 0)
        if cap - used < 1:
            continue
        dist = lo._dist(lo.star_of(f.get("location")), star, pos)
        if reach and reach < dist < 1e9:
            continue
        stock = as_amounts(inventory.get(f.get("location")) or {})
        held = reserved.setdefault(f["location"], Counter())
        stocked = all(stock.get(r, 0.0) - held[r] >= v * n for r, v in cost.items())
        cands.append((not stocked, dist, f["device_code"], f))
    if not cands:
        return None, (f"no autofactory with queue room within {reach:g} ly of {star}" if reach
                      else "no autofactory with queue room")
    stocked_not, _, _, f = min(cands, key=lambda c: c[:3])
    for r, v in cost.items():
        reserved[f["location"]][r] += v * n
    return f, ("queued without enough stock there (it waits for materials)" if stocked_not else "")


def send_steps(d: dict, e: dict) -> list[dict]:
    """Tag a device for the contract: the loadouts pass then delivers it to the system and the exact location."""
    rem = [x for x in d.get("tags") or [] if x == lo.SPARE or x.startswith(("home:", "at:"))]
    return [lo.tag_step(d["device_code"], [lo.to_tag(e["star"]), lo.at_tag(e["location"]), contract_tag(e["designation"])], rem)]


def release_steps(d: dict, des: str) -> list[dict]:
    """The contract is over (or delivery stopped): its leftover device loses the contract's tags and becomes spare."""
    rem = [x for x in d.get("tags") or [] if x == contract_tag(des) or x.startswith(("to:", "at:"))]
    return [lo.tag_step(d["device_code"], [lo.SPARE] if lo.SPARE not in (d.get("tags") or []) else None, rem)]
