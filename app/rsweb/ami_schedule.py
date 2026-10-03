"""AMI helpers for the automation engine: which controller manages what, whether a controller is idle,
and periodic AMI schedules ("every N minutes, if idle: adopt idle drones, set this directive, launch").

The AMI controllers already do the tactical work (mining cycles, survey routes, hauling). The rules here
only *keep them busy*: re-issuing a directive when one finishes, handing new or idle drones to them.
"""
from __future__ import annotations

import re

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


def fleet_of(d: dict) -> str | None:
    return next((t for t in d.get("tags") or [] if t.startswith("fleet:")), None)


def in_transit(d: dict) -> bool:
    """Tagged `to:<star>` for a system other than the one it's in: it's leaving, so nothing should put it to work here."""
    here = re.sub(r"[^a-z0-9\-_:.]", "", star_of(d.get("location")).lower())
    return any(t.startswith("to:") and t[3:] and t[3:] != here[:29] for t in d.get("tags") or [])


def reserved(d: dict) -> bool:
    """Not available to the in-system work rules: a fleet member, or a device on its way to another system."""
    return bool(fleet_of(d)) or in_transit(d)


def adoptable(devices: list[dict], ctrl: dict, managed: dict[str, str]) -> list[str]:
    """Idle drones of the right kind at the controller's location that no controller manages
    (and that belong to the same fleet as the controller, or to none)."""
    want = DRONE_FOR_KIND.get(kind_of(ctrl.get("device_type")))
    if not want or "ferry" in (ctrl.get("tags") or []):
        return []  # the ferry controller takes freighters (loadouts handles that), not in-system drones
    return sorted(d["device_code"] for d in devices
                  if want in (d.get("device_type") or "") and d.get("location") == ctrl.get("location")
                  and str(d.get("status", "")).startswith("idle") and d.get("device_code") not in managed
                  and d.get("device_code") != ctrl.get("device_code") and not is_controller(d)
                  and fleet_of(d) == fleet_of(ctrl) and not in_transit(d)
                  and not d.get("attached_to_device_code") and not d.get("stowed_in_device_code"))


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
                and (not star or star_of(d.get("location")) == star) and not reserved(d)
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


def drone_kind(d: dict) -> str | None:
    """Which kind of controller runs this device type (mining / survey / transport), or None."""
    t = d.get("device_type") or ""
    if is_controller(d):
        return None
    for kind, want in DRONE_FOR_KIND.items():
        if want in t:
            return kind
    return None


def handoffs(devices: list[dict], managed: dict[str, str], busy: set[str], skip: set[str] | None = None,
             ignore: set[str] | None = None, limit: int = 10) -> list[dict]:
    """Idle drones in the system they belong to that no controller runs, matched to that system's controller of the
    right kind. A drone delivered to a system lands at its entry point, not where the controller works, so it
    flies to the controller first. Returns [{"drone", "controller", "from", "to"}]."""
    skip, ignore = skip or set(), ignore or set()
    run_by = dict(managed)
    for d in devices:
        if d.get("controller_device_code"):
            run_by[d["device_code"]] = d["controller_device_code"]
    load: dict[str, int] = {}
    for c in run_by.values():
        load[c] = load.get(c, 0) + 1

    def usable_ctrl(c: dict) -> bool:
        return (is_controller(c) and not reserved(c) and "ferry" not in (c.get("tags") or []) and c.get("location")
                and c.get("in_control_range") is not False and not c.get("location_stale")
                and not (ignore & set(c.get("tags") or [])) and not c.get("stowed_in_device_code")
                and not c.get("attached_to_device_code"))

    ctrls = [c for c in devices if usable_ctrl(c)]
    out = []
    for d in sorted(devices, key=lambda x: x.get("device_code") or ""):
        code, kind = d.get("device_code"), drone_kind(d)
        tags = set(d.get("tags") or [])
        here = star_of(d.get("location"))
        homes = {t[5:] for t in tags if t.startswith("home:")}
        if (not kind or not here or code in run_by or code in busy or code in skip or reserved(d) or "spare" in tags
                or ignore & tags or not str(d.get("status") or "").startswith("idle") or d.get("location_stale")
                or d.get("in_control_range") is False or d.get("stowed_in_device_code") or d.get("attached_to_device_code")
                or (homes and here.lower()[:29] not in homes)):
            continue
        cands = [c for c in ctrls if kind_of(c.get("device_type")) == kind and star_of(c.get("location")) == here
                 and fleet_of(c) == fleet_of(d)]
        pin = next((t[3:].upper() for t in tags if t.startswith("at:") and len(t) > 3), None)
        if pin:  # pinned to a spot: only a controller already there may adopt it (it isn't flown away)
            cands = [c for c in cands if c.get("location") == d.get("location") == pin]
        if not cands:
            continue
        c = min(cands, key=lambda c: (c.get("location") != d.get("location"), load.get(c["device_code"], 0), c["device_code"]))
        load[c["device_code"]] = load.get(c["device_code"], 0) + 1
        out.append({"drone": code, "controller": c["device_code"], "from": d.get("location"), "to": c.get("location"),
                    "launch": bool((c.get("ami_directive") or {}).get("name"))})
        if len(out) >= limit:
            break
    return out


def handoff_steps(h: dict) -> list[dict]:
    steps = []
    if h["from"] != h["to"]:
        st = step(f"{h['drone']} → {h['to']} (to join {h['controller']})", f"/devices/{h['drone']}",
                  {"command": "travel", "destination": h["to"]}, wait=["travel.arrived"], match={"destination": h["to"]},
                  critical=True)
        st["wait_device"] = h["drone"]
        steps.append(st)
    steps.append(step(f"{h['controller']}: adopt {h['drone']}", f"/devices/{h['controller']}",
                      {"command": "adopt", "devices": [h["drone"]]}, critical=True))
    if h.get("launch"):  # the controller is already running a directive: launch so the new drone is put to work
        steps.append(step(f"{h['controller']}: launch", f"/devices/{h['controller']}", {"command": "launch"}))
    return steps


WAKE_TYPES = ("maintenance", "ami_")


def _needs_patrol(d: dict) -> bool:
    if "maintenance" not in (d.get("device_type") or ""):
        return False
    st = str(d.get("status") or "")
    dirv = (d.get("ami_directive") or {}).get("name") if isinstance(d.get("ami_directive"), dict) else d.get("ami_directive")
    return dirv != "patrol" and not st.startswith("patrolling")


def wakeups(devices: list[dict], busy: set[str], done: dict[str, str], skip: set[str] | None = None) -> list[dict]:
    """Devices to switch on now that they're in their home system: maintenance drones get the `patrol` directive
    (that's all they need); inactive AMI controllers get `activate`.
    In the system they belong to and not on their way elsewhere. `done` (code -> star) makes it once per arrival,
    so a device you deactivate or re-task yourself is left alone. Returns [{"code", "activate", "patrol"}]."""
    out = []
    for d in sorted(devices, key=lambda x: x.get("device_code") or ""):
        code, t = d.get("device_code"), d.get("device_type") or ""
        here = star_of(d.get("location"))
        homes = {x[5:] for x in d.get("tags") or [] if x.startswith("home:")}
        if (not any(k in t for k in WAKE_TYPES) or not here or code in busy or code in (skip or set()) or reserved(d)
                or d.get("stowed_in_device_code") or d.get("attached_to_device_code") or d.get("in_control_range") is False
                or (homes and here.lower()[:29] not in homes) or done.get(code) == here):
            continue
        st = str(d.get("status") or "")
        maint = "maintenance" in t
        # maintenance drones only need the patrol directive (confirmed in game) — no activate
        activate = not maint and st.startswith("inactive") and "activate" in (d.get("available_commands") or ["activate"])
        patrol = maint and _needs_patrol(d) and st.startswith(("idle", "inactive", "stationary"))
        if activate or patrol:
            out.append({"code": code, "activate": activate, "patrol": patrol})
    return out


def wakeup_steps(w: dict) -> list[dict]:
    steps = []
    if w["activate"]:
        steps.append(step(f"activate {w['code']}", f"/devices/{w['code']}", {"command": "activate"}))
    if w["patrol"]:
        steps.append(step(f"{w['code']}: directive patrol", f"/devices/{w['code']}",
                          {"command": "set_directive", "directive": "patrol"}))
    return steps
