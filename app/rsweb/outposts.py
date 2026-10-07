"""FTL relays, beacons and system wards dropped by survey missions.

A survey (explore) fleet leaves one FTL relay, one FTL beacon and one system ward in each system it visits that has none
of yours yet:
  • relay  — deployed at an L4/L5 Lagrange point (the only place it works) and activated; it extends remote command and
             BobNet (7.5 ly, chains automatically)
  • beacon — deployed wherever the carrier unloads; it logs the system's traffic from anywhere in the system. If the
             survey finds a civilisation (an event at a body, or a body with intelligent / spacefaring life), the carrier
             picks the beacon up again once everyone is aboard and deploys it at that body: civilisations only send their
             follow-up requests to a beacon AT their planet or moon.
  • ward   — deployed where the carrier unloads and activated: other players can't mine in a warded system (an
             activation may evict miners already there). The game allows 25 per account, and wards don't go with hubs.
They ride in the fleet's carriers (stowed or attached); they don't have to be fleet members.
"""
from __future__ import annotations

from .automations import step
from .placement import is_lagrange
from .traffic import CIV_STAGES

KINDS = {"relay": "ftl_relay", "beacon": "ftl_beacon", "ward": "system_ward"}
LABELS = {"relay": "FTL relay", "beacon": "FTL beacon", "ward": "system ward"}
MAX_WARDS = 25   # per account (game docs)


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def aboard(carriers: set[str], devices: list[dict], holds: dict[str, list[str]] | None = None) -> list[dict]:
    """Devices riding in these carriers (the device list's stowed_in / attached_to, else the stowed map)."""
    by = {d.get("device_code"): d for d in devices}
    out = {d["device_code"]: d for d in devices
           if (d.get("stowed_in_device_code") or d.get("attached_to_device_code")) in carriers}
    for c, kids in (holds or {}).items():
        if c in carriers:
            for k in kids:
                if k in by and k not in out and not by[k].get("location"):
                    out[k] = by[k]
    return list(out.values())


def carried(carriers: set[str], devices: list[dict], kind: str, holds: dict[str, list[str]] | None = None) -> list[dict]:
    t = KINDS[kind]
    return sorted((d for d in aboard(carriers, devices, holds) if d.get("device_type") == t), key=lambda d: d["device_code"])


def deployed_in(devices: list[dict], star: str, kind: str) -> list[dict]:
    """Your deployed relays / beacons in a system."""
    t = KINDS[kind]
    return [d for d in devices if d.get("device_type") == t and d.get("location") and star_of(d["location"]) == star
            and not str(d.get("status") or "").startswith("stowed")]


def needs(devices: list[dict], stars: list[str]) -> dict[str, list[str]]:
    """{kind: [systems in the list with none of yours yet]} (each system once, in list order)."""
    seen = list(dict.fromkeys(star_of(s) for s in stars if s))
    return {k: [s for s in seen if not deployed_in(devices, s, k)] for k in KINDS}


def shortfall(carriers: set[str], devices: list[dict], stars: list[str], holds: dict[str, list[str]] | None = None) -> dict:
    """What a survey mission over `stars` needs against what its carriers hold, plus warning lines."""
    need = needs(devices, stars)
    have = {k: len(carried(carriers, devices, k, holds)) for k in KINDS}
    warn = []
    for k, label in LABELS.items():
        n = len(need[k])
        if n > have[k]:
            warn.append(f"{n} system(s) without your {label} ({', '.join(need[k])}) but only {have[k]} aboard — "
                        f"{n - have[k]} will be left without one")
    wards = sum(1 for d in devices if d.get("device_type") == KINDS["ward"] and d.get("location"))
    if need["ward"] and have["ward"] and wards + min(have["ward"], len(need["ward"])) > MAX_WARDS:
        warn.append(f"you have {wards} wards deployed; the game allows {MAX_WARDS} per account")
    return {"need": need, "have": have, "warnings": warn}


def drop_steps(carriers: set[str], devices: list[dict], star: str, spot: str | None,
               holds: dict[str, list[str]] | None = None, fleet_tag: str | None = None) -> tuple[list[dict], list[str]]:
    """At `spot` in `star` (where the carrier has just unloaded): deploy a relay (and activate it) and a beacon, each only
    when the system has none of yours. A relay needs an L4/L5 point; elsewhere it stays aboard. One that was a fleet
    member (e.g. fetched by the gather phase) leaves the fleet: it stays behind for good."""
    steps, notes = [], []

    def leave(d: dict) -> list[dict]:
        if fleet_tag and fleet_tag in (d.get("tags") or []):
            return [step(f"{d['device_code']}: stays in {star} (leaves the fleet)", f"/devices/{d['device_code']}",
                         {"configuration": {"remove_tags": [fleet_tag]}}, method="PATCH")]
        return []
    if not deployed_in(devices, star, "relay"):
        relays = carried(carriers, devices, "relay", holds)
        if relays and spot and is_lagrange(spot):
            code = relays[0]["device_code"]
            st = step(f"deploy relay {code} at {spot}", f"/devices/{code}", {"command": "deploy"}, wait=["device.deployed"],
                      timeout=120)
            st["wait_device"] = code
            steps += [st, step(f"{code}: activate relay", f"/devices/{code}", {"command": "activate"})] + leave(relays[0])
            notes.append(f"dropping relay {code} at {spot}")
        elif relays:
            notes.append(f"no L4/L5 Lagrange point known in {star}: relay {relays[0]['device_code']} stays aboard")
    if not deployed_in(devices, star, "beacon"):
        beacons = carried(carriers, devices, "beacon", holds)
        if beacons:
            code = beacons[0]["device_code"]
            st = step(f"deploy beacon {code} at {spot or star}", f"/devices/{code}", {"command": "deploy"},
                      wait=["device.deployed"], timeout=120)
            st["wait_device"] = code
            steps += [st] + leave(beacons[0])
            notes.append(f"dropping beacon {code} at {spot or star}")
    if not deployed_in(devices, star, "ward"):
        wards = carried(carriers, devices, "ward", holds)
        if wards:
            code = wards[0]["device_code"]
            st = step(f"deploy ward {code} at {spot or star}", f"/devices/{code}", {"command": "deploy"},
                      wait=["device.deployed"], timeout=120)
            st["wait_device"] = code
            steps += [st, step(f"{code}: activate ward (no mining by other players in {star})", f"/devices/{code}",
                               {"command": "activate"})] + leave(wards[0])
            notes.append(f"dropping ward {code} at {spot or star}")
    return steps, notes


def presence(devices: list[dict]) -> dict[str, dict[str, list[dict]]]:
    """{star: {kind: [your deployed relays / beacons / wards there]}} for the Systems list."""
    out: dict[str, dict[str, list[dict]]] = {}
    for d in devices:
        k = next((k for k, t in KINDS.items() if d.get("device_type") == t), None)
        if k and d.get("location") and not str(d.get("status") or "").startswith("stowed"):
            out.setdefault(star_of(d["location"]), {x: [] for x in KINDS})[k].append(d)
    return out


def civ_places(rows: list[dict], star: str) -> list[str]:
    """Civilisation bodies in a system (from traffic.coverage rows): event locations first, then bodies with
    intelligent / spacefaring life."""
    ev = [r["location"] for r in rows if r["star"] == star and (r.get("open") or r.get("completed"))]
    life = [r["location"] for r in rows if r["star"] == star and r["location"] not in ev
            and (r.get("life") or {}).get("life_stage") in CIV_STAGES]
    return ev + life


def civ_move_steps(carrier: dict, devices: list[dict], star: str, civ: list[str],
                   holds: dict[str, list[str]] | None = None) -> tuple[list[dict], list[str]]:
    """The carrier takes a beacon to the first civilisation body without one: a beacon of yours elsewhere in the
    system is picked up (stowed), else one it carries is used. Nothing to do when a civ body already has a beacon."""
    mine = deployed_in(devices, star, "beacon")
    at = {d["location"] for d in mine}
    if not civ or any(c in at for c in civ):
        return [], []
    target = civ[0]
    code, cloc = carrier["device_code"], carrier.get("location")

    def travel(to: str, why: str) -> dict:
        st = step(f"{code} → {to} ({why})", f"/devices/{code}", {"command": "travel", "destination": to},
                  wait=["travel.arrived"], match={"destination": to}, critical=True)
        st["wait_device"] = code
        return st
    steps = []
    loose = [d for d in mine if "stow" in (d.get("available_commands") or ["stow"])]
    if loose:
        b = sorted(loose, key=lambda d: d["device_code"])[0]
        if b["location"] != cloc:
            steps.append(travel(b["location"], f"pick up beacon {b['device_code']}"))
        st = step(f"stow beacon {b['device_code']} into {code}", f"/devices/{b['device_code']}",
                  {"command": "stow", "target": code}, wait=["device.stowed"], timeout=120, critical=True)
        st["wait_device"] = b["device_code"]
        steps.append(st)
        bcode, how = b["device_code"], f"moving beacon {b['device_code']} from {b['location']}"
    else:
        aboard_b = carried({code}, devices, "beacon", holds)
        if not aboard_b:
            return [], [f"civilisation at {target}, but no beacon of yours in {star} or aboard {code} to put there"]
        bcode, how = aboard_b[0]["device_code"], f"deploying beacon {aboard_b[0]['device_code']}"
    if cloc != target or loose:
        steps.append(travel(target, "civilisation"))
    st = step(f"deploy beacon {bcode} at {target}", f"/devices/{bcode}", {"command": "deploy"}, wait=["device.deployed"],
              timeout=120, critical=True)
    st["wait_device"] = bcode
    steps.append(st)
    return steps, [f"civilisation at {target}: {how} there"]
