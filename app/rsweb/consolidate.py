"""Consolidate a system's stockpiles at its autofactory.

Seen live (2026-10-02): FALQUORYX's autofactory sat `waiting_for_resources` at FALQUORYX-BELT-1 while 211 volatiles and 58
rares lay at FALQUORYX-5 — left there by a contract `delivery`. Nothing moved them back.

Each pass, per system with an autofactory: every other stockpile above a minimum is a pickup. A free in-system transport
controller (not the ferry, not a fleet's, has transport drones or haulers, its directive finished) gets a `delivery`
directive — route {collect: <pile>, deliver: <factory location>}, requirement = what's in the pile — and is launched.
Piles at an open contract's location are left alone (that's the contract's staging). Piles holding what the autofactory is
waiting for go first.
"""
from __future__ import annotations

from typing import Any

from .shapes import as_amounts

FINISHED = ("completed", "done", "complete", "idle", "no_targets", "exhausted")


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def is_factory(d: dict) -> bool:
    return "enqueue_print" in (d.get("available_commands") or []) or "autofactory" in (d.get("device_type") or "")


def _fleet(d: dict) -> bool:
    """A fleet's device away from its station (a stationed fleet's devices at home are the system's own)."""
    from .ami_schedule import on_mission
    return on_mission(d)


def factory_needs(fac: dict, stock: dict[str, float], bps: dict[str, dict]) -> dict[str, float]:
    """What the autofactory is short of for its current / next print (empty if unknown or nothing short)."""
    item = (fac.get("printing") or {}).get("device_type") if isinstance(fac.get("printing"), dict) else None
    if not item:
        q = fac.get("print_queue") or []
        first = q[0] if q else None
        item = first.get("device_type") if isinstance(first, dict) else first
    if not item:
        st = str(fac.get("status") or "")
        item = st[st.find("(") + 1:st.rfind(")")] if "(" in st else None
    cost = as_amounts((bps.get(item) or {}).get("resources")) if item else {}
    return {r: v - stock.get(r, 0.0) for r, v in cost.items() if v > stock.get(r, 0.0)}


def free_controller(c: dict, devices: list[dict], busy: set[str]) -> bool:
    if "transport" not in (c.get("device_type") or "") or "controller" not in (c.get("device_type") or ""):
        return False
    if _fleet(c) or "ferry" in (c.get("tags") or []) or c.get("device_code") in busy or c.get("in_control_range") is False:
        return False
    dv = c.get("ami_directive") if isinstance(c.get("ami_directive"), dict) else {}
    if dv.get("name") == "ferry":
        return False
    running = dv.get("name") and str(c.get("ami_directive_status") or "active") == "active" \
        and not str(dv.get("_eval_state") or "").startswith(FINISHED)
    if running:
        return False
    return any(d.get("controller_device_code") == c.get("device_code") and
               any(k in (d.get("device_type") or "") for k in ("transport_drone", "transport_hauler")) for d in devices)


def plan(devices: list[dict], inventory: dict[str, Any], bps: dict[str, dict], busy: set[str],
         protected: set[str] | None = None, min_amount: float = 100, only: set[str] | None = None) -> list[dict]:
    protected = protected or set()
    piles = {loc: as_amounts(items) for loc, items in (inventory or {}).items()}
    out: list[dict] = []
    stars = sorted({star_of(d.get("location")) for d in devices if is_factory(d) and d.get("location") and not _fleet(d)})
    for star in stars:
        if only and star not in only:
            continue
        facs = [d for d in devices if is_factory(d) and star_of(d.get("location")) == star and not _fleet(d)]
        fac = max(facs, key=lambda f: (sum(piles.get(f["location"], {}).values()), f["device_code"]))
        home = fac["location"]
        needs = factory_needs(fac, piles.get(home, {}), bps)
        pickups = []
        for loc, items in piles.items():
            total = sum(items.values())
            if star_of(loc) != star or loc == home or loc in protected or total < min_amount:
                continue
            helps = {r: min(q, needs[r]) for r, q in items.items() if r in needs and q > 0}
            pickups.append((loc, items, total, helps))
        if not pickups:
            continue
        pickups.sort(key=lambda p: (-sum(p[3].values()), -p[2], p[0]))   # what the factory waits for first, then biggest
        ctrls = sorted((c for c in devices if star_of(c.get("location")) == star and free_controller(c, devices, busy)),
                       key=lambda c: (-sum(1 for d in devices if d.get("controller_device_code") == c["device_code"]), c["device_code"]))
        for (loc, items, total, helps), c in zip(pickups, ctrls):
            out.append({"star": star, "controller": c["device_code"], "collect": loc, "deliver": home,
                        "requirement": {r: int(q) for r, q in items.items() if int(q) > 0}, "total": int(total),
                        "helps": {r: int(v + 0.999) for r, v in helps.items()}, "factory": fac["device_code"],
                        "factory_status": fac.get("status")})
        for loc, items, total, helps in pickups[len(ctrls):]:
            out.append({"star": star, "controller": None, "collect": loc, "deliver": home, "total": int(total),
                        "helps": {r: int(v + 0.999) for r, v in helps.items()}, "factory": fac["device_code"],
                        "why": "no free in-system transport controller with drones"})
    return out


def steps(p: dict) -> list[dict]:
    from .automations import step
    c = p["controller"]
    return [step(f"{c}: delivery {p['collect']} → {p['deliver']} ({p['total']} units)", f"/devices/{c}",
                 {"command": "set_directive", "directive": "delivery",
                  "configuration": {"route": {"collect": p["collect"], "deliver": p["deliver"]}, "requirement": p["requirement"]}},
                 critical=True),
            step(f"{c}: launch", f"/devices/{c}", {"command": "launch"})]


def describe(p: dict) -> str:
    if not p.get("controller"):
        return f"{p['collect']} ({p['total']} units) → {p['deliver']}: waiting — {p['why']}"
    extra = (" — has what the autofactory is waiting for: " + ", ".join(f"{q} {r}" for r, q in p["helps"].items())) if p["helps"] else ""
    return f"{p['controller']} hauls {p['collect']} ({p['total']} units) → autofactory at {p['deliver']}{extra}"
