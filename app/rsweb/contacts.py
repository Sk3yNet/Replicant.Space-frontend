"""Contacts: other players' replicants we have come across, with their game profiles (Activity › Contacts).

Sightings come from what the app already keeps:
    beacon  our FTL beacons' traffic log (kv "traffic" entries: replicant_code, star, logged_at)
    scan    device scans of a system (kv "others" stars: each device's owner / owner_name)
    bobnet  BobNet posts (events "bobnet.new": replicant_code, replicant_name, current_star, time)
    added   looked up by hand in the directory (GET /replicants?name=…)

Profiles: GET /replicants/{code} (name, is_npc, pronouns, description, plan, project, experience_points, location).
Unverified live whether the game shows another player's profile text there; whatever comes back (or the error) is shown.
kv "contacts":

    people   {code: {name, is_npc, pronouns, description, plan, project, xp, location, first_seen, last_seen,
                     stars: {STAR: iso}, sources: {beacon|scan|bobnet|added: count}, profile_at, error, note, hidden}}
    bobnet_seq  the last bobnet.new event seq read

Profiles are fetched for new contacts first, then refreshed when older than REFRESH_HOURS, a few per read. kv
"replicant_profiles" (names for the traffic and map views) is kept up to date from them.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

KV = "contacts"
REFRESH_HOURS = 24
LOOKUPS = 5
SOURCES = ("beacon", "scan", "bobnet", "added")
PROFILE_FIELDS = ("pronouns", "description", "plan", "project")


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def _ts(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sight(people: dict, code: str | None, source: str, star: str = "", at: str | None = None, name: str | None = None) -> bool:
    """Record one sighting; True when the contact is new."""
    if not code:
        return False
    at = at or _now()
    new = code not in people
    p = people.setdefault(code, {"first_seen": at, "last_seen": at, "stars": {}, "sources": {}})
    if name and not p.get("name"):
        p["name"] = name
    if at < (p.get("first_seen") or at):
        p["first_seen"] = at
    if at > (p.get("last_seen") or ""):
        p["last_seen"] = at
    if star:
        if at > (p["stars"].get(star) or ""):
            p["stars"][star] = at
    p["sources"][source] = p["sources"].get(source, 0) + 1
    return new


def gather(state: dict, mine: set[str], traffic: dict, others: dict, bobnet: list[dict], seen_keys: set[str] | None = None) -> int:
    """Fold sightings into state["people"]; returns how many contacts are new. Beacon entries and scans are counted
    once each (state["seen"] holds their keys); bobnet rows are only the ones after state["bobnet_seq"]."""
    people = state.setdefault("people", {})
    seen = set(state.get("seen") or [])
    new = 0
    for e in traffic.get("entries") or []:
        rep = e.get("replicant_code")
        key = f"b{e.get('id')}|{e.get('device_code')}|{e.get('logged_at')}"
        if not rep or rep in mine or key in seen:
            continue
        seen.add(key)
        new += sight(people, rep, "beacon", e.get("star") or star_of(e.get("location")), e.get("logged_at"))
    for star, snap in (others.get("stars") or {}).items():
        if not isinstance(snap, dict):
            continue
        at = snap.get("scanned_at")
        for d in (snap.get("devices") or []) + (snap.get("drones") or []):
            rep = d.get("owner")
            key = f"s{star}|{at}|{rep}"
            if not rep or rep in mine or key in seen:
                continue
            seen.add(key)
            new += sight(people, rep, "scan", star, at, d.get("owner_name"))
    for row in bobnet:
        if row.get("seq"):
            state["bobnet_seq"] = max(int(state.get("bobnet_seq") or 0), int(row["seq"]))
        rep = row.get("replicant_code")
        if not rep or rep in mine:
            continue
        new += sight(people, rep, "bobnet", star_of(row.get("current_star")), row.get("time"), row.get("replicant_name"))
    state["seen"] = sorted(seen)[-5000:]
    return new


def due(people: dict, now: datetime | None = None, limit: int = LOOKUPS) -> list[str]:
    """Contacts whose profile to fetch: never fetched first (newest sighting first), then the stalest."""
    now = now or datetime.now(timezone.utc)
    fresh = [c for c, p in people.items() if not p.get("profile_at") and not p.get("hidden")]
    fresh.sort(key=lambda c: people[c].get("last_seen") or "", reverse=True)
    stale = [c for c, p in people.items() if p.get("profile_at") and not p.get("hidden")
             and (_ts(p["profile_at"]) or now) < now - timedelta(hours=REFRESH_HOURS)]
    stale.sort(key=lambda c: people[c].get("profile_at") or "")
    return (fresh + stale)[:limit]


def apply_profile(p: dict, body: dict) -> None:
    p["name"] = body.get("name") or p.get("name")
    p["is_npc"] = body.get("is_npc")
    for k in PROFILE_FIELDS:
        p[k] = body.get(k) or None
    p["xp"] = body.get("experience_points")
    p["location"] = body.get("location") or body.get("location_name") or p.get("location")
    p["status"] = body.get("status")
    p["profile_at"] = _now()
    p.pop("error", None)


async def fetch(api, people: dict, codes: list[str]) -> int:
    from .api import ApiError
    n = 0
    for code in codes:
        p = people.get(code)
        if p is None:
            continue
        try:
            body = await api.request("GET", f"/replicants/{code}", background=True) or {}
        except ApiError as e:
            p["error"], p["profile_at"] = e.message, _now()
            continue
        apply_profile(p, body if isinstance(body, dict) else {})
        n += 1
    return n


def sync_names(profiles: dict, people: dict) -> dict:
    """kv replicant_profiles (traffic, map and visitor alerts read names there) from the contacts."""
    for code, p in people.items():
        if p.get("profile_at") and not p.get("error"):
            profiles[code] = {"name": p.get("name"), "is_npc": p.get("is_npc"), "description": p.get("description"),
                              "at": p["profile_at"]}
        elif code not in profiles and p.get("name"):
            profiles[code] = {"name": p["name"], "is_npc": p.get("is_npc"), "at": p.get("last_seen")}
    return profiles


async def bobnet_rows(db, after: int, limit: int = 2000) -> list[dict]:
    rows = await db.fetchall("SELECT seq, payload FROM events WHERE event='bobnet.new' AND seq>? ORDER BY seq LIMIT ?",
                             (int(after or 0), limit))
    out = []
    for r in rows:
        try:
            d = json.loads(r["payload"] or "{}")
        except (TypeError, ValueError):
            d = {}
        out.append({**(d if isinstance(d, dict) else {}), "seq": r["seq"]})
    return out


async def update(db, api, mine: set[str], lookups: int = LOOKUPS, traffic: dict | None = None) -> dict:
    """Gather new sightings and fetch a few profiles. Returns {new, fetched}."""
    state = await db.kv_get(KV, {}) or {}
    rows = await bobnet_rows(db, state.get("bobnet_seq") or 0)
    new = gather(state, mine, traffic if traffic is not None else (await db.kv_get("traffic", {}) or {}), await db.kv_get("others", {}) or {}, rows)
    fetched = await fetch(api, state["people"], due(state["people"], limit=lookups)) if api and lookups else 0
    state["read_at"] = _now()
    await db.kv_set(KV, state)
    profiles = await db.kv_get("replicant_profiles", {}) or {}
    await db.kv_set("replicant_profiles", sync_names(profiles, state["people"]))
    return {"new": new, "fetched": fetched}


def listing(people: dict, q: str = "", npcs: bool = True, text_only: bool = False, hidden: bool = False) -> list[dict]:
    q = q.strip().lower()
    out = []
    for code, p in people.items():
        if bool(p.get("hidden")) != hidden:
            continue
        if not npcs and p.get("is_npc"):
            continue
        has_text = any(p.get(k) for k in ("description", "plan", "project"))
        if text_only and not has_text:
            continue
        hay = " ".join(str(p.get(k) or "") for k in ("name", "description", "plan", "project", "note", "location")) + " " + code
        if q and q not in hay.lower() and not any(q in s.lower() for s in p.get("stars") or {}):
            continue
        stars = sorted((p.get("stars") or {}).items(), key=lambda kv: kv[1], reverse=True)
        out.append({**p, "code": code, "has_text": has_text, "star_list": [s for s, _ in stars]})
    return sorted(out, key=lambda r: r.get("last_seen") or "", reverse=True)
