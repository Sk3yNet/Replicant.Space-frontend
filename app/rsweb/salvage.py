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
    out = [s for s in res.get("salvage") or [] if not s.get("depleted") and not s.get("used_up") and (s.get("total") is None or s["total"] > 0)]
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


# --- back to the belt ------------------------------------------------------------------------------------
# Seen live (2026-10-02): after the belts ran dry the controllers were switched to salvage on bodies; with
# recall off the drones stayed at the body once the salvage was used up, and the controller's directive then reported
# `exhausted:[...]:<body>` forever — even after survey drones re-opened sites on the belt. Nothing moved them back.
FINISHED_STATES = ("exhausted", "done", "complete", "no_targets", "idle:no_sources")


def exhausted_place(state: str | None) -> str | None:
    """`exhausted:['carbon', ...]:AEMEROTH-4-2` → AEMEROTH-4-2."""
    s = str(state or "")
    if not s.startswith("exhausted"):
        return None
    place = s.rsplit(":", 1)[-1].strip()
    return place or None


def open_site_count(detail: dict | None) -> int:
    """Open resource sites a belt's detail lists (salvage entries and fully used-up sites don't count)."""
    n = 0
    for s in (detail or {}).get("resource_sites") or []:
        if not isinstance(s, dict):
            n += 1
            continue
        if s.get("site_type") == "salvage" or "-SAL-" in str(s.get("designation") or ""):
            continue
        pct = s.get("resources_remaining_pct")
        if isinstance(pct, dict) and pct and all((v or 0) <= 0 for v in pct.values()):
            continue
        n += 1
    return n


def back_to_belt_plan(ctrls: list[dict], devices: list[dict], managed: dict[str, str], open_sites: dict[str, int],
                      system_belts: dict[str, list[str]], skip: set[str], directive_for: dict[str, str] | None = None) -> list[dict]:
    """Mining controllers that should be mining a belt with open sites but aren't: their directive is exhausted (at a
    body, or a stale exhausted at the belt itself), paused, or a finished salvage. Returns one plan per controller:
    {ctrl, belt, move_ctrl, away (drones to bring back), directive, why}."""
    by = {d.get("device_code"): d for d in devices}
    run_by = dict(managed)
    for d in devices:
        if d.get("controller_device_code"):
            run_by[d["device_code"]] = d["controller_device_code"]
    out = []
    for c in ctrls:
        code = c.get("device_code")
        if code in skip or c.get("in_control_range") is False or c.get("stowed_in_device_code") or c.get("attached_to_device_code"):
            continue
        dv = c.get("ami_directive") if isinstance(c.get("ami_directive"), dict) else {}
        name, state = dv.get("name"), str(dv.get("_eval_state") or "")
        paused = str(c.get("ami_directive_status") or "") == "paused" or str(c.get("status") or "") == "paused"
        place = exhausted_place(state)
        salvage_done = name == "gather_salvage" and state.startswith(FINISHED_STATES)
        if not (place or paused or salvage_done):
            continue
        star = (c.get("location") or "").split("-")[0]
        belt = belt_of(c.get("location"))
        if not belt or open_sites.get(belt, 0) <= 0:   # not at a belt (or its belt is dry): the best belt in its system
            cands = [b for b in system_belts.get(star, []) if open_sites.get(b, 0) > 0]
            belt = max(cands, key=lambda b: (open_sites[b], b)) if cands else None
        if not belt:
            continue
        kids = [k for k, v in run_by.items() if v == code and (by.get(k) or {}).get("device_type") == "mining_drone"]
        away = sorted(k for k in kids if (by.get(k) or {}).get("location") and by[k]["location"] != belt
                      and not str(by[k].get("status") or "").startswith(("travel", "cruis", "surg", "recall")))
        why = (f"exhausted at {place}, its drones are away from {belt}" if place and place != belt else
               f"stale 'exhausted' at {belt}, which now has {open_sites[belt]} open site(s)" if place == belt else
               "finished salvage" if salvage_done else "directive paused")
        d = (directive_for or {}).get(code) or (name if name and name != "gather_salvage" else "gather_evenly")
        out.append({"ctrl": code, "belt": belt, "move_ctrl": c.get("location") != belt, "away": away,
                    "directive": d, "config": (dv.get("config") or {}) if d == name else {}, "why": why,
                    "open_sites": open_sites[belt]})
    return out


def back_to_belt_steps(p: dict) -> list[dict]:
    code, belt, away = p["ctrl"], p["belt"], p["away"]
    steps: list[dict] = []
    if away:  # free them so they can be flown back, then adopt them again at the belt
        steps.append(step(f"{code}: release {len(away)} drone(s) to bring them back", f"/devices/{code}",
                          {"command": "release", "devices": away}))
    if p["move_ctrl"]:
        st = step(f"{code} → {belt}", f"/devices/{code}", {"command": "travel", "destination": belt},
                  wait=["travel.arrived"], match={"destination": belt}, critical=True)
        st["wait_device"] = code
        steps.append(st)
    first = len(steps)
    for k in away:
        steps.append(step(f"{k} → {belt}", f"/devices/{k}", {"command": "travel", "destination": belt}))
    for i, k in enumerate(away):
        w = step(f"wait for {k} at {belt}", "", None, method="WAIT", wait=["travel.arrived"], match={"destination": belt})
        w["wait_device"] = k
        w["seq0_from"] = first + i
        steps.append(w)
    if away:
        steps.append(step(f"{code}: adopt {len(away)} drone(s) back", f"/devices/{code}", {"command": "adopt", "devices": away}))
    body = {"command": "set_directive", "directive": p["directive"]}
    if p.get("config"):
        body["configuration"] = p["config"]
    steps.append(step(f"{code}: {p['directive']} at {belt} ({p['open_sites']} open site(s))", f"/devices/{code}", body, critical=True))
    steps.append(step(f"{code}: launch", f"/devices/{code}", {"command": "launch"}))
    return steps
