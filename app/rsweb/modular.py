"""Getting devices ready to move, for every job (`prepare_moves`, called by create_job):

Survey drones tracking a site can't move ("Cannot cruise while tracking a site - deactivate first", live 2026-10-07).
They keep their site — the miners need it open — until the moment they're moved: `deactivate` goes right before their
first move in the job (the carrier is there by then), and `activate` once they've landed. The loadout pass only moves
a tracking drone when its fleet is leaving the system for good (a new home); mission recalls move them too.

Large devices must be compacted before they move (and unfurled once they're in place).

Devices with the `modular` feature (live 2026-10-07: autofactory and galactic_observatory; the docs add the system hub)
take `compact` / `unfurl` (each ≈30 % of the device's print time; `device.compacting` {completes_at}, then
`device.compacted`; status compacting → compacted → unfurling). They have to be compacted before they're moved.

`with_compaction(steps, devices, bps)` rewrites any job's steps: before a modular device's first move (its own
travel, being stowed or attached) it compacts and waits for `device.compacted`; after it lands (deployed or detached
from a carrier, or the end of a trip it flew itself) it unfurls. WAIT steps' `seq0_from` indexes are remapped.
"""
from __future__ import annotations

from .automations import STEP_TIMEOUT, step


MODULAR_TYPES = ("autofactory", "galactic_observatory", "system_hub")   # live 2026-10-07 + the docs (system hub)


def is_modular(d: dict) -> bool:
    return "modular" in (d.get("features") or []) or "compact" in (d.get("available_commands") or [])


def compacted(d: dict) -> bool:
    return str(d.get("status") or "").startswith(("compacted", "compacting"))


def _subject(st: dict) -> tuple[str | None, str | None]:
    """(device the step moves or lands, "move" | "land") or (None, None)."""
    body, path = st.get("body") or {}, st.get("path") or ""
    if st.get("method", "POST") != "POST" or not path.startswith("/devices/"):
        return None, None
    target = path.split("/")[2]
    cmd = body.get("command")
    if cmd in ("travel", "stow"):
        return target, "move"
    if cmd == "attach":
        return body.get("device"), "move"
    if cmd == "deploy":
        return target, "land"
    if cmd == "detach":
        return body.get("device"), "land"
    return None, None


def compact_seconds(d: dict, bps: dict[str, dict]) -> float:
    """Expected compaction time: 30 % of the print time (the step waits that + 30 min, at least 4 h when unknown; the
    game's own completes_at, when the reply carries one, extends it)."""
    t = float((bps.get(d.get("device_type") or "") or {}).get("print_time") or 0)
    return t * 0.3 if t else 4 * 3600


def compact_step(code: str, bps: dict[str, dict], d: dict, why: str = "before moving") -> dict:
    st = step(f"{code}: compact {why}", f"/devices/{code}", {"command": "compact"}, wait=["device.compacted"],
              timeout=int(max(STEP_TIMEOUT, compact_seconds(d, bps) + 1800)), critical=True)
    st["wait_device"] = code
    return st


def unfurl_step(code: str) -> dict:
    return step(f"{code}: unfurl", f"/devices/{code}", {"command": "unfurl"})


def tracking(d: dict) -> bool:
    return str(d.get("status") or "").startswith(("tracking", "searching"))


def deactivate_step(code: str) -> dict:
    # live 2026-10-07: "Cannot cruise while tracking a site - deactivate first" (moving it closes the site anyway)
    return step(f"{code}: stop tracking its site (deactivate) to move", f"/devices/{code}", {"command": "deactivate"},
                critical=True)


def activate_step(code: str) -> dict:
    return step(f"{code}: activate", f"/devices/{code}", {"command": "activate"})


def with_untracking(steps: list[dict], devices: list[dict]) -> list[dict]:
    """A survey drone tracking (or searching) a site can't move: deactivate it right before its first move — so the
    miners keep their site until the carrier is there — and activate it again once it has landed."""
    by = {d.get("device_code"): d for d in devices}
    held = {c for c, d in by.items() if c and tracking(d)}
    if not held or any((s.get("body") or {}).get("command") == "deactivate" for s in steps):
        return steps
    return _wrap(steps, held, lambda code: deactivate_step(code), activate_step)


def with_compaction(steps: list[dict], devices: list[dict], bps: dict[str, dict] | None = None) -> list[dict]:
    bps = bps or {}
    by = {d.get("device_code"): d for d in devices}
    mods = {c for c, d in by.items() if c and is_modular(d)}
    if not mods or any((s.get("body") or {}).get("command") in ("compact", "unfurl") for s in steps):
        return steps   # nothing modular here, or the job already handles it
    return _wrap(steps, mods, lambda code: None if compacted(by[code]) else compact_step(code, bps, by[code]), unfurl_step)


def prepare_moves(steps: list[dict], devices: list[dict], bps: dict[str, dict] | None = None) -> list[dict]:
    """Every job's steps: drones tracking a site deactivate before moving, large devices compact."""
    return with_compaction(with_untracking(steps, devices), devices, bps)


def _wrap(steps: list[dict], codes: set[str], before_move, after_land) -> list[dict]:
    """Insert before_move(code) ahead of each device's first move and after_land(code) after it lands (deployed,
    detached, or at the end of a trip it flew itself)."""
    touched = [(i, *_subject(s)) for i, s in enumerate(steps)]
    touched = [(i, c, k) for i, c, k in touched if c in codes]
    if not touched:
        return steps
    before: dict[int, list[dict]] = {}
    after: dict[int, list[dict]] = {}
    for code in {c for _, c, _ in touched}:
        mine = [(i, k) for i, c, k in touched if c == code]
        first_move = next((i for i, k in mine if k == "move"), None)
        pre = before_move(code) if first_move is not None else None
        if pre:
            before.setdefault(first_move, []).append(pre)
        for i, k in mine:
            if k == "land":
                after.setdefault(i, []).append(after_land(code))
        last_i, last_k = mine[-1]
        if last_k == "move" and (steps[last_i].get("body") or {}).get("command") == "travel":
            # a trip it flew itself, not boarding anything afterwards: once it's there (after its arrival wait)
            end = last_i
            for j in range(last_i + 1, len(steps)):
                if steps[j].get("method") == "WAIT" and steps[j].get("wait_device") == code:
                    end = j
            after.setdefault(end, []).append(after_land(code))
    out, where = [], {}
    for i, s in enumerate(steps):
        out += before.get(i, [])
        where[i] = len(out)
        out.append(s)
        out += after.get(i, [])
    for s in out:
        if "seq0_from" in s and s["seq0_from"] in where:
            s["seq0_from"] = where[s["seq0_from"]]
    return out
