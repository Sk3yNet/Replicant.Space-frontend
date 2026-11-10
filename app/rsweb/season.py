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


RESET_KV = "season_reset"
# Season 4 (game v4.0.0) began with the scheduled outage of 2026-11-09 20:00 UTC; it reset every system to unexplored
# (live 2026-11-10: the census listed FENIRON, a fleet home scanned many times, as unexplored with no entry point)
SEASON_4_START = "2026-11-09T20:00:00+00:00"


async def reset_at(db: Any) -> str | None:
    """When the galaxy was last reset by a season change: scans and surveys from before then are stale. Recorded by
    the season check; for the season 4 change, which came before that, its start."""
    rec = await db.kv_get(RESET_KV, None)
    if isinstance(rec, dict) and rec.get("at"):
        return str(rec["at"])
    cur = await db.kv_get(SEASON_KV, None) or {}
    if cur.get("number") == 4:
        await db.kv_set(RESET_KV, {"at": SEASON_4_START, "number": 4})
        return SEASON_4_START
    return None


def stale(updated_at: str | None, reset: str | None) -> bool:
    """A scan read before the last season reset (or never)."""
    if not updated_at:
        return True
    if not reset:
        return False
    try:
        a, b = datetime.fromisoformat(str(updated_at)), datetime.fromisoformat(str(reset))
        a = a if a.tzinfo else a.replace(tzinfo=timezone.utc)
        return a < b
    except ValueError:
        return False


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


def compare(snap: dict | None, devices: list[dict], replicants: dict, stars: dict) -> dict:
    """What the time skip changed, against the copy from before (Dashboard › Story › What changed):
    moved / gone / new devices, changed status or AMI directive, replicants that moved, stars added or gone."""
    snap = snap or {}
    before = snap.get("devices") or {}
    now = {d["device_code"]: d for d in devices or [] if isinstance(d, dict) and d.get("device_code")}
    moved, changed, gone, new = [], [], [], []
    for code, b in sorted(before.items()):
        d = now.get(code)
        if not d:
            gone.append({"code": code, "type": b.get("device_type"), "location": b.get("location")})
            continue
        if b.get("location") != d.get("location"):
            moved.append({"code": code, "type": d.get("device_type"), "was": b.get("location"), "now": d.get("location")})
        dir_b = (b.get("ami_directive") or {}).get("name") if isinstance(b.get("ami_directive"), dict) else b.get("ami_directive")
        dir_n = (d.get("ami_directive") or {}).get("name") if isinstance(d.get("ami_directive"), dict) else d.get("ami_directive")
        if b.get("status") != d.get("status") or dir_b != dir_n:
            changed.append({"code": code, "type": d.get("device_type"), "location": d.get("location"),
                            "was": " ".join(x for x in (b.get("status"), dir_b and f"({dir_b})") if x),
                            "now": " ".join(x for x in (d.get("status"), dir_n and f"({dir_n})") if x)})
    for code, d in sorted(now.items()):
        if code not in before:
            new.append({"code": code, "type": d.get("device_type"), "location": d.get("location")})
    reps_b = snap.get("replicants") or {}
    reps = [{"code": c, "name": (r or {}).get("name") or c, "was": reps_b.get(c),
             "now": (r or {}).get("location") or (r or {}).get("current_location")}
            for c, r in sorted((replicants or {}).items()) if isinstance(r, dict) and c in reps_b
            and reps_b.get(c) != (r.get("location") or r.get("current_location"))]
    sb = set(snap.get("stars") or {})
    sn = {x["designation"] for x in (stars or {}).get("stars") or [] if isinstance(x, dict) and x.get("designation")}
    return {"at": snap.get("at"), "season": snap.get("season"), "moved": moved, "changed": changed, "gone": gone,
            "new": new, "replicants": reps, "stars_new": sorted(sn - sb) if sb else [], "stars_gone": sorted(sb - sn) if sn else [],
            "empty": not snap}


# --- scheduled outages (Joe 2026-11-08: game maintenance announced as "Monday, 9th November 2026 Start Time: 20:00 UTC
# Expected duration: Up to 1 hour") --------------------------------------------------------------------------------------
# A window pauses automations `lead` minutes before its start (the same hold as the season pause, with a fresh copy of the
# galaxy) and lifts that hold `after` minutes past its end. A hold you set yourself, or one a new season set, is never
# lifted by a window: only you resume those. Evacuations from asteroids run through any hold.
OUTAGE_KV = "outage_windows"
LEAD_MIN, AFTER_MIN = 30, 15
MONTHS = {m: i + 1 for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"))}


def parse_announcement(text: str) -> tuple[datetime, int] | None:
    """(start in UTC, minutes) from an announcement's text, or None. Takes "9th November 2026" / "November 9, 2026" /
    "2026-11-09", a time "20:00" (UTC unless it says otherwise: then None, never a guess) and "1 hour" / "90 minutes"."""
    import re
    t = " ".join(str(text or "").split())
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
    else:
        m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,})\.?,?\s+(\d{4})", t) \
            or re.search(r"([A-Za-z]{3,})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", t)
        if not m:
            return None
        a, b, y = m[1], m[2], int(m[3])
        day, mon = (a, b) if a.isdigit() else (b, a)
        mo = MONTHS.get(mon[:3].lower())
        if not mo:
            return None
        d = int(day)
    tm = re.search(r"(\d{1,2}):(\d{2})(?:\s*([A-Z]{1,5})\b)?", t[m.end():]) or re.search(r"(\d{1,2}):(\d{2})(?:\s*([A-Z]{1,5})\b)?", t)
    if not tm:
        return None
    zone = (tm[3] or "UTC").upper()
    if zone not in ("UTC", "GMT", "Z"):
        return None
    try:
        start = datetime(y, mo, d, int(tm[1]), int(tm[2]), tzinfo=timezone.utc)
    except ValueError:
        return None
    du = re.search(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?)\b", t[tm.end():] if tm else t, re.I)
    minutes = 60
    if du:
        n = float(du[1])
        minutes = int(round(n * 60)) if du[2].lower().startswith("h") else int(round(n))
    return start, max(1, minutes)


def window_times(w: dict) -> tuple[datetime, datetime, datetime, datetime]:
    """(pause at, start, end, resume at) of an outage window."""
    from datetime import timedelta
    start = datetime.fromisoformat(w["start"])
    end = start + timedelta(minutes=int(w.get("minutes") or 60))
    return (start - timedelta(minutes=int(w.get("lead", LEAD_MIN))), start, end,
            end + timedelta(minutes=int(w.get("after", AFTER_MIN))))


def outage_step(windows: list[dict], hold: dict | None, now: datetime) -> tuple[list[dict], str | None, str | None]:
    """What the schedule does now: (windows updated, "pause" | "resume" | None, the window's id). One action per call."""
    out = [dict(w) for w in windows]
    for w in out:
        pause_at, _, _, resume_at = window_times(w)
        st = w.get("state") or "pending"
        if st == "pending" and now >= resume_at:
            w["state"] = "missed"
        elif st == "pending" and now >= pause_at:
            w["state"] = "holding"
            if not hold:
                w["paused_by_us"] = True
                return out, "pause", w["id"]
        elif st == "holding" and now >= resume_at:
            w["state"] = "done"
            if w.get("paused_by_us") and (hold or {}).get("window") == w["id"]:
                return out, "resume", w["id"]
    return out, None, None
