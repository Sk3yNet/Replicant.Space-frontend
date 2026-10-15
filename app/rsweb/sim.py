"""Simulations (docs /docs/simulations/): timed scenarios run inside a datacenter's replicant interface. The app
prepares a run (compute cores and a replicant at the interface), you start it with a button, the bootstrap logic
(bootstrap.compound) plays it, and when it ends everything from inside it is discarded: only the report is kept —
the score, the build order and when each step of it was reached, and what that teaches the next run.

State: kv `sim` = {"interface": {code, location}, "scenarios": [...], "prep": {...}, "active": {...},
"runs": [report, ...], "learn": {scenario: {variant: [scores]}}}.

Learning: a run follows a build order — compound to `cap` drones (mining + survey, `survey_per_miner` of them survey)
before printing stops. Untried variants run first; after that the best average score, with an occasional retry of the
runner-up so a lucky seed doesn't settle it. Each report records the time every step was reached, and compares it with
the best run so far."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

KV = "sim"
DATACENTER = "MIRFAKA-OBJ-1"   # docs (2026-10-14): the only datacenter so far
CORE = "compute_core"
INTERFACE = "replicant_interface"
# live 2026-10-15: the datacenter's interface is Bill's device (owner Bill, not yours); a device scan from a replicant
# there still finds it (and replaces this if the code changes)
KNOWN_INTERFACE = {"code": "F4791EDF", "location": DATACENTER}


def is_interface(d: dict) -> bool:
    """A replicant interface, however the type is spelled (replicant_interface, "replicant interface")."""
    return INTERFACE in str(d.get("device_type") or "").lower().replace(" ", "_").replace("-", "_")
# docs' scenario table, used until the interface lists its own (GET /devices/{interface}/simulate)
DOC_SCENARIOS = [
    {"code": "mining_rush", "name": "Mining Rush", "description": "Mine 3,000 total resources", "objective_type": "yield_quota",
     "objective_target": 3000, "entry_cost": {"device_type": CORE, "quantity": 1}, "timeout_hours": 4},
    {"code": "mining_sprint", "name": "Mining Sprint", "description": "Mine 10,000 total resources", "objective_type": "yield_quota",
     "objective_target": 10000, "entry_cost": {"device_type": CORE, "quantity": 2}, "timeout_hours": 8},
    {"code": "resource_hunter", "name": "Resource Hunter", "description": "Accumulate 5,000 resources across all locations",
     "objective_type": "yield_quota", "objective_target": 5000, "entry_cost": {"device_type": CORE, "quantity": 1}, "timeout_hours": 8},
    {"code": "hunter_gatherer", "name": "Hunter Gatherer", "description": "Gather 3,000 resources at a single location",
     "objective_type": "yield_quota", "objective_target": 3000, "entry_cost": {"device_type": CORE, "quantity": 1}, "timeout_hours": 8},
]
VARIANTS = [{"cap": 4, "survey_per_miner": 0.34}, {"cap": 6, "survey_per_miner": 0.34}, {"cap": 8, "survey_per_miner": 0.5},
            {"cap": 10, "survey_per_miner": 0.5}]
NAG_EVERY_H = 2.0
SIM_CODE = re.compile(r"^SIM")


def scenario(state: dict, code: str) -> dict:
    return next((s for s in (state.get("scenarios") or DOC_SCENARIOS) if s.get("code") == code),
                next((s for s in DOC_SCENARIOS if s["code"] == code), {"code": code, "name": code, "entry_cost": {}}))


def cost(sc: dict) -> tuple[str, int]:
    c = sc.get("entry_cost") or {}
    return c.get("device_type") or CORE, int(c.get("quantity") or 1)


def sim_codes(state: dict) -> set[str]:
    """Device codes of the run in progress (its loadout and anything printed in it)."""
    a = state.get("active") or {}
    return set(a.get("devices") or [])


def is_sim_device(d: dict, state: dict) -> bool:
    code = d.get("device_code") or ""
    a = state.get("active") or {}
    star = (d.get("location") or "").split("-")[0]
    return code in sim_codes(state) or (bool(a) and (bool(SIM_CODE.match(code)) or star in set(a.get("stars") or [])))


def pick_variant(learn: dict, n_runs: int) -> int:
    """Index into VARIANTS: untried first, then the best average score (lower is better), every 4th run the runner-up."""
    tried = {int(k): v for k, v in (learn or {}).items() if v}
    for i in range(len(VARIANTS)):
        if i not in tried:
            return i
    ranked = sorted(tried, key=lambda i: sum(tried[i]) / len(tried[i]))
    return ranked[1] if len(ranked) > 1 and n_runs % 4 == 3 else ranked[0]


def secs_between(a: str | None, b: str | None) -> float | None:
    try:
        return (datetime.fromisoformat(str(b)) - datetime.fromisoformat(str(a))).total_seconds()
    except (TypeError, ValueError):
        return None


def mark(active: dict, step: str, now: str) -> bool:
    """Record the first time a build-order step was reached. True when it's new."""
    tl = active.setdefault("timeline", [])
    if any(x["step"] == step for x in tl):
        return False
    tl.append({"step": step, "at": now, "t": secs_between(active.get("started_at"), now)})
    return True


def lessons(runs: list[dict], learn: dict, scen: str) -> list[str]:
    """What the finished runs of a scenario say, in a few lines."""
    mine = [r for r in runs if r.get("scenario") == scen]
    done = [r for r in mine if r.get("score_seconds")]
    out = []
    if not mine:
        return out
    out.append(f"{len(mine)} run(s), {len(done)} completed")
    scores = {int(k): v for k, v in (learn or {}).items() if v}
    if scores:
        best = min(scores, key=lambda i: sum(scores[i]) / len(scores[i]))
        v = VARIANTS[best]
        out.append(f"best build order so far: {v['cap']} drones before printing stops "
                   f"(average {sum(scores[best]) / len(scores[best]) / 60:.0f} min over {len(scores[best])} run(s))")
        untried = [VARIANTS[i]["cap"] for i in range(len(VARIANTS)) if i not in scores]
        if untried:
            out.append("not tried yet: " + ", ".join(f"{c} drones" for c in untried))
    if len(done) >= 2:
        best_run = min(done, key=lambda r: r["score_seconds"])
        last = done[-1]
        if last is not best_run:
            slower = [(x["step"], x["t"] - y["t"]) for x in last.get("timeline") or [] for y in best_run.get("timeline") or []
                      if x["step"] == y["step"] and x.get("t") is not None and y.get("t") is not None and x["t"] - y["t"] > 60]
            if slower:
                s, d = max(slower, key=lambda p: p[1])
                out.append(f"last run lost most time against the best at “{s}” (+{d / 60:.0f} min)")
    return out


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def report(active: dict, hist: dict | None, outcome: str) -> dict:
    h = hist or {}
    return {"id": active.get("id"), "scenario": active.get("scenario"), "name": active.get("name"), "seed": active.get("seed"),
            "replicant": active.get("replicant"), "variant": active.get("variant"), "started_at": active.get("started_at"),
            "ended_at": now_iso(), "outcome": outcome, "score_seconds": h.get("score_seconds"),
            "resources_mined": h.get("resources_mined"), "devices_printed": h.get("devices_printed"),
            "timeline": active.get("timeline") or [], "notes": (active.get("log") or [])[-10:]}
