"""Build orders for simulations: an ordered list of steps the owner writes, played one at a time by the run (sim_engine).

One step per line (blank lines and lines starting with # are skipped):
    scan system                       the run's star is scanned (the run scans it first anyway)
    deploy all                        the vessel goes to the best belt and deploys everything it carries
    mine: structural, conductive      deployed drones that aren't mining start, the resources taken in turn; a number
                                      weights one (`mine: structural 4, conductive 2, carbon 1, silicates 1` is a
                                      mining drone's cost: 100 structural, 50 conductive, 25 carbon, 25 silicates)
    print 2 mining_drone              the vessel prints that many more of the type, one at a time
    wait: 500 structural              until the belt's stockpile holds that much (`wait: 2000 total` for all of it)
    mine until objective              keeps deploying and starting new drones until the run ends (never finishes)
    survey                            each survey drone deployed at the belt searches it for resource sites
    controller: gather_evenly         a deployed AMI mining controller at the belt adopts the drones there, takes the
                                      directive (gather_evenly by default, or deplete_smallest) and is launched; it keeps
                                      them mining after the vessel leaves (owner, 2026-10-17)
    next system                       the vessel flies to the nearest system it hasn't visited (prints still aboard go along)
    each system:                      marks where `repeat` goes back to
    repeat                            back to `each system:` (or the top): the loop runs until the run ends

Every system counts toward a yield quota (owner, 2026-10-17): the run adds up the stockpiles at every belt it mined.
A system whose scan shows no belt is skipped: the order jumps to its next `next system`.
Saving an order checks that every print's materials are mined by some step before it (check).
"""
from __future__ import annotations

import re

EXPAND_NAME = "Mining Rush: spread out"
EXPAND = """# Mining Rush, spreading out: every system mined counts toward the quota
scan system
deploy all
mine: structural 4, conductive 2, carbon 1, silicates 1
print 2 mining_drone
print 1 survey_drone
deploy all
survey
mine: structural 4, conductive 2, carbon 1, silicates 1
wait: 300 total
print 1 ami_mining_controller
deploy all
controller: gather_evenly
each system:
print 1 ami_mining_controller
print 3 mining_drone
print 1 survey_drone
next system
deploy all
survey
controller: gather_evenly
repeat"""

DEFAULT = """scan system
deploy all
mine: structural 4, conductive 2, carbon 1, silicates 1
print 2 mining_drone
print 1 survey_drone
deploy all
mine: structural 4, conductive 2, carbon 1, silicates 1
wait: 500 structural
print 3 mining_drone
mine until objective"""

from .bootstrap import RESOURCES  # noqa: E402  (the game's six)

DIRECTIVES = ("gather_evenly", "deplete_smallest")   # mining controller directives that need no configuration
CONTROLLER = "ami_mining_controller"


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
        elif low in ("survey", "survey belt", "search", "search belt"):
            steps.append({"kind": "survey", "raw": raw})
        elif low in ("next system", "next"):
            steps.append({"kind": "next", "raw": raw})
        elif low in ("each system:", "each system", "loop:"):
            steps.append({"kind": "label", "raw": raw})
        elif low in ("repeat", "repeat each system"):
            steps.append({"kind": "repeat", "raw": raw})
        elif low.startswith("controller"):
            d = low.split(":", 1)[1].strip() if ":" in low else "gather_evenly"
            if d not in DIRECTIVES:
                errors.append(f"line {n}: controller directive must be one of {', '.join(DIRECTIVES)}")
            else:
                steps.append({"kind": "controller", "raw": raw, "directive": d})
        elif low in ("mine until objective", "mine until done"):
            steps.append({"kind": "mine_all", "raw": raw})
        elif low.startswith("mine:") or low.startswith("mine "):
            res, bad = [], []
            for part in re.split(r"[,;]+", low.split(":", 1)[1] if ":" in low else low[5:]):
                for m_ in re.finditer(r"([a-z_]+)(?:\s*[x×]?\s*(\d+))?", part):
                    if m_.group(1) not in RESOURCES:
                        bad.append(m_.group(1))
                    else:   # weighted: repeated in turn
                        res += [m_.group(1)] * max(1, min(10, int(m_.group(2) or 1)))
            if bad:
                errors.append(f"line {n}: not a resource: {', '.join(bad)} (one of {', '.join(RESOURCES)})")
            elif not res:
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


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


OLD_BUILTINS = ('# Mining Rush, spreading out: every system mined counts toward the quota\nscan system\ndeploy all\nmine: structural, conductive, silicates\nprint 2 mining_drone\nprint 1 survey_drone\ndeploy all\nsurvey\nmine: structural, conductive, silicates\nwait: 300 total\nprint 1 ami_mining_controller\ndeploy all\ncontroller: gather_evenly\neach system:\nprint 1 ami_mining_controller\nprint 3 mining_drone\nprint 1 survey_drone\nnext system\ndeploy all\nsurvey\ncontroller: gather_evenly\nrepeat', 'scan system\ndeploy all\nmine: structural, conductive, silicates\nprint 2 mining_drone\nprint 1 survey_drone\ndeploy all\nmine: structural, conductive, silicates\nwait: 500 structural\nprint 3 mining_drone\nmine until objective')   # the built-in orders before carbon was mined (1.130.0): a stored copy is updated


def check(steps: list[dict], bps: dict[str, dict]) -> list[str]:
    """Prints that can never happen: a resource the blueprint needs that no earlier mine step gathers (a controller
    gathers everything)."""
    from .shapes import as_amounts
    mined: set[str] = set()
    out = []
    for st in steps:
        if st["kind"] == "mine":
            mined |= set(st["resources"])
        elif st["kind"] == "controller":
            mined |= set(RESOURCES)
        elif st["kind"] == "print":
            need = {r for r, q in as_amounts((bps.get(st["type"]) or {}).get("resources") or {}).items() if q > 0}
            if need - mined:
                out.append(f"{st['raw']}: needs {', '.join(sorted(need - mined))}, which no mine step before it gathers")
    return out


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


def _at(devs: list[dict], belt: str, dtype: str) -> list[dict]:
    """Devices of a type deployed at the belt (not aboard anything)."""
    return sorted((d for d in devs if d.get("device_type") == dtype and d.get("location") == belt
                   and not d.get("stowed_in_device_code") and not d.get("attached_to_device_code")),
                  key=lambda d: d["device_code"])


def _survey(a: dict, devs: list[dict], belt: str) -> tuple[list[dict], bool]:
    done = a.setdefault("searched", {}).setdefault(belt, [])
    todo = [d for d in _at(devs, belt, "survey_drone") if d["device_code"] not in done
            and not str(d.get("status") or "").startswith(("travel", "search", "scan"))]
    done += [d["device_code"] for d in todo]
    return [{"kind": "search", "device": d["device_code"]} for d in todo], not todo


def _controller(a: dict, devs: list[dict], vessel: dict, belt: str, directive: str) -> tuple[list[dict], bool, str]:
    """(actions, done, note): the belt's controller adopts the drones there, takes the directive and is launched."""
    ctls = _at(devs, belt, CONTROLLER)
    sent = a.setdefault("controllers", {})
    if not ctls:
        if any(d.get("device_type") == CONTROLLER for d in _stowed(devs, vessel["device_code"])):
            dep, _ = _deploy(devs, vessel, belt)
            return dep, False, "deploying the mining controller"
        return [], True, "no mining controller here: skipped"
    c = ctls[0]["device_code"]
    if sent.get(c) == belt:
        return [], True, ""
    drones = [d["device_code"] for d in _at(devs, belt, "mining_drone") if d.get("controller_device_code") != c]
    acts = []
    if drones:
        acts.append({"kind": "command", "device": c, "body": {"command": "adopt", "devices": drones},
                     "label": f"{c}: adopt {len(drones)} mining drone(s)"})
    acts += [{"kind": "command", "device": c, "body": {"command": "set_directive", "directive": directive},
              "label": f"{c}: directive {directive}"},
             {"kind": "command", "device": c, "body": {"command": "launch"}, "label": f"{c}: launch"}]
    sent[c] = belt
    return acts, False, f"controller {c} takes over {len(drones)} drone(s) at {belt}"


def _next(a: dict, vessel: dict, star: str) -> tuple[list[dict], bool, str]:
    """The vessel to the nearest system not visited yet (a["near"]: designations, nearest first)."""
    if str(vessel.get("status") or "").startswith(("travel", "cruis", "surg")):
        return [], False, "on the way to the next system"
    frm = a.get("next_from")
    if frm and star != frm:
        a.pop("next_from", None)
        return [], True, f"arrived in {star}"
    to = next((x for x in a.get("near") or [] if x not in (a.get("stars") or []) and x != star), None)
    if not to:
        a.pop("next_from", None)
        return [], True, "no system left to visit in reach"
    a["next_from"] = star
    return [{"kind": "travel", "device": vessel["device_code"], "to": to, "visit": to}], False, f"to {to}"


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
        elif k == "label":
            done = True
        elif k == "repeat":
            start = max([j for j, x in enumerate(steps) if x["kind"] == "label"], default=-1) + 1
            for j in range(start, len(steps)):   # each pass through the loop counts its prints afresh
                a.get("order_base", {}).pop(str(j), None)
            a["loops"] = int(a.get("loops") or 0) + 1
            a["order_idx"] = start
            continue
        elif k == "next":
            acts, done, note = _next(a, vessel, star_of(vessel.get("location")))
            if not done:
                return acts, f"{st['raw']}: {note}"
        elif not belt:
            if scanned:   # no belt here: on to the next system
                j = next((j for j in range(i + 1, len(steps)) if steps[j]["kind"] == "next"), None)
                if j is not None:
                    a["order_idx"] = j
                    continue
            return [], "waiting for the system scan to name a belt"
        elif k == "survey":
            acts, done = _survey(a, devs, belt)
        elif k == "controller":
            acts, done, note = _controller(a, devs, vessel, belt, st["directive"])
            if not done:
                return acts, f"{st['raw']}: {note}"
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
