"""Season changes. A season ends in a time skip (docs, /concepts/story/): devices may move, location events end,
asteroids drift away, AMI directives pause, and the galaxy around you may change. So:

- Hold: when GET /overview reports a new season number (ingest.sync_overview), or you pause by hand (Dashboard ›
  Story), kv "season_hold" is set and the engine stops: no passes, no new automated jobs, and jobs already running wait
  (ones you started yourself still run). Only you lift it (Resume automations).
- Copy before the change: every SNAP_HOURS while not held, kv "season_snapshot" keeps what we'd compare afterwards:
  device places and states (AMI directives included), fleets, loadouts, ward orders, contract deliveries, replicant
  places and the star positions. It isn't refreshed while held, so it shows the galaxy as it was.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

HOLD_KV = "season_hold"
SEASON_KV = "season"
SNAP_KV = "season_snapshot"
SNAP_HOURS = 6.0
DEVICE_KEYS = ("device_type", "location", "status", "tags", "replicant_code", "stowed_in_device_code",
               "attached_to_device_code", "ami_directive", "ami_directive_status", "directive")


def changed(prev: dict | None, season: dict | None) -> bool:
    """A different season number than the one we knew (nothing known yet is not a change)."""
    a, b = (prev or {}).get("number"), (season or {}).get("number")
    return a is not None and b is not None and a != b


def label(season: dict | None) -> str:
    s = season or {}
    return f"season {s.get('number', '?')}" + (f" ({s['title']})" if s.get("title") else "")


def likely_end(ov: dict | None) -> tuple[str, int] | None:
    """When the season probably ends, and in how many days: the season's own end date if the game ever sends one, else
    the latest `visible_until` among storylines and events (the overview names no season end; on 2026-10-20 both the
    Ark's journey and the Ethirel event ran until 2026-10-31)."""
    ov = ov or {}
    se = ov.get("season") or {}
    end = next((se.get(k) for k in ("ends_at", "end", "deadline", "visible_until") if se.get(k)), None)
    if not end:
        ends = [x.get("visible_until") for k in ("storylines", "events") for x in ov.get(k) or []
                if isinstance(x, dict) and x.get("visible_until")]
        end = max(ends, default=None)
    if not end:
        return None
    try:
        day = datetime.fromisoformat(str(end)[:10]).date()
    except ValueError:
        return None
    return day.isoformat(), (day - datetime.now(timezone.utc).date()).days


async def snapshot(db: Any) -> dict:
    """The state worth comparing after a time skip, small enough for the kv store."""
    devices = await db.kv_get("devices", []) or []
    cat = await db.kv_get("stars", {}) or {}
    reps = await db.kv_get("replicants", {}) or {}
    return {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "season": await db.kv_get(SEASON_KV, None),
        "devices": {d["device_code"]: {k: d[k] for k in DEVICE_KEYS if d.get(k) not in (None, [], "")}
                    for d in devices if isinstance(d, dict) and d.get("device_code")},
        "replicants": {c: (r.get("location") or r.get("current_location")) for c, r in reps.items() if isinstance(r, dict)},
        "stars": {s["designation"]: s.get("position") for s in cat.get("stars") or []
                  if isinstance(s, dict) and s.get("designation")},
        "fleets": await db.kv_get("fleets", []) or [],
        "loadouts": await db.kv_get("loadouts", {}) or {},
        "ward_orders": {k: await db.kv_get(k, {}) or {} for k in ("ward_orders", "relay_requests", "beacon_requests")},
        "contract_supply": await db.kv_get("contract_supply", {}) or {},
    }


def summary(snap: dict | None) -> str:
    if not snap:
        return "none yet"
    return (f"{len(snap.get('devices') or {})} devices, {len(snap.get('stars') or {})} stars, "
            f"{len(snap.get('fleets') or [])} fleets")
