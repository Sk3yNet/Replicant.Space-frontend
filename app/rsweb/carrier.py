"""Carrier vessels: what they can pick up / launch in their system, and the job that does it.

A selected pick-up at another location is planned like this:
  • the device can travel  → it flies to the vessel, then stows (all such flights start together)
  • the device can't travel → the vessel flies to it, stows it there, then moves on to the next such location
  • optionally the vessel returns to where it started
Launches (deploy) happen first, before the vessel moves; recalls are sent straight away.
"""
from __future__ import annotations

from typing import Any

from .automations import SHORT_TIMEOUT, STEP_TIMEOUT, step


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def attach_points(dev: dict) -> int:
    from .fleets import capacity
    return capacity(dev)


def is_carrier(dev: dict, blueprints: list[dict]) -> bool:
    if _num(dev.get("stow_capacity")) or attach_points(dev) > 0:
        return True
    bp = next((b for b in blueprints if b.get("device_type") == dev.get("device_type")), {})
    if _num(bp.get("stow_capacity")):
        return True
    t = dev.get("device_type") or ""
    return any(k in t for k in ("vessel", "carrier", "surge_platform", "mobile_fleet", "matrix_container"))


def stow_capacity(dev: dict, blueprints: list[dict]) -> float | None:
    if _num(dev.get("stow_capacity")):
        return _num(dev["stow_capacity"])
    bp = next((b for b in blueprints if b.get("device_type") == dev.get("device_type")), {})
    return _num(bp.get("stow_capacity"))


def can_travel(d: dict) -> bool:
    cmds = d.get("available_commands") or []
    return "travel" in cmds or ("cruise" in (d.get("features") or []) and not cmds)


def can_be_stowed(d: dict) -> bool:
    cmds = d.get("available_commands")
    if cmds:  # the game's own list wins when we have it
        return "stow" in cmds
    return "stow" in (d.get("features") or [])


def can_be_attached(d: dict) -> bool:
    """Something a carrier can take on its attach points: not a carrier or vessel itself."""
    from .fleets import capacity, hold
    t = d.get("device_type") or ""
    return not ("vessel" in t or capacity(d) > 0 or hold(d) > 0 or "replicant" in t)


def carrier_rows(vessel: dict, stowed: list[dict], devices: list[dict], can_hold: bool = True,
                 can_attach: bool = False) -> list[dict]:
    """One row per device in the carrier's system that it could carry (plus what it already carries). `mode`: "stow"
    into its hold, or "attach" to its attach points (a device that can stow goes in the hold when there is one)."""
    from .modular import compacted, is_modular
    vcode, vloc = vessel.get("device_code"), vessel.get("location")
    star = star_of(vloc)
    by_code = {d.get("device_code"): d for d in devices}
    carried = {s.get("device_code"): "stow" for s in stowed if isinstance(s, dict)} if can_hold else {}
    carried.update({d.get("device_code"): "attach" for d in devices if can_attach and d.get("attached_to_device_code") == vcode})
    rows = []
    for code, mode in carried.items():
        d = by_code.get(code) or next((s for s in stowed if s.get("device_code") == code), {})
        rows.append({"code": code, "type": d.get("device_type"), "location": vloc,
                     "status": d.get("status") or ("stowed" if mode == "stow" else "attached"), "mode": mode,
                     "carried": True, "can_launch": True, "can_stow": False, "can_recall": False,
                     "same_loc": True, "mobile": can_travel(d), "note": "in the hold" if mode == "stow" else "attached"})
    for d in devices:
        code = d.get("device_code")
        if code in carried or code == vcode or star_of(d.get("location")) != star:
            continue
        mode = "stow" if can_hold and can_be_stowed(d) else "attach" if can_attach and can_be_attached(d) else None
        if not mode:
            continue
        aboard = str(d.get("status", "")).startswith(("stowed", "attached")) or d.get("stowed_in_device_code") \
            or d.get("attached_to_device_code")
        unfolded = mode == "attach" and is_modular(d) and not compacted(d)
        same = d.get("location") == vloc
        mobile = can_travel(d)
        note = ("aboard another carrier" if aboard else
                "compact it first (large device)" if unfolded else
                "here" if same else
                f"at {d.get('location')} — will fly here" if mobile else
                f"at {d.get('location')} — the {'vessel' if mode == 'stow' else 'carrier'} will go and pick it up")
        rows.append({"code": code, "type": d.get("device_type"), "location": d.get("location"), "status": d.get("status"),
                     "mode": mode, "carried": False, "can_launch": False, "can_stow": not aboard and not unfolded,
                     "can_recall": "recall" in (d.get("available_commands") or []) and not aboard,
                     "same_loc": same, "mobile": mobile, "note": note})
    rows.sort(key=lambda r: (not r["carried"], not r["same_loc"], r["location"] or "", r["type"] or "", r["code"]))
    return rows


def plan(vessel_code: str, vessel_loc: str, rows: list[dict], launch: set[str], stow: set[str], recall: set[str],
         travel_path: str, travel_body_key: str = "command", return_after: bool = True,
         radii: dict[str, float] | None = None, max_cruise_au: float | None = None) -> list[dict]:
    """Steps for the selected launches / pick-ups / recalls.

    travel_path: where to POST a vessel move (a device path, or /replicants/{code}/travel for a replicant's host).
    A device that can travel but is too far to cruise over (more than `max_cruise_au`, as loadout deliveries judge it:
    fleets.far_apart) is picked up by the carrier like one that can't travel, instead of being sent on a long cruise.
    """
    from . import fleets as fl
    by = {r["code"]: r for r in rows}
    steps: list[dict] = []

    def vessel_move(dest: str, why: str) -> dict:
        body = {"command": "travel", "destination": dest} if "/devices/" in travel_path else {"destination": dest}
        st = step(f"vessel → {dest} ({why})", travel_path, body, wait=["travel.arrived"],
                  match={"destination": dest}, critical=True)
        st["wait_device"] = vessel_code
        return st

    def stow_step(code: str) -> dict:
        if (by.get(code) or {}).get("mode") == "attach":   # the carrier takes it on its attach points
            st = step(f"{vessel_code}: attach {code}", f"/devices/{vessel_code}", {"command": "attach", "device": code},
                      wait=["device.attached"], timeout=SHORT_TIMEOUT)
            st["wait_device"] = vessel_code
            return st
        st = step(f"stow {code} in {vessel_code}", f"/devices/{code}", {"command": "stow", "target": vessel_code},
                  wait=["device.stowed"], timeout=SHORT_TIMEOUT)
        st["wait_device"] = code
        return st

    # 1. launches (out of the hold) / detaches, before the vessel goes anywhere
    for code in sorted(launch):
        if by.get(code, {}).get("carried"):
            if by[code].get("mode") == "attach":
                steps.append(step(f"{vessel_code}: detach {code}", f"/devices/{vessel_code}", {"command": "detach", "device": code}))
                continue
            st = step(f"launch {code}", f"/devices/{code}", {"command": "deploy"}, wait=["device.deployed"], timeout=SHORT_TIMEOUT)
            st["wait_device"] = code
            steps.append(st)
    # 2. recalls (fire and forget; skipped for anything also picked up)
    for code in sorted(recall - stow):
        if by.get(code, {}).get("can_recall"):
            steps.append(step(f"recall {code}", f"/devices/{code}", {"command": "recall"}))
    picks = [by[c] for c in sorted(stow) if c in by and by[c]["can_stow"]]
    here = [r for r in picks if r["same_loc"]]
    far = [r for r in picks if not r["same_loc"] and r["mobile"]
           and fl.far_apart(r.get("location"), vessel_loc, radii, max_cruise_au)]
    fly_in = [r for r in picks if not r["same_loc"] and r["mobile"] and r not in far]
    fetch = [r for r in picks if not r["same_loc"] and (not r["mobile"] or r in far)]
    # 3. things already here: stow now
    for r in here:
        steps.append(stow_step(r["code"]))
    # 4. mobile devices elsewhere: all fly in together, then stow as each arrives
    first = len(steps)
    for r in fly_in:
        st = step(f"{r['code']} → {vessel_loc} (to be picked up)", f"/devices/{r['code']}",
                  {"command": "travel", "destination": vessel_loc})
        steps.append(st)
    for i, r in enumerate(fly_in):
        w = step(f"wait for {r['code']} to reach {vessel_loc}", "", None, method="WAIT", wait=["travel.arrived"],
                 match={"destination": vessel_loc}, timeout=STEP_TIMEOUT)
        w["wait_device"] = r["code"]
        w["seq0_from"] = first + i  # arrivals that happen while other steps run still count
        steps.append(w)
        steps.append(stow_step(r["code"]))
    # 5. devices that can't move, or are too far to cruise over: the vessel goes to each location in turn, nearest first
    by_loc: dict[str, list[dict]] = {}
    for r in fetch:
        by_loc.setdefault(r["location"], []).append(r)
    cur, todo = vessel_loc, dict(by_loc)
    while todo:
        loc = min(todo, key=lambda x: (fl.cruise_au(cur, x, radii), x))
        steps.append(vessel_move(loc, "pick-up"))
        for r in todo.pop(loc):
            steps.append(stow_step(r["code"]))
        cur = loc
    if by_loc and return_after:
        steps.append(vessel_move(vessel_loc, "return"))
    return steps
