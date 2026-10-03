"""Maintenance coverage: device wear per system and whether a maintenance drone looks after it.

From the game: devices lose operational capacity with activity and location. A maintenance drone on the `patrol`
directive "picks the most damaged device, cruises to it, deactivates it, and repairs it back to 100 %", then moves on
(live: status "repairing (926637CA)", ami_directive._eval_state "repairing:926637CA", repair {target_device_code,
progress_percent, eta_seconds}). A drone without patrol repairs nothing. Vessels with cradle + print self-repair below
30 % at 1 %/h. So per system: is there a maintenance drone, is it patrolling, and how worn are things.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

SELF_REPAIR_AT = 30.0


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def cap(v: Any) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x * 100 if 0 < x <= 1 else x


def is_maint(d: dict) -> bool:
    return "maintenance" in (d.get("device_type") or "") or "service_bot" in (d.get("device_type") or "") \
        or ("repair" in (d.get("features") or []) and "ami" in (d.get("features") or []))


def patrolling(d: dict) -> bool:
    dv = d.get("ami_directive") if isinstance(d.get("ami_directive"), dict) else {}
    return dv.get("name") == "patrol" and str(d.get("ami_directive_status") or "active") == "active"


def placed(d: dict) -> bool:
    st = str(d.get("status") or "")
    return bool(d.get("location")) and not st.startswith(("stowed", "travel", "cruis", "surg")) and not d.get("stowed_in_device_code")


def repairing(d: dict) -> dict | None:
    r = d.get("repair") if isinstance(d.get("repair"), dict) else None
    if r:
        return {"target": r.get("target_device_code"), "progress": r.get("progress_percent"), "eta_seconds": r.get("eta_seconds")}
    st = str(d.get("status") or "")
    if st.startswith("repairing"):
        return {"target": st[st.find("(") + 1:st.rfind(")")] if "(" in st else None, "progress": None, "eta_seconds": None}
    return None


def report(devices: list[dict], threshold: float = 80.0) -> list[dict]:
    by_star: dict[str, list[dict]] = defaultdict(list)
    for d in devices:
        if d.get("location"):
            by_star[star_of(d["location"])].append(d)
    out = []
    for star, ds in sorted(by_star.items()):
        caps = [(d, cap(d.get("operational_capacity"))) for d in ds]
        caps = [(d, c) for d, c in caps if c is not None]
        damaged = sorted(((d, c) for d, c in caps if c < threshold), key=lambda x: x[1])
        drones = [d for d in ds if is_maint(d)]
        on_patrol = [d for d in drones if patrolling(d)]          # a patrolling drone cruises between jobs: still covers
        idle_drones = [d for d in drones if not patrolling(d) and placed(d)]
        if not damaged:
            verdict = "ok"
        elif on_patrol:
            verdict = "covered" if len(damaged) <= 6 * len(on_patrol) else "busy — more drones would help"
        elif idle_drones:
            verdict = "drone not patrolling"
        else:
            verdict = "no maintenance drone"
        out.append({
            "star": star, "devices": len(ds), "min": round(min((c for _, c in caps), default=100.0), 1),
            "avg": round(sum(c for _, c in caps) / len(caps), 1) if caps else None,
            "damaged": [{"code": d["device_code"], "type": d.get("device_type"), "capacity": round(c, 1),
                         "location": d.get("location"), "self_repairs": c < SELF_REPAIR_AT and "cradle" in (d.get("features") or [])}
                        for d, c in damaged],
            "drones": [{"code": d["device_code"], "location": d.get("location"), "status": d.get("status"), "patrol": patrolling(d),
                        "placed": placed(d), "repair": repairing(d), "capacity": cap(d.get("operational_capacity"))} for d in drones],
            "verdict": verdict, "needs_attention": verdict not in ("ok", "covered"),
        })
    return sorted(out, key=lambda r: (not r["needs_attention"], r["min"]))


def to_patrol(devices: list[dict], busy: set[str]) -> list[dict]:
    """Maintenance drones sitting in a system without the patrol directive (not in a fleet, not bound elsewhere)."""
    from .automations import _reserved
    return [d for d in devices if is_maint(d) and placed(d) and not patrolling(d) and d.get("device_code") not in busy
            and not _reserved(d) and "set_directive" in (d.get("available_commands") or ["set_directive"])]
