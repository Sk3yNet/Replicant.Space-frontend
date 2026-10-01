"""Switch to salvage when the mining sites run out.

Per system, using what targets.system_resources knows (cached site details + depletion events):
  • a belt is *worked out* when we know at least one of its sites and every known site is depleted
  • salvage is *available* when it isn't depleted and its quantity is unknown or above zero
If a belt is worked out and the system has salvage:
  • an AMI mining controller there gets `gather_salvage` {location, recall} for the biggest salvage
    (adopting idle unmanaged mining drones at its location first) and is launched
  • with no mining controller in the system, idle mining drones at worked-out places fly to the
    salvage and `start_mining` its largest resource
When a salvage is depleted the next pass moves on to the next one.
"""
from __future__ import annotations

import re
from collections import defaultdict

from .automations import step

BELT_RE = re.compile(r"^(.*?-BELT-\d+)")


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def belt_of(loc: str | None) -> str | None:
    m = BELT_RE.match(loc or "")
    return m.group(1) if m else None


def body_of(code: str) -> str:
    """The body a salvage belongs to: AEMEROTH-6-7-SAL-1 → AEMEROTH-6-7. The game wants the body, not the salvage code."""
    return re.sub(r"-SAL-\d+$", "", code or "")


def worked_out(res: dict) -> set[str]:
    """Belts whose known sites are all depleted."""
    known, live = defaultdict(int), defaultdict(int)
    for s in res.get("sites") or []:
        b = belt_of(s.get("belt")) or belt_of(s.get("code"))
        if not b:
            continue
        known[b] += 1
        if not s.get("depleted") and (s.get("total") is None or s["total"] > 0):
            live[b] += 1
    return {b for b in known if live[b] == 0}


def available_salvage(res: dict) -> list[dict]:
    out = [s for s in res.get("salvage") or [] if not s.get("depleted") and (s.get("total") is None or s["total"] > 0)]
    return sorted(out, key=lambda s: (s.get("total") is None, -(s.get("total") or 0), s["code"]))


def main_resource(sal: dict) -> str:
    amounts = sal.get("amounts") or {}
    return max(amounts, key=amounts.get) if amounts else "structural"


def at_worked_out_place(loc: str | None, dry_belts: set[str], dead_salvage: set[str]) -> bool:
    return (belt_of(loc) in dry_belts) or (loc in dead_salvage) or (loc in {body_of(c) for c in dead_salvage})


def ami_steps(ctrl: str, sal: str, recall: bool, adopt: list[str]) -> list[dict]:
    steps = []
    if adopt:
        steps.append(step(f"{ctrl}: adopt {len(adopt)} idle drone(s)", f"/devices/{ctrl}", {"command": "adopt", "devices": adopt}))
    body = body_of(sal)
    steps.append(step(f"{ctrl}: gather salvage at {body} ({sal})", f"/devices/{ctrl}",
                      {"command": "set_directive", "directive": "gather_salvage",
                       "configuration": {"location": body, "recall": recall}}, critical=True))
    steps.append(step(f"{ctrl}: launch", f"/devices/{ctrl}", {"command": "launch"}))
    return steps


def drone_steps(code: str, loc: str | None, sal: dict) -> list[dict]:
    steps = []
    body = body_of(sal["code"])
    if loc not in (sal["code"], body):
        st = step(f"{code} → {body} (salvage {sal['code']})", f"/devices/{code}", {"command": "travel", "destination": body},
                  wait=["travel.arrived"], match={"destination": body}, critical=True)
        st["wait_device"] = code
        steps.append(st)
    res = main_resource(sal)
    steps.append(step(f"{code}: salvage {res} at {body}", f"/devices/{code}",
                      {"command": "start_mining", "resource_type": res}))
    return steps
