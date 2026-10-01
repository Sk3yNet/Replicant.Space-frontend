"""AMI helpers for the automation engine: which controller manages what, whether a controller is idle,
and periodic AMI schedules ("every N minutes, if idle: adopt idle drones, set this directive, launch").

The AMI controllers already do the tactical work (mining cycles, survey routes, hauling). The rules here
only *keep them busy*: re-issuing a directive when one finishes, handing new or idle drones to them.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from .automations import step

# which drones each kind of controller looks after
DRONE_FOR_KIND = {"mining": "mining_drone", "survey": "survey_drone", "transport": "transport"}
FINISHED = ("directive.completed", "directive.cleared", "directive.paused")


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def kind_of(dtype: str | None) -> str:
    t = dtype or ""
    for k in ("mining", "survey", "transport", "maintenance", "trade", "fleet"):
        if k in t:
            return k
    return "other"


def is_controller(d: dict) -> bool:
    return "ami" in (d.get("features") or []) or "set_directive" in (d.get("available_commands") or [])


async def managed_by(db) -> dict[str, str]:
    """device_code -> controller_code, from each controller's latest AMI digest (and adoption events)."""
    out: dict[str, str] = {}
    rows = await db.fetchall("SELECT device_code, event, payload FROM events WHERE event LIKE 'ami.%' ORDER BY seq")
    for r in rows:
        p = json.loads(r["payload"] or "{}")
        ctrl = r["device_code"]
        if r["event"].endswith(".digest"):
            for d in p.get("devices") or []:
                if isinstance(d, dict) and d.get("device_code"):
                    out[d["device_code"]] = ctrl
        elif r["event"] == "ami.adopted":
            for d in p.get("devices") or []:
                code = d.get("device_code") if isinstance(d, dict) else d
                if code:
                    out[code] = ctrl
        elif r["event"] == "ami.released":
            for d in p.get("devices") or []:
                code = d.get("device_code") if isinstance(d, dict) else d
                out.pop(code, None)
    # the device list's own `controller_device_code` is authoritative whenever the game sends it
    for d in await db.kv_get("devices", []) or []:
        if "controller_device_code" in d:
            code = d.get("device_code")
            if d.get("controller_device_code"):
                out[code] = d["controller_device_code"]
            else:
                out.pop(code, None)
    return out


async def controller_idle(db, ctrl: dict) -> tuple[bool, str]:
    """(idle?, why). Idle = not coordinating, or its last directive event says it finished/stopped."""
    status = str(ctrl.get("status") or "")
    if "ami_directive" in ctrl or "ami_directive_status" in ctrl:  # the game's own view, when the device list has it
        dirv = ctrl.get("ami_directive") or {}
        state = str(dirv.get("_eval_state") or "")
        if not dirv or str(ctrl.get("ami_directive_status") or "") not in ("active", ""):
            return True, f"directive {ctrl.get('ami_directive_status') or 'none'}"
        if state.startswith(("exhausted", "idle", "done", "complete", "no_targets", "no_sources")):
            return True, f"{dirv.get('name')}: {state.split(':')[0]}"
        return False, f"{dirv.get('name')}: {state.split(':')[0] or 'running'}"
    row = await db.fetchone("SELECT event, created_at FROM events WHERE device_code=? AND event LIKE 'directive.%' "
                            "ORDER BY seq DESC LIMIT 1", (ctrl.get("device_code"),))
    if row and row["event"] in FINISHED:
        return True, f"last directive {row['event'].split('.')[1]}"
    if status.startswith(("idle", "inactive")) or not status:
        return True, f"status {status or 'unknown'}"
    return False, f"status {status}"


def adoptable(devices: list[dict], ctrl: dict, managed: dict[str, str]) -> list[str]:
    """Idle drones of the right kind at the controller's location that no controller manages."""
    want = DRONE_FOR_KIND.get(kind_of(ctrl.get("device_type")))
    if not want or "ferry" in (ctrl.get("tags") or []):
        return []  # the ferry controller takes freighters (loadouts handles that), not in-system drones
    return sorted(d["device_code"] for d in devices
                  if want in (d.get("device_type") or "") and d.get("location") == ctrl.get("location")
                  and str(d.get("status", "")).startswith("idle") and d.get("device_code") not in managed
                  and d.get("device_code") != ctrl.get("device_code") and not is_controller(d))


def schedule_steps(ctrl: dict, sched: dict, adopt: list[str]) -> list[dict]:
    code = ctrl["device_code"]
    steps = []
    if adopt:
        steps.append(step(f"{code}: adopt {len(adopt)} idle drone(s)", f"/devices/{code}", {"command": "adopt", "devices": adopt}))
    body = {"command": "set_directive", "directive": sched["directive"]}
    if sched.get("configuration"):
        body["configuration"] = sched["configuration"]
    steps.append(step(f"{code}: directive {sched['directive']}", f"/devices/{code}", body, critical=True))
    if sched.get("launch", True):
        steps.append(step(f"{code}: launch", f"/devices/{code}", {"command": "launch"}))
    return steps


def targets_of(sched: dict, devices: list[dict]) -> list[dict]:
    """Controllers a schedule applies to: one device, or every controller of a kind (optionally in one system)."""
    tgt = sched.get("target") or ""
    if tgt.startswith("kind:"):
        kind = tgt[5:]
        star = sched.get("star") or ""
        return [d for d in devices if is_controller(d) and kind_of(d.get("device_type")) == kind
                and (not star or star_of(d.get("location")) == star)
                and not (kind == "transport" and "ferry" in (d.get("tags") or []))]
    return [d for d in devices if d.get("device_code") == tgt]


def due(sched: dict, now: datetime | None = None) -> bool:
    if not sched.get("enabled", True):
        return False
    now = now or datetime.now(timezone.utc)
    last = sched.get("last_run")
    if not last:
        return True
    try:
        t = datetime.fromisoformat(last)
    except ValueError:
        return True
    return now - t >= timedelta(minutes=max(1, int(sched.get("every_minutes") or 30)))
