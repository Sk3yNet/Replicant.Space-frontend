"""Beacon traffic audit and civilization contact.

Traffic: every FTL beacon logs "the comings and goings of every device in the system, including other players"
(GET /devices/{beacon}/audit → {audit: [{id, device_code, device_type, replicant_code, travel_type: arrival|departure,
location, logged_at, vector}]}). The app reads each deployed beacon every few minutes, keeps the log, and raises a
"visitor" notification when another replicant's devices arrive in one of your systems.

Civilization contact (why civilization alerts don't arrive): the game only sends a civilization's follow-up requests
("daily messages about new requests") when an FTL beacon is deployed AT the location of an event you completed there —
the planet or moon itself, not the system's Kuiper belt / Oort cloud. Without one you only see new requests by
re-scanning the body. `coverage()` lists inhabited bodies and event locations and whether a beacon sits there;
`placement()` works out how to put one there (beacons can't fly: deploy one a vessel carries, print one on a
vessel at the spot, or carry one there in a vessel).
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

MAX_ENTRIES = 3000
CIV_STAGES = ("intelligent", "spacefaring")
LIFE_STAGES = ("prebiotic", "microbial", "complex", "intelligent", "spacefaring")


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def _ts(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def is_beacon(d: dict) -> bool:
    return "beacon" in (d.get("device_type") or "") or "audit" in (d.get("features") or [])


def deployed(d: dict) -> bool:
    return bool(d.get("location")) and not str(d.get("status") or "").startswith("stowed") and not d.get("stowed_in_device_code")


def beacons(devices: list[dict]) -> list[dict]:
    """Your deployed beacons (the ones that can be audited)."""
    return sorted((d for d in devices if is_beacon(d) and "beacon" in (d.get("device_type") or "") and deployed(d)),
                  key=lambda d: d.get("device_code") or "")


def my_codes(account: dict, replicants: dict, devices: list[dict]) -> set[str]:
    out = set(replicants or {})
    out |= {r.get("replicant_code") for r in (account or {}).get("replicants") or [] if isinstance(r, dict)}
    out |= {d.get("replicant_code") for d in devices or []}
    return {c for c in out if c}


# --- reading --------------------------------------------------------------------------------------------------
def merge(state: dict, beacon: dict, rows: list[dict], now: datetime | None = None) -> list[dict]:
    """Add a beacon's audit rows to the stored log; returns the rows that are new (not seen before).
    The first read of a beacon only sets a baseline: nothing in it counts as new."""
    now = now or datetime.now(timezone.utc)
    code = beacon["device_code"]
    seen = state.setdefault("seen", {})
    known = {(e.get("beacon"), e.get("id")) for e in state.setdefault("entries", [])}
    first = code not in seen
    new = []
    top = int(seen.get(code) or 0)
    for r in rows:
        if not isinstance(r, dict) or r.get("id") is None:
            continue
        rid = r.get("id")
        if (code, rid) in known:
            continue
        e = {**r, "beacon": code, "star": star_of(r.get("location") or beacon.get("location")),
             "beacon_location": beacon.get("location")}
        state["entries"].append(e)
        known.add((code, rid))
        try:
            top = max(top, int(rid))
        except (TypeError, ValueError):
            pass
        if not first:
            new.append(e)
    seen[code] = top
    state["entries"].sort(key=lambda e: (e.get("logged_at") or "", str(e.get("id"))))
    state["entries"] = state["entries"][-MAX_ENTRIES:]
    return new


def visitors(new: list[dict], mine: set[str], include_npcs: bool, profiles: dict[str, dict]) -> dict[tuple[str, str], list[dict]]:
    """New arrivals of other replicants' devices, grouped by (replicant, system)."""
    out: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in new:
        rep = e.get("replicant_code")
        if not rep or rep in mine or e.get("travel_type") != "arrival":
            continue
        if not include_npcs and (profiles.get(rep) or {}).get("is_npc"):
            continue
        out[(rep, e.get("star") or "")].append(e)
    return dict(out)


def visitor_text(rep: str, star: str, rows: list[dict], profiles: dict[str, dict]) -> str:
    p = profiles.get(rep) or {}
    who = p.get("name") or rep
    kinds = defaultdict(int)
    for r in rows:
        kinds[(r.get("device_type") or "device").replace("_", " ")] += 1
    what = ", ".join(f"{n}× {k}" for k, n in sorted(kinds.items(), key=lambda kv: -kv[1]))
    npc = " (NPC)" if p.get("is_npc") else ""
    return f"Visitor in {star}: {who}{npc} — {what} arrived"


def summary(entries: list[dict], mine: set[str], profiles: dict[str, dict]) -> list[dict]:
    """Per other replicant: systems seen in, last seen, device types."""
    by: dict[str, dict] = {}
    for e in entries:
        rep = e.get("replicant_code")
        if not rep or rep in mine:
            continue
        s = by.setdefault(rep, {"replicant_code": rep, "name": (profiles.get(rep) or {}).get("name"),
                                "is_npc": (profiles.get(rep) or {}).get("is_npc"), "stars": set(), "types": defaultdict(int),
                                "arrivals": 0, "departures": 0, "last_seen": "", "devices": set()})
        s["stars"].add(e.get("star"))
        s["devices"].add(e.get("device_code"))
        s["types"][e.get("device_type") or "?"] += 1
        s["arrivals" if e.get("travel_type") == "arrival" else "departures"] += 1
        s["last_seen"] = max(s["last_seen"], e.get("logged_at") or "")
    out = []
    for s in by.values():
        out.append({**s, "stars": sorted(x for x in s["stars"] if x), "types": dict(s["types"]), "devices": len(s["devices"])})
    return sorted(out, key=lambda s: s["last_seen"], reverse=True)


def filtered(entries: list[dict], mine: set[str], star: str = "", others_only: bool = False, limit: int = 300) -> list[dict]:
    rows = [e for e in entries if (not star or e.get("star") == star) and (not others_only or e.get("replicant_code") not in mine)]
    return list(reversed(rows))[:limit]


# --- civilization contact ---------------------------------------------------------------------------------------
def inhabited(systems: dict[str, dict]) -> dict[str, dict]:
    """Bodies with life, from stored system scans: {designation: {life_stage, species, star}}."""
    out: dict[str, dict] = {}
    for star, scan in (systems or {}).items():
        for p in (scan or {}).get("planets") or []:
            for body in [p, *((p.get("moons") or []) if isinstance(p.get("moons"), list) else [])]:
                if not isinstance(body, dict):
                    continue
                stage = str(body.get("life_stage") or "").lower()
                if stage and stage != "none":
                    des = body.get("designation")
                    if des:
                        out[des] = {"life_stage": stage, "species": body.get("species") or body.get("species_name"),
                                    "star": star, "name": body.get("name")}
    return out


def coverage(events: dict[str, dict], devices: list[dict], systems: dict[str, dict]) -> list[dict]:
    """One row per location that matters for civilization contact: event locations (open or completed) and bodies
    with intelligent / spacefaring life. `beacon` = your deployed beacon there; `needs_beacon` = an event was
    discovered or completed there and no beacon sits at that exact location — placing it as soon as a survey finds
    the event means it's already there when you complete it, so the follow-up requests reach you from day one."""
    life = inhabited(systems)
    beacons_at: dict[str, list[str]] = defaultdict(list)
    in_system: dict[str, list[dict]] = defaultdict(list)
    for d in beacons(devices):
        beacons_at[d["location"]].append(d["device_code"])
        in_system[star_of(d["location"])].append({"code": d["device_code"], "location": d["location"]})
    rows: dict[str, dict] = {}

    def row(loc: str) -> dict:
        return rows.setdefault(loc, {"location": loc, "star": star_of(loc), "completed": [], "open": [],
                                     "life": life.get(loc), "beacon": beacons_at.get(loc, []),
                                     "system_beacons": [b for b in in_system.get(star_of(loc), []) if b["location"] != loc]})
    for e in events.values():
        loc = e.get("location")
        if not loc:
            continue
        r = row(loc)
        item = {"designation": e.get("designation"), "title": e.get("title"), "tier": e.get("tier"),
                "at": e.get("closed_at") or e.get("discovered_at")}
        if e.get("status") == "completed":
            r["completed"].append(item)
        elif e.get("status") == "open":
            r["open"].append(item)
    for loc, info in life.items():
        if info["life_stage"] in CIV_STAGES:
            row(loc)
    out = []
    for r in rows.values():
        r["needs_beacon"] = bool(r["completed"] or r["open"]) and not r["beacon"]
        r["state"] = "covered" if r["beacon"] else "needs beacon" if r["needs_beacon"] else "no event yet"
        out.append(r)
    order = {"needs beacon": 0, "no event yet": 1, "covered": 2}
    return sorted(out, key=lambda r: (order[r["state"]], r["location"]))


def redundant_beacons(devices: list[dict], rows: list[dict], busy: set[str] | None = None) -> list[dict]:
    """Beacons a system doesn't need: any beacon logs the whole system's traffic, so once a system has a beacon at a
    civilization's body (an event location or a body with intelligent/spacefaring life), its other beacons are
    redundant; without one, one beacon is kept and the rest are redundant. Beacons already spare are skipped."""
    busy = busy or set()
    civ_locs = {r["location"] for r in rows if r.get("completed") or r.get("open")
                or (r.get("life") or {}).get("life_stage") in CIV_STAGES}
    by_star: dict[str, list[dict]] = defaultdict(list)
    for d in beacons(devices):
        if "spare" not in (d.get("tags") or []) and d.get("device_code") not in busy:
            by_star[star_of(d["location"])].append(d)
    out = []
    for star, bs in sorted(by_star.items()):
        civ = [b for b in bs if b["location"] in civ_locs]
        if civ:
            keep = {b["device_code"] for b in civ}
            why = f"{star} has a beacon at a civilization's body ({', '.join(sorted(b['location'] for b in civ))})"
        else:
            keep = {sorted(bs, key=lambda b: b["device_code"])[0]["device_code"]}
            why = f"{star} already has beacon {next(iter(keep))}"
        out += [{"code": b["device_code"], "location": b["location"], "star": star, "why": why}
                for b in bs if b["device_code"] not in keep]
    return out


def is_vessel(d: dict) -> bool:
    """Can carry a beacon around a system: cruises and has a hold."""
    t = d.get("device_type") or ""
    return "cruise" in (d.get("features") or []) and ("vessel" in t or bool(d.get("stow_capacity")))


def placement(loc: str, devices: list[dict], replicants: dict, stowed_map: dict[str, list[str]],
              busy: set[str] | None = None, use_replicant_vessel: bool = True, keep: set[str] | None = None,
              bps: dict[str, dict] | None = None) -> dict:
    """How to get a beacon deployed at `loc` (beacons can't fly). Cheapest first:
      deploy — a replicant is at loc and its vessel carries one
      carry  — a vessel in the system carries one: fly it there, deploy
      fetch  — (stowing a beacon into a vessel at the same location is confirmed by the player)
               a vessel in the system picks up a loose beacon (tagged `civ` or `spare`, e.g. one just printed on the
               autofactory), flies it there, deploys it
      print  — a replicant is at loc: print one on its vessel (≈100 s), deploy it
      factory — nothing to carry yet: print one on the system's autofactory (tagged `civ`); a later pass fetches it
      none
    Vessels hosting a replicant are only used when use_replicant_vessel is True. `keep` = locations whose beacon
    must stay (other event bodies)."""
    busy, keep = busy or set(), keep or set()
    by = {d.get("device_code"): d for d in devices}
    holds: dict[str, list[str]] = defaultdict(list)
    for d in devices:
        if d.get("stowed_in_device_code"):
            holds[d["stowed_in_device_code"]].append(d["device_code"])
    for carrier, kids in (stowed_map or {}).items():
        for k in kids:
            if k not in holds[carrier]:
                holds[carrier].append(k)

    def beacon_in(vessel: str) -> str | None:
        return next((k for k in holds.get(vessel, []) if "beacon" in ((by.get(k) or {}).get("device_type") or "")
                     and k not in busy), None)

    hosts = {}
    for code, r in (replicants or {}).items():
        h = r.get("hosted_device_code")
        if h:
            hosts[h] = (code, r)
    star = star_of(loc)
    rep_here = None
    for host, (rep, r) in hosts.items():
        rloc = r.get("location") or r.get("current_location") or (by.get(host) or {}).get("location")
        if rloc == loc:
            rep_here = (host, rep, r)
            b = beacon_in(host)
            if b:
                return {"kind": "deploy", "beacon": b, "vessel": host, "replicant": rep,
                        "text": f"deploy beacon {b} from {r.get('name') or rep}'s vessel {host} (already at {loc})"}
    vessels = [v for v in sorted(devices, key=lambda d: (d.get("device_code") in hosts, d.get("device_code") or ""))
               if is_vessel(v) and star_of(v.get("location")) == star and v.get("device_code") not in busy
               and not str(v.get("status") or "").startswith(("stowed", "travel", "cruis", "surg"))
               and (use_replicant_vessel or v.get("device_code") not in hosts)]

    def moves(code: str) -> str:
        rep = hosts.get(code)
        return f" (moves your replicant {rep[1].get('name') or rep[0]})" if rep else ""
    for v in vessels:
        b = beacon_in(v["device_code"])
        if b:
            code = v["device_code"]
            return {"kind": "carry", "beacon": b, "vessel": code, "from": v.get("location"),
                    "replicant": hosts[code][0] if code in hosts else None,
                    "text": f"fly {v.get('device_type', 'vessel').replace('_', ' ')} {code} {v.get('location')} → {loc} "
                            f"and deploy beacon {b}" + moves(code)}
    # any beacon of yours in this system that isn't at a civilization's body can be moved there — it logs the whole
    # system's traffic wherever it sits, so nothing is lost. Spare / civ-tagged ones first, then e.g. the Kuiper beacon.
    loose = sorted((d for d in devices if "beacon" in (d.get("device_type") or "") and star_of(d.get("location")) == star
                    and deployed(d) and d.get("device_code") not in busy and d.get("location") not in keep
                    and d.get("location") != loc and "stow" in (d.get("available_commands") or ["stow"])),
                   key=lambda d: (not ({"civ", "spare"} & set(d.get("tags") or [])), d.get("device_code") or ""))
    if loose and vessels:
        b, v = loose[0], vessels[0]
        code = v["device_code"]
        return {"kind": "fetch", "beacon": b["device_code"], "beacon_at": b["location"], "vessel": code, "from": v.get("location"),
                "replicant": hosts[code][0] if code in hosts else None, "moves_existing": True,
                "text": f"{v.get('device_type', 'vessel').replace('_', ' ')} {code} picks up beacon {b['device_code']} at "
                        f"{b['location']}, flies it to {loc} and deploys it" + moves(code)}
    if loose and not rep_here:
        # there's a beacon to move but no free vessel with a hold right now: wait rather than print another
        held = [v for v in devices if is_vessel(v) and star_of(v.get("location")) == star
                and (use_replicant_vessel or v.get("device_code") not in hosts)]
        b = loose[0]
        return {"kind": "wait", "beacon": b["device_code"],
                "text": f"beacon {b['device_code']} at {b['location']} can be moved there — "
                        + (f"waiting for a vessel ({', '.join(v['device_code'] for v in held)} busy)" if held else
                           f"needs a vessel with a hold in {star} to carry it")}
    if rep_here:
        host, rep, r = rep_here
        return {"kind": "print", "replicant": rep, "vessel": host,
                "text": f"{r.get('name') or rep} is at {loc}: print an FTL beacon on the vessel (≈100 s), then deploy it"}
    from . import printqueue as pq
    fac = pq.least_loaded([f for f in devices if "enqueue_print" in (f.get("available_commands") or [])
                           and star_of(f.get("location")) == star], bps or {})
    if fac:
        return {"kind": "factory", "factory": fac["device_code"], "factory_at": fac.get("location"),
                "has_vessel": bool(vessels),
                "text": f"print an FTL beacon on autofactory {fac['device_code']} ({fac.get('location')}); "
                        + ("a vessel then carries it to " + loc if vessels else
                           "then a vessel with a hold is needed in " + star + " to carry it to " + loc)}
    return {"kind": "none", "text": f"no beacon, vessel or autofactory to use in {star} — beacons can't fly: bring a vessel "
                                    "with a beacon stowed, or send a replicant there (it can print one)"}


def placement_steps(p: dict, loc: str) -> list[dict]:
    from .automations import step

    def travel(vessel: str, frm: str | None, to: str) -> dict:
        st = step(f"{vessel}: travel {frm or '?'} → {to}", f"/devices/{vessel}", {"command": "travel", "destination": to},
                  wait=["travel.arrived"], critical=True, match={"destination": to})
        st["wait_device"] = vessel
        return st

    def deploy(b: str) -> dict:
        return step(f"deploy beacon {b} at {loc}", f"/devices/{b}", {"command": "deploy"}, critical=True)
    if p["kind"] == "deploy":
        return [deploy(p["beacon"])]
    if p["kind"] == "print":
        return [step(f"{p['replicant']}: print ftl_beacon at {loc}", f"/replicants/{p['replicant']}/print",
                     {"device_type": "ftl_beacon"}, critical=True)]
    if p["kind"] == "carry":
        return [travel(p["vessel"], p["from"], loc), deploy(p["beacon"])]
    if p["kind"] == "fetch":
        steps = [] if p["from"] == p["beacon_at"] else [travel(p["vessel"], p["from"], p["beacon_at"])]
        st = step(f"stow beacon {p['beacon']} into {p['vessel']}", f"/devices/{p['beacon']}",
                  {"command": "stow", "target": p["vessel"]}, wait=["device.stowed"], timeout=120, critical=True)
        st["wait_device"] = p["beacon"]
        tag = step(f"{p['beacon']}: drop the civ/spare tag", f"/devices/{p['beacon']}",
                   {"configuration": {"remove_tags": ["civ", "spare"]}}, method="PATCH")
        return steps + [st, tag, travel(p["vessel"], p["beacon_at"], loc), deploy(p["beacon"])]
    if p["kind"] == "factory":
        return [step(f"{p['factory']}: print ftl_beacon for {loc}", f"/devices/{p['factory']}",
                     {"command": "enqueue_print", "device_type": "ftl_beacon", "quantity": 1, "tags": ["civ"]}, critical=True)]
    return []


# --- notifications ----------------------------------------------------------------------------------------------
async def notify(db, hub, title: str, level: str = "alert", link: str = "/traffic") -> None:
    from .db import now_iso
    cur = await db.execute("INSERT INTO notifications(event_id, level, title, body, link, created_at) VALUES(?,?,?,?,?,?)",
                           (None, level, title, None, link, now_iso()))
    if hub:
        hub.publish("notify", {"id": cur.lastrowid, "level": level, "title": title, "link": link})


async def sync(db, api, hub, rule_cfg: dict | None, max_beacons: int = 20, profile_lookups: int = 5) -> dict:
    """Read every deployed beacon's audit log; alert on new visitors (when the rule is on). Returns stats."""
    from .api import ApiError
    from .db import now_iso
    devices = await db.kv_get("devices", []) or []
    state = await db.kv_get("traffic", {}) or {}
    errors: dict[str, str] = {}
    new_all: list[dict] = []
    bs = beacons(devices)[:max_beacons]
    for b in bs:
        try:
            body = await api.request("GET", f"/devices/{b['device_code']}/audit", params={"latest": "true", "limit": 50},
                                     background=True)
        except ApiError as e:
            errors[b["device_code"]] = e.message
            continue
        rows = (body or {}).get("audit") or []
        new_all += merge(state, b, rows)
    state["read_at"] = now_iso()
    state["errors"] = errors
    state["beacons"] = [{"code": b["device_code"], "location": b.get("location")} for b in bs]
    # names of the replicants we've seen
    profiles = await db.kv_get("replicant_profiles", {}) or {}
    mine = my_codes(await db.kv_get("account", {}) or {}, await db.kv_get("replicants", {}) or {}, devices)
    unknown = [r for r in {e.get("replicant_code") for e in state.get("entries", [])} if r and r not in mine and r not in profiles]
    for rep in unknown[:profile_lookups]:
        try:
            p = await api.request("GET", f"/replicants/{rep}", background=True) or {}
            profiles[rep] = {"name": p.get("name"), "is_npc": p.get("is_npc"), "description": p.get("description"),
                             "at": now_iso()}
        except ApiError as e:
            profiles[rep] = {"name": None, "error": e.message, "at": now_iso()}
    await db.kv_set("replicant_profiles", profiles)
    alerts = []
    if rule_cfg:
        rh = rule_cfg.get("repeat_hours")
        try:
            repeat = timedelta(hours=max(0, int(rh if rh not in (None, "") else 6)))   # 0 is a valid choice: every time
        except (TypeError, ValueError):
            repeat = timedelta(hours=6)
        alerted = state.setdefault("alerted", {})
        now = datetime.now(timezone.utc)
        recent = [e for e in new_all if (_ts(e.get("logged_at")) or now) > now - timedelta(hours=24)]
        for (rep, star), rows in visitors(recent, mine, bool(rule_cfg.get("include_npcs", True)), profiles).items():
            key = f"{rep}|{star}"
            last = _ts(alerted.get(key))
            if last and now - last < repeat:
                continue
            alerted[key] = now.isoformat(timespec="seconds")
            text = visitor_text(rep, star, rows, profiles)
            alerts.append(text)
            await notify(db, hub, text, "alert", f"/traffic?star={star}")
    await db.kv_set("traffic", state)
    return {"beacons": len(bs), "new": len(new_all), "alerts": alerts, "errors": errors}


def systems_from_rows(rows: list[Any]) -> dict[str, dict]:
    out = {}
    for r in rows:
        try:
            out[r["star"]] = json.loads(r["data"])
        except (TypeError, ValueError, KeyError):
            continue
    return out
