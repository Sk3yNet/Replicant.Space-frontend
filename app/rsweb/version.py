"""App version, change history and server-run tracking.

Every automation log entry and job is stamped with the version and the server run that wrote it, so anything from
an older version or an earlier run can be told apart. Each start appends a run record ({run, version, fingerprint,
started_at, last_seen, stopped_at}) and logs a "server started" note, including what changed since the previous
version and whether the previous run stopped cleanly (no stopped_at = it was killed / crashed / redeployed hard).

`fingerprint` is a short hash of the app's code, so a code change shows up even if VERSION wasn't bumped.
Bump VERSION and add a CHANGES entry with every release.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timezone
from pathlib import Path

VERSION = "1.9.1"

# newest first: (version, date, summary). Entries before 1.4.0 were reconstructed when versioning was added,
# so their dates are approximate and they group several drops each.
CHANGES: list[tuple[str, str, str]] = [
    ("1.9.1", "2026-10-02", "Fix: print queue Remove sent a 1-based index; dequeue_print is 0-based, so it removed the next item."),
    ("1.9.0", "2026-10-02", "New rule 'Consolidate stockpiles at the autofactory': stray piles in a system (e.g. contract leftovers) are "
                            "hauled to its autofactory by a free in-system transport controller (delivery directive), piles with what "
                            "the factory is waiting for first; open contract locations are left alone. Shown on the Loadouts page."),
    ("1.8.1", "2026-10-02", "Loadout changes reassign controllers: devices made spare are released by their controller (no more spare "
                            "tag flip-flopping while a controller still runs them). New 'Tags & controllers check' on the Loadouts "
                            "page lists any mismatch between tags, controller assignments and the loadouts, and whether the next pass fixes it."),
    ("1.8.0", "2026-10-02", "Belt viability: tracks search times and site lifetimes per belt (from data already read), shows search ÷ "
                            "site life, survey drones needed to keep the miners busy and a verdict on each System page and in "
                            "Diagnostics; 'Belt viability alerts' rule warns once when a belt passes the threshold and names the "
                            "nearest cheaper belt."),
    ("1.7.2", "2026-10-02", "Diagnostics: a belt with no open sites shows the survey searches under way there (drones, % done, when "
                            "the first new site is due) instead of telling you to start a search."),
    ("1.7.1", "2026-10-02", "Devices missing from GET /devices (e.g. freighters surging between systems) are kept for up to 12 h "
                            "at their last position, flagged in transit, unless an event says they're gone — fleets and loadouts "
                            "keep counting them. Sites/salvage only show on the map and Locations list while they're live; sites at "
                            "0% everywhere count as used up; belts your devices are at are re-read every 20 min."),
    ("1.7.0", "2026-10-02", "Fleets: 'End mission & board' — stop the mission now, clear controller directives, bring every device "
                            "back aboard its carriers (stow or attach) and stay where it is. 'Board everyone' does the same for an "
                            "idle fleet. 'Recall now' is now 'Recall & return home'."),
    ("1.6.3", "2026-10-02", "Diagnostics: an idle fleet controller with no directive is shown as waiting for a mission, not as a problem."),
    ("1.6.2", "2026-10-02", "Back to the belt leaves a controller alone while its drones are mining (partial exhaustion, e.g. "
                            "exhausted:['silicates','structural'] while mining the rest). A detach that fails with 'not attached to "
                            "this carrier' counts as done (carriers release cargo on arrival)."),
    ("1.6.1", "2026-10-02", "Fleets: whether a device fits in a hold comes from its own features/commands (stow), not a type list; "
                            "live data confirms drones and controllers stow, transport drones/haulers don't."),
    ("1.6.0", "2026-10-02", "Fleets: cargo vessels (50 hold + 3 attach) and other vessels with a hold count as carriers. The carrying "
                            "budget shows hold slots and attach points separately; stowable riders go in a hold first, transport "
                            "drones/haulers need attach points. Boarding stows or attaches accordingly; unloading deploys or detaches."),
    ("1.5.0", "2026-10-02", "Back to the belt: mining controllers left 'exhausted' at used-up salvage (or stale-exhausted / paused at a "
                            "belt that has re-opened) bring their drones back, re-adopt and relaunch. AMI schedules no longer write off "
                            "a stale 'exhausted'. Salvage recall defaults on. Snapshot reads every belt in mining systems and the "
                            "last 3 h of events. Clean shutdown within Docker's stop timeout."),
    ("1.4.1", "2026-10-02", "Fix: snapshot failed on devices with no location (stowed: location null). A diagnosis error no longer "
                            "loses the capture; failures name the file and line."),
    ("1.4.0", "2026-10-02", "Version + server-run history: log entries and jobs are stamped with version/run; restarts are logged "
                            "with what changed; snapshots carry the history so old information can be told apart."),
    ("1.3.3", "2026-10-02", "Diagnostics page: live read-only snapshot (download as JSON) and a per-drone mining diagnosis."),
    ("1.3.2", "2026-10-02", "Salvage read from its body's resource_sites (resources_remaining_pct); refresh finds belts without a "
                            "stored scan; salvage no longer listed by its body counts as used up."),
    ("1.3.1", "2026-10-02", "Arrivals: idle unmanaged drones join their system's controller; maintenance drones get the patrol "
                            "directive at home (no activate); bound (to:) devices skipped by in-system rules; leaving controllers "
                            "release drones and clear their directive."),
    ("1.3.0", "2026-10-02", "Fleet builder: line-list loadout editor (auto-save, qty 0 removes), attach points available vs needed."),
    ("1.2.0", "2026-10-01", "Contracts tracker + fulfil, re-open sites, mobile fleets (mining/explore/trade), ferry fixes, "
                            "partial device-list guard while the replicant travels."),
    ("1.1.0", "2026-09-30", "AMI schedules, print queue panel, tree by type, loadouts with phases/spares/prints/deliveries, "
                            "salvage when depleted, system resources on the map."),
    ("1.0.0", "2026-09-29", "Web client: dashboard, devices, systems, events stream, since-last-login digest, automations engine."),
]

HERE = Path(__file__).parent


def _fingerprint() -> str:
    h = hashlib.sha256()
    for p in sorted(HERE.rglob("*")):
        if p.is_file() and p.suffix in (".py", ".html", ".js", ".css") and "__pycache__" not in p.parts:
            h.update(p.relative_to(HERE).as_posix().encode())
            h.update(p.read_bytes())
    return h.hexdigest()[:10]


FINGERPRINT = _fingerprint()
BUILD = os.environ.get("APP_BUILD") or os.environ.get("GIT_COMMIT") or ""
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)
MAX_RUNS = 50


def label() -> str:
    return f"v{VERSION}" + (f" ({BUILD[:10]})" if BUILD else "") + f" · {FINGERPRINT}"


def _vt(v: str) -> tuple:
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return (0,)


def changes_since(old: str | None) -> list[tuple[str, str, str]]:
    """CHANGES entries newer than `old` (all of them if old is unknown)."""
    if not old:
        return []
    return [c for c in CHANGES if _vt(c[0]) > _vt(old)]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def register_start(db) -> tuple[dict, dict | None]:
    """Record this run; returns (this run, previous run)."""
    runs = await db.kv_get("server_runs", []) or []
    prev = runs[-1] if runs else None
    me = {"run": RUN_ID, "version": VERSION, "fingerprint": FINGERPRINT, "build": BUILD,
          "started_at": _now(), "last_seen": _now(), "stopped_at": None}
    runs.append(me)
    await db.kv_set("server_runs", runs[-MAX_RUNS:])
    return me, prev


async def heartbeat(db) -> None:
    runs = await db.kv_get("server_runs", []) or []
    for r in reversed(runs):
        if r.get("run") == RUN_ID:
            r["last_seen"] = _now()
            break
    await db.kv_set("server_runs", runs)


async def register_stop(db) -> None:
    runs = await db.kv_get("server_runs", []) or []
    for r in reversed(runs):
        if r.get("run") == RUN_ID:
            r["stopped_at"] = r["last_seen"] = _now()
            break
    await db.kv_set("server_runs", runs)


def start_notes(me: dict, prev: dict | None) -> list[str]:
    """The log lines a start writes."""
    lines = [f"server started — {label()}, run {me['run']}"]
    if not prev:
        lines.append("no earlier run on record (history starts here)")
        return lines
    how = (f"stopped cleanly at {prev['stopped_at']}" if prev.get("stopped_at")
           else f"did not stop cleanly — last seen {prev.get('last_seen')} (killed, crashed or hard redeploy)")
    lines.append(f"previous run {prev.get('run')} (v{prev.get('version')} · {prev.get('fingerprint')}) started {prev.get('started_at')}, {how}")
    if prev.get("version") != VERSION:
        lines.append(f"upgraded v{prev.get('version')} → v{VERSION}")
        for v, d, text in changes_since(prev.get("version")):
            lines.append(f"  v{v} ({d}): {text}")
    elif prev.get("fingerprint") != FINGERPRINT:
        lines.append(f"same version, but the code changed ({prev.get('fingerprint')} → {FINGERPRINT}) — VERSION wasn't bumped")
    return lines


def age_of(entry: dict, runs: list[dict]) -> str:
    """'current' for this run; 'earlier run' for this version's earlier runs; 'older version' otherwise;
    'unversioned' for entries written before versioning existed."""
    if not entry.get("run"):
        return "unversioned"
    if entry.get("run") == RUN_ID:
        return "current"
    return "earlier run" if entry.get("v") == VERSION else "older version"
