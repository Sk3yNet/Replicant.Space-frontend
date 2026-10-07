"""Large devices that must be compacted before they move (and unfurled once they're in place).

Devices with the `modular` feature (live 2026-10-07: autofactory and galactic_observatory; the docs add the system hub)
take `compact` / `unfurl` (each ≈30 % of the device's print time; `device.compacting` {completes_at}, then
`device.compacted`; status compacting → compacted → unfurling). They have to be compacted before they're moved.

`with_compaction(steps, devices, bps)` rewrites any job's steps: before a modular device's first move (its own
travel, being stowed or attached) it compacts and waits for `device.compacted`; after it lands (deployed or detached
from a carrier, or the end of a trip it flew itself) it unfurls. WAIT steps' `seq0_from` indexes are remapped.
"""
from __future__ import annotations

from .automations import STEP_TIMEOUT, step


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


def compact_step(code: str, bps: dict[str, dict], d: dict) -> dict:
    t = float((bps.get(d.get("device_type") or "") or {}).get("print_time") or 0)
    st = step(f"{code}: compact before moving", f"/devices/{code}", {"command": "compact"}, wait=["device.compacted"],
              timeout=int(max(STEP_TIMEOUT, t * 0.3 + 1800)), critical=True)
    st["wait_device"] = code
    return st


def unfurl_step(code: str) -> dict:
    return step(f"{code}: unfurl", f"/devices/{code}", {"command": "unfurl"})


def with_compaction(steps: list[dict], devices: list[dict], bps: dict[str, dict] | None = None) -> list[dict]:
    bps = bps or {}
    by = {d.get("device_code"): d for d in devices}
    mods = {c for c, d in by.items() if c and is_modular(d)}
    if not mods or any((s.get("body") or {}).get("command") in ("compact", "unfurl") for s in steps):
        return steps   # nothing modular here, or the job already handles it
    touched = [(i, *_subject(s)) for i, s in enumerate(steps)]
    touched = [(i, c, k) for i, c, k in touched if c in mods]
    if not touched:
        return steps
    before: dict[int, list[dict]] = {}
    after: dict[int, list[dict]] = {}
    for code in {c for _, c, _ in touched}:
        mine = [(i, k) for i, c, k in touched if c == code]
        first_move = next((i for i, k in mine if k == "move"), None)
        if first_move is not None and not compacted(by[code]):
            before.setdefault(first_move, []).append(compact_step(code, bps, by[code]))
        for i, k in mine:
            if k == "land":
                after.setdefault(i, []).append(unfurl_step(code))
        last_i, last_k = mine[-1]
        if last_k == "move" and (steps[last_i].get("body") or {}).get("command") == "travel":
            # a trip it flew itself, not boarding anything afterwards: unfurl once it's there (after its arrival wait)
            end = last_i
            for j in range(last_i + 1, len(steps)):
                if steps[j].get("method") == "WAIT" and steps[j].get("wait_device") == code:
                    end = j
            after.setdefault(end, []).append(unfurl_step(code))
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
