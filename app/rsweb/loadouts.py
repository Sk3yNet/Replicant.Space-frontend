"""System loadouts: how many of each device a system should have, by its development phase.

Config (kv "loadouts"):
    phases:  [{id, name, order, wants: {device_type: n}}]     e.g. "1 Survey", "2 Mining outpost"
    systems: {STAR: phase_id}                                 the phase a system is at
    ignore_tags: ["keep", ...]   devices with any of these are invisible to loadouts (never counted, tagged or moved)
    settings: print_missing, need_stock, carriers_return, use_replicant_vessels, every_minutes

How a pass works (plan() is pure; the engine turns the plan into jobs):
  1. Count each phased system's devices by type. Devices tagged `to:<star>` count for that star.
  2. Above the loadout → the extras get the `spare` tag. At or below it → local spares lose the tag.
  3. Short → take `spare` devices from other systems (nearest first): they're tagged `to:<star>`
     and the `spare` tag is removed. Still short → print on an autofactory whose stock covers the
     cost (the system's own first); the print is tagged `to:<star>` so it is routed when it comes out.
     With several autofactories in that system, the prints are spread over them evenly by print time.
  4. Delivery: a device tagged for another system flies there itself if it can surge; otherwise a
     surge-capable carrier in its system stows it, flies, and deploys it. On arrival the `to:` tag goes.
Systems without a phase are left alone, except that `spare` devices there can be sent elsewhere.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from typing import Any

from . import fleets as fl
from . import printqueue as pq
from .automations import SHORT_TIMEOUT, STEP_TIMEOUT, step
from .shapes import as_amounts

SPARE = "spare"
GATHER = "gather"   # on its way to the spare depot (stays spare on arrival)
WORKING = ("mining", "searching", "tracking", "scanning", "collecting", "depositing", "printing", "repairing")
DEFAULT_SETTINGS = {"print_missing": True, "need_stock": True, "carriers_return": True,
                    "use_replicant_vessels": False, "every_minutes": 15,
                    "gather_spares": True, "spare_depot": "",   # "" = automatic (see spare_depot())
                    "max_cruise_au": 30,
                    "max_supply_ly": 100}   # prints and spares only from within this many ly of the fleet (0 = any)   # a device further than this from its carrier (in-system) is fetched, not flown over


def spare_depot(cfg: dict, devices: list[dict]) -> str | None:
    """Where idle spares are gathered: the chosen system, else a materials 'destination' system with an autofactory,
    else any 'destination' system, else the system with the most autofactories."""
    s = cfg.get("settings") or {}
    if s.get("spare_depot"):
        return str(s["spare_depot"]).upper()
    fac_stars = Counter(star_of(d.get("location")) for d in devices if is_factory(d) and d.get("location")
                        and not any(str(t).startswith("fleet:") for t in d.get("tags") or []))
    dests = sorted({f["home"] for f in fl.destinations(cfg.get("fleets") or [])})
    for st in dests:
        if fac_stars.get(st):
            return st
    if dests:
        return dests[0]
    if fac_stars:
        return sorted(fac_stars.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    return None


def can_travel(d: dict) -> bool:
    """Can fly on its own inside a system (beacons can't: a carrier has to come and pick them up)."""
    cmds = d.get("available_commands")
    if cmds is not None:
        return "travel" in cmds
    if d.get("features") is not None:
        return "cruise" in d["features"]
    return True   # nothing known: assume it can


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def to_tag(star: str) -> str:
    return "to:" + re.sub(r"[^a-z0-9\-_:.]", "", star.lower())[:29]


def at_tag(location: str) -> str:
    """Pin to an exact location (planet, moon, belt, L-point …): `at:<location>`, lower-case, ≤32 chars."""
    return "at:" + re.sub(r"[^a-z0-9\-_:.]", "", location.lower())[:29]


def is_place(code: str) -> bool:
    """Looks like a star or location code (STAR, STAR-3, STAR-BELT-1 …), not e.g. `fleet:prospectors` (seen live
    2026-10-06 as `to:fleet:prospectors` / `at:fleet:prospectors`, typed into a print's deliver-to box)."""
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9\-_.]*", code.lower())) and ":" not in code


def pinned_at(d: dict) -> str | None:
    """The location a device is pinned to (`at:` tag), upper-case like the game's codes."""
    return next((t[3:].upper() for t in d.get("tags") or [] if t.startswith("at:") and len(t) > 3 and is_place(t[3:])), None)


def home_tag(star: str) -> str:
    return "home:" + re.sub(r"[^a-z0-9\-_:.]", "", star.lower())[:27]


def home_of(d: dict, stars: set[str]) -> str | None:
    """The system a device belongs to (`home:<star>` tag), matched back to a known star code.
    With more than one home tag (shouldn't happen), the one for the system it's in wins."""
    homes = []
    for t in d.get("tags") or []:
        if t.startswith("home:"):
            want = t[5:]
            homes.append(next((s for s in stars if home_tag(s)[5:] == want), want.upper()))
    if not homes:
        return None
    here = star_of(d.get("location"))
    return here if here in homes else homes[0]


def bound_for(d: dict, stars: set[str]) -> str | None:
    """The star a device is tagged to go to (`to:<star>`), matched back to a known star code."""
    for t in d.get("tags") or []:
        if t.startswith("to:") and is_place(t[3:]):
            want = t[3:]
            return next((s for s in stars if to_tag(s)[3:] == want), want.upper())
    return None


def normalize(cfg: Any) -> dict:
    cfg = dict(cfg or {})
    cfg.setdefault("phases", [])         # the templates
    cfg.setdefault("ignore_tags", [])
    cfg.setdefault("fleets", [])         # filled in by the engine (Engine.loadout_cfg)
    if (cfg.get("systems") or cfg.get("roles")) and not cfg.get("fleets_migrated"):
        # a config from before stationed fleets (e.g. an old diagnostics snapshot): read it the new way
        cfg, fleets, _ = fl.migrate(cfg, cfg["fleets"])
        cfg["fleets"] = [fl.resolve_template(f, cfg) for f in fleets]
    s = {**DEFAULT_SETTINGS, **(cfg.get("settings") or {})}
    if not s.get("supply_range_v2"):   # 1.47.1: the default went from 15 to 100 ly; a saved 15 was the old default
        if s.get("max_supply_ly") == 15:
            s["max_supply_ly"] = DEFAULT_SETTINGS["max_supply_ly"]
        s["supply_range_v2"] = True
    cfg["settings"] = s
    cfg["phases"] = sorted(cfg["phases"], key=lambda p: (p.get("order", 0), p.get("name", "")))
    return cfg


def stationed_fleets(cfg: dict) -> list[dict]:
    """Fleets the loadout pass keeps at their loadout in their home system, ordered by (home, id)."""
    return sorted((f for f in cfg.get("fleets") or [] if fl.stationed(f)), key=lambda f: (f["home"], f["id"]))


def worked_systems(cfg: dict) -> set[str]:
    """Systems that are home to a stationed fleet: only that fleet works them, so mining and explore missions can't
    target them, though fleets may pass through or wait there."""
    return fl.worked_systems(cfg.get("fleets") or [])


def template(cfg: dict, tid: str | None) -> dict | None:
    return next((p for p in cfg.get("phases") or [] if p.get("id") == tid), None) if tid else None


def _dist(a: str, b: str, pos: dict[str, dict]) -> float:
    if a == b:
        return 0.0
    pa, pb = pos.get(a), pos.get(b)
    if not pa or not pb:
        return 1e9
    return math.dist([pa.get(k, 0) for k in "xyz"], [pb.get(k, 0) for k in "xyz"])


def _idle(d: dict) -> bool:
    return str(d.get("status") or "").startswith(("idle", "stowed", "inactive", "stationary"))


def _cap(d: dict) -> float:
    try:
        v = float(d.get("operational_capacity"))
        return v * 100 if v <= 1 else v
    except (TypeError, ValueError):
        return 100.0


def can_surge(d: dict) -> bool:
    return "surge" in (d.get("features") or []) and "travel" in (d.get("available_commands") or ["travel"])


FILL_ORDER = ("controller", "survey_drone", "mining_drone")


def fill_rank(device_type: str | None) -> int:
    """Order in which shortfalls are filled (spares, then prints): AMI controllers, survey drones, mining drones, the rest."""
    t = device_type or ""
    for i, k in enumerate(FILL_ORDER):
        if k in t:
            return i
    return len(FILL_ORDER)


PLACED_TYPES = ("ftl_beacon", "ftl_relay")


def placed(d: dict) -> bool:
    """A beacon or relay deployed and working at a location (monitoring / relaying): it stays where it is — never
    spare, never sent to fill a shortfall or gathered at the depot. Stowed ones (a survey crew's next drops) count as usual."""
    return ((d.get("device_type") or "") in PLACED_TYPES and bool(d.get("location"))
            and not d.get("stowed_in_device_code") and not d.get("attached_to_device_code")
            and str(d.get("status") or "").startswith(("monitoring", "relaying")))


def relaying_stars(devices: list[dict], exclude: set[str] | frozenset = frozenset()) -> set[str]:
    """Systems where a relay of ours is switched on (status relaying), not counting the `exclude` devices."""
    return {star_of(d.get("location")) for d in devices if (d.get("device_type") or "") == "ftl_relay"
            and d.get("location") and d.get("device_code") not in exclude and str(d.get("status") or "").startswith("relaying")}


def survey_cargo(devices: list[dict], fleets: list[dict]) -> set[str]:
    """An explore fleet's relays and beacons: carried to be dropped one per new system, so the placement step and the
    wake-up rule leave them alone while they wait at home (seen live 2026-10-09: three Surveyors relays printed in
    FALQUORYX were each sent to its L4 point and switched on)."""
    tags = {fl.fleet_tag(f["id"]) for f in fleets or [] if f.get("role") == "explore"}
    return {d["device_code"] for d in devices if (d.get("device_type") or "") in PLACED_TYPES
            and tags & set(d.get("tags") or [])}


def for_contract(d: dict) -> bool:
    """Tagged for a contract (contractsupply.py): it goes to / stays at the contract's location, no fleet takes it."""
    return any(str(t).startswith("contract:") for t in d.get("tags") or [])


def is_factory(d: dict) -> bool:
    """An autofactory, or another printer that works a queue. Not a vessel: it lists enqueue_print but only prints through
    its replicant, one at a time (seen live 2026-10-09: a fleet's replenishment print queued on a heaven vessel instead
    of the system's autofactories, and never printed)."""
    t = d.get("device_type") or ""
    return "vessel" not in t and ("enqueue_print" in (d.get("available_commands") or []) or "autofactory" in t)


def plan(cfg: dict, devices: list[dict], blueprints: list[dict], inventory: dict[str, dict], stars: dict[str, dict],
         replicant_hosts: dict[str, str], busy: set[str], orders: list[dict], stowed_map: dict[str, list[str]],
         only: set[str] | None = None, open_sites: dict[str, int] | None = None, protect: set[str] | None = None,
         geo: dict[str, dict] | None = None) -> dict:
    """Work out what to tag, print and move. `only`: limit shortfall filling to these stars.
    `open_sites`: star → open mining sites on its belts (recently read); a system known to have none gets no extra
    mining drones (they'd sit idle) until sites open."""
    open_sites = open_sites or {}
    protect = protect or set()   # devices that must stay where they are (e.g. the beacon at a civilization's body)
    cfg = normalize(cfg)
    s = cfg["settings"]
    ignore = set(cfg["ignore_tags"])
    known_stars = set(stars) | {star_of(d.get("location")) for d in devices} | {f.get("home") for f in cfg["fleets"] if f.get("home")}
    pos = {k: (v or {}).get("position") or {} for k, v in stars.items()}
    bps = {b["device_type"]: b for b in blueprints}
    stowed_in = {c: carrier for carrier, kids in (stowed_map or {}).items() for c in kids}
    for d in devices:  # the device list says it directly
        if d.get("stowed_in_device_code") or d.get("attached_to_device_code"):
            stowed_in[d["device_code"]] = d.get("stowed_in_device_code") or d.get("attached_to_device_code")

    groups = stationed_fleets(cfg)           # fleets kept at their loadout in their home system, by (home, id)
    by_tag = {fl.fleet_tag(f["id"]): f for f in groups}

    def fam(d: dict | None) -> str | None:
        """A bootstrap's devices carry boot:<id> (bootstrap.py): they're only ever used by that bootstrap's fleets, and
        its fleets use nothing else — no spares, factories or carriers lent either way."""
        return next((t for t in (d or {}).get("tags") or [] if t.startswith("boot:")), None)

    def fleet_fam(tag: str | None) -> str | None:
        f = by_tag.get(tag or "")
        return f"boot:{f['family']}" if f and f.get("family") else None
    homes_of = defaultdict(list)             # star -> its stationed fleets
    for f in groups:
        homes_of[f["home"]].append(f)

    def fleet_tag_of(d: dict) -> str | None:
        return next((t for t in d.get("tags") or [] if t.startswith("fleet:")), None)

    def visible(d: dict) -> bool:
        """Ignored devices, the replicants' vessels and the devices of fleets that aren't stationed (on a mission, or
        never stationed) are left out; a stationed fleet's devices and fleetless devices are the planner's."""
        tags = set(d.get("tags") or [])
        f = fleet_tag_of(d)
        if placed(d) and not (f in by_tag and by_tag[f]["home"] == star_of(d.get("location"))):
            return False   # a beacon / relay at work where it is: not the planner's to count, spare or move
        return not (ignore & tags) and d.get("device_code") not in replicant_hosts and (not f or f in by_tag)
    # A stationed fleet's device tagged to:<old home> (e.g. printed before the fleet moved) goes to its current home.
    # Seen live 2026-10-07: Miner 1's observatory still tagged to:kelmornea after the fleet moved to LARSELAN.
    retag: dict[str, tuple[str, str]] = {}
    fixed = []
    for d in devices:
        f = by_tag.get(fleet_tag_of(d) or "")
        dest = bound_for(d, known_stars)
        if f and dest and dest != f["home"] and not d.get("location_stale"):
            retag[d["device_code"]] = (to_tag(dest), to_tag(f["home"]))
            d = {**d, "tags": [t for t in d.get("tags") or [] if t != to_tag(dest)] + [to_tag(f["home"])]}
        fixed.append(d)
    devices = fixed
    pool = [d for d in devices if visible(d)]
    stale = [d for d in pool if d.get("location_stale") or not (d.get("location") or d.get("stowed_in_device_code")
                                                                   or d.get("attached_to_device_code"))]
    loc_of = {d.get("device_code"): d.get("location") for d in devices}
    busy = set(busy)
    job_busy = set(busy)   # used by a running job (as opposed to busy because of what it's doing right now)
    for d in devices:
        if d.get("in_control_range") is False:  # out of comms range: can't be commanded right now
            busy = set(busy) | {d.get("device_code")}
        if str(d.get("status") or "").startswith(("tracking", "searching")):  # moving it would close its site
            busy = set(busy) | {d.get("device_code")}
        if d.get("location_stale"):  # last known position only (partial snapshot): count it, don't command it
            busy = set(busy) | {d.get("device_code")}

    def ctrl_star(d: dict) -> str | None:
        c = d.get("controller_device_code")
        return star_of(loc_of.get(c)) if c and loc_of.get(c) else None

    by_code_all = {d.get("device_code"): d for d in devices}

    def is_ferry_ctrl(code: str | None) -> bool:
        c = by_code_all.get(code) or {}
        return "transport" in (c.get("device_type") or "") and (
            ((c.get("ami_directive") or {}).get("name") == "ferry") or FERRY_TAG in (c.get("tags") or []))

    def ferry_side(d: dict) -> bool:
        """Part of the ferry (its freighters, drones, taxi plates): it belongs to the ferry, never to a fleet or spare."""
        return (FERRY_TAG in (d.get("tags") or []) or "taxi" in (d.get("tags") or []) or d.get("taxi_mode") == "taxi"
                or is_ferry_ctrl(d.get("controller_device_code")))

    def unassigned_star(d: dict) -> str | None:
        """The system a fleetless device counts for: where its controller has adopted it, else an old `home:` tag
        (devices from before stationed fleets), else where it is."""
        cs = ctrl_star(d)
        if cs and (cs == star_of(d.get("location")) or is_ferry_ctrl(d.get("controller_device_code"))):
            return cs   # adopted where it is, or one of a ferry's devices: it belongs to the controller's system
        return home_of(d, known_stars) or star_of(d.get("location")) or None
    report: dict[str, dict] = {}
    tag_add: dict[str, set] = defaultdict(set)
    tag_remove: dict[str, set] = defaultdict(set)
    moves: dict[str, str] = {}        # device -> destination star
    for d in devices:   # leftover spare tags on placed beacons / relays go (seen live 2026-10-08: 13 working beacons tagged spare)
        if placed(d) and SPARE in (d.get("tags") or []) and not (ignore & set(d.get("tags") or [])):
            tag_remove[d["device_code"]].add(SPARE)
    # an explore fleet's relay switched on at home (before survey_cargo kept them aboard) stays as the system's relay and
    # leaves the fleet — the first one where no other relay of ours works; the others go with the fleet next mission
    cargo = survey_cargo(devices, cfg["fleets"])
    covered = relaying_stars(devices, cargo)
    for d in sorted((x for x in devices if x["device_code"] in cargo and placed(x)
                     and (x.get("device_type") or "") == "ftl_relay"), key=lambda x: x["device_code"]):
        st = star_of(d.get("location"))
        if st in covered or not is_place(d.get("location") or ""):
            continue
        covered.add(st)
        tag_remove[d["device_code"]].update(t for t in d.get("tags") or [] if t.startswith("fleet:"))
        tag_add[d["device_code"]].add(at_tag(d["location"]))
    for code, (old_t, new_t) in retag.items():
        tag_remove[code].add(old_t)
        tag_add[code].add(new_t)
    assign: dict[str, str] = {}       # device -> fleet tag it joins (set by its move, or straight away)
    unmet: list[dict] = []

    # incoming to each fleet: members on their way home, plus prints ordered for it
    incoming: dict[str, Counter] = defaultdict(Counter)
    for d in pool:
        dest = bound_for(d, known_stars)
        if dest and star_of(d.get("location")) != dest:
            moves[d["device_code"]] = dest
            f = by_tag.get(fleet_tag_of(d) or "")
            if not fleet_tag_of(d) and GATHER not in (d.get("tags") or []) and homes_of.get(dest) and not for_contract(d):
                f = homes_of[dest][0]   # sent before stationed fleets (no fleet tag yet): it joins the fleet there on arrival
            if f and f["home"] == dest:
                incoming[f["id"]][d.get("device_type")] += 1
    for o in orders:
        fid = o.get("fleet") or next((f["id"] for f in homes_of.get(o.get("star") or "", [])), None)
        if fid:
            incoming[fid][o["device_type"]] += 1

    def members(f: dict) -> list[dict]:
        """A stationed fleet's devices, wherever they are right now (a carrier out on a delivery still counts at home).
        One on its way somewhere else doesn't; one on its way home counts once it has arrived (until then: incoming)."""
        tag = fl.fleet_tag(f["id"])
        out = []
        for d in pool:
            if fleet_tag_of(d) != tag:
                continue
            dest = bound_for(d, known_stars)
            if dest and not (dest == f["home"] and star_of(d.get("location")) == dest):
                continue
            out.append(d)
        return out

    # fleetless devices each stationed fleet may take: in its home system, not spare, not on the way elsewhere
    unassigned: dict[str, list[dict]] = defaultdict(list)
    for d in pool:
        if fleet_tag_of(d) or d["device_code"] in replicant_hosts or for_contract(d):
            continue   # a device on its way to (or kept at) a contract's location isn't the fleet there's to take
        tags = set(d.get("tags") or [])
        dest = bound_for(d, known_stars)
        if dest and star_of(d.get("location")) != dest and not ferry_side(d):
            continue
        if not dest and SPARE in tags and not home_of(d, known_stars):
            continue  # spare = belongs to no fleet until it's sent somewhere (one that has arrived counts there)
        st = unassigned_star(d) if ferry_side(d) else (dest or unassigned_star(d))
        if st in homes_of:
            unassigned[st].append(d)

    made_spare: set[str] = set()   # extras this pass: tagged spare and let go by their controller
    taken: set[str] = set()        # fleetless devices a fleet takes this pass

    def pinned(d: dict) -> bool:
        """Can't be made spare: busy (tracking a site, mid-job, out of range), protected, or the ferry's."""
        return d["device_code"] in busy or d["device_code"] in protect or ferry_side(d) or placed(d)

    # 1-2: per stationed fleet: count, mark extras as spare, take fleetless devices at home, un-spare what's needed
    donors: dict[str, list[dict]] = defaultdict(list)  # type -> spare devices anywhere
    from . import wards
    warded = wards.foreign(stars, devices)   # another player's ward or hub: nothing of ours can mine there
    # a replicant's vessel tagged into a fleet is a member: it counts toward the loadout, though the planner never
    # moves, spares or swaps it (seen live 2026-10-08: SOL's heaven_vessel, hosting a replicant, wasn't counted and a
    # second one was printed for the fleet)
    riders: dict[str, Counter] = defaultdict(Counter)
    for d in devices:
        if d.get("device_code") in replicant_hosts and fleet_tag_of(d) in by_tag and not (ignore & set(d.get("tags") or [])):
            riders[fleet_tag_of(d)][d.get("device_type") or "device"] += 1
    for f in groups:
        star, fid, tag = f["home"], f["id"], fl.fleet_tag(f["id"])
        mine = members(f)
        free = [d for d in unassigned.get(star, []) if d["device_code"] not in taken and fam(d) == fleet_fam(tag)]
        rows = []
        wants = fl.station_wants(f)
        for t in sorted(wants):  # types the loadout doesn't mention are "don't care": never spare, never filled
            want = int(wants.get(t) or 0)
            ride = min(want, riders[tag][t])   # the replicants' vessels in the fleet fill part of the loadout
            want -= ride
            have = [d for d in mine if (d.get("device_type") or "device") == t] + \
                   [d for d in free if (d.get("device_type") or "device") == t]
            ward_hold = star in warded and t in wards.MINING_TYPES and want > len(have)
            if ward_hold:   # home warded by another player: send no more miners (the ones there stay)
                want = len(have)
            inc = incoming[fid][t]
            surplus = max(0, len(have) - want)
            # who stays: members first, then non-spare, controller-run, already tagged home here (from before stationed
            # fleets), at home, busy ones (can't be moved anyway), then idle-less-healthy last
            # (seen live: FALQUORYX's home-tagged autofactory was about to be made spare in favor of a fresh untagged one)
            ranked = sorted(have, key=lambda d: (fleet_tag_of(d) != tag, SPARE in (d.get("tags") or []),
                                                 not d.get("controller_device_code"),
                                                 home_tag(star) not in (d.get("tags") or []),
                                                 star_of(d.get("location")) != star,
                                                 d["device_code"] not in busy, _idle(d), -_cap(d), d["device_code"]))
            keep, extra = ranked[:len(have) - surplus], ranked[len(have) - surplus:]
            stuck = [d for d in extra if fleet_tag_of(d) == tag and pinned(d)]
            if stuck:   # a member that can't be let go stays
                extra = [d for d in extra if d not in stuck]
                keep = keep + stuck
            for d in keep:
                code, tags = d["device_code"], set(d.get("tags") or [])
                if SPARE in tags:
                    tag_remove[code].add(SPARE)
                if fleet_tag_of(d) != tag:   # a fleetless device here joins the fleet
                    taken.add(code)
                    tag_add[code].add(tag)
                    assign[code] = tag
                tag_remove[code].update(x for x in tags if x.startswith("home:"))
            for d in extra:
                if fleet_tag_of(d) != tag:
                    continue  # fleetless: another fleet here may take it; if none does, it's made spare below
                code, tags = d["device_code"], set(d.get("tags") or [])
                made_spare.add(code)   # spare: it leaves the fleet and belongs to none until it's assigned again
                tag_add[code].add(SPARE)
                tag_remove[code].update({tag} | {x for x in tags if x.startswith("home:")})
            away = [d["device_code"] for d in keep if star_of(d.get("location")) != star]
            spares_here = [d["device_code"] for d in pool if d.get("device_type") == t and star_of(d.get("location")) == star
                           and SPARE in (d.get("tags") or []) and not fleet_tag_of(d) and not home_of(d, known_stars)]
            rows.append({"type": t, "want": want + ride, "have": len(have) + ride, "riders": ride, "incoming": inc, "away": away, "spares_here": spares_here,
                         "warded": ward_hold,
                         "short": max(0, want - len(have) - inc), "surplus": surplus,
                         "spare": [d["device_code"] for d in extra if fleet_tag_of(d) == tag]})
        ph = next((p for p in cfg["phases"] if p.get("id") == f.get("template")), None)
        if star in warded and any(t in wants for t in wards.MINING_TYPES):
            unmet.append({"star": star, "fleet": f.get("name"), "type": "mining", "n": 0,
                          "why": f"another player's system ward or hub is in {star}: no mining controllers or drones are sent there"})
        report[fid] = {"fleet": f, "star": star, "phase": ph or {"id": "", "name": "custom loadout"}, "rows": rows,
                       "warded": star in warded,
                       "short": sum(r["short"] for r in rows), "surplus": sum(r["surplus"] for r in rows)}
    # fleetless devices at a stationed fleet's home that no fleet there took: spare, if a fleet there counts their type
    for star, ds in unassigned.items():
        counted = {t for f in homes_of[star] for t in fl.station_wants(f)}
        for d in ds:
            code, tags = d["device_code"], set(d.get("tags") or [])
            if code in taken or (d.get("device_type") or "device") not in counted or pinned(d):
                continue
            made_spare.add(code)
            if SPARE not in tags:
                tag_add[code].add(SPARE)
            tag_remove[code].update(x for x in tags if x.startswith("home:"))

    def fleet_now(code: str) -> str | None:
        d = by_code_all.get(code) or {}
        tag = fleet_tag_of(d)
        if tag and tag in tag_remove.get(code, set()):
            tag = None
        return assign.get(code) or tag

    for d in pool:
        code = d["device_code"]
        tags = set(d.get("tags") or [])
        spare_now = (SPARE in tags or SPARE in tag_add[code]) and SPARE not in tag_remove[code]
        if spare_now and not fleet_now(code) and code not in moves and code not in busy and not ferry_side(d) and not for_contract(d):
            donors[d.get("device_type") or "device"].append(d)

    def working(d: dict) -> bool:
        """Busy doing something it can't fly away from (seen live: 'Cannot cruise while mining')."""
        return str(d.get("status") or "").startswith(WORKING)

    # 3: fill shortfalls — spares first, then prints
    reserved: dict[str, Counter] = defaultdict(Counter)  # location -> resources set aside in this plan
    prints: list[dict] = []
    factories = [d for d in pool if is_factory(d) and d["device_code"] not in busy]

    qcap_default = {b["device_type"]: int(b.get("queue_size") or 0) for b in blueprints}
    queue_free: dict[str, int] = {}
    for f in factories:
        cap = int(qcap_default.get(f.get("device_type")) or f.get("queue_capacity") or 10)
        used_q = len(f.get("print_queue") or []) + (1 if f.get("printing") or str(f.get("status") or "").startswith("printing") else 0)
        queue_free[f["device_code"]] = max(0, cap - used_q)
    load = {f["device_code"]: pq.load_seconds(f, bps) for f in factories}   # seconds of printing queued, per factory
    # Prints this app ordered that the device list doesn't show yet count too: with stock for one print a pass, each pass
    # saw every factory idle and the same one (lowest code) got every print (seen live 2026-10-06: 3 on one of 3 factories).
    ordered: dict[str, float] = defaultdict(float)
    for o in orders:
        if o.get("factory"):
            ordered[o["factory"]] += float((bps.get(o.get("device_type")) or {}).get("print_time") or 0) or 1.0
    for code in load:
        load[code] = max(load[code], ordered.get(code, 0.0))

    reach = float(s.get("max_supply_ly") or 0)

    def near(d: dict, star: str) -> bool:
        """Close enough to supply `star`: within max_supply_ly (seen live 2026-10-08: a fleet in SOL was given prints on
        FALQUORYX's autofactories, too far away to be of use)."""
        dist = _dist(star_of(d.get("location")), star, pos)
        return not reach or dist <= reach or dist >= 1e9   # positions unknown: can't tell, so allowed

    def usable(tag: str | None) -> list[dict]:
        """Factories with queue room a fleet may print on: fleetless ones and its own; another fleet's only when there's
        nothing else (e.g. every autofactory belongs to the printing hub)."""
        open_f = [f for f in factories if queue_free.get(f["device_code"], 1) > 0 and fam(f) == fleet_fam(tag)]
        mine = [f for f in open_f if not fleet_tag_of(f) or fleet_tag_of(f) == tag]
        return mine or open_f

    def factory_for(t: str, star: str, tag: str | None = None) -> tuple[dict | None, str]:
        bp = bps.get(t)
        if not bp:
            return None, f"no blueprint for {t}"
        cost = as_amounts(bp.get("resources"))
        if not factories:
            return None, "no autofactory"
        best = None
        open_f = usable(tag)
        if not open_f:
            return None, "every autofactory's print queue is full"
        open_f = [f for f in open_f if near(f, star)]
        if not open_f:
            return None, f"no autofactory within {reach:g} ly of {star} (Fleets › settings: supply range)"
        for f in sorted(open_f, key=lambda f: (star_of(f.get("location")) != star, _dist(star_of(f.get("location")), star, pos))):
            stock = as_amounts(inventory.get(f.get("location")) or {})
            free = {r: stock.get(r, 0.0) - reserved[f.get("location")][r] for r in cost}
            if all(free[r] >= v for r, v in cost.items()):
                return f, ""
            best = best or f
        if s["need_stock"]:
            return None, f"no autofactory has the materials for {t}"
        return best, "queued without enough stock (it waits for materials)"

    order = sorted(report, key=lambda k: (-report[k]["short"], report[k]["star"], k))
    # fill by role, across all fleets: controllers, then survey drones (they open the sites), then miners, then the rest
    # — with one autofactory, miners queued ahead of the surveyors would only sit idle at a belt with no open sites
    work = sorted(((k, row) for k in order if not (only and k not in only and report[k]["star"] not in only)
                   for row in report[k]["rows"]),
                  key=lambda kr: (fill_rank(kr[1]["type"]), order.index(kr[0])))
    for fid, row in work:
        star, tag = report[fid]["star"], fl.fleet_tag(fid)
        if True:
            need = row["short"]
            if need <= 0:
                continue
            if row["type"] == "mining_drone" and open_sites.get(star) == 0:
                # its belts have no open sites right now: more miners would only sit idle
                unmet.append({"star": star, "type": row["type"], "n": need,
                              "why": "waiting — no open mining sites in its belts right now (survey drones must open some first)"})
                continue
            cands = sorted((d for d in donors.get(row["type"], []) if (star_of(d.get("location")) == star or not working(d))
                            and near(d, star) and fam(d) == fleet_fam(tag)),
                           key=lambda d: (_dist(star_of(d.get("location")), star, pos), not _idle(d), -_cap(d), d["device_code"]))
            held = [d for d in donors.get(row["type"], []) if d not in cands and near(d, star) and fam(d) == fleet_fam(tag)]
            for d in cands[:need]:
                donors[row["type"]].remove(d)
                code = d["device_code"]
                tag_remove[code].add(SPARE)
                tag_add[code].discard(SPARE)
                moves[code] = star
                assign[code] = tag
                row.setdefault("from_spares", []).append(code)
                need -= 1
            if need > 0 and held:
                # spares that are still working (mining, tracking …): they'll be sent once idle — don't print instead
                wait = held[:need]
                for d in wait:
                    donors[row["type"]].remove(d)
                row["held_spares"] = [d["device_code"] for d in wait]
                unmet.append({"star": star, "type": row["type"], "n": len(wait),
                              "why": f"spare(s) {', '.join(d['device_code'] for d in wait)} still working — sent once idle"})
                need -= len(wait)
            own = [g for g in factories if fleet_tag_of(g) == tag and queue_free.get(g["device_code"], 1) > 0 and near(g, star)]
            if need > 0 and s["print_missing"] and own and row["type"] in bps:
                # Seen live (2026-10-06): three fleets each with an autofactory, all three prints on one of them. A fleet
                # with its own autofactory prints there; short of stock, the print waits for materials in its queue.
                cost = as_amounts((bps.get(row["type"]) or {}).get("resources"))
                parts = pq.split(own, row["type"], need, bps, load, queue_free)
                n = sum(k for _, k in parts)
                for g, k in parts:
                    stock = as_amounts(inventory.get(g.get("location")) or {})
                    short = any(stock.get(r, 0) - reserved[g["location"]][r] < v * k for r, v in cost.items())
                    for r, v in cost.items():
                        reserved[g["location"]][r] += v * k
                    prints.append({"factory": g["device_code"], "factory_star": star_of(g.get("location")),
                                   "device_type": row["type"], "n": k, "star": star, "fleet": fid,
                                   "note": "the fleet's own autofactory; waits for materials" if short else ""})
                row["printing"] = n
                need -= n
                if need > 0:
                    unmet.append({"star": star, "type": row["type"], "n": need,
                                  "why": f"the fleet's autofactory queue is full ({', '.join(g['device_code'] for g in own)})"})
            elif need > 0 and s["print_missing"]:
                f, why = factory_for(row["type"], star, tag)
                if f:
                    cost = as_amounts((bps.get(row["type"]) or {}).get("resources"))
                    n = need
                    if s["need_stock"]:  # only as many as the stock covers
                        stock = as_amounts(inventory.get(f.get("location")) or {})
                        n = min(need, min((int((stock.get(r, 0) - reserved[f["location"]][r]) // v) for r, v in cost.items() if v > 0),
                                          default=need))
                    # every autofactory in that system shares the work, evenly by print time (with need_stock, only
                    # those at the same stockpile, since the stock check above was for that one)
                    peers = [g for g in factories if star_of(g.get("location")) == star_of(f.get("location"))
                             and (not s["need_stock"] or g.get("location") == f.get("location"))
                             and g in usable(tag)]
                    # Seen live (2026-10-07): Miner 1 and Miner 2 have no autofactory, all three are Printing Hub 1's; both
                    # their prints went on one of them while another sat idle — the split only had the first one to use.
                    parts = pq.split(peers, row["type"], n, bps, load, queue_free)
                    n = sum(k for _, k in parts)
                    for g, k in parts:
                        for r, v in cost.items():
                            reserved[g["location"]][r] += v * k
                        prints.append({"factory": g["device_code"], "factory_star": star_of(g.get("location")),
                                       "device_type": row["type"], "n": k, "star": star, "fleet": fid, "note": why})
                    row["printing"] = n
                    need -= n
                    if need > 0:
                        on = ", ".join(g["device_code"] for g in peers)
                        unmet.append({"star": star, "type": row["type"], "n": need,
                                      "why": f"room/materials for only {n} on {on} this pass"})
                else:
                    unmet.append({"star": star, "type": row["type"], "n": need, "why": why})
            elif need > 0:
                unmet.append({"star": star, "type": row["type"], "n": need, "why": "no spares (printing is off)"})

    # devices away from their home system with nothing to do there (e.g. printed on another system's autofactory,
    # or left behind) go home — by surging themselves or on a carrier, like any other delivery
    # Seen live (2026-10-06): Miner 1's home moved from ITHVALAI to KELMORNEA. Its controllers kept coordinating there and
    # their drones stayed adopted (or searching), so none of them counted as free to go and 30 devices were left behind.
    # A whole working group left in another system goes home together: the controllers drop their directives and let
    # their drones go as they leave (leave_steps), and sites the survey drones hold there are given up.
    def left_behind(d: dict, home: str, tag: str) -> bool:
        code, st = d["device_code"], str(d.get("status") or "")
        if star_of(d.get("location")) in ("", home):
            return False
        if pinned_at(d) and star_of(pinned_at(d)) == star_of(d.get("location")):
            return False   # pinned where it is (`at:` tag), e.g. a fleet's autofactory kept at the main stockpile
        if (code in job_busy or d.get("location_stale") or d.get("in_control_range") is False
                or st.startswith(("travel", "cruis", "surg", "mining", "collecting", "depositing", "printing", "repairing"))):
            return False
        tags = set(d.get("tags") or [])
        # a taxi plate or a ferry's freighter: away from home is its job — while it has one. A plate still tagged taxi but
        # not in taxi mode and run by no controller is idle where its ferry left it (seen live 2026-10-08: Printing
        # Hub 1's four surge plates idle in ITHVALAI, flagged by the check but never sent home)
        orphan = not d.get("controller_device_code") and d.get("taxi_mode") != "taxi" and st.startswith(("idle", "stowed"))
        if d.get("taxi_mode") == "taxi" or ("taxi" in tags and not orphan) or (FERRY_TAG in tags and not is_ami_controller(d)):
            return False
        c = by_code_all.get(d.get("controller_device_code") or "")
        if d.get("controller_device_code") and not c:
            return False   # run by a controller we can't see: leave it to it
        if c and is_ferry_ctrl(c["device_code"]) and not left_behind(c, home, tag):
            return False   # run by a working ferry (its freighters travel between systems)
        if c and star_of(c.get("location")) == star_of(d.get("location")):
            # run by a controller where it is: only if that controller is this fleet's and is going home too
            return fleet_tag_of(c) == tag and not c.get("controller_device_code") and left_behind(c, home, tag)
        return True

    returning = []
    carriers_away: list[tuple[str, str]] = []
    for f in groups:
        tag = fl.fleet_tag(f["id"])
        for d in members(f):
            code, home = d["device_code"], f["home"]
            here = star_of(d.get("location"))
            if (here and here != home and code not in moves and SPARE not in (d.get("tags") or [])
                    and not bound_for(d, known_stars) and fleet_now(code) and left_behind(d, home, tag)):
                if can_surge(d) and _carrier_cap(d, bps) > 0:
                    # a carrier: deliveries may need it where it is; it goes home afterwards only if none does (below).
                    # Seen live 2026-10-07: Miner 1's only carrier was sent home every pass, so it never fetched the
                    # 31 devices waiting for it.
                    carriers_away.append((code, home))
                    continue
                moves[code] = home
                returning.append(code)
                busy.discard(code)
    # already on their way home (to: tag from an earlier pass) but holding a site in the old system: same rule
    for code, dest in list(moves.items()):
        d = by_code_all.get(code) or {}
        f = by_tag.get(fleet_tag_of(d) or "")
        if code in busy and f and f["home"] == dest and left_behind(d, dest, fl.fleet_tag(f["id"])):
            busy.discard(code)

    # spares nobody needs this pass are gathered at the depot (a delivery / autofactory system) for later use
    gathering: list[str] = []
    depot = spare_depot(cfg, devices) if s.get("gather_spares", True) and not only else None
    if depot:
        for t, ds in donors.items():
            for d in ds:
                code = d["device_code"]
                if (code in moves or code in busy or star_of(d.get("location")) == depot or not d.get("location")
                        or d.get("controller_device_code") or working(d) or pinned_at(d) or d.get("location_stale")
                        or d.get("taxi_mode") == "taxi" or "taxi" in (d.get("tags") or []) or fam(d)
                        or not str(d.get("status") or "").startswith(("idle", "stowed", "inactive", "monitoring", "deployed"))):
                    continue
                moves[code] = depot
                gathering.append(code)

    def governed(d: dict) -> bool:
        """A stationed fleet at home where it is counts its type (so 'spare' there is the loadout's own call)."""
        t = d.get("device_type") or "device"
        return any(t in fl.station_wants(f) for f in homes_of.get(star_of(d.get("location")), []))

    # tag hygiene, for every device (whether or not its type is in a phase):
    #  • run by a controller (working) or a taxi plate → never spare
    #  • spare → no home tag (spare = belongs to no system)
    for d in pool:
        code, tags = d["device_code"], set(d.get("tags") or [])
        if code in moves or SPARE not in tags or SPARE in tag_remove[code]:
            continue
        taxi = d.get("taxi_mode") == "taxi" or "taxi" in tags
        if taxi or is_ferry_ctrl(d.get("controller_device_code")):
            # a taxi plate, or a ferry's freighter/plate: it belongs to its ferry, never spare
            tag_remove[code].add(SPARE)
            tag_add[code].discard(SPARE)
            made_spare.discard(code)
        elif d.get("controller_device_code") and not governed(d):
            # spare tag left over, but no loadout covers this type here and a controller is using it: it's working
            tag_remove[code].add(SPARE)
            tag_add[code].discard(SPARE)
            made_spare.discard(code)
        elif d.get("controller_device_code"):
            # spare (the loadout doesn't need it) but a controller still runs it: let it go, keep it spare
            made_spare.add(code)
            homes = {t for t in tags if t.startswith("home:")}
            if homes:
                tag_remove[code].update(homes)
        else:
            homes = {t for t in tags if t.startswith("home:")}
            if homes:
                tag_remove[code].update(homes)

    # `home:` tags are from before stationed fleets: whatever they still sit on, they go
    for d in pool:
        code = d["device_code"]
        old = {t for t in d.get("tags") or [] if t.startswith("home:")}
        if old and code not in moves:
            tag_remove[code].update(old)

    # a device run by a controller in another system (e.g. delivered without being released) is let go
    releases: dict[str, list[str]] = defaultdict(list)
    for d in pool:
        cs = ctrl_star(d)
        if is_ferry_ctrl(d.get("controller_device_code")) or d.get("taxi_mode") == "taxi":
            continue  # a ferry's drones, freighters and taxi plates are meant to be in other systems
        # anything else (incl. an in-system transport controller on delivery/shuttle/consolidate) can't use a device
        # that's in another system: release it so it can work where it is
        if cs and cs != star_of(d.get("location")) and d["device_code"] not in busy and d["device_code"] not in moves:
            releases[d["controller_device_code"]].append(d["device_code"])
    # a device the loadout no longer wants (made spare) is let go by its controller, so the spare tag sticks and the
    # device is free to be sent where it's needed (a ferry's freighters and taxi plates stay with their ferry)
    for code in sorted(made_spare):
        d = by_code_all.get(code) or {}
        c = d.get("controller_device_code")
        if (c and code not in busy and code not in moves and not is_ferry_ctrl(c) and d.get("taxi_mode") != "taxi"
                and code not in releases.get(c, [])):
            releases[c].append(code)

    # controllers and the devices they run (a controller leaving lets its drones go and drops its directive first)
    managed_map: dict[str, list[str]] = defaultdict(list)
    for d in devices:
        if d.get("controller_device_code"):
            managed_map[d["controller_device_code"]].append(d["device_code"])

    # 4: deliveries for everything bound somewhere else
    by_code = {d["device_code"]: d for d in pool}
    arrived, self_moves, batches = [], [], defaultdict(list)
    def why_busy(d: dict) -> str:
        st = str(d.get("status") or "")
        if d.get("in_control_range") is False:
            return "out of comms range"
        if d.get("location_stale"):
            return "its position is unknown right now"
        if st.startswith(("tracking", "searching")):
            return f"it is {st} (moving it would close the site)"
        return "a running job is using it (see Automations)"

    from .modular import compacted as _compacted, folded as _folded, is_modular
    compact: list[tuple[str, str]] = []   # large devices to compact now, before any carrier is assigned
    for code, dest in moves.items():
        d = by_code.get(code)
        if not d:
            continue
        if (code not in busy and star_of(d.get("location")) not in ("", dest) and is_modular(d)
                and not _folded(d)):
            # Compacting takes hours (≈30 % of the print time; an observatory well over 2 h). It starts as soon as the
            # move is planned, on its own; a carrier is only assigned once the device reports compacted.
            if not _compacted(d):
                compact.append((code, dest))
            unmet.append({"star": dest, "type": d.get("device_type") or "device", "n": 1,
                          "why": f"{code} is {'compacting' if _compacted(d) else 'being compacted'} for the trip — "
                                 "a carrier is sent once it's done"})
            continue
        if code in busy:
            if star_of(d.get("location")) != dest:
                unmet.append({"star": dest, "type": d.get("device_type") or "device", "n": 1,
                              "why": f"{code} can't leave {star_of(d.get('location')) or '?'} yet: {why_busy(d)}"})
            continue
        here = star_of(d.get("location"))
        if here == dest:
            arrived.append(code)
        elif can_surge(d) and code not in stowed_in:
            self_moves.append((code, dest))
        else:
            batches[(here, dest, d.get("replicant_code"))].append(code)
    for d in pool:  # tagged earlier and already there
        dest = bound_for(d, known_stars)
        if dest and star_of(d.get("location")) == dest and d["device_code"] not in busy and d["device_code"] not in arrived:
            arrived.append(d["device_code"])

    carriers = [d for d in devices if d["device_code"] not in busy and can_surge(d) and _carrier_cap(d, bps) > 0
                and not (ignore & set(d.get("tags") or [])) and (s["use_replicant_vessels"] or d["device_code"] not in replicant_hosts)
                and not str(d.get("status") or "").startswith(("travel", "cruis", "surg", "stowed"))
                and not d.get("controller_device_code")    # run by a controller (e.g. a ferry's taxi plates) = busy
                and (not fleet_tag_of(d) or fleet_tag_of(d) in by_tag)]   # a fleet on a mission keeps its carriers
    moving = set(moves) | {c for c, _ in self_moves}
    carriers = [c for c in carriers if c["device_code"] not in moving and not bound_for(c, known_stars)]
    used: set[str] = set()
    deliveries = []
    def same_owner(c: dict, owner: str | None) -> bool:
        """A carrier can only take on devices of its own replicant (seen live 2026-10-06: 'Target device belongs to a
        different account'). Since 1.26.0 the job hands each device to the carrier's owner as it boards
        (modular.with_owner_handoff), so any carrier will do — except for a batch whose owner is unknown-mixed or that
        holds a device hosting a replicant (never handed over)."""
        if codes and fam(c) != fam(by_code.get(codes[0])):
            return False   # a bootstrap's carriers carry only its own devices, and its devices only ride them
        if not owner or not c.get("replicant_code") or c["replicant_code"] == owner:
            return True
        return not any((by_code.get(x) or {}).get("hosting_replicant") for x in codes)

    for (here, dest, owner), codes in sorted(batches.items(), key=lambda kv: (-len(kv[1]), kv[0][0], kv[0][1], kv[0][2] or "")):
        codes = sorted(codes)
        while codes:
            stowable = all("stow" in (by_code[x].get("available_commands") or ["stow"]) for x in codes)
            grounded = any(not can_travel(by_code[x]) for x in codes)   # e.g. a beacon: the carrier picks it up, in its hold
            options = [c for c in carriers if star_of(c.get("location")) == here and c["device_code"] not in used
                       and same_owner(c, owner) and c["device_code"] not in codes and (stowable or carry_mode(c, bps) == "attach")
                       and not (grounded and carry_mode(c, bps) == "attach")]
            fetched_from = None
            if not options:
                # none in this system: send the nearest free carrier from another system to pick them up
                remote = [c for c in carriers if star_of(c.get("location")) not in (here, "") and c["device_code"] not in used
                          and same_owner(c, owner) and c["device_code"] not in codes and (stowable or carry_mode(c, bps) == "attach")
                          and not (grounded and carry_mode(c, bps) == "attach") and _free(c, bps, stowed_map) > 0]
                remote.sort(key=lambda c: (bool(owner) and c.get("replicant_code") != owner,
                                           _dist(star_of(c.get("location")), here, pos), -_free(c, bps, stowed_map), c["device_code"]))
                if remote:
                    options, fetched_from = remote[:1], remote[0].get("location")
            if not options:
                what = "gather at the depot" if set(codes) <= set(gathering) else f"go to {dest}"
                hosts = any((by_code.get(x) or {}).get("hosting_replicant") for x in codes)
                whose = f" owned by {owner}" if owner and hosts else ""
                unmet.append({"star": dest, "type": ", ".join(sorted({by_code[c].get('device_type') for c in codes})),
                              "n": len(codes), "why": f"waiting for a surge-capable carrier{whose} in {here} (or a free one "
                                                      f"elsewhere) to {what}" + (" — a device hosting a replicant only rides its own replicant's carrier" if whose else "")})
                break
            c = max(options, key=lambda c: (not owner or c.get("replicant_code") == owner, _free(c, bps, stowed_map),
                                            c["device_code"]))   # a carrier of the devices' own replicant first
            room = int(_free(c, bps, stowed_map))
            if room <= 0:
                used.add(c["device_code"])
                continue
            load, codes = codes[:room], codes[room:]
            used.add(c["device_code"])
            dl = {"carrier": c["device_code"], "carrier_loc": c.get("location"), "from": here, "to": dest,
                  "devices": load, "replicant": replicant_hosts.get(c["device_code"]), "mode": carry_mode(c, bps)}
            if (by_tag.get(fleet_tag_of(c) or "") or {}).get("home") == dest:
                dl["stay"] = True   # the carrier's own fleet lives there: it stays home instead of flying back
            if fetched_from:
                # it flies in first and loads where most of them are
                spots = Counter(by_code[x].get("location") for x in load if can_travel(by_code[x]))
                dl.update({"fetch_from": fetched_from, "carrier_loc": (spots.most_common(1)[0][0] if spots else by_code[load[0]].get("location"))})
            deliveries.append(dl)

    for code, home in carriers_away:   # fleet carriers away from home that no delivery needed: home they go
        if code not in used and code not in moves and code not in busy:
            moves[code] = home
            returning.append(code)
            self_moves.append((code, home))

    # pinned devices (`at:<location>`) in their pin's system but somewhere else in it: send them to the spot
    pins = []
    for d in pool:
        code, pin = d["device_code"], pinned_at(d)
        if (pin and star_of(pin) == star_of(d.get("location")) and d.get("location") != pin and code not in busy
                and code not in moves and code not in arrived and not bound_for(d, known_stars) and not working(d)
                and not d.get("stowed_in_device_code") and not d.get("attached_to_device_code")
                and not d.get("location_stale") and str(d.get("status") or "").startswith(("idle", "inactive"))
                and "travel" in (d.get("available_commands") or ["travel"])):
            pins.append((code, pin))

    # placement: relays at a Lagrange point, mining controllers at the belt, survey controllers at the belt (or the inner
    # system without one) — see placement.py. Idle ones are sent there; working ones are only reported.
    from . import placement as pl
    places, misplaced = [], []
    pinned_codes = {c for c, _ in pins}
    geo = dict(geo or {})
    in_pool = {d["device_code"] for d in pool}
    for d in pool + [x for x in devices if placed(x) and x["device_code"] not in in_pool]:   # placed: report only
        code, t, loc = d["device_code"], d.get("device_type"), d.get("location")
        if (not pl.rule_for(t) or not loc or pinned_at(d) or code in moves or code in arrived or code in pinned_codes
                or bound_for(d, known_stars) or d.get("stowed_in_device_code") or d.get("attached_to_device_code")
                or d.get("location_stale") or SPARE in (d.get("tags") or []) or code in cargo):
            continue
        star = star_of(loc)
        if t == "ftl_relay" and star in relaying_stars(devices, {code}):
            continue   # one working relay covers the system: another isn't sent to its L4/L5 point
        g = geo.get(star) or geo.setdefault(star, pl.geography(star, devices, None, (stars.get(star) or {}).get("entry_point")))
        if pl.ok(t, loc, g):
            continue
        dest = pl.target(t, g)
        idle = str(d.get("status") or "").startswith(("idle", "inactive"))
        if (dest and idle and code not in busy and not working(d) and d.get("in_control_range") is not False
                and "travel" in (d.get("available_commands") or ["travel"])):
            places.append((code, dest))
        else:
            why = ("no " + ("L4/L5 Lagrange point" if pl.rule_for(t) == "lagrange" else "belt or planet") + f" known in {star} — scan it"
                   if not dest else f"left alone while it's {d.get('status') or 'busy'}")
            misplaced.append({"code": code, "type": t, "location": loc, "target": dest, "why": f"{pl.WHY[pl.rule_for(t)]}; {why}"})

    if pool and len(stale) > len(pool) / 2:
        # most positions unknown (e.g. mid-surge snapshot): report, but act on nothing this pass
        unmet.append({"star": "", "type": "data", "n": len(stale),
                      "why": f"{len(stale)} of {len(pool)} devices have no current location — skipping this pass"})
        return {"returning": [], "releases": {}, "report": report, "tag_add": {}, "tag_remove": {}, "moves": {}, "prints": [],
                "self_moves": [], "deliveries": [], "arrived": [], "unmet": unmet, "by_code": by_code, "stale": len(stale),
                "assign": {}}
    return {"compact": compact, "pins": pins, "places": places, "misplaced": misplaced, "returning": returning, "releases": {k: sorted(v) for k, v in releases.items()}, "report": report, "tag_add": {k: sorted(v) for k, v in tag_add.items() if v},
            "tag_remove": {k: sorted(v) for k, v in tag_remove.items() if v}, "moves": moves, "prints": prints,
            "self_moves": self_moves, "deliveries": deliveries, "arrived": sorted(set(arrived)), "unmet": unmet,
            "by_code": by_code, "managed": managed_map, "made_spare": sorted(made_spare), "gathering": sorted(gathering),
            "depot": depot, "assign": assign}


ATTACH_CARRIERS = ("surge_plate", "surge_platform", "surge_carrier", "mobile_fleet")


def carry_mode(d: dict, bps: dict) -> str:
    """"attach" for surge plates/platforms/carriers/fleets (devices attach to them and ride along on a surge),
    "stow" for vessels with a hold."""
    t = d.get("device_type") or ""
    if any(k in t for k in ATTACH_CARRIERS):
        return "attach"
    for src in (d, bps.get(t) or {}):
        try:
            if float(src.get("attach_capacity") or 0) > 0 and not float(src.get("stow_capacity") or 0):
                return "attach"
        except (TypeError, ValueError):
            pass
    return "stow"


def _carrier_cap(d: dict, bps: dict) -> float:
    for src in (d, bps.get(d.get("device_type")) or {}):
        for key in ("stow_capacity", "attach_capacity"):
            try:
                v = float(src.get(key))
                if v > 0:
                    return v
            except (TypeError, ValueError):
                pass
    t = d.get("device_type") or ""
    return {"surge_plate": 1, "surge_platform": 4, "surge_carrier": 9, "mobile_fleet": 36}.get(t, 0)


def _free(d: dict, bps: dict, stowed_map: dict) -> float:
    return _carrier_cap(d, bps) - len((stowed_map or {}).get(d["device_code"], []))


def destination(star: str, stars: dict[str, dict]) -> str:
    """Where to send things in a system: its entry point when the catalog knows it, else the star."""
    return (stars.get(star) or {}).get("entry_point") or star


# --- turning a plan into job steps -----------------------------------------------------------------------
def tag_step(code: str, add: list[str] | None = None, remove: list[str] | None = None) -> dict:
    cfg: dict = {}
    if add:
        cfg["add_tags"] = add
    remove = [t for t in remove or [] if t not in (add or [])]   # a tag in both add and remove is refused
    if remove:
        cfg["remove_tags"] = remove
    what = " ".join([f"+{t}" for t in add or []] + [f"−{t}" for t in remove or []])
    return step(f"tag {code} {what}", f"/devices/{code}", {"configuration": cfg}, method="PATCH")


def tag_steps(p: dict) -> list[dict]:
    """Spare marking only — tags for moves are set by the delivery jobs themselves."""
    out = []
    arrived = set(p.get("arrived") or [])
    for code in sorted(set(p["tag_add"]) | set(p["tag_remove"])):
        if code in p["moves"] or code in arrived:   # the arrival step sets those (and the fleet it joins)
            continue
        add, rem = p["tag_add"].get(code, []), p["tag_remove"].get(code, [])
        if add or rem:
            out.append(tag_step(code, add, rem))
    return out


def print_steps(pr: dict) -> list[dict]:
    from .modular import MODULAR_TYPES
    body = {"command": "enqueue_print", "device_type": pr["device_type"], "quantity": pr["n"],
            "tags": pr.get("tags") or [to_tag(pr["star"])] + ([fl.fleet_tag(pr["fleet"])] if pr.get("fleet") else [])}
    if pr["device_type"] in MODULAR_TYPES and pr.get("factory_star") and pr["factory_star"] != pr["star"]:
        body["flatpack"] = True   # bound for another system: printed compacted, so it needn't fold up (hours) to travel
    return [step(f"print {pr['n']}× {pr['device_type']} on {pr['factory']} for {pr['star']}"
                 + (" (flat-packed)" if body.get("flatpack") else ""), f"/devices/{pr['factory']}", body)]


def release_step(d: dict) -> list[dict]:
    """A device leaving for another system is released from its AMI controller first, or the controller keeps
    commanding it from the old system."""
    c = d.get("controller_device_code")
    if not c:
        return []
    return [step(f"{c}: release {d['device_code']}", f"/devices/{c}", {"command": "release", "devices": [d["device_code"]]})]


def is_ami_controller(d: dict) -> bool:
    return "set_directive" in (d.get("available_commands") or []) or (
        "controller" in (d.get("device_type") or "") and "ami" in (d.get("device_type") or ""))


def leave_steps(d: dict, managed: dict[str, list[str]] | None = None, leaving: set[str] | frozenset = frozenset()) -> list[dict]:
    """Before a device leaves its system: free it from its own controller, and if it *is* an AMI controller
    (e.g. a new survey controller put to work where it was printed), stop its directive and let its drones go.
    `leaving`: devices going with it; one whose controller is among them is let go by that controller instead."""
    steps = [] if d.get("controller_device_code") in leaving else release_step(d)
    if is_ami_controller(d):
        code = d["device_code"]
        kids = sorted((managed or {}).get(code) or [])
        if kids:
            steps.append(step(f"{code}: release {len(kids)} device(s) before leaving", f"/devices/{code}",
                              {"command": "release", "devices": kids}))
        if d.get("ami_directive") or kids:
            steps.append(step(f"{code}: clear directive before leaving", f"/devices/{code}", {"command": "clear_directive"}))
    return steps


def self_move_steps(code: str, dest_star: str, stars: dict, d: dict, managed: dict | None = None,
                    gathering: bool = False, join: str | None = None) -> list[dict]:
    """`join`: the fleet tag it gets as it leaves (a spare sent to a stationed fleet), so it counts as that fleet's
    incoming from then on."""
    tags = set(d.get("tags") or [])
    steps = leave_steps(d, managed)
    drop_spare = SPARE in tags and not gathering
    add = ([to_tag(dest_star)] if to_tag(dest_star) not in tags else []) + ([GATHER] if gathering else []) \
        + ([join] if join and join not in tags else [])
    if add or drop_spare:
        steps.append(tag_step(code, add or None, [SPARE] if drop_spare else None))
    dest = destination(dest_star, stars)
    st = step(f"{code} → {dest}", f"/devices/{code}", {"command": "travel", "destination": dest},
              wait=["travel.arrived"], match={"destination": dest_star}, critical=True)
    st["wait_device"] = code
    steps.append(st)
    steps.append(rehome_step(code, d, dest_star, keep_spare=gathering, join=join))
    return steps


def rehome_step(code: str, d: dict, star: str, keep_spare: bool = False, join: str | None = None) -> dict:
    """On arrival: drop the to: tag (and any spare / old home: tag); `join`: the fleet it now belongs to.
    keep_spare: a spare gathered at the depot — it stays spare and belongs to no fleet."""
    if keep_spare:
        old = [t for t in d.get("tags") or [] if t.startswith(("home:", "at:"))]
        return tag_step(code, None, sorted(set(old) | {to_tag(star), GATHER}))
    old = [t for t in d.get("tags") or [] if t.startswith("home:") or t == SPARE
           or (t.startswith("at:") and star_of(t[3:].upper()) != star)]   # a pin for another system no longer applies
    return tag_step(code, [join] if join and join not in (d.get("tags") or []) else None, sorted(set(old) | {to_tag(star)}))


def delivery_steps(dl: dict, by_code: dict, stars: dict, carriers_return: bool, managed: dict | None = None,
                   gathering: set[str] | frozenset = frozenset(), assign: dict[str, str] | None = None,
                   radii: dict[str, float] | None = None, max_cruise_au: float | None = None) -> list[dict]:
    """`gathering`: spares being taken to the depot — they stay spare and join no fleet there.
    `assign`: device -> the fleet tag it gets as it leaves (spares sent to a stationed fleet).
    Devices near the pick-up point fly over and board; devices that can't fly (beacons) and devices far from it (more than
    `max_cruise_au` of cruising) are fetched by the carrier afterwards, nearest first."""
    assign = assign or {}
    carrier, cloc, dest_star = dl["carrier"], dl["carrier_loc"], dl["to"]
    dest = destination(dest_star, stars)
    rep = dl.get("replicant")
    travel_path = f"/replicants/{rep}/travel" if rep else f"/devices/{carrier}"

    def move(to: str, why: str, match: str) -> dict:
        body = {"destination": to} if rep else {"command": "travel", "destination": to}
        st = step(f"{carrier} → {to} ({why})", travel_path, body, wait=["travel.arrived"], match={"destination": match},
                  critical=True)
        st["wait_device"] = carrier
        return st

    steps: list[dict] = []
    if dl.get("fetch_from"):   # the carrier comes over from another system first
        steps.append(move(destination(dl["from"], stars), f"fly in from {star_of(dl['fetch_from'])} to pick up", dl["from"]))
        if cloc and cloc != destination(dl["from"], stars):
            steps.append(move(cloc, "pick-up point", cloc))
    for code in dl["devices"]:
        steps += leave_steps(by_code.get(code, {}) or {"device_code": code}, managed, set(dl["devices"]))
    for code in dl["devices"]:  # mark them as on their way (and not spare any more, unless just being gathered)
        tags = set(by_code.get(code, {}).get("tags") or [])
        add = ([to_tag(dest_star)] if to_tag(dest_star) not in tags else []) + ([GATHER] if code in gathering else []) \
            + ([assign[code]] if assign.get(code) and assign[code] not in tags else [])
        add = add or None
        rem = [SPARE] if SPARE in tags and code not in gathering else None
        if add or rem:
            steps.append(tag_step(code, add, rem))
    first = len(steps)
    grounded = [c for c in dl["devices"] if not can_travel(by_code.get(c, {}) or {}) and by_code.get(c, {}).get("location") != cloc]
    far = [c for c in dl["devices"] if c not in grounded and by_code.get(c, {}).get("location") != cloc
           and fl.far_apart(by_code.get(c, {}).get("location"), cloc, radii, max_cruise_au)]
    fly = [c for c in dl["devices"] if by_code.get(c, {}).get("location") != cloc and c not in grounded and c not in far]
    for code in fly:
        steps.append(step(f"{code} → {cloc} (to board {carrier})", f"/devices/{code}", {"command": "travel", "destination": cloc}))
    for i, code in enumerate(fly):
        w = step(f"wait for {code} at {cloc}", "", None, method="WAIT", wait=["travel.arrived"], match={"destination": cloc},
                 timeout=STEP_TIMEOUT)
        w["wait_device"] = code
        w["seq0_from"] = first + i
        steps.append(w)
    attach = dl.get("mode") == "attach"
    # Boarding and unloading are critical: if a device doesn't get on, the carrier must not fly off without it
    # (and it must not be re-homed to a system it never reached); if it can't get off, it must not ride back.
    # First everyone at (or flown to) the pick-up point, before the carrier goes anywhere …
    for code in [c for c in dl["devices"] if c not in grounded and c not in far]:
        steps.append(fl.board_step(carrier, code, "attach" if attach else "stow"))
    # … then the carrier fetches the rest, nearest first: devices that can't fly (beacons: into its hold) and far ones
    stops: dict[str, list[str]] = {}
    for code in grounded + far:
        stops.setdefault(by_code[code].get("location"), []).append(code)
    cur = cloc
    while stops:
        loc = min(stops, key=lambda x: (fl.cruise_au(cur, x, radii), x))
        codes = stops.pop(loc)
        steps.append(move(loc, "pick up " + ", ".join(codes), loc))
        cur = loc
        for code in codes:
            steps.append(fl.board_step(carrier, code, "stow" if code in grounded or not attach else "attach"))
    steps.append(move(dest, f"deliver {len(dl['devices'])} to {dest_star}", dest_star))
    for code in dl["devices"]:
        if attach:
            st = step(f"{carrier}: detach {code} in {dest_star}", f"/devices/{carrier}", {"command": "detach", "device": code},
                      critical=True)
        else:
            st = step(f"deploy {code} in {dest_star}", f"/devices/{code}", {"command": "deploy"}, wait=["device.deployed"],
                      timeout=SHORT_TIMEOUT, critical=True)
            st["wait_device"] = code
        steps.append(st)
        steps.append(rehome_step(code, by_code.get(code, {}), dest_star, keep_spare=code in gathering, join=assign.get(code)))
    back = dl.get("fetch_from") or cloc
    if carriers_return and back and not dl.get("stay"):
        steps.append(move(back, "return", star_of(back)))
    return steps


def arrived_steps(code: str, d: dict, stowed_in: dict, join: str | None = None) -> list[dict]:
    steps = []
    status = str(d.get("status") or "")
    if d.get("attached_to_device_code") or "attach" in status:
        carrier = d.get("attached_to_device_code") or stowed_in.get(code)
        steps.append(step(f"{carrier}: detach {code}", f"/devices/{carrier}", {"command": "detach", "device": code}, critical=True))
    elif code in stowed_in or status.startswith("stowed"):
        st = step(f"deploy {code}", f"/devices/{code}", {"command": "deploy"}, wait=["device.deployed"], timeout=SHORT_TIMEOUT,
                  critical=True)
        st["wait_device"] = code
        steps.append(st)
    dest = next((t[3:].upper() for t in d.get("tags") or [] if t.startswith("to:")), None) or star_of(d.get("location"))
    gathered = GATHER in (d.get("tags") or [])   # taken to the depot as a spare: it stays spare there
    steps.append(rehome_step(code, d, dest, keep_spare=gathered, join=join))
    if gathered:
        return steps
    pin = pinned_at(d)
    if pin and star_of(pin) == dest and d.get("location") != pin:
        steps.append(pin_step(code, pin))   # delivered to the system: now the exact spot it was ordered for
    return steps


def pin_step(code: str, loc: str, why: str = "its at: pin") -> dict:
    st = step(f"{code} → {loc} ({why})", f"/devices/{code}", {"command": "travel", "destination": loc},
              wait=["travel.arrived"], match={"destination": loc})
    st["wait_device"] = code
    return st


def describe(p: dict) -> list[str]:
    """Plain-language list of what a pass would do."""
    out = []
    for ctrl, codes in sorted((p.get("releases") or {}).items()):
        spare = [c for c in codes if c in set(p.get("made_spare") or [])]
        other = [c for c in codes if c not in spare]
        if spare:
            out.append(f"{ctrl} releases {', '.join(spare)} (no longer in the loadout: spare)")
        if other:
            out.append(f"{ctrl} releases {', '.join(other)} (run from another system)")
    joins = Counter(t for code, tags in p["tag_add"].items() if code not in p["moves"] for t in tags if t.startswith("fleet:"))
    for t, n in sorted(joins.items()):
        out.append(f"{n} device(s) join {t} (they count for that fleet wherever they go)")
    old = sorted(code for code, tags in p["tag_remove"].items() if any(t.startswith("home:") for t in tags))
    if old:
        out.append(f"drop the old home: tag from {len(old)} device(s)")
    for code, tags in sorted(p["tag_add"].items()):
        if code not in p["moves"] and SPARE in tags:
            d = p["by_code"].get(code, {})
            out.append(f"mark {code} ({d.get('device_type')}, {star_of(d.get('location'))}) as spare")
    for code, tags in sorted(p["tag_remove"].items()):
        if code not in p["moves"] and SPARE in tags:
            out.append(f"{code} is needed where it is: remove spare")
    for pr in p["prints"]:
        out.append(f"print {pr['n']}× {pr['device_type']} on {pr['factory']} ({pr['factory_star']}) for {pr['star']}"
                   + (f" — {pr['note']}" if pr.get("note") else ""))
    for code, dest in p["self_moves"]:
        out.append(f"{code} ({p['by_code'][code].get('device_type')}) flies to {dest}")
    for dl in p["deliveries"]:
        how = "attaches" if dl.get("mode") == "attach" else "stows"
        came = f" (flies in from {star_of(dl['fetch_from'])} to pick them up at {dl['carrier_loc']})" if dl.get("fetch_from") else ""
        out.append(f"{dl['carrier']} {how} {', '.join(dl['devices'])} and carries them from {dl['from']} to {dl['to']}{came}")
    if p.get("gathering"):
        out.append(f"gather {len(p['gathering'])} idle spare(s) at the depot {p.get('depot')}: {', '.join(p['gathering'])}")
    for code in p.get("returning") or []:
        d = p["by_code"].get(code, {})
        out.append(f"{code} ({d.get('device_type')}) is away from its fleet's home, in {star_of(d.get('location'))}: "
                   f"send it back to {p['moves'].get(code)}")
    for code in p["arrived"]:
        out.append(f"{code} has arrived: deploy if stowed, clear its to:/spare tags"
                   + (f", then go to {pinned_at(p['by_code'][code])}" if pinned_at(p["by_code"].get(code, {})) else ""))
    for code, loc in p.get("pins") or []:
        out.append(f"{code} ({p['by_code'][code].get('device_type')}) goes to {loc} (pinned there)")
    for code, loc in p.get("places") or []:
        out.append(f"{code} ({p['by_code'][code].get('device_type')}) is at {p['by_code'][code].get('location')}: "
                   f"move it to {loc}")
    for m in p.get("misplaced") or []:
        out.append(f"{m['code']} ({m['type']}) is at {m['location']}: {m['why']}")
    for r in p.get("routes") or []:
        bits = []
        if r.get("adopt"):
            bits.append(f"adopts freighter(s) {', '.join(a['code'] for a in r['adopt'])}")
        if r.get("resend", True):
            bits.append(f"ferries {r['collect']} → {r['deliver']}")
        fleet = len(r.get("fleet") or []) + len(r.get("adopt") or [])
        out.append(f"{r['controller']} {' and '.join(bits)} ({r.get('from_fleet') or r['source']} → {r.get('to_fleet') or r['dest']}, "
                   f"{r['source']} → {r['dest']}, {fleet} freighter(s))")
    for u in p["unmet"]:
        if u["type"] == "data":
            out.append(f"waiting for good data: {u['why']}")
        elif u["type"] == "materials":
            out.append(f"materials from {u.get('fleet') or u['star']}: {u['why']}")
        else:
            out.append(f"can't fill {u['n']}× {u['type']} in {u['star']}: {u['why']}")
    return out


# --- materials: a fleet's home system ferries to the fleet it names ------------------------------------------
def _stock_total(items: dict) -> float:
    return sum(as_amounts(items).values())


def drop_point(star: str, devices: list[dict], inventory: dict[str, dict], stars: dict[str, dict]) -> str:
    """Where materials should land in a destination: its autofactory, else its biggest stockpile, else its entry point."""
    fac = sorted(d.get("location") for d in devices if star_of(d.get("location")) == star and is_factory(d) and d.get("location"))
    if fac:
        return fac[0]
    piles = sorted(((loc, _stock_total(items)) for loc, items in inventory.items() if star_of(loc) == star), key=lambda kv: -kv[1])
    if piles:
        return piles[0][0]
    return destination(star, stars)


def pickup_point(star: str, inventory: dict[str, dict]) -> str | None:
    piles = sorted(((loc, _stock_total(items)) for loc, items in inventory.items() if star_of(loc) == star and _stock_total(items) > 0),
                   key=lambda kv: -kv[1])
    return piles[0][0] if piles else None


FERRY_TAG = "ferry"
IN_SYSTEM_HAULERS = ("transport_drone", "transport_hauler")


def is_freighter(d: dict) -> bool:
    return "freighter" in (d.get("device_type") or "")


def is_transport_controller(d: dict) -> bool:
    t = d.get("device_type") or ""
    return ("transport" in t and "controller" in t) or (
        "transport" in t and "set_directive" in (d.get("available_commands") or []) and not any(h in t for h in IN_SYSTEM_HAULERS))


def material_routes(cfg: dict, devices: list[dict], inventory: dict[str, dict], stars: dict[str, dict], busy: set[str],
                    current: dict[str, dict], managed: dict[str, str] | None = None) -> tuple[list[dict], list[dict]]:
    """([route], [unmet]). One ferry per source system: a fleet whose `materials` names another fleet sends its home
    system's biggest stockpile to that fleet's home system (a fleet set to take materials in is a destination).

    Interstellar hauling is done by cargo freighters (surge drive) under a *ferry controller*: an AMI
    transport controller in the source that is tagged `ferry`, or already runs freighters, or manages
    nothing else. Transport drones and haulers stay on in-system work, so the controller that runs them
    is never given the ferry. Each pass, idle unmanaged freighters in the source are adopted by the
    ferry controller (flying to it first if they are elsewhere in the system).

    current: controller -> its latest directive {"directive", "configuration", "finished"}.
    managed: device -> the controller that manages it.
    """
    cfg = normalize(cfg)
    managed = managed or {}
    ignore = set(cfg["ignore_tags"])
    pos = {k: (v or {}).get("position") or {} for k, v in stars.items()}
    fleets = cfg.get("fleets") or []
    by_code = {d.get("device_code"): d for d in devices}
    routes, unmet = [], []
    pairs: dict[str, tuple[str, dict, dict]] = {}   # source star -> (dest star, from fleet, to fleet)
    for f in sorted(fleets, key=lambda f: f.get("id") or ""):
        if not f.get("materials") or f.get("materials") == "self" or not f.get("home"):
            continue
        to = fl.materials_target(f, fleets)
        if not to or not to.get("home"):
            unmet.append({"star": f.get("home") or "", "fleet": f.get("name"), "type": "materials", "n": 0,
                          "why": "the fleet it sends to is gone or has no home system"})
        elif to["home"] == f["home"]:
            unmet.append({"star": f["home"], "fleet": f.get("name"), "type": "materials", "n": 0,
                          "why": f"{to.get('name')} is in the same system, nothing to ferry"})
        elif f["home"] in pairs:
            unmet.append({"star": f["home"], "fleet": f.get("name"), "type": "materials", "n": 0,
                          "why": f"{f['home']} already ferries to {pairs[f['home']][2].get('name')} (one route per system)"})
        else:
            pairs[f["home"]] = (to["home"], f, to)

    def runs(ctrl: str) -> list[dict]:
        return [by_code[d] for d, c in managed.items() if c == ctrl and d in by_code]

    for src in sorted(pairs):
        dest, from_f, to_f = pairs[src]
        ctrls = [d for d in devices if star_of(d.get("location")) == src and is_transport_controller(d)
                 and not (ignore & set(d.get("tags") or []))]
        if not ctrls:
            unmet.append({"star": src, "type": "materials", "n": 0, "why": f"no AMI transport controller in {src} to ferry to {dest}"})
            continue

        IN_SYSTEM = ("delivery", "shuttle", "consolidate")

        def directive_of(c: dict) -> str | None:
            cur = current.get(c["device_code"]) or {}
            return None if cur.get("finished") else cur.get("directive")

        # never the controller on in-system work (delivery/shuttle/consolidate): a ferry would replace that job.
        # A controller already ferrying keeps its fleet as it is — the game's ferry also uses transport
        # drones riding surge plates in taxi mode, so drones under a ferry controller are fine.
        ferry_ctrls = [c for c in ctrls if directive_of(c) not in IN_SYSTEM or FERRY_TAG in (c.get("tags") or [])]
        if not ferry_ctrls:
            busy_c = ", ".join(f"{c['device_code']} ({directive_of(c)})" for c in ctrls)
            unmet.append({"star": src, "type": "materials", "n": 0,
                          "why": f"every transport controller in {src} is on in-system work [{busy_c}] — add one for ferrying"})
            continue

        def rank(c: dict) -> tuple:
            fleet = runs(c["device_code"])
            return (FERRY_TAG not in (c.get("tags") or []), directive_of(c) != "ferry",
                    not any(is_freighter(x) for x in fleet), c["device_code"])
        ctrl = sorted(ferry_ctrls, key=rank)[0]
        code = ctrl["device_code"]
        fleet = [x for x in runs(code) if is_freighter(x)]
        held = [d for d in devices if is_freighter(d) and star_of(d.get("location")) == src
                and managed.get(d["device_code"]) not in (None, code) and str(d.get("status") or "").startswith("idle")
                and directive_of(by_code.get(managed[d["device_code"]], {"device_code": ""})) in IN_SYSTEM]
        release: dict[str, list[str]] = {}
        for d in held:  # a freighter stuck under the in-system controller moves over to the ferry controller
            release.setdefault(managed[d["device_code"]], []).append(d["device_code"])
        adopt = sorted((d for d in devices if is_freighter(d) and star_of(d.get("location")) == src
                        and str(d.get("status") or "").startswith("idle")
                        and (d["device_code"] not in managed or d in held)
                        and d["device_code"] not in busy and not (ignore & set(d.get("tags") or []))
                        and bound_for(d, set(stars) | {src, dest}) in (None, src)), key=lambda d: d["device_code"])
        if not fleet and not adopt and not runs(code):
            unmet.append({"star": src, "type": "materials", "n": 0, "why": f"no cargo freighter in {src} for {code} to ferry with"})
            continue
        collect = pickup_point(src, inventory)
        if not collect:
            unmet.append({"star": src, "type": "materials", "n": 0, "why": "nothing stockpiled to send yet"})
            continue
        deliver = drop_point(dest, devices, inventory, stars)
        conf = {"collect": collect, "deliver": deliver}
        cur = current.get(code) or {}
        cc = cur.get("configuration") or {}
        same_route = (star_of(cc.get("collect")) == src and star_of(cc.get("deliver")) == dest
                      and _stock_total(inventory.get(cc.get("collect")) or {}) > 0 or cc == conf)
        running = cur.get("directive") == "ferry" and not cur.get("finished") and same_route
        if running:  # keep its current pick-up while that still has stock — re-sending just resets the run
            conf = {"collect": cc.get("collect"), "deliver": cc.get("deliver")}
            collect, deliver = conf["collect"], conf["deliver"]
        if running and not adopt:
            continue  # already ferrying this route with everything it can use
        if code in busy:
            unmet.append({"star": src, "type": "materials", "n": 0, "why": f"{code} is busy with another job"})
            continue
        routes.append({"controller": code, "source": src, "dest": dest, "collect": collect, "deliver": deliver,
                       "from_fleet": from_f.get("name"), "to_fleet": to_f.get("name"), "fleet_id": from_f.get("id"),
                       "distance": _dist(src, dest, pos), "controller_loc": ctrl.get("location"),
                       "adopt": [{"code": d["device_code"], "location": d.get("location")} for d in adopt],
                       "fleet": [x["device_code"] for x in fleet], "resend": not running, "release": release,
                       "tag": FERRY_TAG not in (ctrl.get("tags") or [])})
    for u in unmet:   # name the sending fleet
        if not u.get("fleet") and u["star"] in pairs:
            u["fleet"] = pairs[u["star"]][1].get("name")
    return routes, unmet


def ferry_steps(r: dict) -> list[dict]:
    code, cloc = r["controller"], r.get("controller_loc")
    steps = []
    if r.get("tag"):
        steps.append(tag_step(code, [FERRY_TAG]))  # marks it as the interstellar controller; in-system planners skip it
    away = [a for a in r.get("adopt") or [] if cloc and a["location"] != cloc]
    first = len(steps)
    for a in away:
        steps.append(step(f"{a['code']} → {cloc} (join {code})", f"/devices/{a['code']}", {"command": "travel", "destination": cloc}))
    for i, a in enumerate(away):
        w = step(f"wait for {a['code']} at {cloc}", "", None, method="WAIT", wait=["travel.arrived"], match={"destination": cloc},
                 timeout=STEP_TIMEOUT)
        w["wait_device"] = a["code"]
        w["seq0_from"] = first + i
        steps.append(w)
    for other, codes in sorted((r.get("release") or {}).items()):
        steps.append(step(f"{other}: release {len(codes)} cargo freighter(s) to {code}", f"/devices/{other}",
                          {"command": "release", "devices": codes}))
    if r.get("adopt"):
        codes = [a["code"] for a in r["adopt"]]
        steps.append(step(f"{code}: adopt {len(codes)} cargo freighter(s)", f"/devices/{code}", {"command": "adopt", "devices": codes}))
    if r.get("resend", True):
        steps.append(step(f"{code}: ferry {r['collect']} → {r['deliver']}", f"/devices/{code}",
                          {"command": "set_directive", "directive": "ferry",
                           "configuration": {"collect": r["collect"], "deliver": r["deliver"]}}, critical=True))
    steps.append(step(f"{code}: launch", f"/devices/{code}", {"command": "launch"}))
    return steps

# --- audit: do tags and controller assignments match the loadouts? ---------------------------------------------
def audit(cfg: dict, devices: list[dict], stars: dict[str, dict], p: dict, handoff_drones: set[str] | None = None,
          replicant_hosts: dict | None = None) -> list[dict]:
    """Inconsistencies between tags, controller assignments and the fleets, each with whether the next pass (`p`, the
    current plan) or the arrival hand-off fixes it. Devices of fleets away on a mission and ignored devices are left out,
    like in the planner."""
    cfg = normalize(cfg)
    ignore = set(cfg["ignore_tags"])
    stationed = {fl.fleet_tag(f["id"]): f for f in stationed_fleets(cfg)}
    every = {fl.fleet_tag(f["id"]): f for f in cfg["fleets"]}
    known = set(stars) | {star_of(d.get("location")) for d in devices} | {f["home"] for f in stationed.values()}
    by = {d.get("device_code"): d for d in devices}
    loc_of = {d.get("device_code"): d.get("location") for d in devices}
    releases = {c for codes in (p.get("releases") or {}).values() for c in codes}
    tag_rem = p.get("tag_remove") or {}
    moves, arrived = p.get("moves") or {}, set(p.get("arrived") or [])
    handoff_drones = handoff_drones or set()
    out: list[dict] = []

    def add(d: dict, issue: str, fixed: bool, how: str) -> None:
        out.append({"code": d["device_code"], "type": d.get("device_type"), "location": d.get("location"),
                    "issue": issue, "fixed": fixed, "how": how})

    for d in devices:
        tags = set(d.get("tags") or [])
        code = d.get("device_code")
        ftags = sorted(t for t in tags if t.startswith("fleet:"))
        if ignore & tags or code in (replicant_hosts or {}) or any(t not in stationed for t in ftags if t in every):
            continue
        here = star_of(d.get("location"))
        homes = sorted(t for t in tags if t.startswith("home:"))
        ctrl = d.get("controller_device_code")
        ctrl_dev = by.get(ctrl) or {}
        ferry = "transport" in (ctrl_dev.get("device_type") or "") and (
            (ctrl_dev.get("ami_directive") or {}).get("name") == "ferry" or FERRY_TAG in (ctrl_dev.get("tags") or []))
        taxi = d.get("taxi_mode") == "taxi" or "taxi" in tags
        removed = set(tag_rem.get(code, []))
        if SPARE in tags and ctrl and not ferry and not taxi:
            add(d, f"tagged spare but still run by {ctrl}", code in releases or SPARE in removed,
                "released by its controller" if code in releases else "spare removed (still needed)")
        if SPARE in tags and ftags:
            add(d, f"tagged spare and {', '.join(ftags)}", bool(set(ftags) & removed) or SPARE in removed,
                "fleet tag removed" if set(ftags) & removed else "spare removed")
        if len(ftags) > 1:
            add(d, f"{len(ftags)} fleet tags: {', '.join(ftags)}", False, "remove all but one on its device page")
        unknown = [t for t in ftags if t not in every]
        if unknown:
            add(d, f"{', '.join(unknown)}: no such fleet", False, "remove the tag, or create the fleet")
        bad = sorted(t for t in tags if t.startswith(("to:", "at:")) and not is_place(t[3:]))
        if bad:
            add(d, f"{', '.join(bad)}: not a location (ignored)", False, "remove the tag on its device page")
        if homes:
            add(d, f"old {', '.join(homes)} tag (from before stationed fleets)", bool(set(homes) & removed) or code in moves,
                "converted: it joins the fleet stationed there, or the tag is dropped")
        if ctrl and loc_of.get(ctrl) and star_of(loc_of[ctrl]) != here and here and not ferry and not taxi:
            add(d, f"run by {ctrl} in {star_of(loc_of[ctrl])} from another system", code in releases or code in moves,
                "released" if code in releases else "moved")
        dest = bound_for(d, known)
        if dest and here == dest:
            add(d, f"arrived in {dest} but still tagged to:{dest.lower()}", code in arrived, "arrival step clears it")
        f = stationed.get(ftags[0]) if ftags else None
        if f and here and here != f["home"] and not dest and SPARE not in tags and not ctrl and d.get("taxi_mode") != "taxi" \
                and str(d.get("status") or "").startswith(("idle", "stowed")) and not d.get("location_stale"):
            add(d, f"idle in {here}, away from {f['name']}'s home {f['home']}", code in moves, "sent home")
        cfleet = next((t for t in ctrl_dev.get("tags") or [] if t.startswith("fleet:")), None)
        if ctrl and cfleet and cfleet not in ftags and not (not ftags and cfleet in stationed):
            add(d, f"run by {ctrl}, a controller of {cfleet}, but not in that fleet", False,
                f"release it from {ctrl}, or add it to the fleet")
        if code in handoff_drones:
            add(d, "idle in its system with no controller", True, "adopted by the system's controller")
    star_order = {s: i for i, s in enumerate(sorted(known))}
    return sorted(out, key=lambda x: (x["fixed"], star_order.get(star_of(x["location"]), 0), x["code"]))


# --- prints ordered by this app (kv "loadout_orders") ------------------------------------------------------
# They count as incoming for their fleet until the device shows up. Seen live (2026-10-07): prints removed from a queue by
# hand kept counting for 48 h, so the fleet looked complete and nothing could be queued to fill it.
ORDER_GRACE_S = 600   # a fresh enqueue may not be in the device list yet


def forget_queued(orders: list[dict], factory: str, device_type: str | None = None, n: int | None = None) -> list[dict]:
    """Drop not-yet-printed orders on `factory` (of `device_type`, the newest `n` of them; all when n is None)."""
    drop = [i for i, o in enumerate(orders) if o.get("factory") == factory and not o.get("device_code")
            and (device_type is None or o.get("device_type") == device_type)]
    drop = set(drop[::-1][:n] if n is not None else drop)
    return [o for i, o in enumerate(orders) if i not in drop]


def queued_counts(dev: dict) -> tuple[Counter, int]:
    """What an autofactory still has to print, by type (its queue plus the current print), and how many it holds
    whose type isn't known (waiting for resources with no type shown)."""
    from . import printqueue as pq
    have: Counter = Counter()
    for it in pq.items(dev):
        have[it["device_type"]] += max(1, int(it.get("quantity") or 1))
    st = str(dev.get("status") or "")
    pr = dev.get("printing")
    if isinstance(pr, dict) and pr.get("device_type"):
        have[pr["device_type"]] += 1
        return have, 0
    if st.startswith("printing") and "(" in st:
        have[st[st.find("(") + 1:st.rfind(")")]] += 1
        return have, 0
    return have, 1 if st.startswith(("printing", "waiting")) else 0


def reconcile_orders(orders: list[dict], devices: list[dict], now_ts: float) -> list[dict]:
    """Drop orders their autofactory no longer holds (removed or cleared by hand, canceled, or lost), and printed
    ones whose new device code never came through after 30 minutes."""
    from .automations import _ts
    by = {d.get("device_code"): d for d in devices}
    out, groups = [], defaultdict(list)
    for o in orders:
        at = _ts(o.get("printed_at") or o.get("at"))
        age = now_ts - at.timestamp() if at else 0
        if o.get("device_code") == "?" and age > 1800:
            continue
        if not o.get("device_code") and o.get("factory") in by and age > ORDER_GRACE_S:
            groups[o["factory"]].append(o)
            continue
        out.append(o)
    for f, os_ in groups.items():
        have, unknown = queued_counts(by[f])
        for o in os_:   # oldest first: those print first
            t = o.get("device_type")
            if have[t] > 0:
                have[t] -= 1
            elif unknown > 0:
                unknown -= 1
            else:
                continue
            out.append(o)
    order = {id(o): i for i, o in enumerate(orders)}
    return sorted(out, key=lambda o: order[id(o)])
