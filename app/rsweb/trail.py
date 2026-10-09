"""Following another replicant's trail through public FTL beacon audit logs (Bill's Skunkworks hunt).

From the game's blog (2026-10): an active FTL beacon logs every departure and arrival in its system — the device, the
owning replicant and, for an interstellar departure, a unit direction vector pointing at the destination system (not
which one). Bill drops a public beacon in every system on his route, hops within FTL relay range, and mostly stays
within 70 ly of Sol, always passing through SOL.

So: find his beacons (other devices in a system: GET /replicants/{ours}/scan/devices), read their audit logs
(GET /devices/{beacon}/audit?replicant_code=<his>), and for each departure rank the catalog stars by how closely
the direction from the beacon's system to the star matches the vector — nearer first among near-equals, since his
hops stay inside relay range. The best match is the next system on the trail; a system where his latest arrival has
no later departure is where he is.

Stored in kv "trail": {target_name, target_code, beacons: {code: {star, location, owner, found_at}},
audit: {beacon: [rows, newest first]}, read_at: {beacon: iso}, seen: [audit ids already notified]}.
"""
from __future__ import annotations

import math
from typing import Any

KV = "trail"
RELAY_LY = 7.5            # an FTL relay's range: Bill's hops stay inside it
GOOD_COS = 0.995          # within ~5.7°: a convincing match
MAX_ANGLE_DEG = 25.0      # candidates shown at all


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def parse_vector(v: Any) -> list[float] | None:
    """'-0.37,0.14,-0.92' | [x, y, z] | {x, y, z} → [x, y, z] (unit length), or None."""
    if v is None:
        return None
    if isinstance(v, str):
        try:
            parts = [float(x) for x in v.replace(" ", "").split(",")]
        except ValueError:
            return None
    elif isinstance(v, dict):
        parts = [float(v.get(k) or 0) for k in "xyz"]
    else:
        try:
            parts = [float(x) for x in v]
        except (TypeError, ValueError):
            return None
    if len(parts) != 3:
        return None
    n = math.sqrt(sum(x * x for x in parts))
    return [x / n for x in parts] if n else None


def _xyz(p: Any) -> list[float] | None:
    if isinstance(p, dict) and all(k in p for k in "xyz"):
        return [float(p[k]) for k in "xyz"]
    return None


def candidates(origin: str, vector: Any, positions: dict[str, Any], limit: int = 6,
               max_angle: float = MAX_ANGLE_DEG) -> list[dict]:
    """Stars the departure from `origin` could be heading for: [{star, angle (deg), cos, distance (ly), in_relay,
    good}] best first — smallest angle, and among near-equal angles (within 1°) the nearer one."""
    v, o = parse_vector(vector), _xyz(positions.get(origin))
    if not v or not o:
        return []
    out = []
    for star, p in positions.items():
        q = _xyz(p)
        if star == origin or not q:
            continue
        d = [b - a for a, b in zip(o, q)]
        dist = math.sqrt(sum(x * x for x in d))
        if not dist:
            continue
        cos = sum(a * b / dist for a, b in zip(v, d))
        ang = math.degrees(math.acos(max(-1.0, min(1.0, cos))))
        if ang <= max_angle:
            out.append({"star": star, "angle": round(ang, 2), "cos": round(cos, 4), "distance": round(dist, 2),
                        "in_relay": dist <= RELAY_LY + 1e-9, "good": cos >= GOOD_COS})
    out.sort(key=lambda c: (round(c["angle"]), c["distance"], c["angle"]))
    return out[:limit]


def legs(audit: dict[str, list[dict]], beacons: dict[str, dict], target_code: str | None,
         positions: dict[str, Any]) -> list[dict]:
    """The target's departures from every beacon we've read, newest first, each with its candidate destinations:
    [{at, from, beacon, device, device_type, vector, candidates, best}]."""
    out = []
    for b, rows in audit.items():
        frm = (beacons.get(b) or {}).get("star")
        for r in rows or []:
            if r.get("travel_type") != "departure" or not r.get("vector"):
                continue
            if target_code and r.get("replicant_code") != target_code:
                continue
            origin = frm or star_of(r.get("location"))
            cands = candidates(origin, r["vector"], positions)
            out.append({"at": r.get("logged_at"), "from": origin, "beacon": b, "device": r.get("device_code"),
                        "device_type": r.get("device_type"), "vector": parse_vector(r["vector"]),
                        "candidates": cands, "best": cands[0] if cands else None, "id": r.get("id")})
    out.sort(key=lambda x: str(x.get("at") or ""), reverse=True)
    return out


def last_seen(audit: dict[str, list[dict]], beacons: dict[str, dict], target_code: str | None) -> dict | None:
    """The target's newest logged movement anywhere: {at, star, travel_type, beacon}. An arrival with nothing after it
    means that's where they are (as far as our beacons know)."""
    best = None
    for b, rows in audit.items():
        for r in rows or []:
            if target_code and r.get("replicant_code") != target_code:
                continue
            at = str(r.get("logged_at") or "")
            if best is None or at > best["at"]:
                best = {"at": at, "star": (beacons.get(b) or {}).get("star") or star_of(r.get("location")),
                        "travel_type": r.get("travel_type"), "beacon": b, "device": r.get("device_code")}
    return best


def beacons_in(scan: dict | None, target_code: str | None, target_name: str | None) -> list[dict]:
    """FTL beacons in a /scan/devices answer that belong to the target (by code, else by owner name)."""
    out = []
    for d in (scan or {}).get("devices") or []:
        if "beacon" not in str(d.get("device_type") or ""):
            continue
        owner_ok = (target_code and d.get("owner_replicant_code") == target_code) or (
            not target_code and target_name and str(d.get("owner_name") or "").lower().startswith(target_name.lower()))
        if owner_ok:
            out.append({"code": d.get("device_code"), "location": d.get("location"), "star": star_of(d.get("location")),
                        "owner": d.get("owner_replicant_code"), "owner_name": d.get("owner_name")})
    return out


def normalize(s: dict) -> dict:
    s = dict(s or {})
    s.setdefault("target_name", "Bill")
    for k in ("beacons", "audit", "read_at"):
        s.setdefault(k, {})
    s.setdefault("seen", [])
    return s


async def scan_for_beacons(api, replicant: str, s: dict) -> list[dict]:
    """The target's beacons among the other devices in the replicant's current system (GET /scan/devices, paged).
    Adds them to `s` and returns them. Raises ApiError."""
    found, cursor = [], None
    for _ in range(6):
        params = {"device_type": "ftl_beacon", "limit": 50, **({"cursor": cursor} if cursor else {})}
        if s.get("target_code"):
            params["owner_replicant_code"] = s["target_code"]
        body = await api.get(f"/replicants/{replicant}/scan/devices", **params) or {}
        found += beacons_in(body, s.get("target_code"), s.get("target_name"))
        cursor = body.get("next_cursor")
        if not cursor:
            break
    for b in found:
        s["beacons"].setdefault(b["code"], {**b, "found_at": _now()})
    return found


def at_star(s: dict, star: str) -> list[dict]:
    """The target's logged movements at beacons in `star`, newest first."""
    rows = []
    for code, b in s["beacons"].items():
        if (b.get("star") or "") != star:
            continue
        for r in s["audit"].get(code) or []:
            if s.get("target_code") and r.get("replicant_code") != s["target_code"]:
                continue
            rows.append({**r, "_beacon": code})
    rows.sort(key=lambda r: str(r.get("logged_at") or ""), reverse=True)
    return rows


MAX_HOPS = 25


def next_hop(s: dict, star: str, positions: dict, visited: list[str]) -> dict:
    """What following the trail does after reading the beacons in `star`:
    {"end": text} — the target arrived here and hasn't left (or the trail goes cold);
    {"go": star, "alts": [...], "why": text} — the latest departure from here points at `go`."""
    rows = at_star(s, star)
    if not rows:
        return {"cold": f"no record of {s['target_name']} at a beacon in {star}"}
    last = rows[0]
    if last.get("travel_type") == "arrival":
        return {"end": f"{s['target_name']} arrived at {last.get('location') or star} ({last.get('logged_at')}) and hasn't left"}
    if last.get("travel_type") != "departure" or not last.get("vector"):
        return {"cold": f"{s['target_name']}'s latest move at {star} isn't a departure with a direction"}
    cands = [c for c in candidates(star, last["vector"], positions) if c["star"] not in visited]
    if not cands:
        return {"cold": f"{s['target_name']} left {star} toward no star the map knows — load the stars around {star}"}
    c = cands[0]
    return {"go": c["star"], "alts": [x["star"] for x in cands[1:4]],
            "why": f"left {star} {last.get('logged_at')}: {c['star']} is {c['angle']}° off the vector, {c['distance']} ly"}


def merge_read(cur: dict, read: dict) -> dict:
    """Put what a (slow) read found into the state as it is now: beacons removed meanwhile stay removed, and a
    target changed meanwhile isn't given the old target's log."""
    cur = normalize(cur)
    if cur.get("target_code") != read.get("target_code"):
        return cur
    for code in read["audit"]:
        if code in cur["beacons"]:
            cur["audit"][code] = read["audit"][code]
            if code in read["read_at"]:
                cur["read_at"][code] = read["read_at"][code]
            if read["beacons"].get(code, {}).get("star") and not cur["beacons"][code].get("star"):
                cur["beacons"][code]["star"] = read["beacons"][code]["star"]
    cur["seen"] = read["seen"]
    if read.get("polled_at"):
        cur["polled_at"] = read["polled_at"]
    return cur


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def read_beacons(api, s: dict, only: str | None = None) -> tuple[list[dict], list[str]]:
    """Read the audit log of the target's beacons (one GET each, newest 50, filtered to the target). Returns the
    rows not seen before (none from a beacon's first read) and any errors; keeps the log in `s`."""
    new, errs = [], []
    for code, b in list(s["beacons"].items()):
        if only and code != only:
            continue
        params = {"latest": "true", "limit": 50}
        if s.get("target_code"):
            params["replicant_code"] = s["target_code"]
        try:
            rows = ((await api.get(f"/devices/{code}/audit", background=True, **params)) or {}).get("audit") or []
        except Exception as e:   # ApiError: say which beacon, carry on
            errs.append(f"{code}: {getattr(e, 'message', e)}")
            continue
        if not b.get("star"):
            b["star"] = next((star_of(r.get("location")) for r in rows if r.get("location")), None)
        first = code not in s["read_at"]     # a beacon's first read only fills its log: its backlog isn't news
        s["audit"][code] = rows[:200]
        s["read_at"][code] = _now()
        seen = set(s["seen"])
        if not first:
            new += [r for r in rows if r.get("id") is not None and str(r["id"]) not in seen]
        s["seen"] = (s["seen"] + [str(r["id"]) for r in rows if r.get("id") is not None and str(r["id"]) not in seen])[-2000:]
    return new, errs


