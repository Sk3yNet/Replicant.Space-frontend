"""Other players' fixed devices, and their comings and goings at our beacons (the Galaxy and System maps).

Drones are kept too (`drones`), but only for the System page: a total per player and type, and how many of
theirs work each belt (the belt's viability line). Fixed devices: what is unlikely to move — FTL beacons, relays, wards, hubs, autofactories, observatories, controllers,
slingshots. Vessels, drones, surge plates and replicants are left out. kv "others":

    stars       {STAR: {scanned_at, by, devices: [{code, type, location, owner, owner_name}], drones: [... + status]}}
                the latest scan of other devices in the system (GET /replicants/{ours}/scan/devices). A new scan
                replaces it whole; a beacon-logged departure removes that device; a snapshot older than KEEP_DAYS goes.
    rep_at      {replicant: star} where each of our replicants was when its system was last scanned: a replicant
                arriving somewhere new scans it (traffic poll).

Traffic arrows come from the beacon audit log (kv "traffic"): an entry's vector points from the beacon's system toward
the other end of the trip (checked live 2026-10-09: FALQUORYX ↔ OTHILETH) — for an arrival toward where it came
from, for a departure toward where it went. The other end is guessed with trail.candidates (nearest star along it).
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from . import trail as tl

KV = "others"
KEEP_DAYS = 7
RAW_MAX = 500
ARROW_HOURS = 1.0
FIXED = ("beacon", "relay", "ward", "hub", "factory", "observatory", "controller", "slingshot")
MOVING = ("vessel", "drone", "plate", "propulsor")


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def _ts(v: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def is_fixed(device_type: str | None) -> bool:
    t = str(device_type or "")
    return any(k in t for k in FIXED) and not any(k in t for k in MOVING)


def normalize(s: dict | None) -> dict:
    s = dict(s or {})
    s.setdefault("stars", {})
    s.setdefault("rep_at", {})
    return s


def record_scan(s: dict, star: str, scanned: list[dict], mine: set[str], by: str = "",
                now: datetime | None = None) -> dict:
    """Replace the system's snapshot with the fixed devices of other players in a scan answer."""
    now = now or _now()
    devs = [{"code": d.get("device_code"), "type": d.get("device_type"), "location": d.get("location"),
             "owner": d.get("owner_replicant_code"), "owner_name": d.get("owner_name")}
            for d in scanned if isinstance(d, dict) and is_fixed(d.get("device_type"))
            and d.get("owner_replicant_code") not in mine and star_of(d.get("location")) == star]
    drones = [{"code": d.get("device_code"), "type": d.get("device_type"), "location": d.get("location"),
               "owner": d.get("owner_replicant_code"), "owner_name": d.get("owner_name"), "status": d.get("status")}
              for d in scanned if isinstance(d, dict) and "drone" in str(d.get("device_type") or "")
              and d.get("owner_replicant_code") not in mine and star_of(d.get("location")) == star]
    s["stars"][star] = {"scanned_at": now.isoformat(timespec="seconds"), "by": by, "devices": devs, "drones": drones,
                        "raw": [d for d in scanned if isinstance(d, dict)][:RAW_MAX]}   # the scan as the game answered
    return s["stars"][star]


def apply_departures(s: dict, entries: list[dict]) -> int:
    """A device a beacon saw leave after the snapshot was taken is gone from it. Returns how many were removed."""
    gone = 0
    for star, snap in s["stars"].items():
        t0 = _ts(snap.get("scanned_at"))
        left = {e.get("device_code") for e in entries
                if e.get("travel_type") == "departure" and e.get("star") == star
                and (_ts(e.get("logged_at")) or t0) > t0} if t0 else set()
        if left:
            before = len(snap["devices"])
            snap["devices"] = [d for d in snap["devices"] if d.get("code") not in left]
            gone += before - len(snap["devices"])
            snap["drones"] = [d for d in snap.get("drones") or [] if d.get("code") not in left]
    return gone


def prune(s: dict, now: datetime | None = None) -> None:
    now = now or _now()
    for star in [k for k, v in s["stars"].items() if (_ts(v.get("scanned_at")) or now - timedelta(days=99))
                 < now - timedelta(days=KEEP_DAYS)]:
        del s["stars"][star]


def summary(snap: dict | None, profiles: dict | None = None) -> list[dict]:
    """[{owner, name, types: {type: n}, devices: [...]}] for one system's snapshot, most devices first."""
    by: dict[str, dict] = {}
    for d in (snap or {}).get("devices") or []:
        o = d.get("owner") or d.get("owner_name") or "?"
        g = by.setdefault(o, {"owner": d.get("owner"), "devices": [], "types": defaultdict(int),
                              "name": d.get("owner_name") or ((profiles or {}).get(o) or {}).get("name") or o})
        g["devices"].append(d)
        g["types"][d.get("type") or "device"] += 1
    return sorted(({**g, "types": dict(g["types"])} for g in by.values()), key=lambda g: -len(g["devices"]))


def drone_totals(snap: dict | None, profiles: dict | None = None) -> list[dict]:
    """[{name, types: {type: n}, n}] — each player's drones in the system, most first."""
    by: dict[str, dict] = {}
    for d in (snap or {}).get("drones") or []:
        o = d.get("owner") or d.get("owner_name") or "?"
        g = by.setdefault(o, {"name": d.get("owner_name") or ((profiles or {}).get(o) or {}).get("name") or o,
                              "types": defaultdict(int), "n": 0})
        g["types"][d.get("type") or "drone"] += 1
        g["n"] += 1
    return sorted(({**g, "types": dict(g["types"])} for g in by.values()), key=lambda g: -g["n"])


def drones_at(snap: dict | None, place: str, kind: str = "mining") -> int:
    """Other players' drones of a kind at a place or inside it (a belt and its sites) at the last scan."""
    return sum(1 for d in (snap or {}).get("drones") or [] if kind in str(d.get("type") or "")
               and (d.get("location") == place or str(d.get("location") or "").startswith(place + "-")))


def warded_by_others(stars: Any, devices: list[dict]) -> set[str]:
    from . import wards
    return wards.foreign(stars, devices)


def map_others(s: dict, positions: dict[str, Any], warded: set[str], profiles: dict | None = None) -> list[dict]:
    """Per system with other players' fixed devices (or warded by someone else): what the galaxy map draws."""
    out = []
    for star in sorted(set(s["stars"]) | set(warded)):
        if star not in positions:
            continue
        snap = s["stars"].get(star) or {}
        groups = summary(snap, profiles)
        if not groups and star not in warded:
            continue
        types = {t for g in groups for t in g["types"]}
        out.append({"star": star, "position": positions[star], "scanned_at": snap.get("scanned_at"),
                    "n": sum(len(g["devices"]) for g in groups),
                    "ward": star in warded or any("ward" in t or "hub" in t for t in types),
                    "owners": [{"name": g["name"], "types": g["types"]} for g in groups]})
    return out


def map_traffic(entries: list[dict], mine: set[str], positions: dict[str, Any], profiles: dict | None = None,
                now: datetime | None = None, hours: float = ARROW_HOURS) -> list[dict]:
    """Other players' arrivals and departures at our beacons in the last `hours`, one arrow per (owner, system, way,
    vector): {star, position, way: in|out, vector, who, types, at (ms), guess}."""
    now = now or _now()
    since = now - timedelta(hours=hours)
    groups: dict[tuple, dict] = {}
    for e in entries:
        rep = e.get("replicant_code")
        at = _ts(e.get("logged_at"))
        v = tl.parse_vector(e.get("vector"))
        star = e.get("star") or star_of(e.get("location"))
        if not rep or rep in mine or not at or at < since or not v or star not in positions:
            continue
        way = "in" if e.get("travel_type") == "arrival" else "out"
        key = (rep, star, way, tuple(round(x, 2) for x in v))
        g = groups.get(key)
        if not g:
            name = ((profiles or {}).get(rep) or {}).get("name") or rep
            g = groups[key] = {"star": star, "position": positions[star], "way": way, "vector": v, "who": name,
                               "types": defaultdict(int), "at": 0}
        g["types"][(e.get("device_type") or "device").replace("_", " ")] += 1
        g["at"] = max(g["at"], int(at.timestamp() * 1000))
    out = []
    for g in groups.values():
        cands = tl.candidates(g["star"], g["vector"], positions, limit=1, max_angle=10)
        out.append({**g, "types": dict(g["types"]), "guess": cands[0]["star"] if cands else None})
    return sorted(out, key=lambda g: g["at"])


def map_trail(t: dict, positions: dict[str, Any]) -> dict | None:
    """The followed replicant's trail (kv "trail"): legs from each logged departure to its best-matching star, and
    where they were last seen."""
    t = tl.normalize(t)
    if not t["beacons"]:
        return None
    legs = []
    for l in tl.legs(t["audit"], t["beacons"], t.get("target_code"), positions):
        b = l.get("best")
        if b and l["from"] in positions and b["star"] in positions:
            legs.append({"from": l["from"], "to": b["star"], "at": l.get("at"), "good": b.get("good"),
                         "a": positions[l["from"]], "b": positions[b["star"]]})
    seen: set[tuple] = set()
    legs = [l for l in legs if not ((l["from"], l["to"]) in seen or seen.add((l["from"], l["to"])))]
    last = tl.last_seen(t["audit"], t["beacons"], t.get("target_code"))
    beacons = sorted({b.get("star") for b in t["beacons"].values() if b.get("star") in positions})
    return {"name": t.get("target_name") or "?", "legs": legs, "beacons": beacons,
            "last": ({**last, "position": positions.get(last["star"])} if last and last.get("star") in positions else None)}


async def scan(api, replicant: str, max_pages: int = 6) -> tuple[str, list[dict]]:
    """Every other device in the replicant's current system (paged). Returns (star, devices). Raises ApiError."""
    star, out, cursor = "", [], None
    for _ in range(max_pages):
        params = {"limit": 50, **({"cursor": cursor} if cursor else {})}
        body = await api.request("GET", f"/replicants/{replicant}/scan/devices", params=params, background=True) or {}
        star = star or body.get("star") or ""
        out += body.get("devices") or []
        cursor = body.get("next_cursor")
        if not cursor:
            break
    return star, out


async def scan_into(db, api, replicant: str, mine: set[str], s: dict | None = None) -> dict:
    """Scan the replicant's system and store the snapshot. Returns the system's snapshot (with "star")."""
    own = s is None
    s = normalize(await db.kv_get(KV, {}) if own else s)
    star, devs = await scan(api, replicant)
    if not star:
        reps = await db.kv_get("replicants", {}) or {}
        r = reps.get(replicant) or {}
        star = star_of(r.get("location") or r.get("current_location"))
    snap = record_scan(s, star, devs, mine, by=replicant)
    s["rep_at"][replicant] = star
    if own:
        await db.kv_set(KV, s)
    return {**snap, "star": star}


async def on_traffic_poll(db, api, entries: list[dict], mine: set[str]) -> dict:
    """Each traffic poll: scan the system of any replicant of ours that has arrived somewhere new, drop devices our
    beacons saw leave, and forget old snapshots."""
    from .api import ApiError
    s = normalize(await db.kv_get(KV, {}))
    reps = await db.kv_get("replicants", {}) or {}
    scanned, errors = [], {}
    for code, r in reps.items():
        star = star_of(r.get("location") or r.get("current_location"))
        if not star or str(r.get("status") or "").startswith(("travel", "cruis", "surg")) or s["rep_at"].get(code) == star:
            continue
        try:
            await scan_into(db, api, code, mine, s)
            scanned.append(star)
        except ApiError as e:
            errors[code] = e.message
            s["rep_at"][code] = star    # don't retry every poll; the System page's button can
    removed = apply_departures(s, entries)
    prune(s)
    await db.kv_set(KV, s)
    return {"scanned": scanned, "removed": removed, "errors": errors}
