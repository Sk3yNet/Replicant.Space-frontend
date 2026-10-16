"""Build orders for simulations: an ordered list of steps the owner writes, played one at a time by the run (sim_engine).

One step per line (blank lines and lines starting with # are skipped):
    scan system                       the run's star is scanned (the run scans it first anyway)
    deploy all                        the vessel goes to the best belt and deploys everything it carries
    mine: structural, conductive      deployed drones that aren't mining start, the resources taken in turn
    print 2 mining_drone              the vessel prints that many more of the type, one at a time
    wait: 500 structural              until the belt's stockpile holds that much (`wait: 2000 total` for all of it)
    mine until objective              keeps deploying and starting new drones until the run ends (never finishes)
"""
from __future__ import annotations

import re
from typing import Any

DEFAULT = """scan system
deploy all
mine: structural, conductive, silicates
print 2 mining_drone
print 1 survey_drone
deploy all
mine: structural, conductive, silicates
wait: 500 structural
print 3 mining_drone
mine until objective"""

from .bootstrap import RESOURCES  # noqa: E402  (the game's six)


def parse(text: str) -> tuple[list[dict], list[str]]:
    """(steps, errors). Each step: {kind, raw, ...}."""
    steps, errors = [], []
    for n, line in enumerate((text or "").splitlines(), 1):
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        low = raw.lower()
        if low in ("scan system", "scan"):
            steps.append({"kind": "scan", "raw": raw})
        elif low in ("deploy all", "deploy"):
            steps.append({"kind": "deploy", "raw": raw})
        elif low in ("mine until objective", "mine until done"):
            steps.append({"kind": "mine_all", "raw": raw})
        elif low.startswith("mine:") or low.startswith("mine "):
            res = [r.strip() for r in re.split(r"[,\s]+", low.split(":", 1)[1] if ":" in low else low[5:]) if r.strip()]
            if not res:
                errors.append(f"line {n}: mine needs at least one resource")
            else:
                steps.append({"kind": "mine", "raw": raw, "resources": res})
        elif m := re.fullmatch(r"print\s+(\d+)\s+([a-z_]+)", low):
            steps.append({"kind": "print", "raw": raw, "n": int(m.group(1)), "type": m.group(2)})
        elif m := re.fullmatch(r"wait:?\s+(\d+(?:\.\d+)?)\s+([a-z_]+)(?:\s+at the belt)?", low):
            steps.append({"kind": "wait", "raw": raw, "qty": float(m.group(1)), "resource": m.group(2)})
        else:
            errors.append(f"line {n}: not understood: {raw}")
    if not steps and not errors:
        errors.append("the order has no steps")
    return steps, errors


def _stowed(devs: list[dict], vessel: str) -> list[dict]:
    return [d for d in devs if d.get("device_code") != vessel
            and (d.get("stowed_in_device_code") == vessel or d.get("attached_to_device_code") == vessel)]


def _idle_miners(devs: list[dict], belt: str) -> list[dict]:
    return [d for d in devs if d.get("device_type") == "mining_drone" and d.get("location") == belt
            and not d.get("stowed_in_device_code") and not str(d.get("status") or "").startswith(("mining", "travel"))]


def _deploy(devs: list[dict], vessel: dict, belt: str) -> tuple[list[dict], bool]:
    """Actions to put everything aboard out at the belt: (actions, done)."""
    if vessel.get("location") != belt:
        if str(vessel.get("status") or "").startswith(("travel", "cruis", "surg")):
            return [], False
        return [{"kind": "travel", "device": vessel["device_code"], "to": belt}], False
    aboard = _stowed(devs, vessel["device_code"])
    return [{"kind": "deploy", "device": d["device_code"]} for d in aboard], not aboard


def _start(devs: list[dict], belt: str, resources: list[str], turn: int) -> list[dict]:
    return [{"kind": "start_mining", "device": d["device_code"], "resource": resources[(turn + i) % len(resources)]}
            for i, d in enumerate(sorted(_idle_miners(devs, belt), key=lambda d: d["device_code"]))]


def advance(a: dict, devs: list[dict], vessel: dict, belt: str | None, inv: dict[str, float], scanned: bool,
            now: str) -> tuple[list[dict], str]:
    """Move the run along its build order: finished steps are marked (a["order_done"]) and the next step's actions
    returned, with a note for the log. Nothing to do yet: ([], what it waits for)."""
    steps = a.get("order") or []
    for _ in range(len(steps) + 1):
        i = int(a.get("order_idx") or 0)
        if i >= len(steps):
            return [], "build order complete"
        st = steps[i]
        k = st["kind"]
        acts: list[dict] = []
        done = False
        if k == "scan":
            done = scanned
        elif not belt:
            return [], "waiting for the system scan to name a belt"
        elif k == "deploy":
            acts, done = _deploy(devs, vessel, belt)
        elif k == "mine":
            a["mine_resources"] = st["resources"]
            acts = _start(devs, belt, st["resources"], int(a.get("mine_turn") or 0))
            a["mine_turn"] = int(a.get("mine_turn") or 0) + len(acts)
            done = not acts
        elif k == "print":
            have = len([d for d in devs if d.get("device_type") == st["type"]])
            base = a.setdefault("order_base", {}).setdefault(str(i), have)
            if have >= base + st["n"]:
                done = True
            elif not str(vessel.get("status") or "").startswith("printing"):
                acts = [{"kind": "print", "device_type": st["type"]}]
        elif k == "wait":
            got = sum(inv.values()) if st["resource"] in ("total", "all") else float(inv.get(st["resource"]) or 0)
            done = got >= st["qty"]
            if not done:
                return [], f"{st['raw']} ({got:g} so far)"
        elif k == "mine_all":
            dep, _ = _deploy(devs, vessel, belt)
            res = a.get("mine_resources") or ["structural"]
            acts = dep or _start(devs, belt, res, int(a.get("mine_turn") or 0))
            a["mine_turn"] = int(a.get("mine_turn") or 0) + len(acts)
            return acts, st["raw"]
        if done:
            a.setdefault("order_done", []).append({"step": i, "at": now})
            a["order_idx"] = i + 1
            continue
        return acts, st["raw"]
    return [], "build order complete"
