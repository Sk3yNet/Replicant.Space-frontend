"""Production report per mining and rock chaser fleet (Fleets › Production, 1.512.0).

Devices an AMI controller runs send no mining events of their own (their controller's digest names them, without
amounts), so the report measures what the piles gain instead: on each inventory poll (about every 2 minutes,
ingest.sync_inventory) the gain of every pile where devices are mining goes to the fleets mining there, split by how
many of their devices mine there (a rig counts as 1.5 drones, the belt rate measured 2026-11-02). Fleetless devices'
share goes to "No fleet". A carrier unloading into a pile where something mines counts too. Kept 31 days per UTC hour
in table fleet_yield.

Beside it, the game's own `mining.stopped` reports (one per mining session of a device no controller runs, with
`quantity_mined`) are summed per fleet for the last 30 days: that's where a rock chaser's history from before this
report shows.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import fleets as fl

ROLES = ("mining", "rock_chaser")
WINDOWS = ((1, "24 h"), (7, "7 days"), (30, "30 days"))
NO_FLEET = "_none"
KEEP_DAYS = 31
WEIGHT = {"mining_rig": 1.5}


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def shares(devices: list[dict]) -> dict[str, dict[str, float]]:
    """{location: {fleet id or "": weight}} of the devices mining now."""
    out: dict[str, dict[str, float]] = {}
    for d in devices:
        if not isinstance(d, dict) or not str(d.get("status") or "").startswith("mining") or not d.get("location"):
            continue
        w = out.setdefault(d["location"], {})
        fid = fl.fleet_of(d) or ""
        w[fid] = w.get(fid, 0.0) + WEIGHT.get(d.get("device_type") or "", 1.0)
    return out


def gains(prev: dict[str, dict], cur: dict[str, dict], mining: dict[str, dict[str, float]]) -> list[tuple[str, str, str, float]]:
    """[(fleet, location, resource, units)] the piles gained since the last poll, split over the fleets mining there
    (`mining`: shares())."""
    out = []
    for loc, w in mining.items():
        old, new = prev.get(loc) or {}, cur.get(loc) or {}
        total = sum(w.values())
        for r, q in new.items():
            g = _num(q) - _num(old.get(r))
            if g <= 0 or not total or loc not in prev:
                continue
            for fid, n in w.items():
                out.append((fid, loc, r, round(g * n / total, 3)))
    return out


async def accrue(db, prev: dict[str, dict], cur: dict[str, dict], mining: dict[str, dict[str, float]],
                 now: datetime | None = None) -> None:
    """Called on each inventory poll, with shares() of the device list."""
    now = now or datetime.now(timezone.utc)
    hour = _iso(now)[:13]
    for fid, loc, r, q in gains(prev, cur, mining):
        await db.execute("INSERT INTO fleet_yield(hour, fleet, location, resource, qty) VALUES(?,?,?,?,?) "
                         "ON CONFLICT(hour, fleet, location, resource) DO UPDATE SET qty = qty + excluded.qty",
                         (hour, fid, loc, r, q))
    st = await db.kv_get("fleet_yield_state") or {}
    if (st.get("pruned") or "") < _iso(now - timedelta(days=1)):
        await db.execute("DELETE FROM fleet_yield WHERE hour < ?", (_iso(now - timedelta(days=KEEP_DAYS))[:13],))
        await db.kv_set("fleet_yield_state", {"since": st.get("since") or _iso(now), "pruned": _iso(now)})


async def rows(db, days: int = 30, now: datetime | None = None) -> list[dict]:
    since = _iso((now or datetime.now(timezone.utc)) - timedelta(days=days))[:13]
    return await db.fetchall("SELECT hour, fleet, location, resource AS res, qty AS q FROM fleet_yield WHERE hour >= ?",
                             (since,))


async def sessions(db, days: int = 30, now: datetime | None = None) -> list[dict]:
    """The game's mining.stopped reports of the last `days`, summed per device."""
    since = _iso((now or datetime.now(timezone.utc)) - timedelta(days=days))
    return await db.fetchall(
        "SELECT device_code, SUM(json_extract(payload, '$.quantity_mined')) AS q, COUNT(*) AS n FROM events "
        "WHERE event = 'mining.stopped' AND received_at >= ? GROUP BY device_code", (since,))


def report(fleets: list[dict], devices: list[dict], hours: list[dict], reported: list[dict] | None = None,
           days: int = 7, now: datetime | None = None, since: str | None = None) -> dict:
    """One entry per mining / rock chaser fleet (and "No fleet" when something mined outside them): totals per window
    and, for the chosen `days`, the split by resource, location and day. `since`: when measuring began."""
    now = now or datetime.now(timezone.utc)
    cuts = {d: _iso(now - timedelta(days=d))[:13] for d, _ in WINDOWS}
    kept = {f["id"]: f for f in fleets if f.get("role") in ROLES}
    owner = {d.get("device_code"): fl.fleet_of(d) for d in devices}
    out: dict[str, dict] = {}

    def entry(fid: str) -> dict:
        if fid not in out:
            f = kept.get(fid) or {"id": NO_FLEET, "name": "No fleet"}
            mem = fl.members(f, devices) if fid in kept else []
            out[fid] = {"id": f["id"], "name": f.get("name") or f["id"], "role": f.get("role") or "",
                        "home": f.get("home") or "", "totals": {d: 0.0 for d, _ in WINDOWS}, "reported": 0.0,
                        "sessions": 0, "res": {}, "locs": {}, "daily": {}, "last": "",
                        "miners": sum(1 for d in mem if (d.get("device_type") or "") in ("mining_drone", "mining_rig")),
                        "mining_now": sum(1 for d in mem if str(d.get("status") or "").startswith("mining"))}
        return out[fid]

    for fid in kept:
        entry(fid)
    for r in hours:
        fid = r.get("fleet") if r.get("fleet") in kept else NO_FLEET
        hour, q = r.get("hour") or "", _num(r.get("q"))
        if hour < cuts[30]:
            continue
        e = entry(fid)
        for d, _ in WINDOWS:
            if hour >= cuts[d]:
                e["totals"][d] += q
        e["last"] = max(e["last"], hour)
        if hour < cuts[days]:
            continue
        res, loc, day = r.get("res") or "?", r.get("location") or "?", hour[:10]
        e["res"][res] = e["res"].get(res, 0.0) + q
        e["locs"][loc] = e["locs"].get(loc, 0.0) + q
        e["daily"][day] = e["daily"].get(day, 0.0) + q
    for r in reported or []:
        fid = owner.get(r.get("device_code"))
        e = entry(fid if fid in kept else NO_FLEET)
        e["reported"] += _num(r.get("q"))
        e["sessions"] += int(r.get("n") or 0)
    items = list(out.values())
    for e in items:
        e["res"] = dict(sorted(e["res"].items(), key=lambda kv: -kv[1]))
        e["locs"] = dict(sorted(e["locs"].items(), key=lambda kv: -kv[1]))
        e["daily"] = dict(sorted(e["daily"].items()))
        e["per_hour"] = e["totals"][1] / 24
    items.sort(key=lambda e: (e["id"] == NO_FLEET, -e["totals"][days], -e["reported"], e["name"].casefold()))
    resources = sorted({k for e in items for k in e["res"]})
    return {"fleets": items, "resources": resources, "days": days, "windows": WINDOWS, "since": since,
            "grand": {d: sum(e["totals"][d] for e in items) for d, _ in WINDOWS},
            "by_res": {k: sum(e["res"].get(k, 0.0) for e in items) for k in resources}}
