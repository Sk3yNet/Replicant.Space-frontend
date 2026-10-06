"""Reset & reform: wipe the assignment tags and rebuild every fleet and controller assignment from where things are.

For when the tags have got messy. plan() is pure; the Fleets page shows it as a preview and only sends it on confirm.

What it rewrites — the assignment tags only: `fleet:<id>`, `to:<star>`, `spare`, `gather`, and old `home:<star>` tags.
Kept as they are: `at:` pins, `civ`, `ferry`, `taxi`, the ignore tags (e.g. `keep`) and any tag of your own.
Left alone entirely: ignored devices, the ferry's devices, devices in a running job (a delivery under way), moving, members of a
fleet away on a mission, and the replicants' own vessels.

How devices are assigned, per system (where a device is, or where the carrier holding it is):
  1. Fleets that aren't stationed keep their current members (wherever they are) up to the fleet's loadout, then take
     unassigned devices (no fleet, and spare or untagged) in the system the fleet sits in to fill the gaps. Extra members leave.
  2. Stationed fleets take, in their home system, their own members first, then the other devices there, up to their
     loadout (a template's 0 lines want none).
  3. Everything else in a system with a stationed fleet becomes `spare` if a fleet there counts its type, and is left
     untagged if none does (the system's own "don't care" devices). In a system without one, devices that were spare stay
     spare and the rest are left untagged.
Controllers: a drone run by a controller outside its new group (another system, another fleet) is released; a stationed
fleet's devices and the fleetless devices in its home are one group. With `full`, every drone is released. The next loadout
pass and the fleets' own phases adopt idle drones in their group again.
"""
from __future__ import annotations

from collections import Counter, defaultdict

from . import ami_schedule as amis
from . import fleets as fl
from .loadouts import home_tag

ASSIGN_PREFIXES = ("home:", "to:", "fleet:")
ASSIGN_TAGS = {"spare", "gather"}
UNTOUCHED_TAGS = {"ferry", "taxi"}


def is_assignment(tag: str) -> bool:
    return tag in ASSIGN_TAGS or tag.startswith(ASSIGN_PREFIXES)


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def where(d: dict, by_code: dict[str, dict]) -> str:
    """The system a device is in — its carrier's, when it's aboard one."""
    host = d.get("attached_to_device_code") or d.get("stowed_in_device_code")
    return star_of(d.get("location") or (by_code.get(host) or {}).get("location"))


def _moving(d: dict) -> bool:
    st = str(d.get("status") or "")
    return st.startswith(("moving", "travel", "surging", "cruising")) or bool(d.get("unlisted")) or bool(d.get("location_stale"))


def plan(cfg: dict, devices: list[dict], fleets: list[dict], busy: set[str], hosts: set[str], full: bool = False) -> dict:
    by_code = {d["device_code"]: d for d in devices}
    ignore = set(cfg.get("ignore_tags") or [])
    on_mission = {f["id"] for f in fleets if fl.away(f)}
    skipped: list[dict] = []
    pool: list[dict] = []
    for d in devices:
        tags = set(d.get("tags") or [])
        why = ("replicant's vessel" if d["device_code"] in hosts else
               "ignored tag" if tags & ignore else
               "ferry / taxi" if tags & UNTOUCHED_TAGS or "ferry" in (by_code.get(d.get("controller_device_code") or "") or {}).get("tags", []) else
               "fleet on a mission" if (fl.fleet_of(d) or "")[6:] in on_mission else
               "in a running job" if d["device_code"] in busy else
               "moving / in transit" if _moving(d) else   # an idle device with a to: tag and no job is a leftover
               "position unknown" if not where(d, by_code) else None)
        if why:
            skipped.append({"code": d["device_code"], "type": d.get("device_type"), "why": why})
        else:
            pool.append(d)

    group: dict[str, str] = {}          # code -> "fleet:<id>" | "home:<star>" | "spare" | ""
    by_star: dict[str, list[dict]] = defaultdict(list)
    for d in pool:
        by_star[where(d, by_code)].append(d)

    def take(cands: list[dict], wants: dict[str, int], label: str, prefer) -> dict[str, int]:
        got: Counter = Counter()
        for d in sorted(cands, key=lambda d: (prefer(d), d["device_code"])):
            t = d.get("device_type")
            if d["device_code"] in group or t not in wants or got[t] >= wants[t]:
                continue
            group[d["device_code"]] = label
            got[t] += 1
        return {t: max(0, n - got[t]) for t, n in wants.items() if n - got[t] > 0}

    short: dict[str, dict[str, int]] = {}
    # 1. fleets that aren't stationed (nor away), in the system most of their members are in (else their home)
    for f in fleets:
        if f["id"] in on_mission or f.get("station"):
            continue
        tag = fl.fleet_tag(f["id"])
        mine = [d for d in pool if tag in (d.get("tags") or [])]
        here = Counter(where(d, by_code) for d in mine).most_common(1)
        star = here[0][0] if here else (f.get("home") or "")
        wants = {t: int(n) for t, n in (f.get("wants") or {}).items() if int(n or 0) > 0}
        # its own members wherever they are (a stranded one is picked up on the next fill or mission), then
        # unassigned devices in its system
        free = mine + [d for d in by_star.get(star, []) if not fl.fleet_of(d)
                       and ("spare" in (d.get("tags") or []) or not any(is_assignment(t) for t in d.get("tags") or []))]
        gap = take(free, wants, tag, lambda d, tag=tag: 0 if tag in (d.get("tags") or []) else 1)
        if gap:
            short[f"fleet {f['name']}"] = gap

    # 2. stationed fleets, at home
    stationed = [f for f in fleets if f.get("station") and f.get("home") and f["id"] not in on_mission]
    at_home: dict[str, list[dict]] = defaultdict(list)
    for f in sorted(stationed, key=lambda f: (f["home"], f["id"])):
        at_home[f["home"]].append(f)
        tag, star = fl.fleet_tag(f["id"]), f["home"]
        wants = fl.station_wants(f)
        cands = [d for d in by_star.get(star, []) if fl.fleet_of(d) in (None, f["id"])]
        htag = home_tag(star)
        gap = take(cands, wants, tag, lambda d, tag=tag, htag=htag: 0 if tag in (d.get("tags") or []) else
                   1 if htag in (d.get("tags") or []) else 2)
        if gap:
            short[f"fleet {f['name']}"] = gap

    # 3. the rest: spare where a stationed fleet counts the type, else untagged (spares elsewhere stay spare)
    for star, ds in by_star.items():
        counted = {t for f in at_home.get(star, []) for t in fl.station_wants(f)}
        for d in ds:
            if d["device_code"] in group:
                continue
            if at_home.get(star):
                group[d["device_code"]] = "spare" if d.get("device_type") in counted else ""
            else:
                group[d["device_code"]] = "spare" if "spare" in (d.get("tags") or []) else ""

    # tag changes: drop every assignment tag that isn't the new one, add the new one
    retag: list[dict] = []
    for d in pool:
        new = group.get(d["device_code"], "")
        old = [t for t in d.get("tags") or [] if is_assignment(t)]
        rem = [t for t in old if t != new]
        add = [new] if new and new not in old else []
        if rem or add:
            retag.append({"code": d["device_code"], "type": d.get("device_type"), "star": where(d, by_code),
                          "add": add, "remove": rem, "to": new or "(untagged)"})

    # controllers: release drones whose controller isn't in their new group
    releases: dict[str, list[str]] = defaultdict(list)
    for d in pool:
        c = by_code.get(d.get("controller_device_code") or "")
        if not c or amis.drone_kind(d) is None:
            continue
        g, cg = group.get(d["device_code"], ""), group.get(c["device_code"], "")
        home_side = {fl.fleet_tag(f["id"]) for f in at_home.get(where(d, by_code), [])}
        together = g == cg or (g == "" and cg in home_side) or (cg == "" and g in home_side)
        if full or g == "spare" or not together or where(c, by_code) != where(d, by_code):
            releases[c["device_code"]].append(d["device_code"])

    counts = Counter(v or "(untagged)" for v in group.values())
    return {"retag": sorted(retag, key=lambda r: (r["star"], r["to"], r["code"])),
            "releases": {k: sorted(v) for k, v in sorted(releases.items())}, "skipped": skipped, "short": short,
            "groups": dict(sorted(counts.items())), "missions": sorted(on_mission), "full": full}


def steps(p: dict) -> list[dict]:
    """Releases first (a drone keeps working for its old controller until it's let go), then the tags."""
    from .automations import step
    out = [step(f"{c}: release {', '.join(ds)}", f"/devices/{c}", {"command": "release", "devices": ds})
           for c, ds in p["releases"].items()]
    for r in p["retag"]:
        cfg: dict = {}
        if r["add"]:
            cfg["add_tags"] = r["add"]
        if r["remove"]:
            cfg["remove_tags"] = r["remove"]
        out.append(step(f"{r['code']} → {r['to']}", f"/devices/{r['code']}", {"configuration": cfg}, method="PATCH"))
    return out
