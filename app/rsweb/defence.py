"""Asteroid defence: track incoming objects and work out how many propulsors it takes to divert them in time.

From the game (docs + GET /locations/<STAR-OBJ-n>):
  object: {designation, object_type: "incoming_asteroid", status: "active", impact_target, impact_eta, impact_likelihood (%),
           required_strength, active_propulsors, current_thrust_per_hour, progress_pct, mass_class, composition, …}
  "Players bring propulsor devices to the asteroid's location, deploy them, and activate them. Each running propulsor
  plate accumulates thrust over time" — once impact likelihood reaches 0 % it's diverted (a permanent mining bonus in
  the system). Required strength grows the closer the impact, so early is cheaper.
  Events: system.object_detected {object_designation, size_class, impact_target, impact_eta, discovery_source},
          diversion.activated / .diverted / .partial / .impacted.

The estimate (labelled as such on the page): thrust per propulsor = current_thrust_per_hour ÷ active_propulsors when
the game says so, else the blueprint's figure, else 4/h (the docs' example). Still needed = required_strength ×
(1 − progress_pct/100); propulsors needed = ⌈still needed ÷ (thrust per propulsor × hours left)⌉. When two readings
show progress, the observed rate is used to project when it will be done.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

DEFAULT_THRUST = 4.0
CLOSED_EVENTS = ("diversion.diverted", "diversion.impacted")
DIVERTING = ("divert", "active", "thrust", "propel")


def _ts(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def _num(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def bp_map(bps: Any) -> dict[str, dict]:
    if isinstance(bps, dict):
        return bps
    return {b.get("device_type"): b for b in bps or [] if isinstance(b, dict) and b.get("device_type")}


def diverter_types(bps: Any) -> list[str]:
    """Blueprints that push asteroids off course (propulsor plates), cruise-capable first."""
    bps = bp_map(bps)
    out = []
    for t, b in (bps or {}).items():
        feats = [str(f) for f in b.get("features") or []]
        if "propulsor" in t or any(k in f for f in feats for k in ("divert", "propulsor", "propuls")):
            out.append(t)
    return sorted(out, key=lambda t: ("cruise" not in ((bps.get(t) or {}).get("features") or []), t))


def is_diverter(d: dict, types: list[str]) -> bool:
    t = d.get("device_type") or ""
    return t in types or "propulsor" in t


def thrust_each(obj: dict, bp: dict | None) -> tuple[float, str]:
    act = _num(obj.get("active_propulsors"))
    thr = _num(obj.get("current_thrust_per_hour"))
    if act > 0 and thr > 0:
        return thr / act, "from the game's current thrust"
    for k in ("thrust_per_hour", "thrust"):
        if bp and _num(bp.get(k)) > 0:
            return _num(bp.get(k)), "from the blueprint"
    return DEFAULT_THRUST, "assumed (docs' example: 4/h per propulsor)"


def observed_rate(history: list[dict]) -> float | None:
    """progress % per hour from the first and last readings that differ."""
    pts = [(h.get("at"), _num(h.get("progress_pct"), -1)) for h in history or [] if h.get("at")]
    pts = [(t, p) for t, p in pts if p >= 0 and _ts(t)]
    if len(pts) < 2:
        return None
    (t0, p0), (t1, p1) = pts[0], pts[-1]
    hrs = (_ts(t1) - _ts(t0)).total_seconds() / 3600
    if hrs <= 0.05 or p1 <= p0:
        return None
    return (p1 - p0) / hrs


def assess(obj: dict, devices: list[dict], bps: dict[str, dict], now: datetime | None = None,
           enroute: int = 0) -> dict:
    now = now or datetime.now(timezone.utc)
    bps = bp_map(bps)
    des = obj.get("designation")
    types = diverter_types(bps)
    bp = bps.get(types[0]) if types else None
    eta = _ts(obj.get("impact_eta"))
    hours = (eta - now).total_seconds() / 3600 if eta else None
    likelihood = _num(obj.get("impact_likelihood"), 100.0)
    progress = _num(obj.get("progress_pct"))
    required = _num(obj.get("required_strength"))
    active = int(_num(obj.get("active_propulsors")))
    per, per_src = thrust_each(obj, bp)
    remaining = max(0.0, required * (1 - progress / 100)) if required else None
    ours_here = [d for d in devices if d.get("location") == des and is_diverter(d, types)]
    ours_idle = [d for d in ours_here if not any(k in str(d.get("status") or "").lower() for k in DIVERTING)]
    have = max(active, len(ours_here)) + enroute
    needed = None
    if remaining is not None and hours and hours > 0:
        needed = math.ceil(remaining / (per * hours)) if remaining > 0 else 0
    rate = observed_rate(obj.get("history") or [])
    done_in = (100 - progress) / rate if rate else None
    if str(obj.get("status") or "active") != "active" or obj.get("closed"):
        verdict = obj.get("closed") or str(obj.get("status"))
    elif likelihood <= 0:
        verdict = "diverted"
    elif hours is not None and hours <= 0:
        verdict = "too late"
    elif needed is None:
        verdict = "unknown — read the object"
    elif have >= needed and (active > 0 or len(ours_here) > 0):
        verdict = "on track"
    elif have >= needed:
        verdict = "propulsors on the way"
    else:
        verdict = f"short by {needed - have}"
    if done_in is not None and hours and done_in > hours and verdict == "on track":
        verdict = f"behind — at this rate done in {done_in:.1f} h, impact in {hours:.1f} h"
    return {"designation": des, "star": star_of(des), "target": obj.get("impact_target"), "eta": obj.get("impact_eta"),
            "hours_left": round(hours, 2) if hours is not None else None, "likelihood": likelihood, "progress": progress,
            "required": required, "remaining": round(remaining, 1) if remaining is not None else None,
            "active": active, "ours_here": [d["device_code"] for d in ours_here],
            "ours_idle": [d["device_code"] for d in ours_idle], "enroute": enroute,
            "thrust_each": round(per, 3), "thrust_source": per_src, "needed": needed,
            "short": max(0, (needed or 0) - have) if needed is not None else None,
            "rate_pct_per_hour": round(rate, 2) if rate else None, "done_in_hours": round(done_in, 1) if done_in else None,
            "verdict": verdict, "diverter_types": types, "mass_class": obj.get("mass_class"),
            "composition": obj.get("composition"), "status": obj.get("status") or "active",
            "threat": verdict not in ("diverted", "too late") and not obj.get("closed") and likelihood > 0}


def plan(a: dict, devices: list[dict], bps: dict[str, dict], busy: set[str], cfg: dict,
         ordered: int = 0) -> dict:
    """What to do for one object: activate idle propulsors there, send idle ones in the system, print the rest."""
    from .automations import _reserved
    bps = bp_map(bps)
    out: dict = {"activate": [], "send": [], "print": None, "notes": []}
    if not a["threat"]:
        return out
    types = a["diverter_types"]
    if cfg.get("activate", True):
        out["activate"] = [c for c in a["ours_idle"] if c not in busy]
    short = a["short"] or 0
    if a["needed"] is None and a["verdict"].startswith("unknown"):
        short = 0
    if short and cfg.get("send_idle", True):
        spare = [d for d in devices if is_diverter(d, types) and star_of(d.get("location")) == a["star"]
                 and d.get("location") != a["designation"] and d["device_code"] not in busy
                 and "cruise" in (d.get("features") or []) and not str(d.get("status") or "").startswith(("stowed", "travel", "cruis"))
                 and not any(k in str(d.get("status") or "").lower() for k in DIVERTING) and not _reserved(d)]
        out["send"] = [d["device_code"] for d in spare[:short]]
        short -= len(out["send"])
    short = max(0, short - ordered)
    if short and cfg.get("print_missing", False):
        if not types:
            out["notes"].append("no propulsor blueprint known — can't print one")
        else:
            t = types[0]
            facs = [d for d in devices if "enqueue_print" in (d.get("available_commands") or [])
                    and star_of(d.get("location")) == a["star"]]
            if not facs:
                out["notes"].append(f"no autofactory in {a['star']} to print {t}")
            else:
                n = min(short, max(1, int(cfg.get("max_prints") or 6)))
                cruise = "cruise" in ((bps.get(t) or {}).get("features") or [])
                out["print"] = {"factory": sorted(facs, key=lambda f: len(f.get("print_queue") or []))[0]["device_code"],
                                "device_type": t, "n": n, "cruise": cruise}
                if not cruise:
                    out["notes"].append(f"{t} can't fly: it has to be carried to {a['designation']}")
    elif short:
        out["notes"].append(f"{short} more propulsor(s) needed — printing is off for this rule")
    return out


def plan_steps(a: dict, p: dict) -> list[tuple[str, list[dict], list[str]]]:
    """[(title, steps, devices)] — one job per device so a failure doesn't hold the others up."""
    from .automations import step
    from .loadouts import at_tag
    des = a["designation"]
    jobs = []
    for c in p["activate"]:
        jobs.append((f"defence: activate {c} at {des}", [step(f"activate {c} at {des}", f"/devices/{c}", {"command": "activate"})], [c]))
    for c in p["send"]:
        tr = step(f"{c}: travel → {des}", f"/devices/{c}", {"command": "travel", "destination": des},
                  wait=["travel.arrived"], critical=True, match={"destination": des})
        tr["wait_device"] = c
        tag = step(f"{c}: pin at {des}", f"/devices/{c}", {"configuration": {"add_tags": [at_tag(des)]}}, method="PATCH")
        jobs.append((f"defence: {c} → {des}", [tag, tr, step(f"activate {c} at {des}", f"/devices/{c}", {"command": "activate"})], [c]))
    pr = p.get("print")
    if pr:
        body = {"command": "enqueue_print", "device_type": pr["device_type"], "quantity": pr["n"],
                "tags": [at_tag(des), "divert"]}
        if pr["cruise"]:
            body["oncomplete"] = {"command": "travel", "destination": des}
        jobs.append((f"defence: print {pr['n']}× {pr['device_type']} for {des}",
                     [step(f"{pr['factory']}: print {pr['n']}× {pr['device_type']} → {des}", f"/devices/{pr['factory']}", body)],
                     []))
    return jobs


def from_detected(payload: dict, location: str | None = None) -> dict:
    des = payload.get("object_designation") or payload.get("designation")
    return {"designation": des, "impact_target": payload.get("impact_target"), "impact_eta": payload.get("impact_eta"),
            "size_class": payload.get("size_class"), "status": "active", "source": payload.get("discovery_source"),
            "history": []}


def merge_reading(obj: dict, reading: dict, at: str) -> dict:
    o = {**obj, **{k: v for k, v in reading.items() if v is not None}}
    hist = list(obj.get("history") or [])
    hist.append({"at": at, "progress_pct": reading.get("progress_pct"), "impact_likelihood": reading.get("impact_likelihood"),
                 "active_propulsors": reading.get("active_propulsors")})
    o["history"] = hist[-48:]
    o["read_at"] = at
    return o
