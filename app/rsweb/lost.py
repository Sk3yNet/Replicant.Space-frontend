"""Lost equipment: devices out of sight, with their last known location, until they're seen again (Devices › Lost).

A device is lost while any of these holds, checked on every device sync:
  surging       on an interstellar trip (the game often leaves a surging device out of the device list until it
                arrives) — or riding in a carrier that is
  unlisted      the game's device list leaves it out (the app keeps such devices 12 h; this record keeps them until seen)
  out of range  no relay of yours reaches it and no replicant is in its system (`in_control_range: false`, or worked
                out from your relaying relays — 7.5 ly — hubs — 15 ly — and where your replicants are)
It's found again once it's listed, in range and not mid-surge; the last 50 finds are kept. One the game says is gone
(decommissioned, destroyed, given away) is dropped. kv "lost": {lost: {code: record}, found: [record + found_at, found_at_location]}.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

KV = "lost"
FOUND_KEEP = 50
RANGE = {"ftl_relay": 7.5, "system_hub": 15.0}
MOVING = ("surg", "travel", "cruis")


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize(s: dict | None) -> dict:
    s = dict(s or {})
    s.setdefault("lost", {})
    s.setdefault("found", [])
    return s


def _xyz(p: Any) -> list[float] | None:
    return [float(p.get(k) or 0) for k in "xyz"] if isinstance(p, dict) else None


def covered_stars(devices: list[dict], replicants: dict, pos: dict[str, Any]) -> tuple[set[str], set[str]]:
    """(stars in control range, stars whose position is known). In range: within reach of a relaying relay or an active
    hub of yours, or where one of your replicants is."""
    sources = []
    for d in devices:
        r = RANGE.get(d.get("device_type") or "")
        st = str(d.get("status") or "")
        if r and d.get("location") and not d.get("location_stale") and not st.startswith(("stowed", "compact", "inactive", "idle")):
            p = _xyz(pos.get(star_of(d["location"])))
            if p:
                sources.append((p, r))
    out = {star_of(r.get("location") or r.get("current_location")) for r in (replicants or {}).values()}
    for star, p in pos.items():
        q = _xyz(p)
        if q and any(math.dist(q, s) <= rng + 1e-9 for s, rng in sources):
            out.add(star)
    return {x for x in out if x}, {x for x in pos if _xyz(pos.get(x))}


def why_lost(d: dict, by: dict[str, dict], covered: set[str], known: set[str]) -> str | None:
    st = str(d.get("status") or "")
    host = by.get(d.get("stowed_in_device_code") or d.get("attached_to_device_code") or "")
    tr = d.get("travel") or {}
    dest = star_of(tr.get("final_destination") or tr.get("destination"))
    here = star_of(d.get("location"))
    if st.startswith(MOVING) and dest and here and dest != here:
        return "surging"
    if host and host is not d:
        hw = why_lost(host, by, covered, known)
        if hw:
            return f"aboard {host.get('device_code')} ({hw})"
    if d.get("unlisted"):
        return "unlisted"
    if d.get("in_control_range") is False:
        return "out of range"
    if here and here in known and here not in covered:
        return "out of range"
    return None


def update(s: dict, devices: list[dict], replicants: dict, pos: dict[str, Any], gone: set[str] | None = None,
           now: str | None = None) -> dict:
    """Fold one device list into the record. Returns {"lost": [new codes], "found": [codes]}."""
    now = now or _now()
    gone = gone or set()
    by = {d.get("device_code"): d for d in devices}
    covered, known = covered_stars(devices, replicants, pos)
    newly, back = [], []
    for code in [c for c in s["lost"] if c in gone]:
        rec = s["lost"].pop(code)
        s["found"].append({**rec, "found_at": now, "found_location": None, "outcome": "gone"})
    for d in devices:
        code = d.get("device_code")
        if not code:
            continue
        why = why_lost(d, by, covered, known)
        rec = s["lost"].get(code)
        fresh_loc = d.get("location") if d.get("location") and not d.get("location_stale") else None
        if why:
            host = d.get("stowed_in_device_code") or d.get("attached_to_device_code")
            tr = d.get("travel") or {}
            if not rec:
                rec = s["lost"][code] = {"code": code, "type": d.get("device_type"), "replicant": d.get("replicant_code"),
                                         "since": now, "last_location": fresh_loc or d.get("location"),
                                         "last_seen": d.get("updated_at") or now}
                newly.append(code)
            elif fresh_loc and not d.get("unlisted") and d.get("in_control_range") is not False:
                rec["last_location"] = fresh_loc   # still visible (e.g. mid-cruise before the surge): keep it current
            if not rec.get("last_location") and host and (by.get(host) or {}).get("location"):
                rec["last_location"] = by[host]["location"]
            rec.update({"reason": why, "status": d.get("status"), "carrier": host,
                        "heading_to": tr.get("final_destination") or tr.get("destination") or rec.get("heading_to"),
                        "arrives_at": tr.get("final_arrives_at") or tr.get("arrives_at") or rec.get("arrives_at"),
                        "updated_at": now})
        elif rec:
            s["lost"].pop(code)
            s["found"].append({**rec, "found_at": now, "found_location": d.get("location"), "outcome": "found"})
            back.append(code)
    s["found"] = s["found"][-FOUND_KEEP:]
    return {"lost": newly, "found": back}


async def track(db, devices: list[dict]) -> dict:
    """The device sync's hook: update the record from the list it just stored."""
    s = normalize(await db.kv_get(KV, {}))
    reps = await db.kv_get("replicants", {}) or {}
    cat = await db.kv_get("stars", {}) or {}
    pos = {x.get("designation"): x.get("position") for x in cat.get("stars") or [] if isinstance(x, dict) and x.get("position")}
    listed = {d.get("device_code") for d in devices}
    candidates = [c for c in s["lost"] if c not in listed]
    gone: set[str] = set()
    if candidates:
        from .ingest import GONE_EVENTS
        rows = await db.fetchall(
            f"SELECT DISTINCT device_code FROM events WHERE event IN ({','.join('?' * len(GONE_EVENTS))}) "
            f"AND device_code IN ({','.join('?' * len(candidates))})", (*GONE_EVENTS, *candidates))
        gone = {r["device_code"] for r in rows}
    out = update(s, devices, reps, pos, gone)
    await db.kv_set(KV, s)
    return out
