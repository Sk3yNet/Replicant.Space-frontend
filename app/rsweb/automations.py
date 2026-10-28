"""Server-side automations: rules the player switches on, and multi-step jobs they launch.

A *rule* watches events (and a periodic tick) and, when its condition holds, creates a *job*:
an ordered list of game commands. Each step may wait for an event (e.g. travel.arrived) before
the next one runs; a step that errors or times out is logged and skipped so one bad target
doesn't strand the drone. Jobs are persisted, so they survive restarts.

Dry run: every rule plans its job as usual but no command is sent; the plan is logged instead.
"""
from __future__ import annotations

import asyncio
import json
import math
from collections import defaultdict
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import time
from typing import Any

from .api import ApiError
from .db import now_iso, row_event
from . import perf
from .shapes import as_amounts  # noqa: F401  (kept for rule authors)
from .ops_rules import OpsRules
from .boot_engine import BootstrapMixin
from .sim_engine import SimMixin
from .stock import StockMixin
from .chase_engine import ChaseMixin

log = logging.getLogger("rsweb.auto")

RESOURCES = ["structural", "conductive", "silicates", "carbon", "volatiles", "rares"]



def _reserved(d: dict) -> bool:
    """Fleet members and devices bound for another system are left out of the in-system work rules."""
    from .ami_schedule import reserved
    return reserved(d)

@dataclass
class Option:
    name: str
    kind: str  # bool | int | float | choice | text
    label: str
    default: Any
    options: list = field(default_factory=list)
    help: str = ""


@dataclass
class Rule:
    id: str
    title: str
    description: str
    options: list[Option] = field(default_factory=list)
    default_on: bool = False   # rules that only watch and alert start switched on


RULES: list[Rule] = [
    Rule("scan_on_arrival", "System scan on arrival",
         "When a replicant's vessel arrives in a system we have no scan for, run a system scan so the "
         "planets and belts are known (other rules and the Systems page use it)."),
    Rule("devices_on_arrival", "Device scan on arrival",
         "When a replicant's vessel arrives in another system, it scans the other devices there at once (GET "
         "/scan/devices; every arrival, since devices come and go). Other players' fixed devices and drones go to the "
         "System page and the Galaxy map, any AMI trade controller to the shops list and any equipment locker to the Lockers page: system scans don't list "
         "shops. Read-only. Without it the traffic poll does the same scan, minutes later.", default_on=True),
    Rule("census_on_arrival", "Stellar census on arrival",
         "When a vessel that can run a stellar census (heaven and cargo vessels) arrives in a system that hasn't had one, "
         "it runs `stellar_census`: the stars around it, explored or not, with positions and entry points. They're added "
         "to the star catalog, which only covers ~70 ly around Sol, so the map, routes and the Unexplored stars list know "
         "them. One action per new system.", default_on=True),
    Rule("ami_schedules", "Run AMI schedules",
         "Master switch for the AMI schedules below: every N minutes each schedule checks its controller(s); "
         "if one is idle (or its directive finished) it adopts idle drones of the right kind at its location, "
         "sets the directive and launches it. The controllers do the actual work."),
    Rule("loadouts", "Keep stationed fleets at their loadout",
         "Every N minutes, for every stationed fleet (Fleets page) that isn't on a mission: devices in its home system with "
         "no fleet join it up to its loadout, extras become spare, spares are sent to fleets that are short, what's still "
         "missing is printed on an autofactory that has the materials and carried there, and fleets' materials are "
         "ferried. Devices with an ignored tag are never touched. Idle drones in the system they belong to that no "
         "controller runs fly to that system's controller of the right kind and are adopted (it launches if it's already "
         "running a directive). Maintenance drones and AMI controllers that arrive home inactive are activated, once.",
         [Option("every_minutes", "int", "Run every (minutes)", 15),
          Option("adopt_arrivals", "bool", "Hand unmanaged drones to their system's controller", True),
          Option("activate_arrivals", "bool", "Set maintenance drones to patrol / activate inactive AMI controllers when they arrive home", True)]),
    Rule("fleet_fill", "Sort out unstationed fleets (fill from spares, drop extras)",
         "Every N minutes, a fleet that isn't stationed (stationed ones are kept full by the loadout pass), isn't on a "
         "mission and is short of its loadout (or its template's) takes "
         "idle spare devices of the missing types, nearest first: they get the fleet's tag, the fleet's carrier tours the "
         "systems they're in to pick them up and flies back, and spares that surge themselves fly to the fleet. Missions "
         "still recruit spares in their gather phase whether this is on or not. Members beyond the loadout (a count "
         "over, or a type its template sets to 0) are dropped as spares the next time they're idle in a system with one "
         "of your hubs (e.g. home for repairs); replicants' vessels, matrices, matrix containers, anything carrying "
         "others, the fleet's relays, beacons and wards and reserved devices (contract:, to:, at:) are never dropped.",
         [Option("every_minutes", "int", "Run every (minutes)", 30)]),
    Rule("consolidate", "Consolidate stockpiles at the autofactory",
         "Every N minutes, in each system with an autofactory: any other stockpile above the minimum is hauled to the "
         "autofactory's location by a free in-system transport controller (not the ferry, not a fleet's, with transport "
         "drones or haulers, its last directive finished) — a `delivery` directive for what's in the pile, then launch. "
         "Piles holding what the autofactory is waiting for go first; piles at an open contract's location are left alone.",
         [Option("every_minutes", "int", "Run every (minutes)", 20),
          Option("min_amount", "int", "Ignore piles smaller than (units)", 100)]),
    Rule("belt_viability", "Belt viability alerts",
         "Tracks every belt you mine: how long survey searches take there and how long a site lasts. Each open site holds "
         "one tracking survey drone, so keeping one miner busy takes about 1 + search time / site life survey drones; "
         "searches get slower every time. When search time passes the threshold below (as % of a site's life), you get an "
         "alert suggesting the nearest belt that is cheaper to search. Figures are on each System page and in Diagnostics.",
         [Option("move_at_percent", "int", "Alert when search time exceeds this % of a site's life", 250)]),
    Rule("contracts", "Work on contracts (in-game events)",
         "Every few minutes, for each open event: if what it asks for is at its location and a replicant is there, fulfill it "
         "(POST /locations/<location>/events/<designation>). Optionally have the system's in-system transport controller "
         "deliver the shortfall from other stockpiles, and send a replicant that's already in the same system.",
         [Option("auto_fulfil", "bool", "Fulfill as soon as it's ready", True),
          Option("auto_deliver", "bool", "Deliver missing materials from elsewhere in the system", False),
          Option("send_replicant", "bool", "Send a replicant already in the same system once the materials are there", False,
                 help="Moves your replicant"),
          Option("every_minutes", "int", "Check every (minutes)", 5)]),
    Rule("reopen_sites", "Re-open resource sites at worked-out belts",
         "Belts never run out, but only open sites can be mined, and a survey drone opens one by searching and then "
         "stays there tracking it. When a mining controller reports exhausted (or a drone is refused because the belt "
         "is exhausted), survey capacity is sent there: the system's AMI survey controller flies to the belt, adopts "
         "idle survey drones and runs belt_search; with no controller, idle survey drones fly there and search. "
         "Tracking drones are never pulled away by other rules.",
         [Option("use_ami", "bool", "Use the system's AMI survey controller when there is one", True),
          Option("drones_per_belt", "int", "Survey drones to keep searching/tracking per belt", 2),
          Option("cooldown_minutes", "int", "Wait before re-trying the same belt (min)", 30)]),
    Rule("salvage_when_depleted", "Salvage when mining sites run out",
         "When every known resource site at a belt is depleted and the system has salvage: an AMI mining controller "
         "in the system is switched to gather_salvage on the biggest salvage (adopting idle drones there) and launched. "
         "With no mining controller in the system, idle mining drones at the worked-out belt fly to the salvage and "
         "mine it. When a salvage runs out, the next one is picked. Back to the belt: a mining controller whose directive "
         "is exhausted at a body (its drones left at used-up salvage), stale-exhausted at a belt that has re-opened, paused, "
         "or done with salvage, while a belt in its system has open sites, gets its drones flown back, re-adopted, and its "
         "directive re-set and launched. A mining controller parked away from any belt (e.g. at a Lagrange point after a "
         "delivery) in a system with a belt goes to the belt too, taking its drones and the system's idle unassigned "
         "mining drones, which it adopts. In a scanned system with no belt, miners go straight to salvage; with no salvage "
         "either, you get an alert (once a day).",
         [Option("use_ami", "bool", "Use the system's AMI mining controller when there is one", True),
          Option("recall", "bool", "AMI: recall its drones when the salvage is used up", True),
          Option("back_to_belt", "bool", "Bring controllers and drones back to a belt once it has open sites again", True),
          Option("back_cooldown_minutes", "int", "Back to the belt: wait before re-trying the same controller (min)", 30),
          Option("rest_when_dry", "bool", "Rest a controller whose belt has no open sites (clear its directive, drones "
                 "stay put) so it doesn't overheat; relaunch it when sites open", True,
                 help="Seen live: a controller left on an exhausted directive with no open sites logs ami_overheat "
                      "and loses about 9 % capacity an hour"),
          Option("drones_per_salvage", "int", "Drones per salvage when there's no AMI (0 = all on one)", 3),
          Option("cooldown_minutes", "int", "Wait before re-trying the same device (min)", 10)]),
    Rule("auto_survey", "Auto-survey new systems",
         "When a vessel arrives in a system with un-surveyed bodies: if an AMI survey controller is there or carried, "
         "deploy it and the survey drones, have it adopt them and run survey_system. Otherwise the drones are "
         "driven body by body (scan planets, search belts). Already-surveyed bodies are skipped.",
         [Option("use_ami", "bool", "Use an AMI survey controller when one is available", True),
          Option("use_idle", "bool", "Also use idle survey drones already in the system", True),
          Option("include_moons", "bool", "Include moons", False, help="Gas giants can have dozens of moons"),
          Option("include_belts", "bool", "Search asteroid belts", True),
          Option("max_targets", "int", "Max bodies per system", 20),
          Option("return_and_stow", "bool", "Return to the vessel and stow when finished", True)]),
    Rule("visitor_alerts", "Visitor alerts (beacon traffic)",
         "Every few minutes the app reads each deployed FTL beacon's traffic log (who arrived and left the system). "
         "When another replicant's devices arrive in one of your systems you get a notification. The full log is on "
         "the Traffic page.",
         [Option("include_npcs", "bool", "Also alert on NPC replicants", True),
          Option("repeat_hours", "int", "Don't repeat an alert for the same replicant and system within (hours)", 6)],
         default_on=True),
    Rule("civ_beacons", "Beacons at civilization event sites",
         "Civilizations only send their follow-up requests (the daily messages about new events) when an FTL beacon is "
         "deployed AT the planet or moon of one of their events — a beacon in the Kuiper belt or Oort cloud doesn't count. "
         "As soon as a survey discovers an event (and every 10 minutes for older ones) this puts a beacon at that body, so "
         "it's already there when you complete the event. Beacons can't fly, so, cheapest first: a vessel in the system "
         "that carries one flies there and deploys it; a vessel picks up a loose beacon (tagged civ or spare — e.g. one "
         "just printed) and takes it there; otherwise one is printed on the system's autofactory (tagged civ) and fetched "
         "on a later pass. Never printed on a vessel (Place beacon on the Traffic page can have a replicant print one).",
         [Option("print_beacons", "bool", "Print a beacon on the system's autofactory when none is free", True,
                 help="60 structural + 20 conductive each"),
          Option("spare_redundant", "bool", "Mark beacons a system doesn't need as spare", True,
                 help="Once a system has a beacon at a civilization's body, its other beacons (e.g. Kuiper/Oort) are spare "
                      "(beacons with a destination, fleet or contract never are); "
                      "a vessel can then take one to a civilization's body, and loose ones are gathered at the spare depot")]),
    Rule("asteroid_defence", "Asteroid defense",
         "Tracks incoming asteroids (from system.object_detected and by reading the object every 15 minutes): hours to "
         "impact, likelihood, required strength and progress, and estimates how many propulsors it takes to divert it in "
         "time. Alerts when the picture changes. It can activate idle propulsors at the asteroid, send idle ones in the "
         "system there, and print the shortfall on the system's autofactory (sent straight to the asteroid when it can fly).",
         [Option("activate", "bool", "Activate idle propulsors at the asteroid", True),
          Option("send_idle", "bool", "Send idle propulsors in the system to the asteroid", True),
          Option("print_missing", "bool", "Print the shortfall on the system's autofactory", False, help="Spends resources"),
          Option("max_prints", "int", "Most propulsors to print per asteroid at a time", 6),
          Option("diverted_pct", "float", "Diverted enough at an impact likelihood of (%)", 1.0,
                 help="The likelihood never reaches 0 %. At or below this, more thrust is wasted: the propulsors are recalled "
                      "before the swing-past"),
          Option("leave_minutes", "int", "Propulsors still pushing leave this long before the impact (minutes)", 45,
                 help="Everything else leaves at the hour; under 30 is raised to 30"),
          Option("recheck_minutes", "int", "Read an asteroid with propulsors on it every (minutes)", 5,
                 help="Others are read every 15 minutes")],
         default_on=True),
    Rule("asteroid_mining", "Asteroid mining",
         "Game v3.6.0: incoming asteroids are resource sites, mined only by devices with the fracture feature (mining "
         "rigs). Slots are shared with other players: small 1, medium 2, large 5. Rigs at an asteroid mine one resource "
         "at a time, your priority first (yields taper, so the first one is the most likely to be finished), then the "
         "rest until it's dust. Idle rigs that can fly go to an asteroid in their system with a free slot. Rocks you "
         "launched (Terraforming) keep some of every resource, so the impact still has its terraform effect. The rigs "
         "are pulled out an hour before the ETA (also when it's diverted or off target), and logistics hauls the "
         "stockpile out while they work. An idle survey drone in the system stays at a rock being mined and scans it for exact amounts.",
         [Option("priority", "text", "Resource priority", "rares, conductive, volatiles",
                 help="Mined in this order, comma separated (structural, conductive, silicates, carbon, volatiles, rares); "
                      "the ones left out come after, on asteroids that aren't yours"),
          Option("launched_resources", "text", "Mine on rocks you launched", "",
                 help="Only these on your own launched rocks (empty: the priority list)"),
          Option("keep_pct", "float", "Leave on rocks you launched (%)", 5.0,
                 help="Of every resource, so the rock isn't mined dry: a mined-out rock has no terraform effect"),
          Option("send_idle", "bool", "Send idle rigs in the system to an asteroid", True),
          Option("min_hours", "float", "Hours of mining a trip must leave", 1.0,
                 help="After the travel time (the game's travel preview) and before the rigs leave again, an hour before the ETA"),
          Option("scan_rocks", "bool", "Keep an idle survey drone at rocks being mined", True,
                 help="A scan gives exact amounts (the location only shows % left); the drone stays until the cutoff"),
          Option("scan_minutes", "int", "Scan a rock every (minutes)", 30)]),
    Rule("maintenance", "Keep maintenance drones patrolling",
         "A maintenance drone only repairs on the `patrol` directive (it then fixes the most worn device in its system, one "
         "after another). Every N minutes, any maintenance drone sitting in a system without patrol gets it (fleet members "
         "and drones bound for another system are left alone). A system with a device below the critical level and no "
         "maintenance drone raises an alert once a day, or gets one printed on its autofactory.",
         [Option("every_minutes", "int", "Run every (minutes)", 15),
          Option("threshold", "int", "Count a device as worn below (%)", 80),
          Option("critical", "int", "Act when a system's worst device is below (%)", 60),
          Option("print_missing", "bool", "Print a maintenance drone for a system that has none", False, help="Spends resources"),
          Option("hub_upkeep", "bool", "Logistics stocks hub upkeep at each hub's location", True,
                 help="The game draws a hub's upkeep from the stockpile at its own spot: enough for 100 % plus one 20 % step")],
         default_on=True),
    Rule("restart_idle_miners", "Restart idle mining drones",
         "Every minute, any mining drone sitting idle at a belt gets back to work. Drones an AMI mining controller "
         "already manages are left alone; with a controller at the same location the drone is handed to it "
         "(adopt, and launch if the controller is idle) instead of being told to mine directly.",
         [Option("prefer_ami", "bool", "Hand idle drones to a mining controller at the same location", True),
          Option("resource", "choice", "Resource (when mining directly)", "same", ["same"] + RESOURCES,
                 help="'same' = whatever it mined last (falls back to structural)"),
          Option("cooldown_minutes", "int", "Wait between retries for the same drone (min)", 10),
          Option("focus_sites", "bool", "Mine one site at a time (drones without a controller)", False,
                 help="Each restarted drone is sent to the open site with the least of its resource left, so the "
                      "drones empty one site after another and its survey drone searches again while the other sites "
                      "keep them busy, instead of every site running out at once. Controllers pick their own sites.")]),
]
RULES_BY_ID = {r.id: r for r in RULES}

# heavy passes run every n-th tick, each on its own tick (stage: (n, offset)): each held the server 3–7 s of CPU every
# minute (snapshot 2026-10-26, 1.271.1); their own cooldowns are in minutes, so a pass every 3 min loses nothing
SPREAD = {"rule_salvage": (3, 0), "rule_restart_idle_miners": (3, 1), "rule_reopen_sites": (3, 2)}
STEP_TIMEOUT = 3600          # waiting for travel/scan to finish
TRIP_GRACE_S = 1800          # past a trip's ETA, a wait (or a refused step) keeps going this long before giving up
NO_ETA_EXTRA_S = 3600        # still moving and no ETA known: this much longer, once
BUSY_RE = re.compile(r"while (the )?(replicant|device|carrier|vessel)? ?is (still )?(travel|cruis|surg|in motion)|"
                     r"already in motion|is (currently )?(travel|surg|cruis)|"
                     r"not in a star system|in transit", re.I)   # mid-trip between systems (seen live 2026-10-11, Surveyor 3's gather)
# "No conductive remaining at any active site" (live 2026-10-23, Delta Force): the belt's open sites lack that resource.
# Nothing to fix by hand: the step is skipped quietly and doesn't count toward stopping the job.
NO_SITE_RE = re.compile(r"\bno \w+ remaining at any active site", re.I)
BUSY_RETRY_S, BUSY_TRIES = 300, 36   # retry such a refusal every 5 min, for up to 3 h
SHORT_TIMEOUT = 120          # waiting for deploy/stow confirmation
MAX_LOG = 300


def severity(entry: dict) -> str:
    """error / warning / info for the Automations log toggles. Entries are logged as info or alert; an alert is an
    error when something stopped or failed, otherwise a warning (skipped steps, warnings, nothing to do)."""
    level = str(entry.get("level") or "info")
    if level in ("error", "warning"):
        return level
    if level != "alert":
        return "info"
    t = str(entry.get("text") or "").lower()
    if t.startswith("stopped:") or " failed" in t or t.startswith("failed"):
        return "error"
    return "warning"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class EngineLock:
    """The engine's lock, re-entrant per task and self-describing.

    Seen live (1.12.2, 2026-10-04): a fleet control (Stop/Resume/End on a stalled mission) took the lock and then
    called cancel(), which took it again. asyncio.Lock isn't re-entrant, so the request hung holding it and every
    tick, every incoming event and every locked page waited behind it for ~43 h with nothing in the log.
    The same task can now nest `async with lock`, and `holder` / `held_for()` tell the watchdog who has it."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._owner: asyncio.Task | None = None
        self._depth = 0
        self.holder: str | None = None
        self.since: float | None = None

    def locked(self) -> bool:
        return self._lock.locked()

    def held_for(self) -> float:
        return time.monotonic() - self.since if self.since is not None and self._lock.locked() else 0.0

    async def acquire(self) -> bool:
        me = asyncio.current_task()
        if me is not None and self._owner is me:
            self._depth += 1
            return True
        t0 = time.monotonic()
        await self._lock.acquire()
        if (waited := (time.monotonic() - t0) * 1000) > 1:
            perf.note_lock_wait(waited)   # counted only inside a page request
        self._owner, self._depth = me, 1
        self.holder = me.get_name() if me is not None else "?"
        self.since = time.monotonic()
        return True

    def release(self) -> None:
        self._depth -= 1
        if self._depth <= 0:
            if self.since is not None:
                perf.note_hold(self.holder or "?", (time.monotonic() - self.since) * 1000)
            self._owner, self._depth, self.holder, self.since = None, 0, None, None
            self._lock.release()

    async def __aenter__(self) -> "EngineLock":
        await self.acquire()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.release()


LOCK_ALERT_SECONDS = 600     # the watchdog alerts once the engine lock has been held this long


def ownership_hint(err: str | None) -> str:
    from .modular import ownership_hint as hint   # modular imports this module
    return hint(err)


def _already_compact(err: str | None) -> bool:
    """A compact refused because the device is already compacted, or already compacting."""
    e = (err or "").lower()
    return ("compact" in e and ("already" in e or "is compacted" in e or "is compacting" in e)
            and not re.search(r"\bnot compact", e))


def _already_unfurled(err: str | None) -> bool:
    """An unfurl refused because the device isn't folded up (already unfurled)."""
    e = (err or "").lower()
    return ("unfurl" in e and "already" in e) or bool(re.search(r"\bnot compact", e))


def _ts(v: str | None) -> datetime | None:
    if not v or not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _trip_eta(resp: dict) -> datetime | None:
    """When a command's answer says it'll be done: arrives_at / final_arrives_at / completes_at, else the route's
    total time (total_time_seconds, time_seconds, or the legs' time_seconds summed) from now."""
    for src in (resp, resp.get("travel") if isinstance(resp.get("travel"), dict) else {}):
        for k in ("final_arrives_at", "arrives_at", "arrival_time", "completes_at"):
            t = _ts(src.get(k))
            if t:
                return t
    secs = resp.get("total_time_seconds") or resp.get("time_seconds") or resp.get("eta_seconds")
    route = resp.get("route")
    if not secs and isinstance(route, list):
        secs = sum(float((x or {}).get("time_seconds") or 0) for x in route if isinstance(x, dict))
    elif not secs and isinstance(route, dict):
        secs = route.get("time_seconds")
    try:
        secs = float(secs or 0)
    except (TypeError, ValueError):
        secs = 0
    return _now() + timedelta(seconds=secs) if secs > 0 else None


def eta_text(t: datetime) -> str:
    """'21:36 UTC (in 4 h 12 min)' — an expected completion for the logs. The game's times carry its own offset
    (-04:00, live 2026-10-20): shown in UTC."""
    t = t.astimezone(timezone.utc)
    mins = max(0, int((t - _now()).total_seconds() // 60))
    span = f"{mins // 60} h {mins % 60} min" if mins >= 60 else f"{mins} min"
    day = "" if t.date() == _now().date() else t.strftime(" %b %d")
    return f"{t.strftime('%H:%M')}{day} UTC (in {span})"


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def step(desc: str, path: str, body: dict | None, wait: list[str] | None = None, timeout: int = STEP_TIMEOUT,
         method: str = "POST", critical: bool = False, match: dict | None = None) -> dict:
    """One command. `wait`: event names that complete it; `match`: payload values the event must carry
    (a key missing from the payload is not held against it)."""
    return {"desc": desc, "method": method, "path": path, "body": body, "wait": wait or [], "timeout": timeout,
            "critical": critical, "match": match or {}, "status": "pending", "started_at": None, "seq0": 0,
            "error": None, "tries": 0}


def job_cargo(job: dict, devices: list[dict] | None = None) -> dict:
    """What a job's carriers carry or will pick up, read from its steps (stow / attach / collect_resources) and, for a
    loadouts delivery, from meta devices. devices: [{code, type, state}] with state "to pick up", "aboard" or
    "dropped"; materials: {resource: amount} with loaded True once the collect step went through."""
    types = {d.get("device_code"): d.get("device_type") for d in devices or []}
    out: dict[str, str] = {}
    materials: dict[str, float] = {}
    loaded = False
    for st in job.get("steps") or []:
        b = st.get("body") or {}
        done = st.get("status") == "done"
        cmd = b.get("command")
        code = None
        if cmd == "stow" and b.get("target"):
            code = str(st.get("path") or "").rstrip("/").rsplit("/", 1)[-1]
        elif cmd == "attach" and b.get("device"):
            code = b["device"]
        elif cmd == "detach" and b.get("device") in out:
            if done:
                out[b["device"]] = "dropped"
            continue
        elif cmd == "collect_resources":
            for r, n in (b.get("resources") or {}).items():
                materials[r] = materials.get(r, 0) + (n or 0)
            loaded = loaded or done
            continue
        if code and code != job.get("device"):
            if out.get(code) != "aboard":
                out[code] = "aboard" if done else "to pick up"
    if job.get("rule") == "loadouts":
        for code in (job.get("meta") or {}).get("devices") or []:
            if code != job.get("device"):
                out.setdefault(code, "to pick up")
    return {"devices": [{"code": c, "type": types.get(c), "state": s} for c, s in out.items()],
            "materials": materials, "loaded": loaded}


def event_matches(st: dict, ev: dict, device: str | None) -> bool:
    if ev.get("event") not in st.get("wait", []):
        return False
    if device and ev.get("device_code") != device:
        return False
    p = ev.get("payload") or {}
    if ev.get("event") == "travel.arrived" and p.get("recalling"):
        return False   # a recalled device coming home, not the trip this step waits for
    if p.get("from_digest") and st.get("match"):
        return False   # an AMI digest only names the event, not where: a step that needs a place waits (or times out)
    for k, v in (st.get("match") or {}).items():
        have = p.get(k, ev.get("location") if k in ("destination", "scan_target", "search_target") else None)
        if have is None:
            continue
        have, want = str(have).upper(), str(v).upper()
        if k == "destination" and have != want and "-" not in have and str(ev.get("location") or "").upper() == want:
            # a surge's arrival names the star; the event's location is where it landed (live 2026-10-28: 15DFB014
            # landed at BELNULEON-10-L4 with destination "BELNULEON", and the trip waited out its hour)
            continue
        if k == "destination" and "-" not in want:
            # traveling "to a star" lands at one of its locations (entry point, L4 …)
            if have != want and not have.startswith(want + "-"):
                return False
        elif have != want:
            return False
    return True


# --- chained commands ("then, on arrival: …") -------------------------------------------------
# name -> (label, argument key or None, argument kind, events that mean it's done, timeout)
CHAIN_COMMANDS: dict[str, tuple] = {
    "deploy":        ("deploy / launch from carrier", None, None, ["device.deployed"], SHORT_TIMEOUT),
    "start_mining":  ("start mining", "resource_type", "resource", [], 0),
    "retarget":      ("switch mined resource", "resource_type", "resource", [], 0),
    "scan":          ("survey scan here", None, None, ["scan.completed"], 3600),
    "search":        ("search belt here", None, None, ["search.completed"], 3600),
    "system_scan":   ("system scan (replicant)", None, None, [], 0),
    "travel":        ("travel to", "destination", "location", ["travel.arrived"], 3600),
    "stow":          ("stow into", "target", "device", ["device.stowed"], 120),
    "attach":        ("attach to", "device", "device", [], 0),
    "launch":        ("AMI: launch", None, None, [], 0),
    "withdraw":      ("AMI: withdraw", None, None, [], 0),
    "activate":      ("activate", None, None, [], 0),
    "unfurl":        ("unfurl", None, None, ["device.unfurled"], 3600),
    "recall":        ("recall", None, None, [], 0),
}


def chain_step(device: str, command: str, arg: str | None, replicant: str | None = None) -> dict:
    """One follow-up step. Raises ValueError for a missing required argument."""
    if command not in CHAIN_COMMANDS:
        raise ValueError(f"unknown follow-up command {command}")
    label, key, kind, wait, timeout = CHAIN_COMMANDS[command]
    arg = (arg or "").strip()
    if key and not arg and command not in ("stow",):
        raise ValueError(f"'{label}' needs a {kind}")
    if command == "system_scan":
        if not replicant:
            raise ValueError("system scan needs a replicant")
        return step(f"system scan by {replicant}", f"/replicants/{replicant}/scan", {}, critical=False)
    body: dict = {"command": command}
    if key and arg:
        body[key] = arg if kind == "resource" else arg.upper()
    st = step(f"{label} {arg}".strip() + f" ({device})", f"/devices/{device}", body, wait=wait, timeout=timeout or STEP_TIMEOUT)
    st["wait_device"] = device
    if command == "travel":
        st["match"] = {"destination": arg.upper()}
        st["allow_far"] = True   # you asked for this trip yourself
    return st


def survey_targets(scan: dict, surveyed: dict, include_moons: bool, include_belts: bool, max_targets: int) -> list[dict]:
    """Bodies to visit, in orbital order: [{"target": code, "action": "scan"|"search"}]."""
    items: list[tuple[float, str, str]] = []
    for p in scan.get("planets") or []:
        des = p.get("designation")
        if not des:
            continue
        au = float(p.get("orbital_distance_au") or 0)
        items.append((au, des, "scan"))
        if include_moons:
            for i in range(1, int(p.get("moon_count") or 0) + 1):
                items.append((au + i * 1e-6, f"{des}-{i}", "scan"))
    if include_belts:
        for b in ((scan.get("asteroid_belt") or {}).get("belts")) or []:
            if b.get("designation"):
                items.append((float(b.get("inner_radius_au") or 0), b["designation"], "search"))
    items.sort()
    out = [{"target": t, "action": a} for _, t, a in items if t not in surveyed]
    return out[:max(0, max_targets)]


class AutomationEngine(OpsRules, BootstrapMixin, SimMixin, StockMixin, ChaseMixin):
    def __init__(self, db, api, hub, worker):
        self.db, self.api, self.hub, self.worker = db, api, hub, worker
        self.lock = EngineLock()
        self.task: asyncio.Task | None = None
        self.stage: str | None = None          # what the current tick is doing (for the watchdog / snapshot)
        self._lock_alerted = False
        self.stage_errors: dict[str, str] = {}  # stage -> its last error (announced once)
        self.tick_n = 0   # ticks run since start (SPREAD)

    # --- settings & persistence ---------------------------------------------------------
    async def settings(self) -> dict:
        s = await self.db.kv_get("automation_settings", None) or {}
        s.setdefault("dry_run", False)
        rules = s.setdefault("rules", {})
        for r in RULES:
            cfg = rules.setdefault(r.id, {})
            cfg.setdefault("enabled", r.default_on)
            for o in r.options:
                cfg.setdefault(o.name, o.default)
        return s

    async def save_settings(self, s: dict) -> None:
        await self.db.kv_set("automation_settings", s)

    async def rule_cfg(self, rule_id: str) -> dict | None:
        s = await self.settings()
        cfg = s["rules"].get(rule_id) or {}
        return cfg if cfg.get("enabled") else None

    async def jobs(self) -> list[dict]:
        return await self.db.kv_get("automation_jobs", []) or []

    async def save_jobs(self, jobs: list[dict]) -> None:
        # keep active jobs + the 50 most recent finished ones
        active = [j for j in jobs if j["status"] in ("running", "waiting")]
        done = [j for j in jobs if j["status"] not in ("running", "waiting")][-50:]
        await self.db.kv_set("automation_jobs", done + active)

    async def log(self, rule: str, text: str, level: str = "info", notify: bool = False) -> None:
        entries = await self.db.kv_get("automation_log", []) or []
        from .version import RUN_ID, VERSION
        entries.append({"at": now_iso(), "rule": rule, "level": level, "text": text, "v": VERSION, "run": RUN_ID})
        await self.db.kv_set("automation_log", entries[-MAX_LOG:])
        log.info("[%s] %s", rule, text)
        if notify:
            title = f"Automation: {text}"
            nlevel = severity({"level": level, "text": text})   # error / warning / info, like the log
            cur = await self.db.execute(
                "INSERT INTO notifications(event_id, level, title, body, link, created_at) VALUES(?,?,?,?,?,?)",
                (None, nlevel, title, None, "/automations", now_iso()))
            self.hub.publish("notify", {"id": cur.lastrowid, "level": nlevel,
                                        "title": title, "link": "/automations"})

    # --- lifecycle ------------------------------------------------------------------------
    def start(self) -> None:
        self.task = asyncio.create_task(self._tick_loop(), name="automations")
        self.lag_task = asyncio.create_task(perf.watch_loop(lambda: self.stage), name="loop-lag")
        perf.start_sampler(lambda: self.stage)

    async def stop(self) -> None:
        for t in (self.task, getattr(self, "lag_task", None)):
            if t:
                t.cancel()
                await asyncio.gather(t, return_exceptions=True)

    async def _tick_loop(self) -> None:
        await asyncio.sleep(15)
        while True:
            try:
                await self.watchdog()
                if self.lock.locked() and self.lock.held_for() > LOCK_ALERT_SECONDS:
                    await asyncio.sleep(60)   # still stuck: don't queue up another tick behind it
                    continue
                t_tick, c_tick = time.perf_counter(), time.thread_time()
                try:
                    await self.tick()
                finally:
                    perf.note_stage("(whole tick)", (time.perf_counter() - t_tick) * 1000, (time.thread_time() - c_tick) * 1000)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("automation tick failed")
                await self.note_tick(error=f"{type(e).__name__}: {e} (during {self.stage or '?'})")
            await asyncio.sleep(60)

    async def watchdog(self) -> None:
        """Alert (once per stall) when the engine lock has been held for over LOCK_ALERT_SECONDS."""
        held = self.lock.held_for()
        if held > LOCK_ALERT_SECONDS:
            if not self._lock_alerted:
                self._lock_alerted = True
                await self.log("engine", f"automations stalled: the engine has been busy for {held / 60:.0f} min "
                                         f"(held by {self.lock.holder or '?'}, tick stage: {self.stage or '-'}) — "
                                         "nothing runs until it's free; restart the app if this persists", "alert", notify=True)
        elif self._lock_alerted and not self.lock.locked():
            self._lock_alerted = False
            await self.log("engine", "automations running again", notify=True)

    async def note_tick(self, started: str | None = None, error: str | None = None) -> None:
        """Heartbeat for the snapshot: when the last tick started / finished, and the last tick error."""
        hb = await self.db.kv_get("engine_tick", {}) or {}
        if started:
            hb["started_at"] = started
        elif error:
            hb["error"], hb["error_at"] = error, now_iso()
        else:
            hb["finished_at"] = now_iso()
        await self.db.kv_set("engine_tick", hb)

    def engine_status(self) -> dict:
        return {"lock_held": self.lock.locked(), "held_by": self.lock.holder, "held_seconds": round(self.lock.held_for()),
                "stage": self.stage}

    # --- sending commands -------------------------------------------------------------------
    async def send(self, method: str, path: str, body: Any, label: str) -> tuple[bool, Any, str | None]:
        from .fleets import RENAMES_KV, with_renamed_tags
        body = with_renamed_tags(method, path, body, await self.db.kv_get(RENAMES_KV, {}) or {})
        status, resp, err = 200, None, None
        try:
            resp = await self.api.request(method, path, json_body=body, background=True)
            if self.worker:
                await self.worker.timers_from_response(path, resp, label)
        except ApiError as e:
            status, err, resp = e.status, e.message, e.body
        await self.db.execute(
            "INSERT INTO actions(at, user, method, path, body, status, response) VALUES(?,?,?,?,?,?,?)",
            (now_iso(), "automation", method, path, json.dumps(body) if body is not None else None, status,
             json.dumps(resp, default=str)[:20000] if resp is not None else None))
        if err is None and method == "PATCH" and isinstance(resp, dict) and isinstance(resp.get("tags"), list):
            await self.remember_tags(path.rstrip("/").split("/")[-1], resp["tags"])
        cmd = (body or {}).get("command") if isinstance(body, dict) else None
        if err is None and cmd in ("launch", "withdraw") and isinstance(resp, dict):
            # docs (AMI): launch answers assigned_devices {already_deployed, deployed, failed, skipped}; withdraw failed
            ad = resp.get("assigned_devices") if isinstance(resp.get("assigned_devices"), dict) else resp
            failed, skipped = ad.get("failed") or [], ad.get("skipped") or []
            if failed or skipped:
                await self.log("ami", f"{label}: {cmd} left out {len(failed)} failed / {len(skipped)} skipped device(s): "
                                      f"{json.dumps(failed or skipped, default=str)[:300]}", "warning")
        return err is None, resp, err

    async def remember_tags(self, code: str, tags: list[str]) -> None:
        """Update the cached device list right away, so the next pass doesn't redo tag changes before the next sync."""
        devices = await self.db.kv_get("devices", []) or []
        for d in devices:
            if d.get("device_code") == code:
                d["tags"] = list(tags)
                await self.db.kv_set("devices", devices)
                return

    # --- jobs --------------------------------------------------------------------------------------
    async def cooperation(self) -> dict:
        """The account's replicant cooperation: {"shared": bool, "public": replicants whose cohort_permission is public}."""
        acct = await self.db.kv_get("account", {}) or {}
        reps = await self.db.kv_get("replicants", {}) or {}
        return {"shared": acct.get("replicant_cooperation") == "shared",
                "public": {c for c, r in reps.items() if isinstance(r, dict) and r.get("cohort_permission") == "public"}}

    async def create_job(self, rule: str, title: str, device: str | None, steps: list[dict], meta: dict | None = None,
                         force: bool = False) -> dict | None:
        """force=True: a job the player asked for directly (e.g. a command chain) — dry run doesn't apply."""
        s = await self.settings()
        if not steps:
            return None
        if s["dry_run"] and not force:
            plan = "; ".join(st["desc"] for st in steps)
            await self.log(rule, f"[dry run] {title}: {plan}")
            return None
        if not force and await self.held():
            await self.log(rule, f"[paused for the season change] not started: {title}")
            return None
        from .modular import prepare_moves
        from .shapes import normalize_blueprints
        bps = {b["device_type"]: b for b in normalize_blueprints(await self.db.kv_get("blueprints", []))}
        # drones tracking a site deactivate before moving; large (modular) devices compact (modular.py)
        steps = prepare_moves(steps, await self.devices(), bps, await self.cooperation())
        jobs = await self.jobs()
        job = {"id": f"{rule}-{int(_now().timestamp() * 1000)}-{len(jobs)}", "rule": rule, "title": title,
               "device": device, "steps": steps, "idx": 0, "status": "running", "created_at": now_iso(),
               "meta": meta or {}, "forced": force}
        from .version import RUN_ID, VERSION
        job["v"], job["run"] = VERSION, RUN_ID
        jobs.append(job)
        await self.save_jobs(jobs)
        await self.log(rule, f"started: {title} ({len(steps)} steps)")
        await self._advance(job["id"])
        return job

    async def _update(self, job: dict) -> None:
        jobs = await self.jobs()
        for i, j in enumerate(jobs):
            if j["id"] == job["id"]:
                jobs[i] = job
        await self.save_jobs(jobs)
        self.hub.publish("state", "automation")

    async def _get(self, job_id: str) -> dict | None:
        return next((j for j in await self.jobs() if j["id"] == job_id), None)

    async def _max_seq(self) -> int:
        row = await self.db.fetchone("SELECT MAX(seq) AS s FROM events")
        return int(row["s"] or 0) if row else 0

    async def _already_happened(self, st: dict, device: str | None) -> bool:
        """Did the awaited event arrive while the command was being sent (before we started waiting)?"""
        if not st["wait"]:
            return False
        marks = ",".join("?" * len(st["wait"]))
        rows = await self.db.fetchall(f"SELECT * FROM events WHERE event IN ({marks}) AND seq > ? ORDER BY seq",
                                      [*st["wait"], st.get("seq0", 0)])
        for r in rows:
            ev = dict(r)
            try:
                ev["payload"] = json.loads(r["payload"] or "{}")
            except ValueError:
                ev["payload"] = {}
            if event_matches(st, ev, device):
                return True
        return False

    async def held(self) -> bool:
        """Paused for a season change (season.py) until you resume."""
        from .season import HOLD_KV
        return bool(await self.db.kv_get(HOLD_KV, None))

    async def season_snapshot_pass(self) -> list[str]:
        """The pre-change copy (season.py), every few hours; never while held, so it keeps the galaxy as it was."""
        from . import season
        # the copy is megabytes: its row's write time says how old it is without reading it
        if (_ts(await self.db.kv_updated(season.SNAP_KV)) or _now() - timedelta(days=1)) > _now() - timedelta(hours=season.SNAP_HOURS):
            return []
        await self.db.kv_set(season.SNAP_KV, await season.snapshot(self.db))
        return []

    async def _let_off_riders(self, st: dict, job: dict) -> bool:
        """Before a carrier's trip: members of another fleet riding it that this trip would carry away from their fleet
        (fleets.foreign_riders) are let off where it is first; the loadout pass then brings them home. True when steps
        were added ahead of this one (it runs after them)."""
        from . import fleets as fl
        body, path = st.get("body") or {}, str(st.get("path") or "")
        if st.get("riders_checked") or not body.get("destination") or not (
                (body.get("command") == "travel" and path.startswith("/devices/")) or path.endswith("/travel")):
            return False
        st["riders_checked"] = True
        carrier = path.split("/")[2] if path.startswith("/devices/") else st.get("wait_device") or job.get("device")
        if not carrier:
            return False
        riders = fl.foreign_riders(carrier, body["destination"], await self.devices(), await self.fleets(), job)
        if not riders:
            return False
        add = fl.let_off_steps(carrier, riders)
        i = job["idx"]
        for s2 in job["steps"][i:]:
            if isinstance(s2.get("seq0_from"), int) and s2["seq0_from"] >= i:
                s2["seq0_from"] += len(add)
        job["steps"][i:i] = add
        st["tries"] -= 1
        await self.log(job["rule"], f"{job['title']}: {carrier} lets off {', '.join(x['code'] for x in riders)} before "
                                    f"leaving for {body['destination']} (another fleet's members)", "warning")
        await self._update(job)
        return True

    async def _advance(self, job_id: str) -> None:
        job = await self._get(job_id)
        if job and not job.get("forced") and await self.held():
            return   # paused for a season change: picks up again when you resume
        while job and job["status"] in ("running", "waiting"):
            if job["idx"] >= len(job["steps"]):
                job["status"] = "done"
                job["finished_at"] = now_iso()
                skipped = sum(1 for s in job["steps"] if s["status"] == "skipped")
                await self._update(job)
                await self.log(job["rule"], f"finished: {job['title']}" + (f" ({skipped} step(s) skipped)" if skipped else ""),
                               notify=True)
                if (job.get("meta") or {}).get("sim"):   # a run's clock runs fast: its next step now, not next tick
                    self.sim_kick()
                return
            st = job["steps"][job["idx"]]
            if st["status"] in ("waiting", "confirm"):
                return  # on_event / tick will move it on (a held trip: your Confirm)
            if st["status"] in ("done", "skipped"):
                job["idx"] += 1
                continue
            st["tries"] += 1
            st["started_at"] = now_iso()
            st["seq0"] = await self._max_seq()
            if st["method"] == "WAIT":
                # nothing to send: just wait for an event, counting from an earlier step if asked
                ref = st.get("seq0_from")
                if isinstance(ref, int) and 0 <= ref < len(job["steps"]):
                    st["seq0"] = job["steps"][ref].get("seq0", st["seq0"])
                # a trip this job sent: wait at least until the game's ETA (+30 min) — a ~500 ly surge outlasts the
                # default hour (seen live 2026-10-10: Surveyors' wait timed out mid-surge, the deploys then failed)
                eta = _ts((job.get("etas") or {}).get(st.get("wait_device") or job.get("device") or ""))
                if eta:
                    st["timeout"] = max(st.get("timeout", STEP_TIMEOUT), (eta - _now()).total_seconds() + 1800)
                ok, resp, err = True, None, None
            else:
                if await self._let_off_riders(st, job):
                    continue   # other fleets' members get off first; this trip goes out after them
                why = await self._beyond_sector(st, job)
                if why:   # beyond the sector range: waits for your Confirm (Automations › Jobs)
                    st["status"], st["hold"], job["status"] = "confirm", why, "waiting"
                    await self._update(job)
                    await self.log(job["rule"], f"{job['title']}: {st['desc']} — {why}; waits for your Confirm on the "
                                                "Automations page", "alert", notify=True)
                    return
                far = await self._too_far(st) or await self._dark_destination(st, job)
                if far:
                    ok, resp, err = False, None, far
                else:
                    exit_via = await self.plan_via(st, job)
                    if await self._ramp_legs(st, job):
                        continue   # to the entry point where it lands, the hop to the off-ramp, then the rest
                    ok, resp, err = await self.send(st["method"], st["path"], st["body"], f"auto: {st['desc']}")
                    if not ok and exit_via and "loop detected" in (err or "").lower() and await self._hop_legs(st, job):
                        await self.log(job["rule"], f"{job['title']}: via {', '.join(exit_via)} refused ({err}); "
                                                    "hopping there first", "warning")
                        continue   # the surge hops inside the system go first, each a trip of its own
                    if not ok and exit_via:   # refused: the route without the surge spots
                        await self.log(job["rule"], f"{job['title']}: via {', '.join(exit_via)} refused ({err}); "
                                                    "the game's own route", "warning")
                        from .hubroute import waypoints
                        rest = waypoints([st["hub_via"]], str(st["body"].get("destination") or "")) if st.get("hub_via") else []
                        st["body"].pop("via", None)
                        if rest:
                            st["body"]["via"] = rest
                        st["exit_via"] = None
                        ok, resp, err = await self.send(st["method"], st["path"], st["body"], f"auto: {st['desc']}")
                    if not ok and st.get("hub_via"):   # the waypoint refused: the direct route, as planned
                        await self.log(job["rule"], f"{job['title']}: via {st['hub_via']} refused ({err}); going direct",
                                       "warning")
                        st["body"].pop("via", None)
                        st["hub_via"] = None
                        ok, resp, err = await self.send(st["method"], st["path"], st["body"], f"auto: {st['desc']}")
            st["response"] = resp if isinstance(resp, (dict, list)) else None
            if ok and job["rule"] == "chain" and self.worker:   # a chain you started: its devices refreshed right away
                self.worker.after_order(st["path"], st["body"])
            if ok and isinstance(resp, dict):
                # long trips: wait at least until the game's own ETA (+30 min) before giving up — this step, and the
                # job's later waits for the same device (travel answers: arrives_at, or a route's time_seconds)
                eta = _trip_eta(resp)
                if eta:
                    st["timeout"] = max(st.get("timeout", STEP_TIMEOUT), (eta - _now()).total_seconds() + 1800)
                    if (st.get("body") or {}).get("command") == "travel" and str(st.get("path") or "").startswith("/devices/"):
                        job.setdefault("etas", {})[st["path"].split("/")[2]] = eta.isoformat(timespec="seconds")
                    if (st.get("body") or {}).get("command") == "travel" or "/travel" in str(st.get("path") or ""):
                        st["eta"] = eta.isoformat(timespec="seconds")
                        await self.log(job["rule"], f"{job['title']}: {st['desc']} — expected {eta_text(eta)}")
            if ok and (st.get("body") or {}).get("command") == "travel" and str(st.get("path") or "").startswith("/devices/"):
                job.setdefault("trips", {})[st["path"].split("/")[2]] = st["body"].get("destination")
            if (not ok and (st.get("body") or {}).get("command") == "deploy"
                    and "already deployed" in (err or "").lower()):
                # seen live (2026-10-04, Surveyors fleet): drones already out — that's what the step wanted
                ok, st["note"] = True, err
                st["wait"] = []
            if (not ok and (st.get("body") or {}).get("command") in ("deactivate", "withdraw", "clear_directive", "release")
                    and re.search(r"nothing to deactivate|not deployed|not launched|no directive|not adopted", (err or "").lower())):
                # stopping something that isn't running (stowed, already idle, never launched): what the step wanted
                ok, st["note"] = True, err
                st["wait"] = []
            if (not ok and (st.get("body") or {}).get("command") == "travel" and "not deployed" in (err or "").lower()
                    and str(st.get("path") or "").startswith("/devices/")):
                # seen live (2026-10-15, Surveyor 3 recall): a controller already stowed in its vessel was told to fly
                # to it and board. Aboard something already: nothing to fly — its waits for this trip are dropped
                code = st["path"].split("/")[2]
                devs_now = await self.devices()
                cur = next((d for d in devs_now if d.get("device_code") == code), {})
                host = next((d for d in devs_now if d.get("device_code") == (cur.get("stowed_in_device_code")
                                                                             or cur.get("attached_to_device_code"))), {})
                fleet_tags = {x for x in cur.get("tags") or [] if x.startswith("fleet:")}
                # only aboard a carrier of its own fleet counts (aboard another fleet's vessel, it still has to move)
                if host and fleet_tags & set(host.get("tags") or []):
                    ok, st["note"] = True, f"{err} — already aboard {cur.get('stowed_in_device_code') or cur.get('attached_to_device_code')}"
                    st["wait"] = []
                    for s2 in job["steps"][job["idx"] + 1:]:
                        if s2.get("method") == "WAIT" and s2.get("wait_device") == code and s2.get("status") == "pending":
                            s2["status"], s2["note"] = "skipped", "already aboard"
            if (not ok and (st.get("body") or {}).get("command") in ("travel", "stow", "attach")
                    and re.search(r"while (scanning|searching)", (err or "").lower()) and not st.get("scan_retry")
                    and str(st.get("path") or "").startswith("/devices/")):
                # live 2026-10-16 (Miner_3, Miner_4): a drone started a scan after the job was planned. Wait for it
                # to finish (scan.completed / search.completed), then try once more
                code = st["body"].get("device") if st["body"].get("command") == "attach" else st["path"].split("/")[2]
                w = step(f"wait for {code} to finish its scan", "", None, method="WAIT",
                         wait=["scan.completed", "search.completed"], timeout=3600)
                w["wait_device"] = code
                again = step(st["desc"], st["path"], dict(st["body"]), wait=list(st.get("wait") or []),
                             timeout=st.get("timeout", STEP_TIMEOUT), critical=st.get("critical", False),
                             match=dict(st.get("match") or {}))
                again["wait_device"], again["scan_retry"] = st.get("wait_device"), True
                job["steps"][job["idx"] + 1:job["idx"] + 1] = [w, again]
                for s2 in job["steps"][job["idx"] + 3:]:
                    if isinstance(s2.get("seq0_from"), int) and s2["seq0_from"] > job["idx"]:
                        s2["seq0_from"] += 2
                ok, st["note"] = True, f"{err} — waiting for {code}'s scan, then again"
                st["wait"] = []
            if (not ok and (st.get("body") or {}).get("command") == "attach" and "already attached" in (err or "").lower()
                    and str(st.get("path") or "").startswith("/devices/")):
                # seen live (2026-10-15, Prospector 4, Miner_3): "Target device is already attached to a carrier".
                # Attached to this carrier or another of its fleet: that's what the step wanted. Attached to another
                # carrier: that carrier detaches it (a deploy only frees a stowed device; the game answers "already
                # deployed", live 2026-10-20 at FALQUORYX). The device list names no carrier: deploy it, then attach again
                carrier, code = st["path"].split("/")[2], st["body"].get("device")
                devs_now = await self.devices()
                by_now = {d.get("device_code"): d for d in devs_now}
                other = (by_now.get(code) or {}).get("attached_to_device_code")
                fleet_tags = {x for x in (by_now.get(carrier) or {}).get("tags") or [] if x.startswith("fleet:")}
                if other and (other == carrier or fleet_tags & set((by_now.get(other) or {}).get("tags") or [])):
                    ok, st["note"] = True, f"{err} — already attached to {other}, a carrier of this fleet"
                    st["wait"] = []
                else:
                    if other and other in by_now:
                        dep = step(f"{other}: detach {code} (for {carrier} to attach it)", f"/devices/{other}",
                                   {"command": "detach", "device": code}, timeout=SHORT_TIMEOUT, critical=True)
                    else:
                        dep = step(f"deploy {code} out of {other or 'the carrier it is on'} (for {carrier} to attach it)",
                                   f"/devices/{code}", {"command": "deploy"}, wait=["device.deployed"], timeout=SHORT_TIMEOUT,
                                   critical=True)
                        dep["wait_device"] = code
                    again = step(st["desc"], st["path"], dict(st["body"]), wait=list(st.get("wait") or []),
                                 timeout=st.get("timeout", STEP_TIMEOUT), critical=st.get("critical", False))
                    again["wait_device"], again["no_retry_attach"] = st.get("wait_device"), True
                    if not st.get("no_retry_attach"):
                        job["steps"][job["idx"] + 1:job["idx"] + 1] = [dep, again]
                        for s2 in job["steps"][job["idx"] + 3:]:
                            if isinstance(s2.get("seq0_from"), int) and s2["seq0_from"] > job["idx"]:
                                s2["seq0_from"] += 2
                        ok, st["note"] = True, (f"{err} — on {other}: detaching it first" if other and other in by_now
                                                else f"{err} — on {other or 'a carrier outside the fleet'}: deploying it first")
                        st["wait"] = []
            m_short = re.search(r"insufficient (\w+) at location: need [\d.]+, have ([\d.]+)", (err or "").lower())
            if (not ok and m_short and (st.get("body") or {}).get("command") == "collect_resources"
                    and str(((job.get("meta") or {}).get("logistics") or {}).get("req") or "").startswith(
                        ("return:", "consolidate:", "evacuate:"))):
                # moving a pile home: someone took part of it since the plan (an AMI ferry, live 2026-10-20 at
                # ALPHARDAN-BELT-1). Load what's there instead; stop only when nothing is left to load
                res = {k: v for k, v in dict(st["body"].get("resources") or {}).items()}
                key = next((k for k in res if k.lower() == m_short.group(1)), None)
                if key is not None and (st.get("short_retries") or 0) < 5:
                    have = math.floor(float(m_short.group(2)))
                    if have >= 1:
                        res[key] = have
                    else:
                        res.pop(key)
                    if res:
                        again = step(st["desc"], st["path"], {**st["body"], "resources": res}, wait=list(st.get("wait") or []),
                                     timeout=st.get("timeout", STEP_TIMEOUT), critical=st.get("critical", False))
                        again["wait_device"], again["short_retries"] = st.get("wait_device"), (st.get("short_retries") or 0) + 1
                        job["steps"][job["idx"] + 1:job["idx"] + 1] = [again]
                        for s2 in job["steps"][job["idx"] + 2:]:
                            if isinstance(s2.get("seq0_from"), int) and s2["seq0_from"] > job["idx"]:
                                s2["seq0_from"] += 1
                        ok, st["note"] = True, f"{err} — loading what is there"
                        st["wait"] = []
            if not ok and "already at destination" in (err or "").lower():
                ok, st["note"] = True, err  # nothing to do: count it as done and move on
                st["wait"] = []
            if (not ok and (st.get("body") or {}).get("command") == "detach"
                    and "not attached to this carrier" in (err or "").lower()):
                # seen live: carriers let go of their cargo on arrival, so the device is already off — that's the goal
                ok, st["note"] = True, err
                st["wait"] = []
            if (not ok and (st.get("body") or {}).get("command") == "stow"
                    and "already stowed" in (err or "").lower()):
                # seen live (2026-10-06, Surveyors recall): the drone was already aboard — that's what the step wanted
                ok, st["note"] = True, err
                st["wait"] = []
            if not ok and (st.get("body") or {}).get("command") == "compact" and _already_compact(err):
                # the device is already compacted (or folding): that's what the step wanted. Still folding: keep
                # waiting for device.compacted; done: move on
                ok, st["note"] = True, err
                if "compacting" not in (err or "").lower():
                    st["wait"] = []
                    from .modular import remember   # so the next pass sends the carrier instead of compacting again
                    await remember(self.db, st["path"].split("/")[2], True, now_iso())
            if not ok and (st.get("body") or {}).get("command") == "unfurl" and _already_unfurled(err):
                ok, st["note"] = True, err   # already unfolded: nothing to do
                st["wait"] = []
                from .modular import remember
                await remember(self.db, st["path"].split("/")[2], False, now_iso())
            if not ok and re.match(r"^/locations/[^/]+/events/[^/]+$", st.get("path") or "") \
                    and "already completed" in (err or "").lower():
                # seen live (2026-10-08): "Event already completed by this account" — the contract is done (by an
                # earlier try, or by hand): that's the goal, so the run collects its rewards and goes home
                ok, st["note"] = True, err
                st["wait"] = []
                from .gameevents import DONE_KV
                done = await self.db.kv_get(DONE_KV, {}) or {}
                done[st["path"].rsplit("/", 1)[1]] = now_iso()
                await self.db.kv_set(DONE_KV, done)
            if (not ok and (st.get("body") or {}).get("command") == "change_owner"
                    and "already belongs to that replicant" in (err or "").lower()):
                # seen live (2026-10-06): a second owner pass for a device the first had already moved
                ok, st["note"] = True, err
                st["wait"] = []
            if not ok:
                hint = next((h for k, h in (st.get("hint") or {}).items() if k in (err or "").lower()), None)
                st["error"] = err = f"{err} — {hint}" if hint else err
                if st["tries"] < 2 and re.search(r"\brate.?limit|too many requests", (err or "").lower()):
                    await self._update(job)
                    return  # retry on next tick
                if BUSY_RE.search(err or "") and (st["tries"] < BUSY_TRIES or await self._still_moving(st)) \
                        and not await self._off_course(st, job):
                    # the game refuses while something is still on its way ("Cannot deploy while replicant is
                    # travelling"): try again in a few minutes instead of skipping it
                    eta = await self._moving_eta(st)
                    when = max(eta + timedelta(seconds=60), _now() + timedelta(seconds=60)) if eta \
                        else _now() + timedelta(seconds=BUSY_RETRY_S)
                    st["status"], st["retry_at"] = "retrying", when.isoformat(timespec="seconds")
                    job["status"] = "waiting"
                    await self._update(job)
                    if eta and st.get("logged_eta") != eta.isoformat(timespec="minutes"):
                        st["logged_eta"] = eta.isoformat(timespec="minutes")
                        await self.log(job["rule"], f"{job['title']}: '{st['desc']}' — {err}; still on its way, expected "
                                                    f"{eta_text(eta)} — trying again then")
                    elif not eta and st["tries"] == 1:
                        await self.log(job["rule"], f"{job['title']}: '{st['desc']}' — {err}; trying again every "
                                                    f"{BUSY_RETRY_S // 60} min")
                    return
                if st["critical"]:
                    st["status"] = "failed"
                    job["status"] = "failed"
                    await self._update(job)
                    await self.log(job["rule"], f"stopped: {job['title']} — {st['desc']} failed: {err}{ownership_hint(err)}", "alert", notify=True)
                    return
                st["status"] = "skipped"
                quiet = bool(NO_SITE_RE.search(err or ""))
                await self.log(job["rule"], f"{job['title']}: skipped '{st['desc']}' ({err}){ownership_hint(err)}",
                               "info" if quiet else "alert")
                job["idx"] += 1
                done_steps = [x for x in job["steps"][:job["idx"]] if x["method"] != "WAIT"
                              and not NO_SITE_RE.search(str(x.get("error") or ""))]
                last3, last6 = done_steps[-3:], done_steps[-6:]
                if ((len(last3) == 3 and all(x["status"] == "skipped" and x.get("error") == err for x in last3))
                        or (len(last6) == 6 and all(x["status"] == "skipped" for x in last6))):
                    job["status"] = "failed"
                    await self._update(job)
                    await self.log(job["rule"], f"stopped: {job['title']} — 3 steps in a row failed with: {err}{ownership_hint(err)}", "alert", notify=True)
                    return
                await self._update(job)
                continue
            if st["wait"]:
                wait_dev = st.get("wait_device") or job.get("device")
                if await self._already_happened(st, wait_dev) or (st["method"] == "WAIT" and await self._arrived_now(st, job)):
                    st["status"] = "done"
                    job["idx"] += 1
                else:
                    st["status"] = "waiting"
                    job["status"] = "waiting"
                    await self._update(job)
                    return
            else:
                st["status"] = "done"
                job["idx"] += 1
            job["status"] = "running"
            await self._update(job)
            job = await self._get(job_id)

    async def _moving_eta(self, st: dict) -> datetime | None:
        """When the device a refused step names (or the carrier it rides in, or the device a detach / stow names) is due
        to arrive, from the device list's travel info."""
        path = str(st.get("path") or "")
        if not path.startswith("/devices/"):
            return None
        by = {x.get("device_code"): x for x in await self.devices()}
        cands = [by.get(path.split("/")[2]), by.get(str((st.get("body") or {}).get("device") or "")),
                 by.get(str((st.get("body") or {}).get("target") or ""))]
        etas = []
        for d in [c for c in cands if c]:
            host = by.get(d.get("attached_to_device_code") or d.get("stowed_in_device_code") or "")
            for x in (d, host):
                tr = (x or {}).get("travel") or {}
                t = _ts(tr.get("final_arrives_at") or tr.get("arrives_at"))
                if t and t > _now() - timedelta(seconds=TRIP_GRACE_S):   # due, or only just overdue
                    etas.append(t)
        return max(etas) if etas else None

    async def _still_moving(self, st: dict) -> bool:
        """A refused step whose device (or the carrier it rides in) is still on a long trip: keep retrying past the usual
        3 h until that trip's ETA (+30 min) (seen live 2026-10-11: a detach after a ~500 ly carry gave up mid-surge)."""
        from .loadouts import in_transit
        eta = await self._moving_eta(st)
        if not eta or _now() > eta + timedelta(seconds=TRIP_GRACE_S):
            return False
        path = str(st.get("path") or "")
        if not path.startswith("/devices/"):
            return False
        by = {x.get("device_code"): x for x in await self.devices()}
        d = by.get(path.split("/")[2])
        target = (st.get("body") or {}).get("device")   # detach / attach name the carried device
        return any(x and in_transit(x, by) for x in (d, by.get(target or "")))

    async def plan_via(self, st: dict, job: dict) -> list[str]:
        """A device trip's waypoints, set once in the step's body `via`: the surge spots (_exit_via) first, then any hub
        waypoint (_hub_via), then the off-ramp. A spot in another system than the stop before it gets
        its star first (live 2026-10-24: "Cannot travel directly from CAELZAROR to ISOLITHOR-KUIPER (different systems)"),
        and a destination in another system than the last waypoint gets its star last (live 2026-10-26: "Cannot travel
        directly from ISOLITHOR-KUIPER to KHAZENEN-BELT-1"). Returns the surge spots."""
        from . import hubroute
        exit_via = await self._exit_via(st, job)
        await self._hub_via(st, job)
        if exit_via:
            ramp = [x for x in exit_via if x == st.get("arrive_via")]
            hubs_ = [x for x in st["body"].get("via") or [] if x not in exit_via]
            d = next((x for x in await self.devices() if x.get("device_code") == str(st.get("path")).split("/")[2]), None) or {}
            st["body"]["via"] = hubroute.waypoints(
                hubroute.stars_between(d.get("location") or "", [x for x in exit_via if x not in ramp] + hubs_ + ramp),
                str(st["body"].get("destination") or "").upper())
        return exit_via

    async def trip_via(self, device: str, dest: str) -> list[str]:
        """The waypoints the job runner would give `device`'s trip to `dest` (surge hops, a hub, the off-ramp), for a
        trip sent by hand (a replicant's travel form). Nothing is logged."""
        st = {"path": f"/devices/{device}", "body": {"command": "travel", "destination": dest}, "desc": ""}
        await self.plan_via(st, {"rule": None, "title": ""})
        via = list(st["body"].get("via") or [])
        ramp = st.get("arrive_via")
        if ramp in via and await self._hub_entry(star_of(ramp)):
            # one trip can't take the off-ramp where surges land at the entry point (it cruises from there): leave it out
            via = via[:via.index(ramp)]
            while via and star_of(via[-1]) == star_of(ramp):
                via.pop()
            from . import hubroute
            via = hubroute.waypoints(via, dest.upper()) if via else []
        return via

    async def _hop_legs(self, st: dict, job: dict) -> bool:
        """A trip inside one system whose surge spots the game refused as a `via` (live 2026-10-27: F7E30727
        ISOLITHOR-1-L4 → ISOLITHOR-BELT-1 via ISOLITHOR-KUIPER: "Loop detected: already at ISOLITHOR", then cruised 499 AU
        for 57 min; from the belt to ISOLITHOR-1-L4 the same via was taken). Each spot becomes a trip of its own ahead of
        this one (a 30 s surge hop), and this one then cruises the rest.
        True when steps were added ahead of this one (it runs after them)."""
        body, path = st.get("body") or {}, str(st.get("path") or "")
        via = [x for x in body.get("via") or [] if "-" in x]
        if not via or not path.startswith("/devices/") or body.get("command") != "travel":
            return False
        code, dest = path.split("/")[2], str(body.get("destination") or "").upper()
        d = next((x for x in await self.devices() if x.get("device_code") == code), None) or {}
        if star_of(d.get("location")) != star_of(dest) or any(star_of(x) != star_of(dest) for x in via):
            return False
        add = []
        for x in via:
            h = step(f"{code} → {x} (surge hop)", path, {"command": "travel", "destination": x}, wait=["travel.arrived"],
                     match={"destination": x})
            h.update(wait_device=code, exit_checked=True, hub_checked=True, riders_checked=True)
            add.append(h)
        body.pop("via", None)
        st["exit_via"] = None
        i = job["idx"]
        for s2 in job["steps"][i:]:
            if isinstance(s2.get("seq0_from"), int) and s2["seq0_from"] >= i:
                s2["seq0_from"] += len(add)
        job["steps"][i:i] = add
        st["tries"] -= 1
        await self._update(job)
        return True

    async def _hub_entry(self, star: str) -> str | None:
        """Where surges into `star` land when that's its entry point: once a scan has found it, or with an active hub
        there (anyone's; game docs, owner 2026-10-25). None otherwise (they land at the Oort cloud or Kuiper belt)."""
        from . import wardhub as wh
        _, entry = await self._surge_spots(star, await self.cruise_radii())
        if not entry:
            return None
        hubs = {star_of(x.get("location")) for x in await self.devices() if wh.is_hub(x) and wh.active(x)} \
            | await self.star_lookup("hubs")
        return entry if star in hubs or await self.system_scan(star) else None

    async def _ramp_legs(self, st: dict, job: dict) -> bool:
        """An off-ramp (a surge spot in the destination system, _exit_via) into a system whose surges land at its entry
        point (_hub_entry), where the game then cruises to the off-ramp as a via (live 2026-10-28: F7E30727
        ANLOREN → ISOLITHOR-BELT-1 via ISOLITHOR-KUIPER cruised 527 AU from ISOLITHOR-1-L4, 49 min, though the game's
        route priced that leg at 13 s; three Delta trips waited out their hour). The trip is split: to the entry point,
        a surge hop of its own to the off-ramp (30 s), then this trip cruises the rest.
        True when steps were added ahead of this one (it runs after them)."""
        from . import hubroute
        body, path = st.get("body") or {}, str(st.get("path") or "")
        ramp = st.get("arrive_via")
        if not ramp or ramp not in (body.get("via") or []) or body.get("command") != "travel" or not path.startswith("/devices/"):
            return False
        ds = star_of(ramp)
        entry = await self._hub_entry(ds)
        if not entry or entry == ramp:
            return False
        code = path.split("/")[2]
        via = list(body.get("via") or [])
        before = via[:via.index(ramp)]
        while before and star_of(before[-1]) == ds:   # the star stars_between put ahead of the off-ramp
            before.pop()
        flags = {"wait_device": code, "exit_checked": True, "hub_checked": True, "riders_checked": True, "sector_ok": True}
        first = step(f"{code} → {entry} (entry point, then a surge hop to {ramp})", path,
                     {"command": "travel", "destination": entry} | ({"via": hubroute.waypoints(before, entry)} if before else {}),
                     wait=["travel.arrived"], match={"destination": entry})
        hop = step(f"{code} → {ramp} (surge hop)", path, {"command": "travel", "destination": ramp},
                   wait=["travel.arrived"], match={"destination": ramp})
        add = []
        for h in (first, hop):
            h.update(flags)
            add.append(h)
        body.pop("via", None)
        st["exit_via"], st["arrive_via"] = None, None
        i = job["idx"]
        for s2 in job["steps"][i:]:
            if isinstance(s2.get("seq0_from"), int) and s2["seq0_from"] >= i:
                s2["seq0_from"] += len(add)
        job["steps"][i:i] = add
        st["tries"] -= 1
        await self._update(job)
        if job.get("rule"):
            await self.log(job["rule"], f"{job['title']}: {code} flies to {entry} first, hops to {ramp}, then on to "
                                        f"{body.get('destination')} (surges into {ds} land at its entry point)")
        return True

    async def _hub_via(self, st: dict, job: dict) -> None:
        """A device's trip to another system goes through a hub (ours or another player's: any star the catalog flags
        has_hub) when the game's previews say that's enough faster (hubroute.py): sets the step's `via` once, before it's first sent. Previews that fail leave it direct."""
        from . import hubroute
        from . import wardhub as wh
        body = st.get("body") or {}
        path = str(st.get("path") or "")
        if (body.get("command") != "travel" or "via" in body or body.get("dry_run") or not path.startswith("/devices/")
                or st.get("hub_checked") or (job.get("meta") or {}).get("sim")):
            return
        st["hub_checked"] = True
        code, dest = path.split("/")[2], str(body.get("destination") or "")
        devs = await self.devices()
        d = next((x for x in devs if x.get("device_code") == code), None) or {}
        # a hub slows every ship arriving in its system, whoever owns it (owner 2026-10-22)
        hubs = {star_of(x.get("location")) for x in devs if wh.is_hub(x) and wh.active(x)} | await self.star_lookup("hubs")
        if not d.get("location") or not hubs:
            return
        pos = await self.star_lookup("pos")
        cands = hubroute.candidates(star_of(d["location"]), star_of(dest), hubs, pos)
        if not cands:
            return

        async def preview(extra: dict) -> float | None:
            try:
                return hubroute.total_seconds(await self.api.request("POST", path, json_body={**body, **extra, "dry_run": True},
                                                                     background=True))
            except ApiError:
                return None
        direct = await preview({})
        best = None
        for h in cands:
            t = await preview({"via": hubroute.waypoints([h], dest)})
            if hubroute.worth_it(direct, t) and (best is None or t < best[1]):
                best = (h, t)
        if best:
            body["via"], st["hub_via"] = hubroute.waypoints([best[0]], dest), best[0]
            st["desc"] += f" (via hub {best[0]})"
            if job.get("rule"):
                await self.log(job["rule"], f"{job['title']}: {code} → {dest} via the hub at {best[0]}: "
                                            f"{hubroute.hm(best[1])} instead of {hubroute.hm(direct)}")

    async def _dark_destination(self, st: dict, job: dict) -> str | None:
        """A trip planned unescorted into comms coverage that has since gone dark: don't leave (live 2026-10-20: two
        trade freighters planned for SOL-3-1 at 13:22 left at 16:03 and arrived out of comms range, cargo aboard)."""
        star = st.get("need_comms")
        if not star or (st.get("body") or {}).get("command") != "travel":
            return None
        devices = await self.devices()
        if star in await self.commandable(devices):
            return None
        from .logistics import riding_hosts
        reps = await self.db.kv_get("replicants", {}) or {}
        hosts = riding_hosts(devices, {r.get("hosted_device_code"): c for c, r in reps.items() if r.get("hosted_device_code")})
        if set(hosts) & set([job.get("device")] + list((job.get("meta") or {}).get("devices") or [])):
            return None   # a replicant flies with this job: it brings comms along
        return f"{star} is out of comms range now (no relay network or replicant there): not leaving without an escort"

    async def _surge_spots(self, star: str, radii: dict[str, float]) -> tuple[set[str], str]:
        """A system's surge-capable places and its entry point. The game docs (owner 2026-10-25) name three: the Oort
        cloud, the Kuiper belt and the entry point (usually a planet's L5 next to the belt). Live dry runs agree: from
        FALQUORYX-1-L4 (its entry point) the Kuiper belt and the Oort cloud were 30 s `surge_hop`s, another planet's L4
        and its own L5 a cruise."""
        cat = await self.db.kv_get("stars", {}) or {}
        entry = next((str(x.get("entry_point") or "").upper() for x in cat.get("stars") or []
                      if isinstance(x, dict) and x.get("designation") == star), "")
        return ({x.upper() for x in radii if star_of(x) == star and x.upper().endswith(("-OORT", "-KUIPER"))}
                | ({entry} - {""})), entry

    async def _exit_via(self, st: dict, job: dict) -> list[str]:
        """Surge hops (owner 2026-10-22): a surge-capable device hops in seconds between a system's surge-capable places
        (_surge_spots), then cruises the rest, and the game routes through them when they're in the trip's `via`
        (ISOLITHOR-BELT-1 → ISOBAREA via ISOLITHOR-KUIPER: 515 s, instead of 3544 s by a 499 AU cruise to the entry
        point). The waypoints this trip gets, once, before it's first sent:
          • from a belt or a body: the nearest such place, when the game would cruise much further (leaving the system,
            it cruises to the entry point; inside it, to the destination);
          • inside the system: then the one nearest the destination (Delta Force: 499 AU, 57 min from ISOLITHOR-1-L4 to
            a belt next to the Kuiper belt).
        Each needs to save at least 30 AU and a quarter of the cruise."""
        from .fleets import cruise_au
        body, path = st.get("body") or {}, str(st.get("path") or "")
        if (body.get("command") != "travel" or st.get("exit_checked") or body.get("via") or body.get("dry_run")
                or not path.startswith("/devices/") or (job.get("meta") or {}).get("sim")):
            return []
        st["exit_checked"] = True
        code, dest = path.split("/")[2], str(body.get("destination") or "").upper()
        d = next((x for x in await self.devices() if x.get("device_code") == code), None) or {}
        here = str(d.get("location") or "").upper()
        if "surge" not in (d.get("features") or []) or not here or here == dest:
            return []
        radii = await self.cruise_radii()
        hs = star_of(here)
        spots, entry = await self._surge_spots(hs, radii)
        def gap(x: str, y: str) -> float:
            return cruise_au(x, y, radii)
        pos, via, saved_from = here, [], 0.0
        at_spot = here in spots
        inside = star_of(dest) == hs
        if not at_spot and (inside or entry):
            game = gap(here, entry) if not inside else gap(here, dest)
            near = min(spots, key=lambda x: (gap(here, x), x), default=None)
            tail = min((gap(x, dest) for x in spots), default=game) if inside else 0.0
            if near and game - (gap(here, near) + tail) >= max(30.0, game / 4):
                via, pos, saved_from = [near], near, game
        if inside and (via or at_spot):
            now_au = gap(pos, dest)
            hop = min(spots - {pos}, key=lambda x: (gap(x, dest), x), default=None)
            if hop and now_au - gap(hop, dest) >= max(30.0, now_au / 4):
                via.append(hop)
                saved_from = saved_from or now_au
        ds = star_of(dest)
        if not inside and not re.fullmatch(rf"{re.escape(ds)}-\d+-L[45]", dest):
            # the off-ramp (owner 2026-10-22: 2D6BFDF1 surged to ISOLITHOR-1-L4, then cruised 499 AU to ISOLITHOR-BELT-1):
            # the destination system's surge spot nearest the destination, when the game would arrive much further off
            # (at the entry point; not known: an inner L-point, as seen live)
            dspots, dentry = await self._surge_spots(ds, radii)
            if dest not in dspots:
                game = gap(dentry or f"{ds}-1-L4", dest)
                ramp = min(dspots, key=lambda x: (gap(x, dest), x), default=None)
                if ramp and game - gap(ramp, dest) >= max(30.0, game / 4):
                    via.append(ramp)
                    st["arrive_via"] = ramp
                    saved_from += game
        if via:
            st["exit_via"] = via
            st["desc"] += f" (surge hop via {', '.join(via)})"
            if job.get("rule"):
                await self.log(job["rule"], f"{job['title']}: {code} → {dest} via {', '.join(via)} (surge hop), instead of "
                                            f"≈{saved_from:.0f} AU of cruise")
        return via

    async def _beyond_sector(self, st: dict, job: dict) -> str | None:
        """The sector range (owner 2026-10-22: logistics sent freighter 2D6BFDF1 from SOLARES on a 241 h trip to
        ISOLITHOR to fetch a stockpile): an automated trip longer than logistics.SECTOR_RANGE_H at the game's surge pace,
        or of unknown length, isn't sent until you Confirm it. Measured from the catalog; a trip over the range or off
        the catalog by the game's preview of its planned route (a hub can bring it under).
        Trips you asked for (a forced job, an allow_far step), simulations and previews go as they are."""
        from . import hubroute
        from .logistics import SECTOR_RANGE_H, SURGE_S_PER_LY
        body, path = st.get("body") or {}, str(st.get("path") or "")
        rep = path.startswith("/replicants/") and path.endswith("/travel")
        if (not body.get("destination") or not (rep or (body.get("command") == "travel" and path.startswith("/devices/")))
                or body.get("dry_run") or st.get("allow_far") or st.get("sector_ok") or job.get("forced")
                or (job.get("meta") or {}).get("sim")):
            return None
        if st.get("sector_h") is None:
            devs = {x.get("device_code"): x for x in await self.devices()}
            code = path.split("/")[2]
            if rep:
                code = ((await self.db.kv_get("replicants", {}) or {}).get(code) or {}).get("hosted_device_code") or ""
            d = devs.get(code) or {}
            car = devs.get(d.get("stowed_in_device_code") or d.get("attached_to_device_code") or "") or {}
            here, there = star_of(d.get("location") or car.get("location")), star_of(str(body["destination"]).upper())
            pos = await self.star_lookup("pos")
            hours = None
            if here and here == there:
                hours = 0.0
            elif here in pos and there in pos:
                pa, pb = pos[here], pos[there]
                hours = math.dist([float(pa.get(k) or 0) for k in "xyz"], [float(pb.get(k) or 0) for k in "xyz"]) * SURGE_S_PER_LY / 3600
            if code and (hours is None or hours > SECTOR_RANGE_H):
                # over the range by the catalog, or not in it: the game's time for the route the trip will take
                # (surge hops, a hub, the off-ramp), else its own route
                via = body.get("via") or await self.trip_via(code, str(body["destination"]).upper())
                for v in ([via] if via else []) + [None]:
                    try:
                        secs = hubroute.total_seconds(await self.api.request(
                            "POST", path, json_body={**{k: x for k, x in body.items() if k != "via"},
                                                     **({"via": v} if v else {}), "dry_run": True}, background=True))
                    except ApiError:
                        continue
                    if secs is not None:
                        hours = secs / 3600
                        break
            st["sector_h"] = round(hours, 1) if hours is not None else -1.0
        h = st["sector_h"]
        if 0 <= h <= SECTOR_RANGE_H:
            return None
        return (f"about {h:.0f} h of travel, beyond the {SECTOR_RANGE_H:.0f} h sector range" if h >= 0
                else "a trip of unknown length (beyond the sector range?)")

    async def confirm_trip(self, job_id: str) -> bool:
        """Your Confirm for a trip held at the sector range: it goes out now."""
        async with self.lock:
            job = await self._get(job_id)
            if not job or job["status"] != "waiting" or job["idx"] >= len(job["steps"]):
                return False
            st = job["steps"][job["idx"]]
            if st.get("status") != "confirm":
                return False
            st["sector_ok"], st["status"], job["status"] = True, "pending", "running"
            st.pop("hold", None)
            await self._update(job)
            await self.log(job["rule"], f"{job['title']}: {st['desc']} confirmed beyond the sector range")
            await self._advance(job_id)
            return True

    async def _too_far(self, st: dict) -> str | None:
        """The one check every automated trip goes through: a drone or AMI controller isn't sent on a long cruise inside
        a system (seen live 2026-10-11: a survey drone at an Oort cloud sent ~320 min to the inner system). Over the
        loadout settings' max cruise (AU) the step isn't sent; fleets fetch such devices with a carrier instead. A step
        marked allow_far (a follow-up you asked for yourself) is sent as is. Returns why not, or None."""
        body = st.get("body") or {}
        path = str(st.get("path") or "")
        if body.get("command") != "travel" or st.get("allow_far") or not path.startswith("/devices/"):
            return None
        code = path.split("/")[2]
        d = next((x for x in await self.devices() if x.get("device_code") == code), None) or {}
        t = str(d.get("device_type") or "")
        dest, here = str(body.get("destination") or ""), d.get("location")
        from .fleets import cruise_au
        if not ("drone" in t or "controller" in t) or not here or star_of(here) != star_of(dest) or "surge" in (d.get("features") or []):
            return None
        au, limit = cruise_au(here, dest, await self.cruise_radii()), await self.max_cruise_au()
        if au <= limit:
            return None
        return (f"not sent: {code} would cruise ≈{au:.0f} AU from {here} to {dest} (over the {limit:g} AU limit in "
                "Fleets › settings) — a carrier should fetch it")

    async def _arrived_now(self, st: dict, job: dict) -> bool:
        """A wait for an arrival whose device is already standing at the destination (seen live 2026-10-11: Surveyors'
        assemble waited for its survey controller to land at SOLARES-BELT-1 when it already had, before the job began —
        the event was older than the wait, and nothing moved for half an hour)."""
        if "travel.arrived" not in (st.get("wait") or []):
            return False
        path = str(st.get("path") or "")
        code = st.get("wait_device") or (path.split("/")[2] if path.startswith("/devices/") else None) or job.get("device")
        dest = str((st.get("match") or {}).get("destination") or (st.get("body") or {}).get("destination") or "").upper()
        by = {x.get("device_code"): x for x in await self.devices()}
        d, seen = by.get(code), set()
        # already aboard a carrier standing there counts too (live 2026-10-18: a survey controller with recall on stowed
        # its drones in the fleet's vessel itself, and the recall waited out an hour per drone)
        while d and not d.get("location") and (d.get("stowed_in_device_code") or d.get("attached_to_device_code")) \
                and d.get("device_code") not in seen:
            seen.add(d.get("device_code"))
            d = by.get(d.get("stowed_in_device_code") or d.get("attached_to_device_code"))
        if not d or not dest or not d.get("location") or d.get("location_stale"):
            return False
        if str(d.get("status") or "").lower().startswith(("travel", "cruis", "surg", "moving", "recalling", "returning")):
            return False
        loc = str(d["location"]).upper()
        return loc == dest or ("-" not in dest and star_of(loc) == dest)

    async def _not_arrived(self, st: dict, job: dict) -> str | None:
        """A wait for an arrival that's over: is the device in the system it was sent to? None = yes (or unknown)."""
        code = st.get("wait_device") or job.get("device")
        dest = (st.get("match") or {}).get("destination") or (job.get("trips") or {}).get(code or "")
        d = next((x for x in await self.devices() if x.get("device_code") == code), None)
        if not d or not dest or not d.get("location") or d.get("location_stale"):
            return None
        if star_of(d["location"]) != star_of(dest):
            return f"{code} didn't arrive at {dest}: it's at {d['location']}"
        return None

    async def _off_course(self, st: dict, job: dict) -> str | None:
        """For a step after a trip in this job: the carrier is now heading somewhere else, or has stopped somewhere
        else, than the trip's destination. None = on course (or no trip to compare with)."""
        trips = job.get("trips") or {}
        if not trips:
            return None
        by = {x.get("device_code"): x for x in await self.devices()}
        code = str(st.get("path") or "").split("/")[2] if str(st.get("path") or "").startswith("/devices/") else None
        d = by.get(code or "") or {}
        cands = [c for c in (d.get("stowed_in_device_code"), d.get("attached_to_device_code"), code) if c in trips]
        if not cands:
            cands = [c for c in trips if c in by]
        for c in cands:
            dest, car = star_of(trips[c]), by.get(c) or {}
            tr = car.get("travel") or {}
            heading = star_of(tr.get("final_destination") or tr.get("destination"))
            moving = str(car.get("status") or "").startswith(("travel", "cruis", "surg"))
            if moving and heading and heading != dest:
                return f"{c} is heading to {heading}, not {dest} (trip canceled or redirected)"
            if not moving and car.get("location") and not car.get("location_stale") and star_of(car["location"]) != dest:
                return f"{c} is at {car['location']}, not {dest}"
        return None

    async def _still_travelling(self, st: dict, job: dict, now) -> bool:
        """A wait for an arrival that timed out while the device is evidently still on its way (moving, or left out of
        the device list mid-surge): extend it until the trip's ETA (+30 min); with no ETA known, one more hour."""
        code = st.get("wait_device") or job.get("device")
        d = next((x for x in await self.devices() if x.get("device_code") == code), None)
        first = _ts(st.setdefault("first_started_at", st.get("started_at") or now.isoformat(timespec="seconds"))) or now
        tr = (d or {}).get("travel") or {}
        # the later of the trip's ETA and the current leg's: the game's route can underprice a leg (live 2026-10-28:
        # F7E30727's whole trip was due 22:52, its 527 AU leg to ISOLITHOR-KUIPER 23:45, and the wait gave up at 22:41)
        etas = [t for t in (_ts(tr.get("final_arrives_at")), _ts(tr.get("arrives_at"))) if t]
        eta = max(etas) if etas else _ts((job.get("etas") or {}).get(code or ""))
        limit = (eta + timedelta(seconds=TRIP_GRACE_S)) if eta else (first + timedelta(seconds=st.get("timeout", STEP_TIMEOUT)
                                                                                    + NO_ETA_EXTRA_S))
        if not d or now > limit:
            return False
        st_ = str(d.get("status") or "")
        moving = st_.startswith(("travel", "cruis", "surg")) or (d.get("unlisted") and not d.get("location"))
        if not moving:
            return False
        st["started_at"] = now.isoformat(timespec="seconds")
        if not st.get("extended"):
            await self.log(job["rule"], f"{job['title']}: {code} is still on its way — waiting longer for '{st['desc']}'")
        st["extended"] = int(st.get("extended") or 0) + 1
        await self._update(job)
        return True

    async def cancel(self, job_id: str) -> None:
        async with self.lock:
            job = await self._get(job_id)
            if job and job["status"] in ("running", "waiting"):
                job["status"] = "cancelled"
                job["finished_at"] = now_iso()
                await self._update(job)
                await self.log(job["rule"], f"canceled: {job['title']}")

    # --- event & tick handling ------------------------------------------------------------------
    async def on_event(self, ev: dict, late: bool = False) -> None:
        """`late`: a replayed event (see ingest.LATE_EVENT). It still updates what we know and wakes waiting jobs,
        but doesn't fire reactive rules: they'd act on a world that has moved on since."""
        async with self.lock:
            name = ev.get("event") or ""
            p = ev.get("payload") or {}
            # remember what has been surveyed
            if name in ("scan.completed", "search.completed") and not ev.get("virtual"):
                target = (p.get("scan_target") or p.get("search_target") or p.get("target") or p.get("body")
                          or p.get("designation") or ev.get("location"))
                if target:
                    surveyed = await self.db.kv_get("surveyed", {}) or {}
                    surveyed[target] = now_iso()
                    await self.db.kv_set("surveyed", surveyed)
            # an AMI survey controller finished survey_system: the whole system counts as surveyed
            # (not any directive: a belt_search finishing on the same controller says nothing about the planets)
            if (name == "directive.completed" and "survey" in (ev.get("device_type") or "") and not ev.get("virtual")
                    and (p.get("directive") or "survey_system") == "survey_system"):
                await self.mark_system_surveyed(star_of(ev.get("location")) or ev.get("star") or "", ev.get("device_code"))
            # tags changed while it was printing (Print queue panel): put them on the new device now
            if name == "print.completed" and p.get("new_device_code"):
                from . import printqueue as pqm
                edits = await self.db.kv_get(pqm.EDITS_KV, {}) or {}
                lst = edits.get(ev.get("device_code") or "") or []
                was = [str(t) for t in p.get("tags") or []]
                e = pqm.edit_for(lst, p.get("device_type"), was)
                if e:
                    lst.remove(e)
                    edits[ev["device_code"]] = lst
                    if not lst:
                        edits.pop(ev["device_code"], None)
                    await self.db.kv_set(pqm.EDITS_KV, edits)
                    add = [t for t in e["to"] if t not in was]
                    rem = [t for t in was if t not in e["to"]]
                    if add or rem:
                        await self.send("PATCH", f"/devices/{p['new_device_code']}",
                                        {"configuration": {**({"add_tags": add} if add else {}),
                                                           **({"remove_tags": rem} if rem else {})}},
                                        f"auto: {p['new_device_code']} gets the tags set while it printed")
                    p = {**p, "tags": list(e["to"])}   # what follows (orders, dispatch) sees the new tags
            # a print ordered for a system came out: remember its code until the device list shows it
            if name == "print.completed":
                from .loadouts import at_tag, to_tag
                orders = await self.db.kv_get("loadout_orders", []) or []
                tags = [str(t) for t in p.get("tags") or []]
                hit = next((o for o in orders if not o.get("device_code") and o["device_type"] == p.get("device_type")
                            and (to_tag(o["star"]) in tags or o.get("factory") == ev.get("device_code"))), None)
                if hit:
                    new = p.get("new_device_code")
                    hit["device_code"], hit["printed_at"] = new or "?", now_iso()
                    await self.db.kv_set("loadout_orders", orders)
                    if new and hit.get("location") and at_tag(hit["location"]) not in tags:
                        await self.send("PATCH", f"/devices/{new}", {"configuration": {"add_tags": [at_tag(hit["location"])]}},
                                        f"auto: pin {new} at {hit['location']}")
                    if new and to_tag(hit["star"]) not in tags:
                        # the game didn't carry the print's tags over: tag it ourselves so it is routed, not re-printed
                        await self.send("PATCH", f"/devices/{new}", {"configuration": {"add_tags": [to_tag(hit["star"])]}},
                                        f"auto: tag new {p.get('device_type')} {new} for {hit['star']}")
            # a print that's bound for another system: dispatch it as soon as it's out, not at the next loadout pass
            if name == "print.completed":
                new = p.get("new_device_code")
                tags = [str(t) for t in p.get("tags") or []]
                hit_order = any(o.get("device_code") == new for o in await self.db.kv_get("loadout_orders", []) or [])
                if new and (hit_order or any(t.startswith("to:") for t in tags)):
                    pend = await self.db.kv_get("dispatch_pending", {}) or {}
                    pend[new] = now_iso()
                    await self.db.kv_set("dispatch_pending", pend)
            # wake waiting jobs
            for job in await self.jobs():
                if job["status"] != "waiting":
                    continue
                st = job["steps"][job["idx"]]
                wait_dev = st.get("wait_device") or job.get("device")
                if st["status"] == "waiting" and event_matches(st, ev, wait_dev):
                    st["status"] = "done"
                    job["idx"] += 1
                    job["status"] = "running"
                    await self._update(job)
                    await self._advance(job["id"])
                elif (st["status"] == "waiting" and name == "travel.cancelled" and "travel.arrived" in st.get("wait", [])
                      and wait_dev and ev.get("device_code") == wait_dev):
                    # a canceled trip never arrives: the device turns back to where it started (the event's
                    # return_time_seconds), so stop waiting for it instead of sitting out the timeout
                    back = int(float(p.get("return_time_seconds") or 0))
                    st["error"] = (f"travel canceled — {wait_dev} returns to {p.get('origin') or 'where it started'}"
                                   + (f" (≈{back // 60} min)" if back else ""))
                    # whatever came next (deploy, unload …) was meant for the destination: stop here, so nothing is
                    # done back where it started (a fleet mission stalls; Retry flies again)
                    st["status"], job["status"] = "failed", "failed"
                    await self._update(job)
                    await self.log(job["rule"], f"stopped: {job['title']} — {st['error']}", "alert", notify=True)
            if name in ("site.depleted", "salvage.depleted") and not late:
                try:
                    await self.rule_salvage()
                except Exception as e:
                    log.exception("salvage rule failed")
                    await self.log("engine", f"salvage rule failed: {e}", "alert")
            if name in ("event.discovered", "event.completed", "print.completed") and not ev.get("virtual"):
                await self.civ_on_event(ev)
            if name.startswith("simulation."):
                try:
                    await self.sim_on_event(ev)
                except Exception:
                    log.exception("simulation end handling failed")
            if name == "system.object_detected":
                asyncio.create_task(self._locked_sync_objects())
            if name == "system.impact_warning" and not late:
                try:
                    await self.evacuate_pass(True)
                except Exception as e:
                    log.exception("evacuation failed")
                    await self.log("evacuate", f"evacuation failed: {e}", "alert", notify=True)
            # rules triggered by arrivals
            if name == "bobnet.new" and not late:
                try:
                    await self.bill_moved(p)
                except Exception as e:
                    log.exception("bill move check failed")
                    await self.log("trail", f"checking Bill's post failed: {e}", "alert")
                try:
                    await self.shop_post(p)
                except Exception as e:
                    log.exception("shop post check failed")
                    await self.log("shop", f"checking a shop's BobNet post failed: {e}", "alert")
            if name == "travel.arrived" and not late:
                try:
                    await self.on_arrival(ev)
                except Exception as e:
                    log.exception("arrival rules failed")
                    await self.log("engine", f"arrival handling failed: {e}", "alert")

    async def tick(self) -> None:
        async with self.lock:
            now = _now()
            self.stage = "jobs"
            await self.note_tick(started=now_iso())
            held = await self.held()
            for job in await self.jobs():
                if held and not job.get("forced"):
                    continue
                if job["status"] == "running":
                    await self._advance(job["id"])  # e.g. a rate-limited retry
                    continue
                if job["status"] != "waiting":
                    continue
                st = job["steps"][job["idx"]]
                if st.get("status") == "confirm":
                    continue   # beyond the sector range: waits for your Confirm
                if st.get("status") == "retrying":
                    off = await self._off_course(st, job)
                    if off:   # turned back or went elsewhere: the retried step was meant for the destination
                        st["status"], st["error"], job["status"] = "failed", off, "failed"
                        await self._update(job)
                        await self.log(job["rule"], f"stopped: {job['title']} — {off}", "alert", notify=True)
                        continue
                    if now >= (_ts(st.get("retry_at")) or now):
                        st["status"], job["status"] = "pending", "running"
                        await self._update(job)
                        await self._advance(job["id"])
                    continue
                started = _ts(st.get("started_at")) or now
                if (st.get("method") == "WAIT" or "travel.arrived" in (st.get("wait") or [])) \
                        and (now - started).total_seconds() > 60 and await self._arrived_now(st, job):
                    st["status"], st["note"] = "done", "already there"
                    job["idx"] += 1
                    job["status"] = "running"
                    await self._update(job)
                    await self._advance(job["id"])
                    continue
                if (now - started).total_seconds() > st.get("timeout", STEP_TIMEOUT) \
                        and "travel.arrived" in (st.get("wait") or []) and await self._still_travelling(st, job, now):
                    continue   # still on its way (or out of sight mid-surge): keep waiting
                if (now - started).total_seconds() > st.get("timeout", STEP_TIMEOUT) and "travel.arrived" in (st.get("wait") or []):
                    miss = await self._not_arrived(st, job)
                    if miss:   # not where the job sent it: don't go on to deploy / unload somewhere else
                        st["status"], st["error"], job["status"] = "failed", miss, "failed"
                        await self._update(job)
                        await self.log(job["rule"], f"stopped: {job['title']} — {miss}", "alert", notify=True)
                        continue
                if (now - started).total_seconds() > st.get("timeout", STEP_TIMEOUT):
                    st["status"] = "skipped"
                    st["error"] = "timed out waiting for " + "/".join(st["wait"])
                    job["idx"] += 1
                    job["status"] = "running"
                    await self._update(job)
                    await self.log(job["rule"], f"{job['title']}: '{st['desc']}' timed out, moving on", "alert")
                    await self._advance(job["id"])
            if held:   # paused for a season change: nothing else runs until you resume
                self.stage = None
                await self.note_tick()
                return
            for stage in ("season_snapshot_pass", "evacuate_pass", "rock_mining_pass", "rock_chase_pass", "run_fleets", "auto_deals_pass", "contract_supply_pass", "logistics_pass", "component_pass", "fill_fleets", "fleet_owners", "dispatch_new_prints", "rule_contracts", "refresh_known_belts", "track_viability",
                          "rule_consolidate", "rule_reopen_sites", "rule_salvage", "rest_dry_controllers", "rule_restart_idle_miners",
                          "run_due_schedules", "run_due_repeats", "run_due_loadouts", "civ_beacon_pass", "maintenance_pass",
                          "decommission_queue_pass", "trail_follow_pass", "trail_pass", "fleet_rename_pass", "place_tag_pass", "rider_pass", "stray_pass", "pathing_pass", "observatory_pass", "auto_scout_pass", "repair_return_pass", "hub_watch_pass", "bootstrap_pass", "pending_home_pass", "ward_orders_pass", "relay_rollout_pass", "news_pass", "sim_pass", "prospector_pass", "stock_pass"):
                every, at = SPREAD.get(stage, (1, 0))
                if self.tick_n and self.tick_n % every != at:
                    continue   # a heavy pass that runs every few ticks, on a tick of its own (all run on the first)
                self.stage = stage
                await asyncio.sleep(0)   # let waiting page requests run between stages
                t_stage, c_stage = time.perf_counter(), time.thread_time()
                try:   # one stage failing on odd data mustn't stop every stage after it, every tick
                    out = await getattr(self, stage)()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    log.exception("engine stage %s failed", stage)
                    key = f"{stage}: {type(e).__name__}: {e}"[:300]
                    if self.stage_errors.get(stage) != key:   # say so once per distinct error, not every minute
                        self.stage_errors[stage] = key
                        await self.log("engine", f"stage {stage} failed: {type(e).__name__}: {e}", "alert", notify=True)
                    continue
                finally:
                    perf.note_stage(stage, (time.perf_counter() - t_stage) * 1000, (time.thread_time() - c_stage) * 1000)
                self.stage_errors.pop(stage, None)
                if stage == "civ_beacon_pass":
                    for line in out or []:
                        await self.log("civ_beacons", line)
            self.stage = None
            self.tick_n += 1
            await self.note_tick()

    async def trail_follow_pass(self) -> list[str]:
        """Map › Trail › Follow to the end: the replicant that scanned flies to the system the latest departure points
        at, looks for the target's beacons there and reads them; an arrival with no departure after it ends the
        follow (the target is there), a departure sends it on to the next system (another candidate if this one has
        no record of the target). State in kv trail["follow"]."""
        from . import trail as tl
        s = tl.normalize(await self.db.kv_get(tl.KV, {}) or {})
        fo = s.get("follow") or {}
        if not fo.get("active"):
            return []
        reps = await self.db.kv_get("replicants", {}) or {}
        rep = fo.get("replicant")
        host = (reps.get(rep) or {}).get("hosted_device_code")
        name = (reps.get(rep) or {}).get("name") or rep
        out: list[str] = []

        def note(text: str) -> None:
            fo.setdefault("log", []).append({"at": now_iso(), "text": text})
            fo["log"] = fo["log"][-40:]
            out.append(text)

        async def finish(text: str, level: str = "mention") -> None:
            fo["active"], fo["result"] = False, text
            note(text)
            await self.log("trail", f"follow {s['target_name']}: {text}")
            await self._notify(level, f"Trail: {text}", "/trail")

        if not rep or rep not in reps:
            await finish("stopped: the replicant following the trail is gone", "warning")
        elif fo.get("state") == "travel":
            here = star_of((reps[rep] or {}).get("location") or (reps[rep] or {}).get("current_location"))
            job = next((j for j in await self.jobs() if j["id"] == fo.get("job")), None) if fo.get("job") else None
            if not fo.get("job") and here == fo.get("star"):
                fo["state"] = "arrived"
            elif not fo.get("job"):
                st = step(f"{name} → {fo['star']} (following {s['target_name']})", f"/replicants/{rep}/travel",
                          {"destination": fo["star"]}, wait=["travel.arrived"], timeout=12 * 3600, critical=True)
                st["wait_device"] = host
                j = await self.create_job("trail", f"follow {s['target_name']}: {name} → {fo['star']}", host, [st],
                                          {"devices": [host] if host else []}, force=True)
                if j:
                    fo["job"] = j["id"]
                    note(f"{name} sets off for {fo['star']}")
            elif job is None or job["status"] == "done":
                fo["state"], fo["job"] = "arrived", None
                note(f"{name} reached {fo['star']}")
            elif job["status"] in ("failed", "cancelled"):
                err = next((x.get("error") for x in job["steps"] if x.get("error")), job["status"])
                await finish(f"stopped: {name} couldn't travel to {fo['star']} ({err})", "warning")
        if fo.get("active") and fo.get("state") == "arrived":
            star = fo["star"]
            try:
                # everything of his in the system: any device is a sighting, his trade controller ends the trail
                from . import others as oth
                snap = await oth.scan_into(self.db, self.api, rep, set(reps))
                s = {**s, **{k: v for k, v in tl.normalize(await self.db.kv_get(tl.KV, {}) or {}).items() if k == "sightings"}}
                tl.record_sightings(s, snap.get("raw") or [], star=snap.get("star") or star)
                for b in tl.beacons_in({"devices": snap.get("raw") or []}, s.get("target_code"), s.get("target_name")):
                    s["beacons"].setdefault(b["code"], {**b, "found_at": now_iso()})
                for code, b in list(s["beacons"].items()):
                    if b.get("star") == star or not b.get("star"):
                        await tl.read_beacons(self.api, s, code)
            except ApiError as e:
                fo["tries"] = int(fo.get("tries") or 0) + 1
                note(f"reading {star} failed: {e.message}")
                if fo["tries"] >= 3:
                    await finish(f"stopped: couldn't read the beacons at {star} ({e.message})", "warning")
            else:
                fo["tries"] = 0
                fo.setdefault("visited", []).append(star)
                cat = await self.db.kv_get("stars", {}) or {}
                pos = {x.get("designation"): x.get("position") for x in cat.get("stars") or [] if isinstance(x, dict)}
                h = tl.next_hop(s, star, pos, fo["visited"])
                shop = tl.shop_at(s, star)
                seen = tl.latest_sighting(s)
                newest_here = (tl.at_star(s, star) or [{}])[0].get("logged_at") or ""
                if shop:   # his trade controller is here: that's the shop, the end of the trail
                    await self.shop_found(s, shop)
                    await finish(f"found {s['target_name']}'s shop: trade controller {shop['code']} at {shop['location']} — "
                                 f"{name} is there too")
                    h = {}
                elif seen and seen.get("star") and seen["star"] != star and seen["star"] not in fo["visited"] \
                        and seen["at"] > str(newest_here) and "end" not in h \
                        and not (seen.get("travel_type") == "departure" and tl.resets_from(s, seen["star"])):   # → SOL
                    # a newer sighting elsewhere beats where the beacons here point
                    h = {"go": seen["star"], "alts": [], "why": f"{seen['via']} was seen there {seen['at']}"}
                if not fo.get("active"):
                    pass
                elif "end" in h:
                    await finish(f"found {s['target_name']}: {h['end']} — {name} is there too")
                elif "go" in h and len(fo["visited"]) < tl.MAX_HOPS:
                    fo.update(state="travel", star=h["go"], alts=h["alts"], job=None)
                    note(f"at {star}: {h['why']} — on to {h['go']}")
                elif "go" in h:
                    await finish(f"stopped after {tl.MAX_HOPS} systems at {star}: the trail goes on to {h['go']}", "warning")
                elif fo.get("alts"):
                    nxt, fo["alts"] = fo["alts"][0], fo["alts"][1:]
                    fo.update(state="travel", star=nxt, job=None)
                    note(f"at {star}: {h['cold']} — trying the next candidate, {nxt}")
                elif tl.is_bill(s) and not fo.get("sol_tried") and star != tl.BILL_HOME:
                    # Bill always passes through SOL: a cold trail (his location may have reset) is picked up there
                    fo.update(state="travel", star=tl.BILL_HOME, job=None, sol_tried=True)
                    note(f"at {star}: {h['cold']} — {s['target_name']} always passes through {tl.BILL_HOME}: picking "
                         "the trail up there")
                else:
                    await finish(f"the trail goes cold at {star}: {h['cold']}", "warning")
        s["follow"] = fo
        await self.db.kv_set(tl.KV, s)
        self.hub.publish("state", "trail")
        return out

    async def shop_found(self, s: dict, shop: dict) -> None:
        """The followed replicant's trade controller was seen: its shop is listed there (and no longer set aside)."""
        cache = await self.db.kv_get("traders_cache", {}) or {}
        e = dict(cache.get(shop["code"]) or {})
        e.update({"controller_code": shop["code"], "location": shop["location"], "star": shop["star"],
                  "owner_name": e.get("owner_name") or s.get("target_name"),
                  "owner_replicant_code": e.get("owner_replicant_code") or s.get("target_code"), "read_at": now_iso()})
        e.setdefault("trades", [])
        cache[shop["code"]] = e
        if shop.get("star"):   # remembered: leaving it, he resets to SOL (trail.resets_from)
            s["shop_stars"] = sorted(set(s.get("shop_stars") or []) | {shop["star"]})
        await self.db.kv_set("traders_cache", cache)
        from . import shop as sh
        wanted = await self.db.kv_get(sh.LISTING_KV, {}) or {}
        if shop["code"] in wanted:   # you asked for his listing: the follower is in the system now
            try:
                trades = await sh.read_listing(self.db, self.api, shop["code"])
            except ApiError as e:
                log.info("reading %s's trades failed: %s", shop["code"], e.message)
            else:
                wanted.pop(shop["code"])
                await self.db.kv_set(sh.LISTING_KV, wanted)
                await self.log("shop", f"listing of {shop['code']} at {shop['location']}: {len(trades)} trades read")

    async def shop_post(self, payload: dict) -> str | None:
        """A shop owner's BobNet post that the shop is open ("Rock Droppers is open for shoppers! … Find me in the
        Oort."): move that shop to the system and spot it names (owner, 2026-10-20: Dash announces each move)."""
        from . import shop as sh
        cat = await self.db.kv_get("stars", {}) or {}
        post = sh.open_post(payload, {x.get("designation") for x in cat.get("stars") or [] if isinstance(x, dict)})
        if not post:
            return None
        await sh.shop_opened(self.db, post, now_iso())
        text = f"{post['shop_name']} ({post['owner_name']}) is open at {post['location']} (BobNet)"
        await self.log("shop", text)
        return text

    async def bill_moved(self, payload: dict) -> str | None:
        """Bill posted that his Skunkworks moved: set his shop aside where it was, clear the beacon logs, and when you
        follow him, pick the trail up at SOL (leaving his shop he can't be followed: he resets to SOL)."""
        from . import trail as tl
        from .shop import is_bills, known_traders, mark_gone
        cat = await self.db.kv_get("stars", {}) or {}
        stars = {x.get("designation") for x in cat.get("stars") or [] if isinstance(x, dict)}
        mv = tl.bill_move_post(payload, stars)
        if not mv:
            return None
        s = tl.normalize(await self.db.kv_get(tl.KV, {}) or {})
        last = tl.latest_sighting(s) if tl.is_bill(s) else None
        frm = mv["from"] or (last or {}).get("star")
        now = now_iso()
        try:   # where the shop is now, if the directory already lists it there
            from .shop import refresh_directory
            await refresh_directory(self.db, self.api, list(await self.db.kv_get("replicants", {}) or {}))
        except Exception:
            log.exception("trader directory refresh after Bill's post failed")
        closed = now_iso()   # after the refresh: his shop is closed until it's seen again (owner, 2026-10-20)
        for code, t in (await known_traders(self.db)).items():   # his shop isn't where we last saw it any more
            if is_bills(t) and (tl.star_of(t.get("location")) == frm if frm else t["seen_at"] < now):
                await mark_gone(self.db, code, t.get("location"), closed)
        from .shop import LISTING_KV
        wanted = await self.db.kv_get(LISTING_KV, {}) or {}
        known = await known_traders(self.db)
        for code, rq in wanted.items():   # a listing of his old shop: logistics goes once his shop is seen again
            if code not in known and rq.get("job"):
                await self.cancel(rq.pop("job"))
        if wanted:
            await self.db.kv_set(LISTING_KV, wanted)
        text = "Bill is on the move" + (f" — left {frm}" if frm else "") + f": “{mv['text'][:160]}”"
        if tl.is_bill(s):
            fo = s.get("follow") or {}
            if fo.get("job"):
                await self.cancel(fo["job"])
            s["moved"] = {"at": now, "from": frm, "text": mv["text"]}
            # the old route is history, and leaving his shop he can't be followed anyway — he resets to SOL (owner,
            # 2026-10-13): drop the beacon logs; the beacons stay known and are read again for the new route
            s["audit"] = {b: [] for b in s["audit"]}
            s["trail_reset_at"] = now
            if fo.get("active"):   # following him is started by hand on the Trail page only (owner, 2026-10-22)
                fo.update(active=False, job=None, result="stopped: Bill moved his shop")
                fo.setdefault("log", []).append({"at": now, "text": f"{text} — stopped (follow him again from Trail)"})
                s["follow"] = fo
                text += " — stopped following him (start it again on the Trail page)"
            s.pop("polled_at", None)   # re-read his beacons on the next pass
            await self.db.kv_set(tl.KV, s)
            self.hub.publish("state", "trail")
        await self.log("trail", text, notify=True)
        await self._notify("mention", f"Trail: {text}", "/trail")
        return text

    async def trail_pass(self) -> list[str]:
        """Map › Trail: every 15 minutes re-read the followed replicant's beacons (trail.py); each new departure or
        arrival of theirs raises a notification (level 'mention': the bell's badge counts it)."""
        from . import trail as tl
        s = tl.normalize(await self.db.kv_get(tl.KV, {}) or {})
        last = _ts(s.get("polled_at"))
        if not s["beacons"] or (last and (_now() - last).total_seconds() < 900):
            return []
        s["polled_at"] = now_iso()
        new, _errs = await tl.read_beacons(self.api, s)   # a beacon's first read returns nothing new
        await self.db.kv_set(tl.KV, tl.merge_read(await self.db.kv_get(tl.KV, {}) or {}, s))
        self.hub.publish("state", "trail")
        out = []
        for r in new:
            if s.get("target_code") and r.get("replicant_code") != s["target_code"]:
                continue
            where = r.get("location") or "?"
            text = (f"{s['target_name']}: {r.get('travel_type')} at {where}"
                    + (f" (heading {r.get('vector')})" if r.get("travel_type") == "departure" and r.get("vector") else ""))
            cur = await self.db.execute(
                "INSERT INTO notifications(event_id, level, title, body, link, created_at) VALUES(?,?,?,?,?,?)",
                (None, "mention", text, None, "/trail", now_iso()))
            self.hub.publish("notify", {"id": cur.lastrowid, "level": "mention", "title": text, "link": "/trail"})
            out.append(text)
        return out

    async def stray_pass(self) -> list[str]:
        """Members of stationed fleets away from home (fleets.strays), owner 2026-10-21. One riding another fleet's idle
        carrier (not a logistics one: the logistics pass brings those home) is let off where it is, so the loadout pass
        can bring it home. One stuck away for an hour with nothing bringing it is reported once. A fleet's move to a new
        home stays open (home_moves) until every member is there: reported when it's complete, and once if members are
        still missing after two hours."""
        from . import fleets as fl
        items = await self.fleets()
        devices = await self.devices()
        busy = self.busy_devices(await self.jobs())
        prev = await self.db.kv_get("fleet_strays", {}) or {}
        cur = fl.strays(items, devices, busy)
        # the loadout pass plans to send these home and waits for a carrier: not "nothing is bringing it home"
        lines = (await self.db.kv_get("loadouts_last", {}) or {}).get("lines") or []
        queued = {ln.split(" ", 1)[0] for ln in lines if "is away from its fleet's home" in ln}
        for s in cur:
            s["queued"] = s["code"] in queued
        now = _now()
        logistics = {fl.fleet_tag(f["id"])[6:] for f in items if f.get("role") == "logistics"}
        commandable = await self.commandable(devices)
        out, alerts, state = [], [], {}
        for s in cur:
            p = prev.get(s["code"]) or {}
            e = {**s, "since": p.get("since") or now_iso(), "alerted": p.get("alerted"), "let_off": p.get("let_off", 0)}
            state[s["code"]] = e
            car = s["riding"]
            if car and not s["busy"] and car not in busy and s["riding_fleet"] != fl.fleet_tag(s["fleet"])[6:] \
                    and s["riding_fleet"] not in logistics and s["star"] in commandable and e["let_off"] < 2:
                d = next((x for x in devices if x.get("device_code") == s["code"]), {})
                mode = "attach" if d.get("attached_to_device_code") == car else "stow"
                job = await self.create_job("fleets", f"{s['name']}: {car} lets off {s['code']} (its fleet is in {s['home']})",
                                            car, fl.let_off_steps(car, [{"code": s["code"], "mode": mode}]),
                                            {"devices": [car, s["code"]], "fleet": s["fleet"]})
                if job:
                    e["let_off"] += 1
                    busy.add(car)
                    out.append(f"{car} lets off {s['code']}")
            age = (now - (_ts(e["since"]) or now)).total_seconds() / 60
            if age >= fl.STRAY_ALERT_MINUTES and not e["alerted"] and not s["moving"] and not s["busy"]:
                e["alerted"] = now_iso()
                alerts.append(f"{s['code']} ({(s['type'] or 'device').replace('_', ' ')}, {s['name']}) in {s['star']}, "
                              f"home {s['home']}: {fl.stray_reason(s)}")
        if alerts:
            await self.log("fleets", "away from their fleet for an hour or more: " + "; ".join(alerts), "alert", notify=True)
        back = [c for c, p in prev.items() if c not in state and p.get("alerted")]
        if back:
            await self.log("fleets", f"back with their fleet: {', '.join(sorted(back))}")
        await self.db.kv_set("fleet_strays", state)
        moves = await self.db.kv_get("home_moves", {}) or {}
        if moves:
            names = {f["id"]: f for f in items}
            for fid, m in list(moves.items()):
                f = names.get(fid)
                if not f or f.get("home") != m["to"]:
                    moves.pop(fid)   # the fleet is gone or moved on (that move made its own entry)
                    continue
                left = [s for s in cur if s["fleet"] == fid]
                hours = (now - (_ts(m["at"]) or now)).total_seconds() / 3600
                if not left:
                    await self.log("fleets", f"{f.get('name')}: move to {m['to']} complete, every member is there", notify=True)
                    moves.pop(fid)
                elif hours >= 2 and not m.get("alerted"):
                    m["alerted"] = now_iso()
                    await self.log("fleets", f"{f.get('name')}: move to {m['to']} still missing "
                                   + "; ".join(f"{s['code']} in {s['star']} ({fl.stray_reason(s)})" for s in left),
                                   "alert", notify=True)
                elif hours >= 48:
                    moves.pop(fid)   # two days: the stray list and its alerts carry on without it
            await self.db.kv_set("home_moves", moves)
        return out

    async def rider_pass(self) -> list[str]:
        """A stationed fleet moved home: the vessels in it that host a replicant fly there too (the loadout pass never
        moves a replicant's vessel, so without this it stayed behind). Only after a home change — a replicant you send
        elsewhere afterwards isn't dragged back. Devices stowed in the vessel travel with it."""
        from . import fleets as fl
        pending = await self.db.kv_get("rider_moves", {}) or {}
        if not pending:
            return []
        items = {f["id"]: f for f in await self.fleets()}
        devices = await self.devices()
        reps = await self.db.kv_get("replicants", {}) or {}
        host_rep = {r.get("hosted_device_code"): c for c, r in reps.items() if r.get("hosted_device_code")}
        busy = self.busy_devices(await self.jobs())
        out = []
        for fid, p in list(pending.items()):
            f = items.get(fid)
            if not f or f.get("home") != p["home"]:
                pending.pop(fid)   # the fleet is gone, or has moved on again (that move made its own entry)
                continue
            riders = [d for d in fl.members(f, devices) if d.get("device_code") in host_rep]
            left = []
            for d in riders:
                code = d["device_code"]
                if star_of(d.get("location")) == p["home"]:
                    continue
                if code in busy or str(d.get("status") or "").startswith(("travel", "cruis", "surg")) \
                        or p["sent"].count(code) >= 3:   # on its way (or tried three times: leave it be)
                    left.append(code) if p["sent"].count(code) < 3 else None
                    continue
                rep = host_rep[code]
                st = step(f"{reps[rep].get('name') or rep} follows {f['name']} to {p['home']}", f"/replicants/{rep}/travel",
                          {"destination": p["home"]}, wait=["travel.arrived"], timeout=12 * 3600, critical=True)
                st["wait_device"] = code
                job = await self.create_job("fleets", f"{f['name']}: {reps[rep].get('name') or rep}'s vessel {code} follows "
                                            f"the fleet to {p['home']}", code, [st], {"devices": [code], "fleet": fid})
                if job:
                    p["sent"].append(code)
                    out.append(f"{code} → {p['home']}")
                left.append(code)
            if not left or (_now() - (_ts(p.get("at")) or _now())).total_seconds() > 48 * 3600:
                pending.pop(fid)   # everyone's there (or it's been two days: give up quietly)
        await self.db.kv_set("rider_moves", pending)
        return out

    async def place_tag_pass(self) -> list[str]:
        """`to:<location>` tags typed by hand (e.g. to:sol-3) become to:<system> + at:<location> (loadouts.place_tag_fix),
        so the loadout pass delivers them to the system and then to the spot."""
        from . import loadouts as lo
        devices = await self.devices()
        if not any(t.startswith("to:") for d in devices for t in d.get("tags") or []):
            return []
        slugs = await self.star_lookup("slugs")
        extra = lo.star_slugs({star_of(d.get("location")) for d in devices if d.get("location")})
        if set(extra) - set(slugs):
            slugs = {**slugs, **extra}
        fleet_ids = {f.get("id") for f in await self.fleets() if f.get("id")}
        busy = self.busy_devices(await self.jobs())
        out = []
        for d in devices:
            fix = lo.place_tag_fix(d, slugs, fleet_ids)
            if not fix or d["device_code"] in busy:
                continue
            add, rem = fix
            job = await self.create_job("tags", f"{d['device_code']}: {rem[0]} → {' + '.join(add) or 'its pin'}", d["device_code"],
                                        [lo.tag_step(d["device_code"], add or None, rem)], {"devices": [d["device_code"]]})
            if job:
                out.append(f"{d['device_code']}: {rem[0]} read as {' + '.join(add)}")
        return out

    async def fleet_rename_pass(self) -> list[str]:
        """A renamed fleet's devices still carrying the old fleet: tag in the game (marked `_retag` by
        fleets.apply_renames): one PATCH each, swapping the old tag for the new; tried again every 10 minutes."""
        devices = await self.devices()
        todo = [d for d in devices if d.get("_retag")]
        if not todo:
            return []
        last = _ts(await self.db.kv_get("retag_at", None))
        if last and (_now() - last).total_seconds() < 600:
            return []
        busy = self.busy_devices(await self.jobs())
        steps = []
        for d in todo:
            if d["device_code"] in busy:
                continue
            new = next((t for t in d.get("tags") or [] if t.startswith("fleet:")), None)
            if not new or new == d["_retag"]:
                continue
            steps.append(step(f"{d['device_code']}: {d['_retag']} → {new}", f"/devices/{d['device_code']}",
                              {"configuration": {"add_tags": [new], "remove_tags": [d["_retag"]]}}, method="PATCH"))
        if not steps:
            return []
        await self.db.kv_set("retag_at", now_iso())
        await self.create_job("fleets", f"renamed fleet: retag {len(steps)} device(s)", None, steps, {"devices": []}, force=True)
        return [f"retag {len(steps)}"]

    async def news_pass(self, force: bool = False) -> list[str]:
        """The game's changelog and blog, every 6 hours (news.py): new versions and posts go to the bell."""
        from . import news
        s = await self.db.kv_get(news.KV, {}) or {}
        last = _ts(s.get("tried_at"))
        if not force and last and (_now() - last).total_seconds() < news.EVERY:
            return []
        if not force and "replicant.space" not in str(getattr(self.api.http, "base_url", "")):
            return []   # a dev / test setup against the mock server: don't reach out to the real site
        s["tried_at"] = now_iso()
        try:
            changelog = news.parse_changelog(await news.fetch("/changelog/"))
            blog = news.parse_blog(await news.fetch("/blog/"))
        except Exception as e:
            s["error"] = f"{type(e).__name__}: {e}"[:300]
            await self.db.kv_set(news.KV, s)
            return []
        if not changelog and not blog:
            s["error"] = "the changelog and blog pages had no entries the parser recognizes (layout changed?)"
            await self.db.kv_set(news.KV, s)
            return []
        new_c, new_b = news.new_items(s, changelog, blog)
        s.update({"checked_at": now_iso(), "error": None, "changelog": changelog[:news.KEEP] or s.get("changelog") or [],
                  "blog": blog[:news.KEEP] or s.get("blog") or [],
                  "known_versions": sorted(set(s.get("known_versions") or []) | {c["version"] for c in changelog}),
                  "known_posts": sorted(set(s.get("known_posts") or []) | {p["id"] for p in blog})})
        s["new"] = {"changelog": [c["version"] for c in new_c] + [v for v in (s.get("new") or {}).get("changelog") or []
                                                                  if v not in {c["version"] for c in new_c}][:10],
                    "blog": [p["id"] for p in new_b]}
        await self.db.kv_set(news.KV, s)
        out = []
        for c in reversed(new_c):
            await self._notify("mention", news.headline(c), c["url"])
            await self.log("news", news.headline(c))
            out.append(c["version"])
        for p in reversed(new_b):
            text = f"New blog post by {p['author']}: {p['title']}"
            await self._notify("mention", text, p["url"])
            await self.log("news", text)
            out.append(p["id"])
        if out:
            self.hub.publish("state", "news")
        return out

    async def _notify(self, level: str, text: str, link: str = "/fleets") -> None:
        cur = await self.db.execute("INSERT INTO notifications(event_id, level, title, body, link, created_at) VALUES(?,?,?,?,?,?)",
                                    (None, level, text, None, link, now_iso()))
        self.hub.publish("notify", {"id": cur.lastrowid, "level": level, "title": text, "link": link})

    async def pathing_pass(self, force: bool = False) -> list[str]:
        """Stationed mining fleets with a heading / auto-prospect / auto-relocate (pathing.py): the observatory
        prospects along the heading when few systems lie ahead; when the home has been dry for a while the fleet picks
        its next home (best-ranked system ahead that nothing excludes), announces it, and after the grace period makes
        it its home — the loadout pass then moves the fleet. Every 5 minutes."""
        from . import pathing as pa
        from . import prospects as pr
        last = _ts(await self.db.kv_get("pathing_at", None))
        if not force and last and (_now() - last).total_seconds() < 300:
            return []
        await self.db.kv_set("pathing_at", now_iso())
        items = await self.fleets()
        mine = [f for f in items if f.get("role") == "mining" and f.get("station") and f.get("home")
                and (f.get("auto_relocate") or f.get("auto_prospect"))]
        if not mine:
            return []
        devices = await self.devices()
        inp = await pr.rank_inputs(self.db, self, devices)
        xyz_of = inp["xyz"]
        out, changed, now = [], False, _now()
        nothing = ((await self.db.kv_get("salvage_state", {}) or {}).get("nothing") or {})
        # the salvage rule re-marks "nothing to mine" every 24 h while it's still true; an older mark is out of date
        nothing = {k: v for k, v in nothing.items() if _ts(v) and (now - _ts(v)).total_seconds() < 26 * 3600}
        for f in mine:
            home, heading = f["home"], (f.get("heading") or {}).get("vector")
            cone = float(f.get("cone") or pa.DEFAULT_CONE)
            # (prospecting: observatory_pass)
            if not f.get("auto_relocate"):
                continue
            # --- a planned move: carry it out once the grace period is over (if the target is still free)
            if f.get("next_home"):
                if now < (_ts(f.get("next_home_at")) or now):
                    continue
                nxt, block = f["next_home"], pa.taken(f, items).get(f["next_home"])
                if block or nxt in inp["warded"]:
                    f["path_note"] = f"{nxt} isn't free any more ({block or 'warded'}) — choosing again"
                    f.pop("next_home", None), f.pop("next_home_at", None)
                    changed = True
                    continue
                f["prev_home"], f["home"] = home, nxt
                f.pop("next_home", None), f.pop("next_home_at", None), f.pop("depleted_since", None)
                f["path_note"] = f"moved home {home} → {nxt} at {now_iso()} — " + await self.start_relocation(f, home, nxt)
                changed = True
                out.append(f"{f['name']}: home {home} → {nxt}")
                await self.log("fleets", f"{f['name']}: new home {nxt} (was {home}) — the loadout pass moves the fleet there")
                await self._notify("warning", f"{f['name']} is moving home: {home} → {nxt}")
                continue
            # --- is the home dry, and for long enough?
            why = pa.dry(home, devices, inp["viability"], nothing, inp["warded"], (await self.system_belts({home}))[home])
            if not why:
                if f.pop("depleted_since", None):
                    changed = True
                continue
            if not f.get("depleted_since"):
                f["depleted_since"], f["path_note"] = now_iso(), f"{home} looks dry: {why}"
                changed = True
                continue
            if (now - _ts(f["depleted_since"])).total_seconds() < float(f.get("dry_hours") or pa.DRY_FOR_HOURS) * 3600:
                continue
            rows = (await pr.rank(self.db, self, devices, home, limit_unscanned=0, weigh=True))["rows"]
            star, note = pa.choose(f, items, rows, xyz_of, devices, heading, cone, await self.commandable(devices))
            if star:
                f["next_home"] = star
                f["next_home_at"] = (now + timedelta(minutes=float(f.get("grace_minutes") or pa.GRACE_MINUTES))).isoformat(timespec="seconds")
                f["path_note"] = f"{home} is dry ({why}); next home {star}: " + ", ".join(note)
                changed = True
                out.append(f"{f['name']}: next home {star}")
                await self.log("fleets", f"{f['name']}: {f['path_note']}")
                await self._notify("warning", f"{f['name']}: {home} is dry — moving to {star} at "
                                              f"{f['next_home_at'][11:16]} UTC unless you cancel ({', '.join(note[:2])})")
            else:
                msg = f"{home} is dry ({why}) but no system qualifies as the next home: " + "; ".join(note)
                if f.get("path_note") != msg:
                    f["path_note"] = msg
                    changed = True
                    await self.log("fleets", f"{f['name']}: {msg}", "alert")
                    await self._notify("warning", f"{f['name']}: {msg}")
        if changed:
            await self.save_fleets(items)
        return out

    async def hub_watch_pass(self) -> list[str]:
        """Map › Wards & hubs: a warning in the bell when a hub of ours needs attention — its 7-day shield ends within a
        day, its capacity (as last reported, less 10 %/day) is under 50 %, or its system holds less than one
        maintenance's worth. Once per hub and state, and again only after it recovered."""
        from . import wardhub as wh
        from .shapes import normalize_inventory
        devices = await self.devices()
        if not any(wh.is_hub(d) for d in devices):
            return []
        rows = wh.hubs(devices, await wh.hub_events(self.db),
                       wh.stock_by_star(normalize_inventory(await self.db.kv_get("inventory", []))))
        seen = await self.db.kv_get("hub_alerts", {}) or {}
        out = []
        for h in rows:
            why = []
            if h["shielded"] and h["shield_left_h"] is not None and h["shield_left_h"] < 24:
                why.append(f"its shield ends in {h['shield_left_h']:.0f} h")
            if not h["shielded"] and h["projected"] is not None and h["projected"] < 50:
                why.append(f"capacity about {h['projected']:.0f} %")
            if h["short"]:
                why.append("short of one maintenance in " + h["star"] + ": "
                           + ", ".join(f"{v:g} {k}" for k, v in h["short"].items()))
            key = "|".join(sorted(w.split(" ")[0] for w in why))
            if not why:
                seen.pop(h["code"], None)
                continue
            if seen.get(h["code"]) == key:
                continue
            seen[h["code"]] = key
            text = f"Hub {h['code']} at {h['location']}: " + "; ".join(why)
            cur = await self.db.execute(
                "INSERT INTO notifications(event_id, level, title, body, link, created_at) VALUES(?,?,?,?,?,?)",
                (None, "warning", text, None, "/wards", now_iso()))
            self.hub.publish("notify", {"id": cur.lastrowid, "level": "warning", "title": text, "link": "/wards"})
            out.append(text)
        await self.db.kv_set("hub_alerts", seen)
        return out

    async def observatory_pass(self) -> list[str]:
        """prospecting.py: every fleet's deployed observatory unfurls and prospects, direction by direction, and compacts
        when every direction is used up or its fleet is set to leave."""
        from . import fleets as fl
        from . import pathing as pa
        from . import prospecting as pr
        from .loadouts import bound_for
        from .modular import compact_step, compacted, folded, unfurl_step
        from .shapes import normalize_blueprints
        devices = await self.devices()
        obs = [d for d in devices if d.get("device_type") == "galactic_observatory" and fl.fleet_of(d)]
        if not obs:
            return []
        fleet_list = await self.fleets()
        items = {fl.fleet_tag(f["id"])[6:]: f for f in fleet_list}
        fleets_changed = False
        jobs = await self.jobs()
        by_id = {j["id"]: j for j in jobs}
        busy = self.busy_devices(jobs)
        runs = await self.db.kv_get("observatory_runs", {}) or {}
        lmoves = await self.db.kv_get("loadout_moves", {}) or {}
        lfresh = bool(_ts(lmoves.get("at")) and (_now() - _ts(lmoves["at"])).total_seconds() < 3600)
        pos = await self.star_lookup("pos_all")   # shared: read only
        bps = {b["device_type"]: b for b in normalize_blueprints(await self.db.kv_get("blueprints", []))}
        out: list[str] = []
        for o in obs:
            code, st = o["device_code"], str(o.get("status") or "")
            f = items.get(fl.fleet_of(o) or "")
            here = star_of(o.get("location"))
            if not f or not here or o.get("stowed_in_device_code") or o.get("attached_to_device_code") \
                    or o.get("location_stale") or st.startswith(("travel", "cruis", "surg", "stowed")):
                continue
            run = runs.get(code) or {}
            if run.get("star") != here:
                run = {"star": here, "used": [], "idx": 0, "found": 0}
            if st.startswith("prospecting") and not (run.get("live") and _ts(run["live"].get("completes_at"))
                                                    and _ts(run["live"]["completes_at"]) > _now()):
                # the prospect's direction / start / end (for the galaxy map): from the device list, else its details once
                from .observatory import live_prospect
                live = live_prospect(o)
                if not live:
                    try:
                        live = live_prospect(await self.api.request("GET", f"/devices/{code}", background=True) or {})
                    except ApiError:
                        live = None
                if live:
                    run["live"] = {k: live.get(k) for k in ("direction", "started_at", "completes_at", "origin")}
            # the prospect in flight: wait for it, then judge it by the new stars it found
            if run.get("job"):
                j = by_id.get(run["job"])
                if j and j["status"] in ("running", "waiting"):
                    runs[code] = run
                    continue
                err = next((x.get("error") for x in (j or {}).get("steps", []) if x.get("status") == "failed"), None)
                dense = bool(j and j["status"] == "failed" and "no new stars visible" in str(err or "").lower())
                if dense:
                    # too dense that way (docs: try another direction) — the star isn't used up by a refusal; the next
                    # direction goes out in this same pass
                    run.setdefault("used", []).append(run.get("idx") or 0)
                    run.pop("job", None)
                    run["note"] = f"refused that way: {err}"
                elif j and j["status"] == "failed" and not pr.already_surveyed(err):
                    run.pop("job", None)
                    run["retry_at"] = (_now() + timedelta(minutes=30)).isoformat(timespec="seconds")
                    run["note"] = f"prospect refused: {err}"
                    runs[code] = run
                    continue
                if dense:
                    pass
                elif not pr.already_surveyed(err):
                    if st.startswith("prospecting"):
                        runs[code] = run
                        continue
                    rows = await self.db.fetchall("SELECT payload FROM events WHERE event='prospect.completed' AND device_code=? "
                                                  "AND seq > ?", (code, run.get("seq0") or 0))
                    if not rows and _ts(run.get("at")) and (_now() - _ts(run["at"])).total_seconds() < 6 * 3600:
                        runs[code] = run
                        continue   # finished but the event isn't in yet
                    before = set(run.get("known") or [])   # the stars known when this prospect started
                    found = set()
                    for r in rows:
                        found |= {(x.get("designation") if isinstance(x, dict) else x)
                                  for x in json.loads(r["payload"] or "{}").get("stars") or []}
                    new = len(found - before - {None})
                    run["found"] = int(run.get("found") or 0) + new
                else:
                    new = 0
                if not dense:
                    # docs + live (2026-10-08): a star can be prospected from only once — after one prospect (or
                    # "already been surveyed"), every direction is used up there
                    run["used"] = list(range(pr.N_DIRECTIONS))
                    pf = await self.db.kv_get("prospected_from", {}) or {}
                    pf[here] = {"at": now_iso(), "by": code, "found": new,
                                "outcome": "already surveyed" if pr.already_surveyed(err) else f"{new} new star(s)"}
                    await self.db.kv_set("prospected_from", pf)
                    run.pop("job", None)
            moving = (lmoves.get("moves") or {}).get(code)
            leaving = (bool(f.get("next_home")) or (bound_for(o, set(pos), {x["id"]: x.get("home") for x in fleet_list}) not in (None, here))
                       or bool(moving and moving != here and lfresh))
            nxt = pr.next_index(run)
            run["done"] = nxt is None
            runs[code] = run
            if code in busy or (_ts(run.get("retry_at")) and _now() < _ts(run["retry_at"])):
                continue
            if moving and moving != here and lfresh:
                run["note"] = f"loadouts is moving it to {moving}"
                continue   # loadouts compacts and carries it; nothing for this pass to do
            if (f.get("mission") or {}).get("status") in ("running", "stalled", "stopped"):
                run["note"] = "its fleet is on a mission"
                continue   # the mission packs up / unloads the observatory; nothing for this pass to do
            if run["done"] and f.get("role") == "explore" and f.get("advance") and here == f.get("home") and not leaving:
                # advance along the heading: the farthest star ahead becomes the fleet's home; the loadout pass packs up
                # and carries everyone there (the observatory compacts for the trip), leaving a relay and a beacon behind
                if await self._advance_explorer(f, fleet_list, devices, pos, out):
                    fleets_changed = True
                    continue
            if leaving or run["done"]:   # pack up: the fleet moves on, or nothing is left to find here
                if not compacted(o) and "compact" in (o.get("available_commands") or ["compact"]) \
                        and not st.startswith(("prospecting", "unfurling")):
                    why = "the fleet is leaving" if leaving else f"every direction tried in {here}"
                    if await self.create_job("observatory", f"{f['name']}: compact observatory {code} ({why})", code,
                                             [compact_step(code, bps, o, f"({why})")], {"devices": [code]}):
                        out.append(f"{code}: compacting ({why})")
                continue
            if st.startswith(("compacting", "unfurling", "prospecting")):
                continue   # busy (a prospect started by hand counts too)
            if folded(o) or st.startswith("compact"):
                if await self.create_job("observatory", f"{f['name']}: unfurl observatory {code} to prospect", code,
                                         [unfurl_step(code)], {"devices": [code]}):
                    out.append(f"{code}: unfurling")
                continue
            if "prospect" not in (o.get("available_commands") or []):
                continue
            dirs = pr.directions(pr.base_heading((f.get("heading") or {}).get("vector"), pa.xyz(pos.get(here))))
            if not (f.get("heading") or {}).get("vector"):
                # one prospect per star: emptiest hemisphere first (a fleet's set heading still goes first)
                dirs = pr.rank_directions(dirs, pa.xyz(pos.get(here)), await self.star_lookup("xyz"))
            v = dirs[nxt]
            seq0 = (await self.db.fetchone("SELECT max(seq) AS s FROM events") or {"s": 0})["s"] or 0
            job = await self.create_job("observatory", f"{f['name']}: prospect from {here} (direction {nxt + 1} of {len(dirs)})",
                                        code, [step(f"{code}: prospect {v}", f"/devices/{code}", {"command": "prospect", "direction": v})],
                                        {"devices": [code]})
            if job:
                from .census import observatory_stars
                found_so_far, unplaced = await observatory_stars(self.db)
                run.update({"idx": nxt, "job": job["id"], "seq0": seq0, "at": now_iso(),
                            "known": sorted(set(pos) | set(found_so_far) | set(unplaced))})
                run.pop("note", None)
                out.append(f"{code}: prospecting from {here}, direction {nxt + 1}")
        await self.db.kv_set("observatory_runs", runs)
        if fleets_changed:
            await self.save_fleets(fleet_list)
        return out

    async def prospector_pass(self) -> list[str]:
        """Fleets with role "prospector" (an observatory and a carrier): tour the stars within `radius` ly (30) of the fleet's
        home, one prospect each (observatory_pass prospects and compacts), then the carrier takes the observatory to the next
        star — the emptiest neighborhood not prospected from yet, nearer first, commandable. A tour that hasn't moved on or
        found anything for STALL_HOURS raises a warning (once per stall)."""
        from . import fleets as fl
        from . import pathing as pa
        from . import prospecting as pr
        from .modular import folded
        STALL_HOURS = 12
        fleet_list = await self.fleets()
        mine = [f for f in fleet_list if f.get("role") == "prospector" and f.get("home")]
        if not mine:
            return []
        devices = await self.devices()
        jobs = {j["id"]: j for j in await self.jobs()}
        busy = self.busy_devices(list(jobs.values()))
        stars = await self.star_lookup("by")      # shared: read only
        xyz_all = await self.star_lookup("xyz")
        runs = await self.db.kv_get("observatory_runs", {}) or {}
        pfrom = await self.db.kv_get("prospected_from", {}) or {}
        allowed = await self.commandable(devices)
        out, changed = [], False
        for f in mine:
            if f.get("station"):
                f["station"] = False   # it tours: the loadout pass doesn't keep it at home
                changed = True
            tour = f.setdefault("tour", {"since": now_iso()})
            ms = fl.members(f, devices)
            obs = next((d for d in ms if d.get("device_type") == "galactic_observatory"), None)
            car = next((d for d in ms if fl.is_carrier(d) and fl.capacity(d) > 0), None)
            if not obs or not car:
                tour["note"] = "needs a galactic observatory and a carrier with an attach point"
                continue
            j = jobs.get(tour.get("job"))
            if j and j["status"] in ("running", "waiting"):
                continue
            if j and j["status"] == "failed":
                err = next((s.get("error") for s in j["steps"] if s.get("status") == "failed"), "")
                tour["note"] = f"last move failed: {err}"
            tour.pop("job", None)
            here = star_of(obs.get("location") or "")
            if here and here != tour.get("star"):
                tour.update({"star": here, "since": now_iso(), "alerted": False})
                tour.pop("note", None)   # "moving to <here>" is over: a later stall alert names what's wrong here
                changed = True
            if here and (pfrom.get(here) or {}).get("at", "") > (tour.get("since") or ""):
                tour["since"] = pfrom[here]["at"]   # a prospect here counts as progress
            # moving on: the observatory is folded and done with this star (one prospect each)
            run = runs.get(obs["device_code"]) or {}
            ready = here and obs.get("location") and folded(obs) and (here in pfrom or (run.get("star") == here and run.get("done")))
            if ready and obs["device_code"] not in busy and car["device_code"] not in busy:
                taken = {(x.get("tour") or {}).get("target") for x in mine if x is not f} | \
                        {(x.get("tour") or {}).get("star") for x in mine if x is not f}
                nxt, left = pr.next_star(here, f["home"], xyz_all, set(pfrom), taken - {None}, allowed,
                                         float(f.get("radius") or 30))
                tour["left"] = left
                if not nxt:
                    tour["note"] = f"no star left to prospect within {f.get('radius') or 30} ly of {f['home']}"
                    if not tour.get("alerted"):
                        tour["alerted"] = True
                        await self._notify("warning", f"{f['name']}: {tour['note']}", "/fleets")
                    continue
                dest = fl.destination(nxt, stars)
                steps = []
                # every other member comes along (live 2026-10-16: the matrix containers stayed at FALQUORYX-BELT-1
                # while the surge platforms towed the observatories away): the carrier collects them first
                cc = car["device_code"]
                free = fl.seats(car, devices)
                if obs.get("attached_to_device_code") != cc:
                    free["attach"] -= 1   # kept for the observatory
                strays: dict[str, list[tuple[str, str]]] = {}
                no_room = []
                for d in ms:
                    if d is obs or d is car or cc in (d.get("stowed_in_device_code"), d.get("attached_to_device_code")):
                        continue
                    host = next((x for x in devices if x.get("device_code") in
                                 (d.get("stowed_in_device_code"), d.get("attached_to_device_code"))), None)
                    loc = d.get("location") or (host or {}).get("location")
                    mode = fl.take_seat(free, d) if loc and not host else None
                    if mode:
                        strays.setdefault(loc, []).append((d["device_code"], mode))
                    else:
                        no_room.append(d["device_code"])
                at = car.get("location")
                if strays:
                    tour_steps, at = fl.pickup_tour(cc, at, strays)
                    steps += tour_steps
                if at != obs.get("location"):   # the carrier comes to the observatory
                    steps += [step(f"{cc} → {obs['location']}", f"/devices/{cc}",
                                   {"command": "travel", "destination": obs["location"]}, critical=True),
                              fl._wait_arrive(cc, obs["location"])]
                steps += [fl.board_step(car["device_code"], obs["device_code"], "attach"),
                          step(f"{car['device_code']} → {dest}", f"/devices/{car['device_code']}",
                               {"command": "travel", "destination": dest}, critical=True),
                          fl._wait_arrive(car["device_code"], dest, nxt),
                          step(f"{car['device_code']}: detach {obs['device_code']}", f"/devices/{car['device_code']}",
                               {"command": "detach", "device": obs["device_code"]}, critical=True)]
                job = await self.create_job("fleets", f"{f['name']}: observatory {here} → {nxt} (prospecting tour)",
                                            cc, steps, {"devices": [cc, obs["device_code"]] + [c for g in strays.values()
                                                                                                for c, _ in g]})
                if job:
                    tour.update({"job": job["id"], "target": nxt, "note": f"moving to {nxt} ({left} star(s) left in range)"
                                 + (f"; picking up {', '.join(c for g in strays.values() for c, _ in g)} first" if strays else "")
                                 + (f"; can't take {', '.join(no_room)} (no room or aboard another device)" if no_room else "")})
                    out.append(f"{f['name']}: {here} → {nxt}")
                    changed = True
                continue
            # stalled: nothing found or moved for too long
            since = _ts(tour.get("since"))
            if since and (_now() - since).total_seconds() > STALL_HOURS * 3600 and not tour.get("alerted"):
                tour["alerted"] = True
                await self._notify("warning", f"{f['name']}: the prospecting tour has stalled at {here or '?'} for "
                                              f"{STALL_HOURS} h+ — {tour.get('note') or (runs.get(obs['device_code']) or {}).get('note') or 'check the observatory'}",
                                   "/fleets")
                changed = True
        if changed:
            await self.save_fleets(fleet_list)
        return out

    async def dark_home(self, f: dict, star: str, devices: list[dict]) -> str | None:
        """Why a fleet can't move its home to `star`, or None: nothing there could be commanded on arrival (no active
        relay network covers it and no replicant rides with the fleet). Live 2026-10-19: Miner_4 relocated to OXETH,
        which neither, and all 30 devices aboard 15DFB014 went out_of_range before they could be unloaded."""
        from . import fleets as fl
        star = star_of(star)
        if not star or star in await self.commandable(devices):
            return None
        if fl.fleet_replicant(f, devices, await self.db.kv_get("replicants", {}) or {}):
            return None
        return (f"{star} is outside an active relay network and no replicant rides with the fleet: nothing could be "
                f"commanded there on arrival — get a relay to {star} (or put a replicant aboard) first")

    async def start_relocation(self, f: dict, old: str, new: str, devices: list[dict] | None = None) -> str:
        """A stationed explore fleet whose home just changed moves there together, in one relocation mission (pack up —
        large devices compacted — everyone aboard, gather strays, travel, unload), when its carriers' hold slots and
        attach points cover every rider. Otherwise, and always for a mining fleet (owner 2026-10-21: no assembly stage),
        the loadout pass carries it piecemeal: its own and the logistics fleets' carriers each take as many as they hold
        and leave as they fill. Returns what happens, for the log."""
        from . import fleets as fl
        from .shapes import normalize_blueprints
        devices = devices if devices is not None else await self.devices()
        # a new home never waits for a complete loadout (owner 2026-10-21): the loadout pass fills the fleet there
        dark = await self.dark_home(f, new, devices) if old and new and old != new \
            and (f.get("mission") or {}).get("status") not in ("running", "stalled", "stopped") else None
        if dark:
            f["home"], f["pending_home"] = old, new   # it stays; pending_home_pass moves it once it can be commanded there
            return f"{new} is pending: {dark}"
        f.pop("pending_home", None)
        if old and new and old != new and f.get("materials") == "self":
            # a destination fleet (takes materials in): what's stockpiled at its old home follows it (logistics.py); any
            # other fleet's old piles are just stray ones, returned to the nearest gather point
            from .logistics import KV_MOVES
            moves = await self.db.kv_get(KV_MOVES, {}) or {}
            moves[old] = {"to": new, "fleet": f.get("id"), "at": now_iso()}
            await self.db.kv_set(KV_MOVES, moves)
        solo = fl.solo_flyers(f, devices)
        here = fl.together(f, devices, prefer=old, free=solo)
        start = here["at"] if here["ready"] else old   # where it's gathered: it leaves from there
        if f.get("role") != "explore" or not f.get("station") or old == new \
                or (f.get("mission") or {}).get("status") in ("running", "stalled", "stopped"):
            f.pop("move_partial", None)
            return "the loadout pass moves it"
        bps = {b["device_type"]: b for b in normalize_blueprints(await self.db.kv_get("blueprints", []))}
        if not [d for d in fl.members(f, devices) if star_of(d.get("location")) != new]:
            return "nothing to move"
        short = fl.attach_points(f, devices, bps)["now"]["short"]
        if short > 0:
            return f"its carriers are {short} seat(s) short of carrying everyone, so the loadout pass moves it piecemeal"
        m = {"status": "running", "phase": None, "idx": 0, "targets": [new], "started_at": now_iso(), "log": [],
             "opts": {"partial": bool(f.pop("move_partial", False))}, "auto": True, "relocate": True, "from": start,
             "solo": sorted(solo)}
        self._mlog(m, f"relocating {start} → {new}: pack up, everyone aboard, travel together, unload"
                      + (f"; {', '.join(sorted(solo))} fly there on their own after their current task" if solo else ""))
        f["mission"] = m
        return "moving together in one relocation mission (everyone aboard before anything leaves)"

    async def pending_home_pass(self) -> list[str]:
        """Fleets with a new home nothing could be commanded at yet (start_relocation): move once it can be."""
        from . import fleets as fl
        items = await self.fleets()
        waiting = [f for f in items if f.get("pending_home")]
        if not waiting:
            return []
        devices = await self.devices()
        out = []
        for f in waiting:
            new = f["pending_home"]
            dark = await self.dark_home(f, new, devices)
            if dark:
                note = f"{new} pending: {dark}"
                if f.get("path_note") != note:
                    f["path_note"] = note
                continue
            old = f["home"]
            f["home"] = new
            f.pop("pending_home", None)
            how = await self.start_relocation(f, old, new, devices)
            f["path_note"] = f"moving home {old} → {new} — {how}"
            out.append(f"{f['name']}: {old} → {new}")
            await self.log("fleets", f"{f['name']}: {new} reachable — moving home {old} → {new}: {how}", notify=True)
        await self.save_fleets(items)
        return out

    async def _advance_explorer(self, f: dict, fleets: list[dict], devices: list[dict], pos: dict, out: list[str]) -> bool:
        """Move an advancing explore fleet's home to the farthest star ahead (pathing.advance_target). Without a replicant
        aboard only stars inside relay coverage qualify (arrivals outside it couldn't be commanded). A relay and a beacon
        of the fleet's in the old system stay there (fleet tag off, pinned where they are) when there's none of ours
        besides; a fleet without them just moves. True when it moved."""
        from . import fleets as fl
        from . import pathing as pa
        from . import prospecting as pr
        from .loadouts import at_tag
        home = f["home"]
        xyz_of = {k: v for k, v in ((k, pa.xyz(p)) for k, p in pos.items()) if v}
        heading = (f.get("heading") or {}).get("vector") or pr.base_heading(None, xyz_of.get(home))
        riding = bool(fl.fleet_replicant(f, devices, await self.db.kv_get("replicants", {}) or {}))
        skip = set(pa.taken(f, fleets)) | set(f.get("advanced_from") or [])
        reach = await self.commandable(devices, {k: {"position": v} for k, v in pos.items()}) if not riding else set()
        nxt = pa.advance_target(home, xyz_of, heading, float(f.get("cone") or pa.DEFAULT_CONE),
                                float(f.get("hop_ly") or 30), skip,
                                (lambda s: True) if riding else (lambda s: s in reach))
        if not nxt:
            note = (f"nothing ahead within {f.get('hop_ly') or 30:g} ly" + ("" if riding else " inside relay coverage")
                    + " — staying in " + home)
            if f.get("path_note") != note:
                f["path_note"] = note
                await self.log("fleets", f"{f['name']}: {note}", notify=True)
                return True
            return False
        tag = fl.fleet_tag(f["id"])
        steps = []
        for t in ("ftl_relay", "ftl_beacon"):
            mine_here = [d for d in devices if d.get("device_type") == t and star_of(d.get("location")) == home
                         and not d.get("stowed_in_device_code")]
            ours = [d for d in mine_here if tag in (d.get("tags") or [])]
            if ours and len(mine_here) == len(ours):   # no other one of ours there: leave this one behind
                d = ours[0]
                steps.append(step(f"{d['device_code']}: stays in {home}", f"/devices/{d['device_code']}",
                                  {"configuration": {"add_tags": [at_tag(d["location"])], "remove_tags": [tag]}}, method="PATCH"))
        if steps:
            await self.create_job("fleets", f"{f['name']}: leave a relay / beacon in {home}", None, steps, {"devices": []})
        f.setdefault("advanced_from", []).append(home)
        f["prev_home"], f["home"], f["station"] = home, nxt, True
        f["path_note"] = f"advancing from {home} to {nxt} (farthest star ahead within {f.get('hop_ly') or 30:g} ly)"
        # one relocation mission: compact the observatory and board everyone (nothing leaves until all are aboard —
        # far out there's no relay, so anything left behind couldn't be commanded once the replicant goes), fly together,
        # unload at the new home. The loadout pass leaves a fleet on a mission alone.
        how = await self.start_relocation(f, home, nxt, devices)
        f["path_note"] += f" — {how}"
        out.append(f"{f['name']}: advancing {home} → {nxt}")
        await self.log("fleets", f"{f['name']}: every direction prospected from {home} — advancing to {nxt}", notify=True)
        return True

    SCOUT_MIN_CAPACITY = 50.0   # a member at or below this (%) ends the scouting: finish the system, go home for repairs
    SCOUT_START_CAPACITY = 85.0  # a run starts only with every member at or above this (%)
    SCOUT_RADIUS_LY = 100.0     # no further than this from home; nothing left inside it ends the scouting too

    async def scout_next(self, f: dict, items: list[dict], devices: list[dict], here: str | None = None,
                         exclude: set[str] | frozenset = frozenset()) -> tuple[str | None, str]:
        """The next star an auto-scouting fleet surveys: one not surveyed yet (scanned, every body but the belts
        surveyed), nearest its home first and then nearest where it is — so it works outward ring by ring through
        adjacent systems. Without a replicant riding along: only systems in our relay coverage. Not another player's
        warded system, another fleet's home, or a star another fleet is heading to. (star, why-none)."""
        from . import fleets as fl
        from . import pathing as pa
        from . import wards
        from .census import observatory_stars
        cat = await self.db.kv_get("stars", {}) or {}
        pos = {x.get("designation"): x.get("position") for x in cat.get("stars") or [] if isinstance(x, dict)}
        found, _ = await observatory_stars(self.db)
        for k, v in found.items():
            pos.setdefault(k, v.get("position"))
        xyz_of = {k: v for k, v in ((k, pa.xyz(p)) for k, p in pos.items()) if v}
        home = f.get("home") or ""
        if home not in xyz_of:
            return None, f"{home or 'its home'} has no position in the star catalog"
        scans = {r["star"]: r["data"] for r in await self.db.fetchall("SELECT star, data FROM systems")}
        surveyed = await self.db.kv_get("surveyed", {}) or {}
        busy = {star_of(t) for g in items if g.get("id") != f.get("id") and (g.get("mission") or {}).get("status") in ("running", "stalled")
                for t in (g["mission"].get("targets") or [])}
        skip = busy | (fl.worked_systems(items) - {home}) | wards.foreign(cat, devices) | set(exclude)
        riding = bool(fl.fleet_replicant(f, devices, await self.db.kv_get("replicants", {}) or {}))
        reach = await self.commandable(devices, {k: {"position": v} for k, v in pos.items()}) if not riding else set()

        def done(star: str) -> bool:
            if star not in scans:
                return False
            try:
                scan = json.loads(scans[star] or "{}")
            except ValueError:
                return False
            return not survey_targets(scan, surveyed, False, False, 1)   # every body but the belts surveyed
        here_xyz = xyz_of.get(here or home) or xyz_of[home]
        cands = []
        for s, p in xyz_of.items():
            if s in skip or done(s) or (not riding and s not in reach):
                continue
            dh, dc = math.dist(p, xyz_of[home]), math.dist(p, here_xyz)
            if dh > self.SCOUT_RADIUS_LY:
                continue
            cands.append((round(dh / 2.0), dc, s))   # 2-ly rings outward from home; within a ring, the nearest hop first
        if not cands:
            return None, (f"every known system within {self.SCOUT_RADIUS_LY:g} ly of {home} "
                          + ("" if riding else "inside relay coverage ") + "is surveyed or taken")
        return min(cands)[2], ""

    async def system_surveyed(self, star: str) -> bool:
        """Every body but the belts surveyed (scout_next's test), or an AMI survey_system reported nothing left."""
        star = star_of(star)
        if star in (await self.db.kv_get("ami_surveyed", {}) or {}):
            return True
        row = await self.db.fetchone("SELECT data FROM systems WHERE star=?", (star,))
        if not row:
            return False
        try:
            scan = json.loads(row["data"] or "{}")
        except ValueError:
            return False
        return not survey_targets(scan, await self.db.kv_get("surveyed", {}) or {}, False, False, 1)

    def scout_unhealthy(self, f: dict, devices: list[dict], start_at: float | None = None) -> list[str]:
        """Members at or below the in-run limit (50 %), or — to start a run — below `start_at` (85 %)."""
        from . import fleets as fl
        from .loadouts import _cap
        return [d["device_code"] for d in fl.members(f, devices)
                if (_cap(d) < start_at if start_at is not None else _cap(d) <= self.SCOUT_MIN_CAPACITY)]

    async def deploy_for_repairs(self, f: dict, weak: list[str], devices: list[dict]) -> list[str]:
        """Only deployed devices get repaired: deploy the fleet's worn members still stowed in (or attached to) a vessel
        parked in its home system. Readiness counts deployed members at home, so the next run takes them aboard again."""
        from . import fleets as fl
        by = {d.get("device_code"): d for d in devices}
        home, out = f.get("home") or "", []
        for code in weak:
            d = by.get(code) or {}
            host = by.get(d.get("stowed_in_device_code") or d.get("attached_to_device_code") or "")
            if (not host or not home or star_of(host.get("location")) != home or fl.in_flight(host)
                    or str(host.get("status") or "").lower().startswith(("travel", "cruis", "surg"))):
                continue
            ok, _resp, err = await self.send("POST", f"/devices/{code}", {"command": "deploy"},
                                             f"auto: deploy {code} for repairs ({f.get('name') or f.get('id')})")
            out.append(f"{f.get('name') or f.get('id')}: deployed {code} for repairs" if ok
                       else f"{f.get('name') or f.get('id')}: deploying {code} for repairs failed: {err}")
        return out

    async def repair_return_pass(self) -> list[str]:
        """Mining and survey fleets (auto-scouting ones have their own rule): a member at or below 50 % sends the fleet
        home — a mission in its watch phase recalls now (then returns and unloads as usual). Back home, members below
        85 % still aboard a vessel there are deployed, once each, so the maintenance drone can repair them."""
        items = await self.fleets()
        devices = await self.devices()
        out, changed = [], False
        for f in items:
            if f.get("role") not in ("mining", "explore"):
                continue
            m = f.get("mission") or {}
            if m.get("status") in ("running", "stalled"):
                if m.get("scouting") or m.get("repair_return") or m.get("status") != "running" or m.get("end_here"):
                    continue
                weak = self.scout_unhealthy(f, devices)
                if weak and m.get("phase") == "watch":
                    m["recall_now"] = m["repair_return"] = True
                    if f["role"] == "explore":
                        m["targets"] = (m.get("targets") or [])[:m.get("idx", 0) + 1]   # no further targets
                    why = f"{', '.join(weak)} at or below {self.SCOUT_MIN_CAPACITY:g} %"
                    self._mlog(m, f"returning home for repairs ({why})")
                    await self.log("fleets", f"{f.get('name')}: returning home for repairs ({why})", notify=True)
                    out.append(f"{f.get('name')}: home for repairs")
                    changed = True
                continue
            if f.get("auto_scout"):
                continue   # auto_scout_pass deploys those
            weak = self.scout_unhealthy(f, devices, self.SCOUT_START_CAPACITY)
            tried = set(f.get("repair_deploys") or [])
            sent = await self.deploy_for_repairs(f, [c for c in weak if c not in tried], devices) if weak else []
            if sent:
                out += sent
                f["repair_deploys"] = sorted(tried | {c for c in weak if any(c in x for x in sent)})
                changed = True
            elif not weak and f.get("repair_deploys"):
                f["repair_deploys"] = []
                changed = True
        if changed:
            await self.save_fleets(items)
        return out

    async def auto_scout_pass(self) -> list[str]:
        """Explore fleets with 'auto-scout' ticked: when one is free and every member is above 50 % capacity, send it
        to the next unsurveyed system (scout_next); each time it finishes a system it takes the next one, until a member
        is down to 50 % — then it finishes that system and returns home for repairs. A run starts only with every member at
        85 % or more, and only with its loadout complete at home."""
        from . import fleets as fl
        items = await self.fleets()
        free = [f for f in items if f.get("role") == "explore" and f.get("auto_scout")
                and (f.get("mission") or {}).get("status") not in ("running", "stalled", "stopped")]
        if not free:
            return []
        devices = await self.devices()
        out, changed = [], False
        for f in free:
            weak = self.scout_unhealthy(f, devices, self.SCOUT_START_CAPACITY)
            if weak:
                tried = set(f.get("repair_deploys") or [])
                sent = await self.deploy_for_repairs(f, [c for c in weak if c not in tried], devices)
                if sent:   # once per worn member: the device list may lag the deploy
                    out += sent
                    f["repair_deploys"], changed = sorted(tried | {c for c in weak if any(c in x for x in sent)}), True
                note = f"waiting for repairs: {', '.join(weak)} below {self.SCOUT_START_CAPACITY:g} % capacity"
                if f.get("scout_note") != note:
                    f["scout_note"], changed = note, True
                continue
            if f.get("repair_deploys"):
                f["repair_deploys"], changed = [], True
            r = fl.readiness(f, devices)
            if not r["ready"]:   # nothing leaves short: the loadout pass fills it at home first
                note = f"waiting: {fl.readiness_text(r)}"
                if f.get("scout_note") != note:
                    f["scout_note"], changed = note, True
                continue
            star, why = await self.scout_next(f, items, devices)
            if not star:
                if f.get("scout_note") != why:
                    f["scout_note"], changed = why, True
                continue
            m = {"status": "running", "phase": None, "idx": 0, "targets": [star], "started_at": now_iso(), "log": [],
                 "opts": {}, "auto": True, "scouting": True}
            self._mlog(m, f"started automatically: scouting {star} (nearest unsurveyed system)")
            f["mission"], f["scout_note"], changed = m, f"scouting from {f.get('home')}", True
            out.append(f"{f['name']}: scouting {star}")
            await self.log("fleets", f"{f['name']}: scouting {star} (auto)")
        if changed:
            await self.save_fleets(items)
        return out

    async def decommission_queue_pass(self) -> list[str]:
        """Devices asked to be decommissioned at an autofactory (decommission.py): once one is idle at its
        autofactory, decommission it there (the autofactory learns its blueprint)."""
        from . import decommission as dc
        q = await self.db.kv_get(dc.KV, {}) or {}
        if not q:
            return []
        devices = await self.devices()
        by = {d.get("device_code"): d for d in devices}
        busy = self.busy_devices(await self.jobs())
        out = []
        for code, e in list(q.items()):
            d = by.get(code)
            if not d:
                q.pop(code)   # gone from the list: decommissioned (or given away)
                continue
            if code in busy:
                continue
            sent = _ts(e.get("sent_at"))
            if sent and (_now() - sent).total_seconds() > 600 and not await self.db.fetchone(
                    "SELECT 1 FROM events WHERE event='device.decommissioned' AND device_code=?", (code,)):
                # its job ended (failed, timed out, canceled) and the device is still here: try again
                e.pop("sent_at", None)
                await self.log("decommission", f"{code}: still here after its decommission job ended — trying again")
            if not dc.ready(d, e):
                continue
            st = step(f"{code}: decommission at autofactory {e['factory']}", f"/devices/{code}", {"command": "decommission"},
                      wait=["device.decommissioned"], timeout=3600, critical=True)
            st["wait_device"] = code
            job = await self.create_job("decommission", f"{code} ({d.get('device_type')}): decommission at autofactory "
                                        f"{e['factory']} ({e['at']})", code, [st], {"devices": [code]})
            if job:
                e["sent_at"] = now_iso()
                out.append(f"{code} → decommission at {e['at']}")
        await self.db.kv_set(dc.KV, q)
        return out

    # --- loadouts --------------------------------------------------------------------------------------
    async def loadout_cfg(self) -> dict:
        """Templates, settings and ignore tags (kv "loadouts"), plus `fleets`: every fleet with its template resolved.
        The first call after an upgrade turns the old home fleets and materials roles into fleets (fleets.migrate)."""
        from . import fleets as fl
        from .ami_schedule import set_stationed
        from .loadouts import normalize
        raw = await self.db.kv_get("loadouts", {}) or {}
        items = await self.db.kv_get("fleets", []) or []
        if not raw.get("fleets_migrated"):
            cat = await self.db.kv_get("stars", {}) or {}
            pos = {x.get("designation"): x.get("position") or {} for x in (cat.get("stars") or []) if isinstance(x, dict)}
            had = {f.get("id") for f in items}
            raw, items, changed = fl.migrate(raw, items, pos)
            if changed:
                await self.db.kv_set("fleets", items)
                await self.db.kv_set("loadouts", raw)
                for f in items:
                    if f["id"] not in had:
                        await self.log("fleets", f"home fleet of {f['home']} is now the stationed fleet {f['name']} "
                                                 f"(fleet:{f['id']}); its devices get the fleet tag on the next loadout pass")
        cfg = normalize(raw)
        cfg["fleets"] = [fl.resolve_template(dict(f), cfg) for f in items]
        set_stationed({fl.fleet_tag(f["id"]): f["home"] for f in cfg["fleets"] if fl.stationed(f)})
        return cfg

    async def geography(self, devices: list[dict], stars: dict[str, dict]) -> dict[str, dict]:
        """Per system: belts, Lagrange points and inner planets (from its scan, the catalog and device positions)."""
        from . import placement as pl
        scans = {r["star"]: r["data"] for r in await self.db.fetchall("SELECT star, data FROM systems")}
        belts_seen = list((await self.db.kv_get("belt_reads", {}) or {}).keys())
        out = {}
        for star in {star_of(d.get("location")) for d in devices if d.get("location")}:
            try:
                scan = json.loads(scans[star]) if star in scans else None
            except ValueError:
                scan = None
            out[star] = pl.geography(star, devices, scan, (stars.get(star) or {}).get("entry_point"), belts_seen)
        return out

    async def cruise_radii(self) -> dict[str, float]:
        """Distance from its star (AU) of every place the stored system scans name — to tell long cruises from short."""
        from .fleets import system_radii
        out: dict[str, float] = {}
        for r in await self.db.fetchall("SELECT data FROM systems"):
            try:
                out.update(system_radii(json.loads(r["data"])))
            except ValueError:
                pass
        return out

    async def component_pass(self) -> list[str]:
        """Factories waiting for component devices (components.py): spares are pinned to the factory's location, the
        rest printed at another autofactory, with the waiting item's tags. Every 5 minutes."""
        from . import components as comp
        last = _ts(await self.db.kv_get("components_last", None))
        if last and (_now() - last).total_seconds() < 300:
            return []
        await self.db.kv_set("components_last", now_iso())
        devices = await self.devices()
        gaps = comp.shortfalls(devices)
        if not gaps:
            return []
        busy = self.busy_devices(await self.jobs())
        blocked = {g["factory"] for g in gaps}
        out = []
        for g in gaps:
            pinned, printed, note = await self.pin_or_print(g["type"], g["short"], g["location"], devices, busy,
                                                            f"components for {g['factory']}", tags=g["tags"], not_at=blocked)
            what = ", ".join(x for x in [f"{len(pinned)} spare(s) sent" if pinned else "",
                                         f"{printed} queued to print" if printed else ""] if x)
            if what:
                out.append(f"{g['factory']} needs {g['short']} {g['type'].replace('_', ' ')}: {what}")
                await self.log("components", out[-1])
            elif note:
                await self.log("components", f"{g['factory']} needs {g['short']} {g['type'].replace('_', ' ')}: {note}")
        return out

    async def commandable(self, devices: list[dict], stars: dict | None = None, without: set[str] | None = None,
                          networked: bool = False) -> set[str]:
        """Systems you can give commands in: an active relay network's coverage (relaynet.coverage), or where one of your
        replicants is (other than the replicants in `without`). `networked`: the network's coverage only, without the
        systems a replicant covers just by being there (it may leave)."""
        from . import relaynet
        if stars is None:
            pos = await self.star_lookup("pos")   # shared, positions only
        else:
            pos = {k: (v or {}).get("position") for k, v in stars.items() if (v or {}).get("position")}
        reps = await self.db.kv_get("replicants", {}) or {}
        rstars = relaynet.replicant_stars({c: r for c, r in reps.items() if c not in (without or ())}, devices)
        from .shapes import normalize_blueprints
        bps = {b["device_type"]: b for b in normalize_blueprints(await self.db.kv_get("blueprints", []))}
        net = relaynet.networks(devices, pos, bps)
        cov = relaynet.coverage(net, pos, rstars)
        cover = set().union(*(cov["covered"][i] for i in cov["active"])) if networked else cov["commandable"]
        return cover - relaynet.dark_stars(devices)

    async def evacuation_inputs(self) -> tuple[dict, set[str], dict]:
        """(tracked objects, objects the game warned us about in the last two hours, system scans by star)."""
        import json as _json
        objs = await self.defence_objects()
        # warned in the last two hours (defence_objects files each warning under its asteroid's designation)
        warned = {des for des, o in objs.items() if _ts(o.get("warned_at")) and (_now() - _ts(o["warned_at"])).total_seconds() < 7200}
        scans = {}
        for r in await self.db.fetchall("SELECT star, data FROM systems"):
            try:
                scans[r["star"]] = _json.loads(r["data"] or "{}")
            except ValueError:
                pass
        return objs, warned, scans

    async def evacuation_targets(self, devices: list[dict], stars: dict) -> dict[str, str]:
        """{object location: where its stockpile goes} for every object due within the evacuation window."""
        from . import evacuate as ev
        objs, warned, scans = await self.evacuation_inputs()
        hot = ev.due(objs, warned=warned, pct=await self.diverted_pct())
        obj_at = ev.at_location(objs)
        from .rockmining import open_rocks   # v3.6.0: a rock being mined has its pile hauled out while the rigs work
        worked = {s.get("rock") for s in (await self.db.kv_get("rock_mining", {}) or {}).values()} & set(open_rocks(objs))
        hot = {**{r: 0.0 for r in worked}, **hot}
        return {loc: ev.safe_spot(star_of(loc), (obj_at.get(loc) or {}).get("impact_target"), stars, scans.get(star_of(loc)),
                                  here=loc, avoid=set(hot))
                for loc in hot}

    async def evacuate_pass(self, force: bool = False) -> list[str]:
        """Evacuation before impact (evacuate.py): devices at an object due within the hour leave for a safe spot in
        the system (propulsors still pushing at the rule's leave_minutes, 45), grounded ones board a carrier there; piles go via logistics."""
        from . import evacuate as ev
        from . import loadouts as lo
        objs, warned, scans = await self.evacuation_inputs()
        pct = await self.diverted_pct()
        if not ev.due(objs, warned=warned, pct=pct):
            return []
        devices = await self.devices()
        cat = await self.db.kv_get("stars", {}) or {}
        stars = {x.get("designation"): x for x in cat.get("stars") or [] if isinstance(x, dict)}
        reps = await self.db.kv_get("replicants", {}) or {}
        hosts = {r.get("hosted_device_code") for r in reps.values() if r.get("hosted_device_code")}
        busy = self.busy_devices(await self.jobs())
        try:
            lv = float(((await self.settings())["rules"].get("asteroid_defence") or {}).get("leave_minutes") or ev.DIVERTER_MINUTES)
        except (TypeError, ValueError):
            lv = ev.DIVERTER_MINUTES
        p = ev.plan(objs, devices, stars, busy, hosts, scans, warned=warned, pct=pct,
                    diverter_minutes=min(max(lv, 30.0), ev.EVAC_MINUTES))
        from . import fleets as fl
        out = []
        boards: dict[str, list[dict]] = {}
        for b in p["boards"]:
            boards.setdefault(b["carrier"], []).append(b)
        for m in p["moves"]:
            code = m["code"]
            steps = [fl.board_step(code, b["code"], b["mode"]) for b in boards.get(code, [])]
            if m.get("deactivate"):
                steps.append(step(f"{code}: deactivate", f"/devices/{code}", {"command": "deactivate"}))
            steps.append(step(f"{code} → {m['to']} (evacuate {m['from']}: {m['why']})", f"/devices/{code}",
                              {"command": "travel", "destination": m["to"]}, critical=True))
            codes = [code] + [b["code"] for b in boards.get(code, [])]
            by = {d["device_code"]: d for d in devices}
            for c in codes:   # an at: pin to the object would send it straight back (loadouts pins)
                pin = lo.at_tag(m["from"])
                if pin in ((by.get(c) or {}).get("tags") or []):
                    steps.append(lo.tag_step(c, remove=[pin]))
            if await self.create_job("evacuate", f"evacuate {code} from {m['from']} → {m['to']}", code, steps, {"devices": codes}):
                out.append(f"{code}: leaving {m['from']} for {m['to']} ({m['why']})")
        for s_ in p["stuck"]:
            key = f"evac-stuck:{s_['code']}"
            if self.stage_errors.get(key) != s_["why"]:
                self.stage_errors[key] = s_["why"]
                await self.log("evacuate", f"{s_['code']} at {s_['location']} can't be evacuated: {s_['why']}", "alert", notify=True)
        for line in out:
            await self.log("evacuate", line, "alert", notify=True)
        return out

    async def dark_networks(self, devices: list[dict], pos: dict, bps: dict, reps: dict) -> list[set[str]]:
        """What each relay network without a replicant in reach covers: a replicant anywhere in one makes everything it
        covers commandable (logistics rescues)."""
        from . import relaynet
        net = relaynet.networks(devices, pos, bps)
        cov = relaynet.coverage(net, pos, relaynet.replicant_stars(reps, devices))
        return [stars for i, stars in cov["covered"].items() if i not in cov["active"]]

    async def supply_reach(self) -> float:
        """The loadout supply range (ly, Fleets › settings): nothing is fetched from farther."""
        from .loadouts import DEFAULT_SETTINGS
        try:
            v = ((await self.loadout_cfg()).get("settings") or {}).get("max_supply_ly")
            return float(v if v is not None else DEFAULT_SETTINGS["max_supply_ly"])
        except (TypeError, ValueError):
            return float(DEFAULT_SETTINGS["max_supply_ly"])

    async def max_cruise_au(self) -> float:
        from .fleets import MAX_CRUISE_AU
        try:
            return float((await self.loadout_cfg())["settings"].get("max_cruise_au") or MAX_CRUISE_AU)
        except (TypeError, ValueError):
            return MAX_CRUISE_AU

    async def loadout_orders(self) -> list[dict]:
        """Prints ordered for a system and not yet seen as a device (they count as incoming)."""
        orders = await self.db.kv_get("loadout_orders", []) or []
        codes = {d.get("device_code") for d in await self.devices()}
        jobs = {j["id"]: j for j in await self.jobs()}
        now = _now()
        keep = []
        for o in orders:
            if o.get("device_code") and o["device_code"] in codes:
                continue  # the printed device is in the device list now, tagged for its system
            j = jobs.get(o.get("job"))
            if j and (j["status"] == "failed" or (j["steps"] and j["steps"][0]["status"] == "skipped")):
                continue  # the enqueue didn't happen
            at = _ts(o.get("at"))
            if at and (now - at).total_seconds() > 48 * 3600:
                continue
            keep.append(o)
        from .loadouts import reconcile_orders
        keep = reconcile_orders(keep, await self.devices(), now.timestamp())
        if len(keep) != len(orders):
            await self.db.kv_set("loadout_orders", keep)
        return keep

    async def loadout_plan(self, only: set[str] | None = None) -> dict:
        from .loadouts import plan
        from .shapes import normalize_blueprints, normalize_inventory
        cfg = await self.loadout_cfg()
        devices = await self.devices()
        bps = normalize_blueprints(await self.db.kv_get("blueprints", []))
        inv = {i.get("location"): i.get("items") or {} for i in normalize_inventory(await self.db.kv_get("inventory", []))}
        cat = await self.db.kv_get("stars", {}) or {}
        stars = {s.get("designation"): s for s in (cat.get("stars") or []) if isinstance(s, dict)}
        reps = await self.db.kv_get("replicants", {}) or {}
        hosts = {r.get("hosted_device_code"): code for code, r in reps.items() if r.get("hosted_device_code")}
        jobs = await self.jobs()
        busy = self.busy_devices(jobs)
        # a carrier whose delivery just failed (out of comms, mid-surge …) sits out for 30 minutes; one that failed twice
        # with the same cargo in 6 hours sits out the rest of those 6 hours (live 2026-10-20: five plates failed the
        # same attach every half hour)
        fails: dict[tuple, int] = defaultdict(int)
        for j in jobs:
            fin = _ts(j.get("finished_at") or j.get("created_at"))
            if j["rule"] == "loadouts" and j["status"] == "failed" and fin and (_now() - fin).total_seconds() < 6 * 3600:
                fails[(j.get("device"), tuple(sorted(j.get("meta", {}).get("devices", []))))] += 1
        for j in jobs:
            fin = _ts(j.get("finished_at") or j.get("created_at"))
            if j["rule"] != "loadouts" or j["status"] != "failed" or not fin:
                continue
            age = (_now() - fin).total_seconds()
            if age < 1800 or (age < 6 * 3600 and fails[(j.get("device"), tuple(sorted(j.get("meta", {}).get("devices", []))))] >= 2):
                busy.add(j.get("device"))
                busy.update(j.get("meta", {}).get("devices", []))
        # an approved shop trade waiting on logistics keeps one logistics vessel with a replicant aboard free for it, the
        # nearest that can surge: only such a vessel can fulfill it, and a device run can tie it up for hours (owner
        # 2026-10-20). Holding them all kept every fleet carrier towing a replicant off relay runs (owner 2026-10-20).
        # Contracts don't: a pre-approved civilization's are open nearly all the time.
        from . import logistics as lg
        deals = [r for r in ((await self.db.kv_get(lg.KV_STATE, {}) or {}).get("requests") or [])
                 if r.get("kind") == "trade" and not r.get("skipped")]
        if deals:
            from . import fleets as fl
            from .loadouts import can_surge
            ltags = {fl.fleet_tag(f["id"]) for f in await self.fleets() if f.get("role") == "logistics"}
            by = {d.get("device_code"): d for d in devices}
            pos = {k: v.get("position") for k, v in stars.items() if v.get("position")}
            vessels = [c for c in lg.riding_hosts(devices, hosts) if c not in busy and can_surge(by.get(c) or {})
                       and ltags & set((by.get(c) or {}).get("tags") or [])]
            for r in deals:
                to = star_of(r.get("to"))
                pick = min(vessels, key=lambda c: (lg._dist(star_of((by.get(c) or {}).get("location")), to, pos), c), default=None)
                if pick:
                    busy.add(pick)
                    vessels.remove(pick)
        from .loadouts import material_routes
        from .traffic import beacons as _beacons
        civ_locs = {r["location"] for r in await self.civ_coverage() if r.get("completed") or r.get("open")}
        protect = {b["device_code"] for b in _beacons(devices) if b.get("location") in civ_locs}
        p = plan(cfg, devices, bps, inv, stars, hosts, busy, await self.loadout_orders(),
                 await self.db.kv_get("stowed_map", {}) or {}, only, await self.known_open_sites(), protect,
                 await self.geography(devices, stars), await self.commandable(devices, stars))
        current = {}
        live: set[str] = set()
        for d in devices:
            if "transport" not in (d.get("device_type") or ""):
                continue
            if "ami_directive" in d:  # the game says what it's doing: trust that over events and memory
                dirv = d.get("ami_directive") or {}
                state = str(dirv.get("_eval_state") or "")
                current[d["device_code"]] = {"directive": dirv.get("name"), "configuration": dirv.get("config") or {},
                                             "finished": not dirv or state.startswith(("done", "complete"))
                                             or str(d.get("ami_directive_status") or "active") != "active"}
                live.add(d["device_code"])
                continue
            row = await self.db.fetchone("SELECT event, payload, created_at FROM events WHERE device_code=? AND event LIKE 'directive.%' "
                                         "ORDER BY seq DESC LIMIT 1", (d["device_code"],))
            if row:
                pl = json.loads(row["payload"] or "{}")
                current[d["device_code"]] = {"directive": pl.get("directive"), "configuration": pl.get("configuration"),
                                             "finished": row["event"] in ("directive.completed", "directive.cleared", "directive.paused"),
                                             "at": row["created_at"]}
        sent = await self.db.kv_get("loadout_ferries", {}) or {}
        for ctrl, f in sent.items():
            if ctrl in live:
                continue
            cur = current.get(ctrl)
            later_finish = cur and cur.get("finished") and (cur.get("at") or "") > (f.get("at") or "")
            if not later_finish:  # what we sent is still what it's doing, whatever the event payloads say
                current[ctrl] = {"directive": "ferry", "configuration": f.get("configuration"), "finished": False}
        cfg_only = {**cfg, "fleets": [f for f in cfg["fleets"] if not only or f.get("id") in only or f.get("home") in only
                                      or not f.get("materials") or f.get("materials") == "self"]}
        from .ami_schedule import managed_by
        from .fleets import fleet_tag
        logistics_tags = {fleet_tag(f["id"]) for f in cfg["fleets"] if f.get("role") == "logistics" and f.get("id")}   # the ferry never adopts a logistics
        busy_f = busy | {d["device_code"] for d in devices if logistics_tags & set(d.get("tags") or [])}
        routes, unmet = material_routes(cfg_only, devices, inv, stars, busy_f, current, await managed_by(self.db))
        p["routes"], p["unmet"] = routes, p["unmet"] + unmet
        return p

    async def apply_loadouts(self, only: set[str] | None = None, manual: bool = False) -> list[str]:
        from . import loadouts as lo
        cfg = await self.loadout_cfg()
        p = await self.loadout_plan(only)
        cat = await self.db.kv_get("stars", {}) or {}
        stars = {s.get("designation"): s for s in (cat.get("stars") or []) if isinstance(s, dict)}
        stowed_in = {c: k for k, kids in (await self.db.kv_get("stowed_map", {}) or {}).items() for c in kids}
        lines = lo.describe(p)
        started = 0
        # what this pass moves to another system (the observatory pass leaves those alone instead of unfurling them —
        # seen live 2026-10-09: Miner 2's observatory unfurled while loadouts was compacting it for the trip to ITHVALAI)
        await self.db.kv_set("loadout_moves", {"at": now_iso(), "moves": dict(p.get("moves") or {})})
        radii, far_au = await self.cruise_radii(), await self.max_cruise_au()
        for ctrl, codes in sorted((p.get("releases") or {}).items()):
            why = "no longer in the loadout" if set(codes) <= set(p.get("made_spare") or []) else "in another system / spare"
            started += bool(await self.create_job("loadouts", f"loadouts: {ctrl} releases {len(codes)} device(s) ({why})", ctrl,
                                                  [lo.step(f"{ctrl}: release {', '.join(codes)}", f"/devices/{ctrl}",
                                                           {"command": "release", "devices": codes})],
                                                  {"devices": codes}, force=manual))
        tags = lo.tag_steps(p)
        if tags:
            started += bool(await self.create_job("loadouts", f"loadouts: spare tags ({len(tags)})", None, tags,
                                                  {"devices": []}, force=manual))
        started += await self.start_compactions(p, force=manual)
        orders = await self.db.kv_get("loadout_orders", []) or []
        for pr in p["prints"]:
            job = await self.create_job("loadouts", f"loadouts: print {pr['n']}× {pr['device_type']} for {pr['star']}",
                                        pr["factory"], lo.print_steps(pr), {"devices": [], "star": pr["star"]}, force=manual)
            if job:
                started += 1
                orders += [{"star": pr["star"], "fleet": pr.get("fleet"), "device_type": pr["device_type"],
                            "factory": pr["factory"], "at": now_iso(), "job": job["id"]} for _ in range(pr["n"])]
        await self.db.kv_set("loadout_orders", orders)
        for code, dest in p["self_moves"]:
            started += bool(await self.create_job("loadouts", f"loadouts: {code} → {dest}", code,
                                                  lo.self_move_steps(code, dest, stars, p["by_code"][code], p.get("managed"),
                                                                    gathering=code in set(p.get("gathering") or []),
                                                                    join=(p.get("assign") or {}).get(code)),
                                                  {"devices": [code], "star": dest}, force=manual))
        for dl in p["deliveries"]:
            started += bool(await self.create_job(
                "loadouts", f"loadouts: {dl['carrier']} carries {len(dl['devices'])} {dl['from']} → {' → '.join([dl['to']] + list(dl.get('stops') or []))}", dl["carrier"],
                lo.delivery_steps(dl, p["by_code"], stars, cfg["settings"]["carriers_return"], p.get("managed"),
                                                  gathering=set(p.get("gathering") or []), assign=p.get("assign"),
                                                  radii=radii, max_cruise_au=far_au),
                {"devices": dl["devices"], "star": dl["to"]}, force=manual))
        if p["deliveries"]:   # a carrier was wanted at the pick-up: the hubs' weights (balance.py)
            await self.note_demand({f"lo:{dl['carrier']}:{dl['from']}:{now_iso()}": dl["from"] for dl in p["deliveries"]})
        ferries = await self.db.kv_get("loadout_ferries", {}) or {}
        for r in p.get("routes") or []:
            job = await self.create_job("loadouts", f"loadouts: ferry {r['source']} → {r['dest']}"
                                        + (f" (+{len(r['adopt'])} freighter)" if r.get("adopt") else ""), r["controller"],
                                        lo.ferry_steps(r), {"devices": [a["code"] for a in r.get("adopt") or []], "star": r["dest"]},
                                        force=manual)
            if job:
                started += 1
                if r.get("resend", True):
                    ferries[r["controller"]] = {"configuration": {"collect": r["collect"], "deliver": r["deliver"]}, "at": now_iso()}
        await self.db.kv_set("loadout_ferries", ferries)
        on = lambda code, loc: [lo.activate_step(code)] if lo.relay_ready_at(p["by_code"].get(code) or {}, loc) else []  # noqa: E731
        for code, loc in p.get("pins") or []:
            started += bool(await self.create_job("loadouts", f"loadouts: {code} → {loc} (pinned)", code,
                                                  [lo.pin_step(code, loc)] + on(code, loc), {"devices": [code]}, force=manual))
        for code, loc in p.get("places") or []:
            started += bool(await self.create_job("loadouts", f"loadouts: {code} → {loc} (placement)", code,
                                                  [lo.pin_step(code, loc, "where its type works")] + on(code, loc),
                                                  {"devices": [code]}, force=manual))
        for code in p["arrived"]:
            steps = lo.arrived_steps(code, p["by_code"][code], stowed_in, join=(p.get("assign") or {}).get(code))
            if steps:
                started += bool(await self.create_job("loadouts", f"loadouts: {code} arrived", code, steps,
                                                      {"devices": [code]}, force=manual))
        started += await self._arrival_followups(cfg, p, manual, lines)
        await self.db.kv_set("loadouts_last", {"at": now_iso(), "lines": lines, "jobs": started,
                                               "dry_run": (await self.settings())["dry_run"] and not manual,
                                               "escort_wanted": lo.escort_wanted(p)})
        return lines

    async def _arrival_followups(self, cfg: dict, p: dict, manual: bool, lines: list[str]) -> int:
        """After the moves: drones that reached their system join its controller; inactive maintenance drones and
        AMI controllers that reached home are switched on (once per arrival)."""
        from .ami_schedule import handoff_steps, handoffs, managed_by, wakeup_steps, wakeups
        from . import loadouts as lo
        rc = {"adopt_arrivals": True, "activate_arrivals": True, **((await self.settings())["rules"].get("loadouts") or {})}
        devices = await self.devices()
        busy = self.busy_devices(await self.jobs())
        skip = set(p.get("moves") or {}) | set(p.get("arrived") or []) | {c for dl in p.get("deliveries") or [] for c in dl["devices"]} \
            | {dl["carrier"] for dl in p.get("deliveries") or []}
        started = 0
        if rc.get("adopt_arrivals", True):
            for h in handoffs(devices, await managed_by(self.db), busy, skip, set(cfg.get("ignore_tags") or [])):
                lines.append(f"{h['drone']} joins controller {h['controller']}"
                             + (f" (flies {h['from']} → {h['to']})" if h["from"] != h["to"] else ""))
                started += bool(await self.create_job("loadouts", f"loadouts: {h['controller']} adopts {h['drone']}", h["controller"],
                                                      handoff_steps(h), {"devices": [h["drone"]]}, force=manual))
        if rc.get("activate_arrivals", True):
            done = await self.db.kv_get("loadout_woken", {}) or {}
            # a survey crew's relays wait switched off, and one working relay per system is enough
            skip = skip | lo.survey_cargo(devices, cfg.get("fleets") or []) | {
                d["device_code"] for d in devices if (d.get("device_type") or "") == "ftl_relay"
                and star_of(d.get("location")) in lo.relaying_stars(devices, {d["device_code"]})}
            by = {d.get("device_code"): d for d in devices}
            for w in wakeups(devices, busy, done, skip):
                code = w["code"]
                what = " + ".join(x for x, on in (("activate", w["activate"]), ("patrol", w["patrol"])) if on)
                lines.append(f"{what} {code} ({by[code].get('device_type')}) — it's home")
                job = await self.create_job("loadouts", f"loadouts: {what} {code}", code, wakeup_steps(w),
                                            {"devices": [code]}, force=manual)
                if job:
                    started += 1
                    done[code] = star_of(by[code].get("location"))
            await self.db.kv_set("loadout_woken", done)
        return started

    async def start_compactions(self, p: dict, force: bool = False) -> int:
        """Large devices the loadout plan moves to another system are compacted first, on their own (hours), so no
        carrier waits for it; the plan assigns the carrier once they report compacted."""
        from .modular import compact_seconds, compact_step
        from .shapes import normalize_blueprints
        bps = {b["device_type"]: b for b in normalize_blueprints(await self.db.kv_get("blueprints", []))}
        n = 0
        for code, dest in p.get("compact") or []:
            d = p["by_code"].get(code) or {}
            hours = compact_seconds(d, bps) / 3600
            job = await self.create_job("loadouts", f"loadouts: compact {code} ({d.get('device_type')}) for the trip to {dest} "
                                                    f"(≈{hours:.1f} h)", code,
                                        [compact_step(code, bps, d, f"for the trip to {dest}")], {"devices": [code]},
                                        force=force)
            n += bool(job)
        return n

    async def dispatch_new_prints(self, max_wait_minutes: int = 15) -> list[str]:
        """Prints that just came out bound for another system: refresh the device list until they show up, then start
        only the deliveries (carrier or own surge) that involve them — the rest waits for the regular pass."""
        from . import loadouts as lo
        pend = await self.db.kv_get("dispatch_pending", {}) or {}
        if not pend or not await self.rule_cfg("loadouts"):
            return []
        now = _now()
        pend = {c: at for c, at in pend.items() if _ts(at) and (now - _ts(at)).total_seconds() < max_wait_minutes * 60}
        codes = {d.get("device_code") for d in await self.devices()}
        if not set(pend) <= codes and self.worker:
            last = _ts(await self.db.kv_get("dispatch_sync_at", None))
            if not last or (now - last).total_seconds() >= 20:   # the new device isn't listed yet: re-read (≤ every 20 s)
                await self.db.kv_set("dispatch_sync_at", now.isoformat(timespec="seconds"))
                try:
                    await self.worker.sync_devices()
                except Exception as e:  # noqa: BLE001 — try again next tick
                    log.info("dispatch: device sync failed: %s", e)
                codes = {d.get("device_code") for d in await self.devices()}
        ready = {c for c in pend if c in codes}
        if not ready:
            await self.db.kv_set("dispatch_pending", pend)
            return []
        tried = _ts(await self.db.kv_get("dispatch_try_at", None))
        if tried and (now - tried).total_seconds() < 30:
            return []
        await self.db.kv_set("dispatch_try_at", now.isoformat(timespec="seconds"))
        cfg = await self.loadout_cfg()
        cat = await self.db.kv_get("stars", {}) or {}
        stars = {s.get("designation"): s for s in (cat.get("stars") or []) if isinstance(s, dict)}
        p = await self.loadout_plan()
        radii, far_au = await self.cruise_radii(), await self.max_cruise_au()
        started: list[str] = []
        if await self.start_compactions({**p, "compact": [x for x in p.get("compact") or [] if x[0] in ready]}):
            started.append("compacting " + ", ".join(c for c, _ in p.get("compact") or [] if c in ready))
        for code, dest in p["self_moves"]:
            if code in ready:
                if await self.create_job("loadouts", f"loadouts: {code} → {dest} (just printed)", code,
                                         lo.self_move_steps(code, dest, stars, p["by_code"][code], p.get("managed"),
                                                                    gathering=code in set(p.get("gathering") or []),
                                                                    join=(p.get("assign") or {}).get(code)),
                                         {"devices": [code], "star": dest}):
                    started.append(f"{code} flies to {dest}")
                ready.discard(code), pend.pop(code, None)
        for dl in p["deliveries"]:
            if ready & set(dl["devices"]):
                if await self.create_job("loadouts", f"loadouts: {dl['carrier']} carries {len(dl['devices'])} {dl['from']} → {' → '.join([dl['to']] + list(dl.get('stops') or []))} "
                                         "(just printed)", dl["carrier"],
                                         lo.delivery_steps(dl, p["by_code"], stars, cfg["settings"]["carriers_return"], p.get("managed"),
                                                  gathering=set(p.get("gathering") or []), assign=p.get("assign"),
                                                  radii=radii, max_cruise_au=far_au),
                                         {"devices": dl["devices"], "star": dl["to"]}):
                    started.append(f"{dl['carrier']} carries {', '.join(dl['devices'])} to {dl['to']}")
                for c in dl["devices"]:
                    ready.discard(c), pend.pop(c, None)
        for u in p["unmet"]:   # nothing free to carry it now: say so, and leave it to the regular pass
            if "waiting for a surge-capable carrier" in u.get("why", "") and ready:
                await self.log("loadouts", f"just printed {', '.join(sorted(ready))}: {u['why']} — the next pass retries")
                for c in list(ready):
                    pend.pop(c, None)
                break
        await self.db.kv_set("dispatch_pending", pend)
        for line in started:
            await self.log("loadouts", f"dispatched right after printing: {line}")
        return started

    async def run_due_loadouts(self) -> None:
        cfg = await self.rule_cfg("loadouts")
        if not cfg:
            return
        last = _ts(((await self.db.kv_get("loadouts_last", {})) or {}).get("at"))
        if last and (_now() - last).total_seconds() < 60 * max(1, int(cfg.get("every_minutes") or 15)):
            return
        await self.apply_loadouts()

    # --- AMI schedules ---------------------------------------------------------------------------------
    async def schedules(self) -> list[dict]:
        return await self.db.kv_get("ami_schedules", []) or []

    async def save_schedules(self, items: list[dict]) -> None:
        await self.db.kv_set("ami_schedules", items)

    async def run_due_schedules(self) -> None:
        from .ami_schedule import due
        if not await self.rule_cfg("ami_schedules"):
            return
        items = await self.schedules()
        changed = False
        for sched in items:
            if due(sched):
                await self.run_schedule(sched)
                changed = True
        if changed:
            await self.save_schedules(items)

    async def run_schedule(self, sched: dict, manual: bool = False) -> list[str]:
        """Apply one schedule now. Mutates sched['last_run'/'last_result']; returns what happened per controller."""
        from .ami_schedule import adoptable, controller_idle, managed_by, schedule_steps, targets_of
        devices = await self.devices()
        managed = await managed_by(self.db)
        busy = self.busy_devices(await self.jobs())
        results = []
        warded = await self.warded(devices)
        for ctrl in targets_of(sched, devices):
            code = ctrl["device_code"]
            if code in busy:
                results.append(f"{code}: already running a job")
                continue
            if ctrl.get("in_control_range") is False:   # the game refuses it ("Device is out of comms range"): skip, quietly
                results.append(f"{code}: out of comms range — skipped")
                continue
            if "mining" in (ctrl.get("device_type") or "") and star_of(ctrl.get("location")) in warded:
                results.append(f"{code}: another player's ward or hub in {star_of(ctrl.get('location'))} — no mining there")
                continue
            rest = (await self.db.kv_get("rested", {}) or {}).get(code)
            if rest and not manual and await self.open_sites_now(rest["belt"]) <= 0:
                results.append(f"{code}: resting at {rest['belt']} until it has open sites (an exhausted controller overheats)")
                continue
            idle, why = await controller_idle(self.db, ctrl)
            if sched.get("only_idle", True) and not idle:
                results.append(f"{code}: busy ({why})")
                continue
            dv = ctrl.get("ami_directive") or {}
            state = str(dv.get("_eval_state") or "")
            if dv.get("name") == "gather_salvage" and not state.startswith(("done", "complete", "no_targets")):
                results.append(f"{code}: on salvage — left alone")
                continue
            if not manual and dv.get("name") == sched["directive"] and state.startswith("exhausted"):
                from .salvage import belt_of, exhausted_place
                place = exhausted_place(state)
                pb = belt_of(place)
                if pb and pb == place and await self.open_sites_now(pb) > 0:
                    pass  # stale: the belt it ran dry at has open sites again — re-sending restarts it
                elif place and not pb:
                    results.append(f"{code}: exhausted at {place} (its drones are off the belt) — 'Salvage when depleted' "
                                   "brings them back to a belt with open sites")
                    continue
                else:
                    results.append(f"{code}: exhausted on {sched['directive']} at {place or '?'} — re-sending it won't help until sites re-open")
                    continue
            adopt = adoptable(devices, ctrl, managed) if sched.get("adopt", True) else []
            use = await self.print_tilt(sched, ctrl, devices)
            job = await self.create_job("ami_schedules", f"{use.get('name') or use['directive']} → {code}", code,
                                        schedule_steps(ctrl, use, adopt), {"schedule": sched.get("id"), "devices": adopt},
                                        force=manual)
            results.append(f"{code}: {'started' if job else 'planned (dry run)'}"
                           + (f", adopting {len(adopt)}" if adopt else "") + f" ({why})")
        if not results:
            results.append("no matching controllers")
        sched["last_run"] = _now().isoformat(timespec="seconds")
        sched["last_result"] = "; ".join(results)
        return results

    # --- repeating console commands --------------------------------------------------------------------
    async def run_due_repeats(self, only: str | None = None) -> None:
        """Note finds in each repeat's system, then send the ones that are due (only=id: that one, now)."""
        from . import repeats as rp
        items = await self.db.kv_get(rp.KV, []) or []
        if not any(r.get("enabled", True) for r in items) and not only:
            return
        devices = await self.devices()
        reps = await self.db.kv_get("replicants", {}) or {}
        marks = ",".join("?" * len(rp.FOUND_EVENTS))
        changed = False
        for r in items:
            if only and r.get("id") != only or not only and not r.get("enabled", True):
                continue
            star = rp.star_for(r["path"], devices, reps) or r.get("star") or ""
            for row in await self.db.fetchall(f"SELECT * FROM events WHERE seq > ? AND event IN ({marks}) ORDER BY seq",
                                              (int(r.get("seen_seq") or 0), *rp.FOUND_EVENTS)):
                r["seen_seq"] = row["seq"]
                changed = True
                ev = row_event(row)
                where = ev.get("star") or str(ev.get("location") or ev["payload"].get("location") or "").split("-")[0]
                if star and where and where != star:
                    continue
                r["finds"] = int(r.get("finds") or 0) + 1
                r["last_find"] = f"{rp.find_text(ev)} at {ev.get('location') or where or '?'}"
                if r.get("stop_on_find") and r.get("enabled", True):
                    r["enabled"] = False
                    await self.log("repeats", f"{r['name']}: stopped, found {r['last_find']}", notify=True)
            if not (only or rp.due(r)):
                continue
            r["star"] = star
            sub, cmd = rp.subject(r["path"]), (r.get("body") or {}).get("command") if isinstance(r.get("body"), dict) else None
            d = next((x for x in devices if x.get("device_code") == sub[1]), None) if sub and sub[0] == "device" and cmd else None
            if d and d.get("available_commands") is not None and cmd not in d["available_commands"]:
                r["last_result"] = f"waiting: {cmd} not available now ({d.get('status') or '?'})"   # sends once it is
                changed = True
                continue
            ok, _resp, err = await self.send(r["method"], r["path"], r.get("body"), f"repeat {r['name']}")
            r["runs"] = int(r.get("runs") or 0) + 1
            r["last_run"] = _now().isoformat(timespec="seconds")
            r["last_result"] = "ok" if ok else f"error: {err}"
            r["errors"] = 0 if ok else int(r.get("errors") or 0) + 1
            changed = True
            if r["errors"] >= rp.MAX_ERRORS and r.get("enabled", True):
                r["enabled"] = False
                await self.log("repeats", f"{r['name']}: stopped after {r['errors']} failed sends in a row ({err})",
                               "alert", notify=True)
        if changed:
            await self.db.kv_set(rp.KV, items)

    async def print_needs(self, devices: list[dict]) -> dict[str, float]:
        """What waiting prints lack, per resource (prospects.print_short), and the system scans (kept a minute)."""
        from . import prospects as pr
        inp = await pr.rank_inputs(self.db, self, devices)
        return inp.get("short") or {}

    async def print_tilt(self, sched: dict, ctrl: dict, devices: list[dict]) -> dict:
        """A gather_evenly schedule on a mining controller turns into maintain_ratios leaning toward what waiting prints
        lack, among what its belt offers (owner 2026-10-22). Bootstrap controllers keep their own ratios."""
        from . import prospects as pr
        from .salvage import belt_of
        if sched.get("directive") != "gather_evenly" or "mining" not in (ctrl.get("device_type") or "") \
                or any(t.startswith("boot:") for t in ctrl.get("tags") or []):
            return sched
        row = await self.db.fetchone("SELECT data FROM systems WHERE star=?", (star_of(ctrl.get("location")),))
        offered = pr.belt_resources(json.loads(row["data"] or "{}") if row else {}, belt_of(ctrl.get("location")))
        ratios = pr.print_ratios(offered, await self.print_needs(devices))
        if not ratios:
            return sched
        # the game wants them under "ratios", not flat as its docs show (live 2026-10-25)
        return {**sched, "directive": "maintain_ratios", "configuration": {"ratios": ratios},
                "name": f"{sched.get('name') or 'gather_evenly'}, leaning to what prints wait on"}

    # --- helpers for rules ------------------------------------------------------------------------
    def busy_devices(self, jobs: list[dict]) -> set[str]:
        out = set()
        for j in jobs:
            if j["status"] in ("running", "waiting"):
                out.add(j.get("device"))
                out.update(j.get("meta", {}).get("devices", []))
        return out

    async def devices(self) -> list[dict]:
        devs = await self.db.kv_get("devices", []) or []
        s = await self.db.kv_get("sim", {}) or {}
        if s.get("active"):   # a simulation's devices are its own (sim_engine): no other automation touches them
            from . import sim
            devs = [d for d in devs if not sim.is_sim_device(d, s)]
        return devs

    async def warded(self, devices: list[dict] | None = None) -> set[str]:
        """Systems another player has warded (wards.foreign): the mining rules leave them alone."""
        from . import wards
        return wards.foreign({"stars": await self.star_lookup("flagged")}, devices if devices is not None else await self.devices())

    async def star_lookup(self, name: str) -> Any:
        """Lookups over the star catalog, built once per catalog update and shared (don't change them): "pos"
        {star: position} for stars with one, "pos_all" (every star, {} without one), "by" {star: record}, "xyz" {star: (x, y, z)}, "names" (set), "slugs"
        (loadouts.star_slugs), "flagged" (records flagged has_ward / has_hub), "hubs" (set of stars flagged has_hub)."""
        from . import loadouts as lo
        from . import pathing as pa

        def recs(cat):
            return [x for x in (cat or {}).get("stars") or [] if isinstance(x, dict)]
        fns = {"pos": lambda c: {x.get("designation"): x["position"] for x in recs(c) if x.get("position")},
               "pos_all": lambda c: {x.get("designation"): x.get("position") or {} for x in recs(c)},
               "by": lambda c: {x.get("designation"): x for x in recs(c)},
               "xyz": lambda c: {x.get("designation"): pa.xyz(x["position"]) for x in recs(c) if x.get("position")},
               "names": lambda c: {x["designation"] for x in recs(c) if x.get("designation")},
               "slugs": lambda c: lo.star_slugs({x["designation"] for x in recs(c) if x.get("designation")}),
               "flagged": lambda c: [x for x in recs(c) if x.get("has_ward") or x.get("has_hub")],
               "hubs": lambda c: {x["designation"] for x in recs(c) if x.get("has_hub") and x.get("designation")}}
        return await self.db.kv_derived("stars", name, fns[name], {})

    async def replicant_for_host(self, device_code: str) -> tuple[str, dict] | None:
        reps = await self.db.kv_get("replicants", {}) or {}
        for code, r in reps.items():
            if r.get("hosted_device_code") == device_code:
                return code, r
        return None

    async def stowed_in(self, vessel: str) -> list[dict]:
        """Devices stowed in a vessel: from the vessel's detail, else the host replicant's detail."""
        items: list = []
        try:
            detail = await self.api.request("GET", f"/devices/{vessel}", background=True)
            items = (detail or {}).get("stowed_devices") or []
        except ApiError:
            pass
        if not items:
            rep = await self.replicant_for_host(vessel)
            if rep:
                try:
                    detail = await self.api.request("GET", f"/replicants/{rep[0]}", background=True)
                    items = (detail or {}).get("stowed_devices") or []
                except ApiError:
                    items = rep[1].get("stowed_devices") or []
        return [i for i in items if isinstance(i, dict) and i.get("device_code")]

    async def system_scan(self, star: str) -> dict | None:
        row = await self.db.fetchone("SELECT data FROM systems WHERE star=?", (star,))
        if row:
            return json.loads(row["data"])
        try:
            data = await self.api.request("GET", f"/locations/{star}", background=True)
        except ApiError:
            return None
        if isinstance(data, dict) and data.get("planets") is not None:
            await self.db.execute("INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                                  (star, json.dumps(data), now_iso()))
            return data
        return None

    # --- rules -------------------------------------------------------------------------------------------
    async def mark_system_surveyed(self, star: str, by: str | None = None) -> None:
        if not star:
            return
        done = await self.db.kv_get("ami_surveyed", {}) or {}
        done[star] = now_iso()
        await self.db.kv_set("ami_surveyed", done)
        scan = await self.system_scan(star)
        if scan:
            surveyed = await self.db.kv_get("surveyed", {}) or {}
            for t in survey_targets(scan, {}, True, True, 10_000):
                surveyed.setdefault(t["target"], now_iso())
            await self.db.kv_set("surveyed", surveyed)
        await self.log("auto_survey", f"{star} fully surveyed" + (f" by {by}" if by else ""))

    async def arrived_from_elsewhere(self, ev: dict, star: str) -> bool:
        """True when this arrival brought the device into `star` from another system (not an in-system hop)."""
        p = ev.get("payload") or {}
        if str(p.get("travel_type") or "").startswith("surge"):
            return True
        origin = p.get("origin")
        if not origin:
            row = await self.db.fetchone("SELECT payload FROM events WHERE device_code=? AND event='travel.departed' "
                                         "ORDER BY seq DESC LIMIT 1", (ev.get("device_code"),))
            origin = (json.loads(row["payload"] or "{}") if row else {}).get("origin")
        return bool(origin) and star_of(origin) != star

    async def on_arrival(self, ev: dict) -> None:
        vessel = ev.get("device_code")
        p = ev.get("payload") or {}
        dest = p.get("destination") or ev.get("location")
        star = star_of(dest) or ev.get("star")
        if not vessel or not star or p.get("recalling"):
            return
        if not await self.arrived_from_elsewhere(ev, star):
            return  # moving around inside a system never re-triggers the arrival rules
        s = await self.settings()
        enabled = {rid for rid, cfg in s["rules"].items() if cfg.get("enabled")}
        if not enabled:
            return
        if "scan_on_arrival" in enabled:
            await self.rule_scan_on_arrival(vessel, star)
        if "devices_on_arrival" in enabled:
            await self.rule_devices_on_arrival(vessel, star)
        if "census_on_arrival" in enabled:
            await self.rule_census_on_arrival(vessel, star)
        if "auto_survey" in enabled:
            stowed = await self.stowed_in(vessel)
            await self.rule_auto_survey(vessel, dest, star, stowed, s["rules"]["auto_survey"])

    async def rule_scan_on_arrival(self, vessel: str, star: str) -> None:
        rep = await self.replicant_for_host(vessel)
        if not rep:
            return
        if await self.db.fetchone("SELECT 1 FROM systems WHERE star=?", (star,)):
            return
        if (await self.settings())["dry_run"]:
            await self.log("scan_on_arrival", f"[dry run] would scan {star} with {rep[1].get('name') or rep[0]}")
            return
        ok, resp, err = await self.send("POST", f"/replicants/{rep[0]}/scan", {}, f"auto: scan {star}")
        if ok and isinstance(resp, dict) and resp.get("planets") is not None:
            await self.db.execute("INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                                  (star, json.dumps(resp), now_iso()))
            await self.log("scan_on_arrival", f"scanned {star}: {len(resp.get('planets') or [])} planets")
        else:
            await self.log("scan_on_arrival", f"scan of {star} failed: {err}", "alert")

    async def rule_devices_on_arrival(self, vessel: str, star: str) -> None:
        from . import others, traffic as tr
        from .api import ApiError
        rep = await self.replicant_for_host(vessel)
        if not rep:
            return
        if (await self.settings())["dry_run"]:
            await self.log("devices_on_arrival", f"[dry run] would scan the devices in {star} with {rep[1].get('name') or rep[0]}")
            return
        mine = tr.my_codes(await self.db.kv_get("account", {}) or {}, await self.db.kv_get("replicants", {}) or {},
                           await self.db.kv_get("devices", []) or [])
        try:
            snap = await others.scan_into(self.db, self.api, rep[0], mine)
        except ApiError as e:
            await self.log("devices_on_arrival", f"device scan of {star} failed: {e.message}", "alert")
            return
        shops = snap.get("shops") or []
        await self.log("devices_on_arrival", f"scanned the devices in {snap.get('star') or star}: "
                       f"{len(snap.get('devices') or [])} fixed, {len(snap.get('drones') or [])} drones of others"
                       + (f", shops: {', '.join(shops)}" if shops else ""))
        if self.hub:
            self.hub.publish("state", "traffic")

    async def rule_census_on_arrival(self, vessel: str, star: str) -> None:
        from .census import can_census
        if star in (await self.db.kv_get("census", {}) or {}):
            return
        d = next((x for x in await self.devices() if x.get("device_code") == vessel), None)
        if not d or not can_census(d):
            return
        await self.run_census(vessel, star)

    async def run_census(self, device: str, star: str, manual: bool = False) -> tuple[list[dict], str | None]:
        """`stellar_census` from `device` (in `star`): the stars around it are stored and merged into the catalogue.
        Follows extra pages (at most 5). Returns (stars, error)."""
        from . import census
        if (await self.settings())["dry_run"] and not manual:
            await self.log("census_on_arrival", f"[dry run] would run a stellar census in {star} with {device}")
            return [], None
        pages, err = [], None
        for n in range(1, 6):
            body = {"command": "stellar_census", **({"page": n} if n > 1 else {})}
            ok, resp, err = await self.send("POST", f"/devices/{device}", body,
                                            f"{'' if manual else 'auto: '}stellar census in {star}" + (f" (page {n})" if n > 1 else ""))
            if not ok or not isinstance(resp, dict):
                break
            pages.append(resp)
            err = None
            if n >= int(resp.get("total_pages") or 1):
                break
        if not pages:
            await self.log("census_on_arrival", f"stellar census in {star} with {device} failed: {err}", "alert")
            return [], err
        found = await census.record(self.db, star, device, pages)
        new = [s["designation"] for s in found if s.get("explored") is False]
        await self.log("census_on_arrival", f"stellar census in {star}: {len(found)} stars"
                       + (f", unexplored: {', '.join(new)}" if new else ", all explored"), notify=bool(new))
        return found, err

    async def rule_auto_survey(self, vessel: str, vessel_loc: str, star: str, stowed: list[dict], cfg: dict) -> None:
        jobs = await self.jobs()
        if any(j["rule"] == "auto_survey" and j["status"] in ("running", "waiting") and j["meta"].get("star") == star
               for j in jobs):
            return
        if star in (await self.db.kv_get("ami_surveyed", {}) or {}):
            return  # an AMI survey_system already covered every body
        # a fleet's vessel and its passengers away from its station are the fleet's (live 2026-10-21: Surveyor 1's heaven
        # vessel flown into PHEVENIA had its survey controller and a drone deployed and sent off while the fleet boarded)
        by = {d.get("device_code"): d for d in await self.devices()}
        if _reserved(by.get(vessel) or {}):
            return
        stowed = [i for i in stowed if not _reserved({**(by.get(i.get("device_code")) or i), "location": vessel_loc})]
        for d in by.values():
            st_ = str(((d.get("ami_directive") or {}).get("_eval_state")) or "")
            if "survey" in (d.get("device_type") or "") and star_of(d.get("location")) == star and st_.startswith("no_targets"):
                await self.mark_system_surveyed(star, d["device_code"])  # the controller says there's nothing left to survey
                return
        started = (await self.db.kv_get("ami_survey_started", {}) or {}).get(star)
        if started and (_now() - (_ts(started) or _now())).total_seconds() < 12 * 3600:
            return  # an AMI survey of this system is (or was recently) under way; don't restart it on every arrival
        scan = await self.system_scan(star)
        if not scan:
            await self.log("auto_survey", f"no scan data for {star}; enable 'System scan on arrival' or scan manually")
            return
        surveyed = await self.db.kv_get("surveyed", {}) or {}
        targets = survey_targets(scan, surveyed, bool(cfg.get("include_moons")), bool(cfg.get("include_belts", True)),
                                 int(cfg.get("max_targets") or 20))
        if not targets:
            return
        busy = self.busy_devices(jobs)
        if cfg.get("use_ami", True) and await self.survey_with_ami(vessel, vessel_loc, star, stowed, cfg, busy):
            return
        drones = [(i["device_code"], True) for i in stowed
                  if "survey" in (i.get("device_type") or "") and i["device_code"] not in busy
                  and "controller" not in (i.get("device_type") or "")]
        if cfg.get("use_idle", True):
            have = {d for d, _ in drones}
            for d in await self.devices():
                if _reserved(d):
                    continue
                if ("survey" in (d.get("device_type") or "") and star_of(d.get("location")) == star
                        and str(d.get("status", "")).startswith("idle") and d["device_code"] not in busy | have):
                    drones.append((d["device_code"], False))
        if not drones:
            return
        # round-robin the targets over the drones
        per: dict[str, list[dict]] = {d: [] for d, _ in drones}
        for i, t in enumerate(targets):
            per[drones[i % len(drones)][0]].append(t)
        for drone, was_stowed in drones:
            mine = per[drone]
            if not mine:
                continue
            steps = []
            if was_stowed:
                steps.append(step(f"deploy {drone}", f"/devices/{drone}", {"command": "deploy"},
                                  wait=["device.deployed"], timeout=SHORT_TIMEOUT, critical=True))
            for t in mine:
                steps.append(step(f"{drone} → {t['target']}", f"/devices/{drone}",
                                  {"command": "travel", "destination": t["target"]}, wait=["travel.arrived"],
                                  match={"destination": t["target"]}))
                steps.append(step(f"{t['action']} {t['target']}", f"/devices/{drone}", {"command": t["action"]},
                                  wait=[f"{t['action']}.completed"], match={f"{t['action']}_target": t["target"]}))
            if cfg.get("return_and_stow", True) and was_stowed:
                steps.append(step(f"{drone} → back to {vessel_loc}", f"/devices/{drone}",
                                  {"command": "travel", "destination": vessel_loc}, wait=["travel.arrived"],
                                  match={"destination": vessel_loc}))
                steps.append(step(f"stow {drone} in {vessel}", f"/devices/{drone}",
                                  {"command": "stow", "target": vessel}, wait=["device.stowed"], timeout=SHORT_TIMEOUT))
            await self.create_job("auto_survey", f"survey {star} with {drone} ({len(mine)} bodies)", drone, steps,
                                  {"star": star, "targets": [t["target"] for t in mine]})

    async def survey_with_ami(self, vessel: str, vessel_loc: str, star: str, stowed: list[dict], cfg: dict,
                              busy: set) -> bool:
        """Survey via an AMI survey controller (carried or already in the system). True if a job was made."""
        devices = [d for d in await self.devices() if not _reserved(d)]
        carried_ctrl = next((i for i in stowed if "survey" in (i.get("device_type") or "") and "controller" in (i.get("device_type") or "")
                             and i["device_code"] not in busy), None)
        in_system = next((d for d in devices if "survey" in (d.get("device_type") or "") and "controller" in (d.get("device_type") or "")
                          and star_of(d.get("location")) == star and not str(d.get("status", "")).startswith("stowed")
                          and d["device_code"] not in busy), None)
        ctrl = carried_ctrl or in_system
        if not ctrl:
            return False
        code = ctrl["device_code"]
        steps = []
        if carried_ctrl:
            st = step(f"deploy survey controller {code}", f"/devices/{code}", {"command": "deploy"},
                      wait=["device.deployed"], timeout=SHORT_TIMEOUT, critical=True)
            st["wait_device"] = code
            steps.append(st)
        drones = [i["device_code"] for i in stowed if "survey_drone" in (i.get("device_type") or "") and i["device_code"] not in busy]
        for dcode in drones:
            st = step(f"deploy {dcode}", f"/devices/{dcode}", {"command": "deploy"}, wait=["device.deployed"], timeout=SHORT_TIMEOUT)
            st["wait_device"] = dcode
            steps.append(st)
        here = ctrl.get("location") if not carried_ctrl else vessel_loc
        if cfg.get("use_idle", True):   # idle belt surveyors here too: a survey controller adopts them (live 2026-10-24)
            drones += [d["device_code"] for d in devices if ("survey_drone" in (d.get("device_type") or "")
                                                            or d.get("device_type") == "belt_surveyor")
                       and d.get("location") == here
                       and str(d.get("status", "")).startswith("idle") and d["device_code"] not in busy | set(drones)]
        # a survey controller works from the belt (or the inner system when there's none): take it and its drones there
        from . import placement as pl
        cat = await self.db.kv_get("stars", {}) or {}
        entry = next((x.get("entry_point") for x in (cat.get("stars") or []) if isinstance(x, dict) and x.get("designation") == star), None)
        geo = pl.geography(star, await self.devices(), await self.system_scan(star), entry)
        spot = pl.target("ami_survey_controller", geo) if not pl.ok("ami_survey_controller", here, geo) else None
        if spot and drones:
            movers = [code] + drones
            first = len(steps)
            for c in movers:
                steps.append(step(f"{c} → {spot}", f"/devices/{c}", {"command": "travel", "destination": spot},
                                  critical=c == code))
            for i, c in enumerate(movers):
                w = step(f"wait for {c} at {spot}", "", None, method="WAIT", wait=["travel.arrived"],
                         match={"destination": spot}, timeout=STEP_TIMEOUT)
                w["wait_device"], w["seq0_from"] = c, first + i
                steps.append(w)
        from .ami_schedule import managed_by
        already = [d for d, c in (await managed_by(self.db)).items() if c == code]
        if not drones and not already:
            return False  # a controller with no drones can't survey anything; let the drone path decide
        if drones:
            steps.append(step(f"{code}: adopt {len(drones)} survey drone(s)", f"/devices/{code}", {"command": "adopt", "devices": drones}))
        config = {"planets": "all", "moons": "all" if cfg.get("include_moons") else "none",
                  "recall": bool(cfg.get("return_and_stow", True))}
        steps.append(step(f"{code}: survey_system", f"/devices/{code}",
                          {"command": "set_directive", "directive": "survey_system", "configuration": config}, critical=True))
        steps.append(step(f"{code}: launch", f"/devices/{code}", {"command": "launch"}))
        job = await self.create_job("auto_survey", f"survey {star} with AMI {code} ({len(drones) + len(already)} drones)", code, steps,
                                    {"star": star, "devices": drones, "ami": True})
        if job:
            started = await self.db.kv_get("ami_survey_started", {}) or {}
            started[star] = now_iso()
            await self.db.kv_set("ami_survey_started", started)
        return True if job is not None or (await self.settings())["dry_run"] else False

    async def exhausted_places(self) -> list[str]:
        """Places the game told us are mined out ("Belt exhausted", or a controller's exhausted state), for 12 h."""
        ex = await self.db.kv_get("exhausted_places", {}) or {}
        now = _now()
        live = {k: v for k, v in ex.items() if (now - (_ts(v) or now)).total_seconds() < 12 * 3600}
        if len(live) != len(ex):
            await self.db.kv_set("exhausted_places", live)
        return list(live)

    # --- fleets ---------------------------------------------------------------------------------------
    async def fleets(self) -> list[dict]:
        return (await self.loadout_cfg())["fleets"]

    async def save_fleets(self, items: list[dict]) -> None:
        from .ami_schedule import set_stationed
        from .fleets import fleet_tag, stationed
        derived = ("roster", "points", "job", "zero", "report", "route", "sends_to", "takes_from", "target_options",
                   "owner_move", "owner_hosts", "owners", "orders", "outposts")   # page-only fields, never stored
        keep = [{k: v for k, v in f.items() if k not in derived} for f in items]
        if any(f.get("materials") == "supply" or f.get("supply_to") for f in keep):   # nearest receiver, by home
            from .fleets import resolve_supply
            resolve_supply(keep, await self.star_lookup("pos_all"))
        # a stationed fleet that changed home: its replicants' vessels follow it there (rider_pass)
        before = {f.get("id"): f.get("home") for f in await self.db.kv_get("fleets", []) or [] if f.get("station")}
        moved = {f["id"]: f["home"] for f in keep if stationed(f) and f.get("home") and before.get(f["id"])
                 and before[f["id"]] != f["home"]}
        if moved:
            pending = await self.db.kv_get("rider_moves", {}) or {}
            for fid, home in moved.items():
                pending[fid] = {"home": home, "at": now_iso(), "sent": []}
            await self.db.kv_set("rider_moves", pending)
            hm = await self.db.kv_get("home_moves", {}) or {}   # open until every member is there (stray_pass)
            for fid, home in moved.items():
                hm[fid] = {"from": before[fid], "to": home, "at": now_iso()}
            await self.db.kv_set("home_moves", hm)
        await self.db.kv_set("fleets", keep)
        set_stationed({fleet_tag(f["id"]): f["home"] for f in items if stationed(f)})

    def _mlog(self, m: dict, text: str) -> None:
        m.setdefault("log", []).append({"at": now_iso(), "text": text})
        m["log"] = m["log"][-60:]

    async def auto_deals_pass(self) -> list[str]:
        """Trade fleets are retired (2026-10-12): the logistics service (logistics_pass) loads, delivers and fulfills approved
        contracts and trades. A trade fleet becomes a logistics fleet — same members and loadout — once no mission of its own
        is under way (a run already going finishes first)."""
        items = await self.fleets()
        out = []
        for f in items:
            if f.get("role") == "logistics" and (f.get("station") or f.get("materials")):
                # a logistics fleet works everywhere: never kept at home by the loadout pass, never a ferry's source (seen
                # in a snapshot 2026-10-13: a converted fleet still stationed at FALQUORYX, sending materials to printing-hub)
                f["station"], f["materials"] = False, ""
                out.append(f"{f.get('name')}: a logistics fleet isn't stationed and sends no materials — cleared")
            if f.get("role") != "trade" or (f.get("mission") or {}).get("status") in ("running", "stalled", "stopped"):
                continue
            f["role"] = "logistics"
            for k in ("auto_contracts", "auto_trades", "deals_done"):
                f.pop(k, None)
            if (f.get("mission") or {}).get("status") not in ("running", "stalled", "stopped"):
                f.pop("mission", None)
            out.append(f"{f.get('name')}: trade fleet is now a logistics fleet (trade fleets are retired)")
        if out:
            await self.save_fleets(items)
            for line in out:
                await self.log("fleets", line)
        return out

    async def listing_requests(self, reps: dict) -> tuple[list[str], list[dict]]:
        """Shop listings you asked for (Shop › Get listing): read now where a replicant of yours is in the shop's system
        (the game answers only then), else hand them to the logistics plan. Bill's too: it waits until his shop is seen.
        Returns (log lines, listings for lg.plan)."""
        from . import shop as sh
        wanted = await self.db.kv_get(sh.LISTING_KV, {}) or {}
        if not wanted:
            return [], []
        known = await sh.known_traders(self.db)
        here = {star_of((r or {}).get("location") or (r or {}).get("current_location")) for r in reps.values()} - {""}
        live = {j["id"] for j in await self.jobs() if j["status"] in ("running", "waiting")}
        lines, todo = [], []
        for ctrl, rq in list(wanted.items()):
            t = known.get(ctrl) or {}
            name = t.get("shop_name") or ctrl
            star = t.get("star") or star_of(t.get("location"))
            if star and star in here:
                try:
                    trades = await sh.read_listing(self.db, self.api, ctrl)
                except ApiError as e:
                    rq["tries"] = int(rq.get("tries") or 0) + 1
                    if rq["tries"] < 3:
                        continue
                    lines.append(f"listing of {name} at {t.get('location')}: reading it failed 3 times ({e.message}) — dropped")
                else:
                    lines.append(f"listing of {name} at {t.get('location')}: {len(trades)} trades read")
                wanted.pop(ctrl)
                continue
            if not t.get("location") or rq.get("job") in live:   # Bill's too: a logistics member goes (2026-10-22)
                continue
            todo.append({"controller": ctrl, "location": t["location"],
                         "label": f"get the listing of {name} at {t['location']}"})
        await self.db.kv_set(sh.LISTING_KV, wanted)
        return lines, todo

    async def logistics_pass(self, force: bool = False) -> list[str]:
        """The logistics service (logistics.py): every couple of minutes, track what prints, approved contracts and approved trades
        need, and send free logistics-fleet devices with it — priority: queued prints, local, long-range, stockpile returns."""
        from . import gameevents as gev
        from . import loadouts as lo
        from . import logistics as lg
        from . import wards
        from .shapes import normalize_blueprints, normalize_inventory
        last = _ts(await self.db.kv_get("logistics_last", None))
        if not force and last and (_now() - last).total_seconds() < 110:
            return []
        await self.db.kv_set("logistics_last", now_iso())
        for j, why in lg.stale_rescues(await self.jobs(), await self.devices()):
            j["status"], j["finished_at"] = "cancelled", now_iso()
            await self._update(j)
            await self.log("logistics", f"canceled: {j['title']} — {why}", notify=True)
        items = await self.fleets()
        if not lg.logistics_fleets(items):
            await self.db.kv_set(lg.KV_STATE, {"at": now_iso(), "requests": [], "runs": [], "unmet": [], "fleet_devices": [],
                                               "note": "no logistics fleet yet (Fleets: a fleet with role logistics)"})
            return []
        devices = await self.devices()
        bps = {b["device_type"]: b for b in normalize_blueprints(await self.db.kv_get("blueprints", []))}
        inv = {i.get("location"): i.get("items") or {} for i in normalize_inventory(await self.db.kv_get("inventory", []))}
        inv_raw = {k: dict(v) for k, v in inv.items()}   # before what jobs will pick up is taken out (backlog sample)
        cat = await self.db.kv_get("stars", {}) or {}
        stars = {x.get("designation"): x for x in cat.get("stars") or [] if isinstance(x, dict)}
        pos = {k: v.get("position") for k, v in stars.items() if v.get("position")}
        reps = await self.db.kv_get("replicants", {}) or {}
        hosts = {r.get("hosted_device_code"): c for c, r in reps.items() if r.get("hosted_device_code")}
        jobs = await self.jobs()
        busy = self.busy_devices(jobs)
        by_code0 = {d["device_code"]: d for d in devices}
        hosts = lg.riding_hosts(devices, hosts)
        incoming: dict[str, dict] = {}
        inv_at = _ts(await self.db.kv_updated("inventory"))
        leaving: set[str] = set()   # replicants a job is about to move: the system they're in now isn't covered for long
        fulfilling: set[str] = set()   # deals a logistics job is on its way to fulfill: one replicant each
        for j in jobs:
            if j["status"] not in ("running", "waiting"):
                continue
            t = (j.get("meta") or {}).get("logistics")
            if t and any(str(st.get("desc") or "").startswith(("fulfill ", "execute ")) for st in j["steps"][j.get("idx", 0):]):
                # live 2026-10-25: three replicant carriers left for Geothermal Surplus at ELUSORYR-3 within two hours
                fulfilling |= {str(x).split(":", 1)[1] for x in (t.get("fulfils") or [t.get("req")]) if x and ":" in str(x)}
            if t and t.get("to"):
                incoming[t["to"]] = {r: incoming.get(t["to"], {}).get(r, 0) + q for r, q in (t.get("carry") or {}).items()}
            # what any job (logistics run, fleet mission, relocation) is still going to pick up is spoken for, and so is
            # what it loaded since the last stockpile poll (seen live 2026-10-13: four haulers sent to KELMORNEA-3 for what
            # an earlier trip was already fetching; 2026-10-17: three loads at an ALPHARDAN-BELT-1 already emptied)
            at: dict[str, str] = {}
            for i, st in enumerate(j["steps"]):
                b, path = st.get("body") or {}, str(st.get("path") or "")
                code = path.split("/")[2] if path.startswith("/devices/") else None
                pending = i >= j.get("idx", 0)
                if b.get("command") == "travel" and code:
                    at[code] = b.get("destination")
                    if pending and code in hosts:
                        leaving.add(hosts[code])
                elif path.startswith("/replicants/") and path.endswith("/travel") and pending:
                    leaving.add(path.split("/")[2])
                elif b.get("command") == "collect_resources" and code and "(the payout)" not in st["desc"] \
                        and "(received)" not in st["desc"] and st.get("status") not in ("skipped", "failed"):
                    sent = _ts(st.get("started_at"))
                    if st.get("status") == "done" and sent and inv_at and sent < inv_at:
                        continue   # the stockpile list already shows it gone
                    named = st["desc"].rsplit(" at ", 1)[1] if " at " in st["desc"] else None   # "…: load 8 silicates at X"
                    pile = named or at.get(code) or (by_code0.get(code) or {}).get("location")
                    if pile:
                        lg.take_from(inv, pile, as_amounts(b.get("resources") or {}))
        taken, mission_stars, away_fleets = set(), set(), set()
        from .fleets import fleet_tag
        for f in items:
            m = f.get("mission") or {}
            if m.get("status") in ("running", "stalled", "stopped"):
                away_fleets.add(fleet_tag(f["id"]))   # out there on purpose: not stranded
                mission_stars |= {star_of(x) for x in m.get("targets") or []}
                if m.get("contract") or m.get("trade"):
                    from .fleets import deal as _deal
                    d = _deal(m)
                    taken.add(d.get("designation") or f"{d.get('controller')}:{d.get('trade_code')}")
        locked = wards.foreign(cat, devices)
        contracts = []
        from .fleets import device_counts as fl_counts
        life, approvals = await gev.life_map(self.db), await self.db.kv_get(gev.APPROVALS_KV, {}) or {}
        for des, e in (await gev.load(self.db)).items():
            if e.get("status") != "open" or not e.get("location") or des in taken or star_of(e["location"]) in locked:
                continue
            if not await gev.approved(self.db, e, life, approvals):
                continue
            best = gev.progress(e, inv, devices, reps).get("best") or {}
            price = {x["resource"]: x["need"] for x in best.get("resources") or []}
            dshort = any(x["short_here"] for x in best.get("devices") or [])
            if price or best.get("devices"):
                rw = e.get("rewards") if isinstance(e.get("rewards"), dict) else {}
                contracts.append({"designation": des, "location": e["location"], "title": e.get("title"), "price": price,
                                  "devices_short": dshort, "fulfilling": des in fulfilling, "rewards": rw.get("resources") or {},
                                  "reward_devices": fl_counts(rw.get("devices"))})
        from collections import Counter
        from .fleets import device_counts as fl_counts
        ok = await self.db.kv_get(lg.KV_TRADES, {}) or {}
        trades = []
        from .shop import known_traders
        for ctrl, t in (await known_traders(self.db)).items():
            for tr in t.get("trades") or []:
                key = f"{ctrl}:{tr.get('trade_code')}"
                once = key in (ok.get("trades") or {})   # only a trade you approved: shop listings are never fulfilled
                # a ward's species lock stops other players completing location events (contracts) in its system, not a
                # shop's trades: Bill's shop is often at SOL, which is warded (owner 2026-10-20: an approved SOL trade was
                # left out of the requests)
                if not once or key in taken or not t.get("location") or tr.get("current_stock") == 0:
                    continue
                n_ = lg.trade_times(ok, key, tr.get("current_stock"))   # units approved (the game sells one per call)
                price = lg.times(as_amounts((tr.get("criteria") or {}).get("resources") or {}), n_)
                dneed = lg.times(fl_counts((tr.get("criteria") or {}).get("devices")), n_)
                there = Counter(d.get("device_type") for d in devices if d.get("location") == t["location"])
                if price or dneed:
                    trades.append({"key": key, "controller": ctrl, "trade_code": tr.get("trade_code"), "name": tr.get("name"),
                                   "location": t["location"], "price": price, "once": once, "times": n_,
                                   "devices_short": any(there[k] < n for k, n in dneed.items()),
                                   "rewards": lg.times(as_amounts((tr.get("rewards") or {}).get("resources") or {}), n_),
                                   "reward_devices": lg.times(fl_counts((tr.get("rewards") or {}).get("devices")), n_)})
        # a replicant about to leave doesn't cover its system for a delivery planned now (seen live 2026-10-17: two
        # deliveries to RHYBRENEUS-3 sent unescorted found the system out of comms range on arrival)
        commandable = await self.commandable(devices, stars, without=leaving)
        evac = await self.evacuation_targets(devices, stars)   # piles where an asteroid is due: {location: safe spot}
        drop_raw = lambda s_: lo.drop_point(s_, devices, inv, stars)  # noqa: E731
        homes_now = {f.get("home") for f in items if f.get("home") and f.get("station")}
        moves = await self.db.kv_get(lg.KV_MOVES, {}) or {}
        left = {star_of(l) for l, i in inv.items() if sum(as_amounts(i).values()) >= 1}
        moved = {o: e["to"] for o, e in moves.items() if o in left and o not in homes_now and e.get("to")}
        if moved != {o: e.get("to") for o, e in moves.items()}:
            await self.db.kv_set(lg.KV_MOVES, {o: moves[o] for o in moved})   # emptied, or a home again: done
        from . import consolidate as co
        sent = await self.db.kv_get("logistics_ami", {}) or {}   # AMI deliveries the logistics handed off: on their way until done
        for c, e in list(sent.items()):
            d = by_code0.get(c) or {}
            age = (_now() - (_ts(e.get("at")) or _now())).total_seconds()
            if age > 600 and (co.free_controller(d, devices, set()) or age > 3 * 3600):
                sent.pop(c)
            elif e.get("to"):
                incoming[e["to"]] = {r: incoming.get(e["to"], {}).get(r, 0) + q for r, q in (e.get("carry") or {}).items()}
                for g in e.get("legs") or []:   # the controller hasn't finished: its pick-up is spoken for too
                    lg.take_from(inv, g["pile"], g["take"])
        ami = [c for c in devices if co.free_controller(c, devices, busy) and c["device_code"] not in sent]
        # systems the Consolidate rule looks after (it's on, and they have an AMI transport controller): logistics leaves
        # their piles to it
        no_consolidate = ({star_of(c.get("location")) for c in devices if "transport" in (c.get("device_type") or "")
                           and "controller" in (c.get("device_type") or "") and c.get("location")}
                          if await self.rule_cfg("consolidate") else set())
        from .fleets import members as fl_members
        for f in lg.logistics_fleets(items):   # a member an AMI controller adopted (a ferry, say) is let go when idle
            for d in fl_members(f, devices):
                c = d.get("controller_device_code")
                if c and d["device_code"] not in busy and c not in busy and str(d.get("status") or "").startswith("idle"):
                    if await self.create_job("logistics", f"logistics: {c} releases {d['device_code']}", c,
                                             [step(f"{c}: release {d['device_code']} (it belongs to {f.get('name')})",
                                                   f"/devices/{c}", {"command": "release", "devices": [d["device_code"]]})],
                                             {"devices": [d["device_code"], c]}):
                        busy |= {d["device_code"], c}
        repair_to: dict[str, int] = defaultdict(int)   # repair devices on their way (lg.repair_runs)
        for j in jobs:
            t = (j.get("meta") or {}).get("logistics") or {}
            if j["status"] in ("running", "waiting") and str(t.get("req") or "").startswith("repair:"):
                repair_to[star_of(t.get("to"))] += 1
        out, listings = await self.listing_requests(reps)
        # relays go first: a vessel hosting a replicant near relays or beacons waiting for an escort is left to the loadouts
        ll = await self.db.kv_get("loadouts_last", {}) or {}
        escorts = {}
        if ll.get("escort_wanted") and _ts(ll.get("at")) and (_now() - _ts(ll["at"])).total_seconds() < 3600:
            escorts = lg.keep_escorts(ll["escort_wanted"], items, devices, hosts, busy, bps, pos, await self.supply_reach())
            busy |= set(escorts)
        wanted = None
        from . import wardhub as wh
        mcfg = await self.rule_cfg("maintenance") or {}   # hub upkeep is stocked at the hub's own spot (owner 2026-10-25)
        upkeep = (wh.upkeep_rows(devices, {l: as_amounts(i) for l, i in inv.items()})
                  if mcfg.get("hub_upkeep", True) and mcfg else [])
        p = lg.plan(fleets=items, devices=devices, bps=bps, inventory=inv, pos=pos, commandable=commandable, hosts=hosts,
                    busy=busy, reach=lg.SECTOR_RANGE_LY, contracts=contracts, trades=trades, incoming=incoming,
                    replicants=reps, drop=lambda s_: lo.destination(s_, stars) if drop_raw(s_) in evac else drop_raw(s_),
                    busy_stars=mission_stars,
                    prefs=await self.db.kv_get(lg.KV_PREFS, {}) or {}, ami=ami, moved=moved,
                    dark_nets=await self.dark_networks(devices, pos, bps, reps), away_fleets=away_fleets,
                    no_consolidate=no_consolidate, evacuate=evac, listings=listings,
                    repair_threshold=float(((await self.settings())["rules"].get("maintenance") or {}).get("threshold") or 80),
                    repair_to=repair_to,
                    # repair patrols stay in the network or at a fleet's home (live 2026-10-22: one sent to ERAJAELAE, lit
                    # only by a survey fleet's replicant, arrived after the fleet left and is stranded out of range)
                    repair_stars=(await self.commandable(devices, stars, networked=True)) | (homes_now & commandable),
                    lanes=(await self.db.kv_get(lg.KV_STATE, {}) or {}).get("lanes"), upkeep=upkeep,
                    rescue_pause=lg.rescue_pauses(await self.db.kv_get(lg.KV_RESCUE_PAUSE, {})))
        for r in p["requests"]:   # a rescue beyond the sector range waits for your OK (owner 2026-10-22): one alert each
            key = f"rescue-ok:{r['id']}"
            if r.get("confirm") and key not in self.stage_errors:
                self.stage_errors[key] = "asked"
                await self.log("logistics", f"{r['label']}: " + (f"about {r['trip_h']:.0f} h of travel" if r.get("trip_h") is not None
                                                                     else "trip of unknown length") + " — waits for your Confirm "
                                            "on Fleets › Logistics", "alert", notify=True)
        by_code = by_code0
        pickups = await self.db.kv_get(lg.KV_PICKUPS, {}) or {}
        # devices a deal paid out: once they're at the site (fleetless, going nowhere), they're sent to the gather point,
        # carried like any delivery (logistics carriers first, a replicant along beyond the network)
        for loc, pk in list(pickups.items()):
            left = dict(pk.get("types") or {})
            for d in devices:
                t = d.get("device_type")
                tags = d.get("tags") or []
                if d.get("location") != loc or not left.get(t) or any(x.startswith(("fleet:", "to:")) for x in tags) \
                        or d["device_code"] in busy:
                    continue
                if await self.create_job("logistics", f"logistics: {d['device_code']} (payout at {loc}) → {pk['to']}",
                                         d["device_code"], [lo.tag_step(d["device_code"], [lo.to_tag(pk["to"])])],
                                         {"devices": [d["device_code"]]}):
                    left[t] -= 1
            left = {t: n for t, n in left.items() if n > 0}
            if not left or (_now() - (_ts(pk.get("at")) or _now())).total_seconds() > 86400:
                pickups.pop(loc)
            else:
                pk["types"] = left
        template = ((await self.db.kv_get("event_settings", {}) or {}).get("fulfil") or "").strip() or None
        for run in p["runs"]:
            steps = lg.run_steps(run, by_code, template)
            if not steps:
                continue
            codes = lg.run_devices(run)
            legs = [g for c in run.get("cargos") or [] for g in c.get("legs") or []] + (run.get("legs") or [])
            job = await self.create_job("logistics", f"logistics: {lg.describe(run)}", codes[0] if codes else run.get("ami"), steps,
                                        {"devices": codes, "logistics": {"req": run.get("req"), "to": run["to"], "carry": run["carry"],
                                                                         "legs": legs, **({"fulfils": [run.get("req")] + [
                                                                             x["req"] for x in run.get("also") or []]}
                                                                             if run.get("fulfil") else {})}})
            if job:
                run["job"] = job["id"]
                if run["kind"] == "listing":
                    from .shop import LISTING_KV
                    wanted = wanted if wanted is not None else await self.db.kv_get(LISTING_KV, {}) or {}
                    if run["controller"] in wanted:
                        wanted[run["controller"]]["job"] = job["id"]
                if run.get("ami"):
                    sent[run["ami"]] = {"to": run["to"], "carry": run["carry"], "at": now_iso(), "req": run.get("req"),
                                        "legs": legs}
                if run.get("fulfil") and run.get("reward_devices") and run.get("home"):
                    pickups[run["to"]] = {"types": run["reward_devices"], "to": star_of(run["home"]), "at": now_iso()}
                out.append(lg.describe(run))
                if run.get("fulfil"):   # an approval counts once (each trade the run fulfills)
                    keys = [x["req"].split(":", 1)[1] for x in run.get("also") or [] if x["kind"] == "trade"]
                    if run["kind"] == "trade":
                        keys.append(run["req"].split(":", 1)[1])
                    for key in keys:
                        ok.setdefault("trades", {}).pop(key, None)
                        ok.setdefault("counts", {}).pop(key, None)
                        ok.setdefault("done", {})[key] = now_iso()
                    if keys:
                        await self.db.kv_set(lg.KV_TRADES, ok)
        if wanted is not None:
            from .shop import LISTING_KV
            await self.db.kv_set(LISTING_KV, wanted)
        await self.db.kv_set("logistics_ami", sent)
        await self.db.kv_set(lg.KV_PICKUPS, pickups)
        await self.db.kv_set(lg.KV_STATE, {"at": now_iso(), "requests": p["requests"], "runs": p["runs"], "unmet": p["unmet"],
                                           "fleet_devices": p["fleet_devices"], "reserve": p["reserve"], "escorts": escorts,
                                           "lanes": p["lanes"]})
        await self.sample_backlog(items, devices, inv_raw, drop_raw, busy, p, bps, reps)
        await self.note_demand({f"req:{r['id']}": r.get("to") for r in p["requests"] if not r.get("skipped")})
        out += await self.balance_pass(items, devices, hosts, pos, commandable, stars, inv_raw, set(escorts))
        for line in out:
            await self.log("logistics", line)
        return out

    async def note_demand(self, wants: dict[str, str | None]) -> None:
        """What needed a carrier where ({key: location}), first seen, kept balance.DEMAND_DAYS: the hubs' weights."""
        from . import balance as bal
        dem = await self.db.kv_get(bal.KV_DEMAND, {}) or {}
        now = _now()
        dem = {k: e for k, e in dem.items() if (now - (_ts(e.get("at")) or now)).total_seconds() < bal.DEMAND_DAYS * 86400}
        for k, loc in wants.items():
            if loc and k not in dem:
                dem[k] = {"star": star_of(loc), "at": now_iso()}
        await self.db.kv_set(bal.KV_DEMAND, dem)

    async def balance_pass(self, items: list[dict], devices: list[dict], hosts: dict, pos: dict, commandable: set[str],
                           stars: dict, inv: dict, keep: set[str]) -> list[str]:
        """Idle logistics carriers spread over the hubs by demand (balance.py, owner 2026-10-23)."""
        from . import balance as bal
        from . import loadouts as lo
        from . import logistics as lg
        jobs = await self.jobs()
        busy = self.busy_devices(jobs)
        idle = {c: (_ts(t) or _now()).timestamp() for c, t in (await self.db.kv_get(bal.KV_IDLE, {}) or {}).items()}
        p = bal.plan(items, devices, jobs, hosts, busy, pos, commandable, lg.SECTOR_RANGE_LY,
                     await self.db.kv_get(bal.KV_DEMAND, {}) or {}, idle, _now().timestamp(), keep,
                     leeway=await self.db.kv_get(bal.KV_LEEWAY, bal.LEEWAY_PCT))
        await self.db.kv_set(bal.KV_IDLE, {c: datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="seconds")
                                           for c, t in idle.items()})
        await self.db.kv_set("hub_balance", {"at": now_iso(), **{k: p[k] for k in ("hubs", "weights", "classes")}})
        out = []
        for m in p["moves"]:
            code, rep = m["code"], m.get("replicant")
            dest = lo.drop_point(m["to"], devices, inv, stars)
            st = step(f"{code} → {dest} ({bal.CLASSES[m['cls']]}: {m['from']} has more than its share, {m['to']} fewer)",
                      f"/replicants/{rep}/travel" if rep else f"/devices/{code}",
                      {"destination": dest} if rep else {"command": "travel", "destination": dest},
                      wait=["travel.arrived"], match={"destination": m["to"]}, critical=True)
            st["wait_device"] = code
            if await self.create_job("logistics", f"logistics: {code} rebalances {m['from']} → {m['to']}", code, [st],
                                     {"devices": [code], "star": m["to"]}):
                out.append(f"{code} rebalances {m['from']} → {m['to']}")
        return out

    async def sample_backlog(self, items: list[dict], devices: list[dict], inv: dict, drop: Any, busy: set[str],
                             p: dict, bps: dict, reps: dict) -> None:
        """Dashboard › Logistics backlog and Trends (metrics.py): request ages every pass, a sample every ten minutes."""
        from . import logistics as lg, metrics, relaynet
        from .fleets import strays
        try:
            now = _now()
            jobs_req = {str((j.get("meta") or {}).get("logistics", {}).get("req")) for j in await self.jobs()
                        if j.get("rule") == "logistics" and j.get("status") in ("running", "waiting")
                        and (j.get("meta") or {}).get("logistics", {}).get("req")}
            track = metrics.track(await self.db.kv_get(metrics.KV_TRACK, {}),
                                  {r["id"] for r in p["requests"] if not r.get("skipped")} | jobs_req, now)
            await self.db.kv_set(metrics.KV_TRACK, track)
            if not await metrics.due(self.db):
                return
            receiving = {drop(f["home"]) for f in items if f.get("home") and (f.get("station") or f.get("materials") == "self")}
            carriers = lg.fleet_devices(items, devices)
            pos = await self.star_lookup("pos")
            cov = relaynet.coverage(relaynet.networks(devices, pos, bps), pos, relaynet.replicant_stars(reps, devices))
            relay = set().union(*[cov["covered"].get(i, set()) for i in cov["active"]]) if cov["active"] else set()
            await metrics.record(self.db, {
                **metrics.backlog(inventory=inv, receiving=receiving, carriers=carriers, devices=devices,
                                  strays=strays(items, devices, busy),
                                  fleet_homes={f["id"]: f.get("home") or "" for f in items if f.get("id")},
                                  requests=p["requests"], unmet=p["unmet"]),
                **metrics.ages(track, now), **metrics.carrier_use(carriers, busy), **metrics.missions(items),
                "expired_24h": await metrics.noted(self.db, "expired", now), **metrics.roles(items, devices),
                **await metrics.mining_rates(self.db, now),
                "cmd_systems": len(cov["commandable"] - relaynet.dark_stars(devices)), "relay_systems": len(relay)})
        except Exception as e:   # a chart sample never stops logistics
            log.warning("backlog sample failed: %s", e)

    async def contract_supply_pass(self, force: bool = False) -> list[str]:
        """Contracts asking for devices at their location (contractsupply.py): every few minutes, for each contract
        you set delivering (Contracts page) or a trade fleet is running, send fleetless spare / idle devices there and
        print only what you authorized; when a contract closes, its leftover devices become spare again."""
        from . import contractsupply as cs
        from . import fleets as fl
        from . import gameevents as gev
        from . import loadouts as lo
        from .shapes import normalize_blueprints, normalize_inventory
        last = _ts(await self.db.kv_get("contract_supply_last", None))
        if not force and last and (_now() - last).total_seconds() < 240:
            return []
        await self.db.kv_set("contract_supply_last", now_iso())
        supply = await self.db.kv_get(cs.KV, {}) or {}
        evs = await gev.load(self.db)
        devices = await self.devices()
        fleets_now = await self.fleets()
        running = {fl.deal(f["mission"]).get("designation") for f in fleets_now
                   if (f.get("mission") or {}).get("status") in ("running", "stalled") and (f["mission"].get("contract"))}
        # trade runs paid in devices: supplied like a contract, keyed trade:<controller>:<trade code>
        trade_runs: dict[str, dict] = {}
        for f in fleets_now:
            m_ = f.get("mission") or {}
            if m_.get("status") in ("running", "stalled") and m_.get("trade"):
                dl = fl.deal(m_)
                if dl["devices"] and dl["location"]:
                    trade_runs[dl["supply_key"]] = {
                        "designation": dl["supply_key"], "location": dl["location"], "star": dl["star"], "status": "open",
                        "title": dl["label"], "_need": dl["devices"]}
        # trades you approved for the logistics service (Shop: approve) that are paid in devices: supplied the same way
        from . import logistics as lg
        from .shop import known_traders
        ok = await self.db.kv_get(lg.KV_TRADES, {}) or {}
        if ok.get("trades"):
            for ctrl, t in (await known_traders(self.db)).items():
                for tr in t.get("trades") or []:
                    key = f"{ctrl}:{tr.get('trade_code')}"
                    need = lg.times(fl.device_counts((tr.get("criteria") or {}).get("devices")),
                                    lg.trade_times(ok, key, tr.get("current_stock")))
                    if need and t.get("location") and key in (ok.get("trades") or {}):
                        trade_runs.setdefault(f"trade:{key}", {
                            "designation": f"trade:{key}", "location": t["location"], "star": star_of(t["location"]),
                            "status": "open", "title": f"trade {tr.get('name') or tr.get('trade_code')}", "_need": need})
        out: list[str] = []
        jobs = await self.jobs()
        busy = self.busy_devices(jobs)
        # closed (or no longer wanted) contracts: their leftover devices go spare, the entry goes
        for des in list(supply):
            e = evs.get(des) or trade_runs.get(des)
            if des in trade_runs or (e and e.get("status") == "open" and (supply[des].get("active") or des in running)):
                continue
            tag = cs.contract_tag(des)
            left = [d for d in devices if tag in (d.get("tags") or []) and d.get("device_code") not in busy]
            steps = [st for d in left for st in cs.release_steps(d, des)]
            if steps:
                job = await self.create_job("contracts", f"contract {des}: {len(left)} leftover device(s) back to spare",
                                            None, steps, {"devices": [d["device_code"] for d in left]})
                if not job:
                    continue   # dry run: keep the entry so they're released once it's off
                out.append(f"{des}: released {len(left)}")
            if not e or e.get("status") != "open":
                supply.pop(des, None)
        cfg = await self.loadout_cfg()
        ls = cfg.get("settings") or {}
        reach = float(ls.get("max_supply_ly") if ls.get("max_supply_ly") is not None else lo.DEFAULT_SETTINGS["max_supply_ly"])
        stationed = {f["home"] for f in lo.stationed_fleets(cfg)}
        ignore = set(cfg.get("ignore_tags") or [])
        reps = await self.db.kv_get("replicants", {}) or {}
        hosts = {r.get("hosted_device_code") for r in reps.values() if r.get("hosted_device_code")}
        inv = {i.get("location"): i.get("items") or {} for i in normalize_inventory(await self.db.kv_get("inventory", []))}
        bps = normalize_blueprints(await self.db.kv_get("blueprints", []))
        cat = await self.db.kv_get("stars", {}) or {}
        pos = {x.get("designation"): x.get("position") or {} for x in cat.get("stars") or [] if isinstance(x, dict)}
        from .traffic import beacons as _beacons
        civ_locs = {r["location"] for r in await self.civ_coverage() if r.get("completed") or r.get("open")}
        protect = {b["device_code"] for b in _beacons(devices) if b.get("location") in civ_locs}   # as the loadouts pass
        for des, e in [*evs.items(), *trade_runs.items()]:
            if e.get("status") != "open" or not e.get("location"):
                continue
            prog = ({"best": {"devices": [{"device_type": t, "need": n} for t, n in e["_need"].items()]}} if "_need" in e
                    else gev.progress(e, inv, devices, reps))
            if not cs.need_of(prog):
                continue
            entry = supply.get(des) or {}
            active = bool(entry.get("active")) or des in running or des in trade_runs
            if not active and des not in supply:
                continue
            p = cs.plan(e, prog, entry, devices, hosts=hosts, busy=busy, protect=protect, ignore=ignore,
                        stationed=stationed, blueprints=bps, inventory=inv, pos=pos, reach=reach, active=active)
            by = {d.get("device_code"): d for d in devices}
            entry.update({"location": e["location"], "star": e["star"], "assigned": p["assigned"], "printed": p["printed"],
                          "orders": p["orders"], "checked_at": now_iso(), "rows": p["rows"], "ready": p["ready"]})
            types = entry.setdefault("printed_types", {})
            for c in p["printed"]:
                if c in by:
                    types[c] = by[c].get("device_type")
            if p["send"]:
                steps = [st for d in p["send"] for st in cs.send_steps(d, e)]
                codes = [d["device_code"] for d in p["send"]]
                if await self.create_job("contracts", f"contract {des}: send {', '.join(codes)} to {e['location']}",
                                         None, steps, {"devices": codes}):
                    for d in p["send"]:
                        entry["assigned"][d["device_code"]] = {"type": d.get("device_type"), "at": now_iso()}
                    busy |= set(codes)
                    out.append(f"{des}: sending {len(codes)}")
            for pr in p["prints"]:
                if await self.create_job("contracts", f"contract {des}: print {pr['n']}× {pr['device_type']} on "
                                         f"{pr['factory']} for {e['location']}", pr["factory"], lo.print_steps(pr),
                                         {"devices": [], "star": e["star"]}):
                    entry["orders"].append({"type": pr["device_type"], "factory": pr["factory"], "n": pr["n"], "at": now_iso()})
                    busy.add(pr["factory"])
                    out.append(f"{des}: printing {pr['n']}× {pr['device_type']}")
            supply[des] = entry
        await self.db.kv_set(cs.KV, supply)
        for line in out:
            await self.log("contracts", line)
        return out

    async def _contract_option_ready(self, m: dict, inv: dict, devices: list[dict]) -> bool:
        from . import fleets as fl
        from . import gameevents as gev
        e = (await gev.load(self.db)).get(fl.deal(m).get("designation") or "")
        if not e or e.get("status") != "open":
            return False
        prog = gev.progress(e, inv, devices, {})
        if any(c["chosen"] for c in prog["criteria"]):   # you picked the option: only that one counts
            return bool(prog["best"]["ready_here"])
        return any(c["ready_here"] for c in prog["criteria"])

    async def _deal_devices_short(self, m: dict, devices: list[dict]) -> str:
        """A contract run whose contract also asks for devices at its location: what's still missing there."""
        from . import fleets as fl
        from . import gameevents as gev
        dl = fl.deal(m)
        if dl["kind"] == "trade":   # a trade paid in devices: they must be at the shop's location
            have = {t: sum(1 for d in devices if d.get("device_type") == t and d.get("location") == dl["location"])
                    for t in dl["devices"]}
            short = {t: n - have[t] for t, n in dl["devices"].items() if n > have[t]}
            return ", ".join(f"{n}× {t.replace('_', ' ')}" for t, n in sorted(short.items()))
        des = dl.get("designation")
        if not m.get("contract") or not des:
            return ""
        e = (await gev.load(self.db)).get(des)
        if not e or e.get("status") != "open":
            return ""
        prog = gev.progress(e, {}, devices, {})
        short = {x["device_type"]: x["short_here"] for x in (prog.get("best") or {}).get("devices") or [] if x["short_here"]}
        return ", ".join(f"{n}× {t.replace('_', ' ')}" for t, n in sorted(short.items()))

    async def _deal_gate(self, fleet: dict, m: dict, devices: list[dict], arriving: bool = False) -> tuple[str, str]:
        """A trade run at its wait phase: ("go", "") when the whole price is at the site and the fleet's replicant is
        there; ("wait", why) while it isn't (or materials are still on their way); ("rewind", why) when the site is
        short and nothing covers it — the mission steps back to deliver (cargo still aboard at the site) or to gather
        (load) the missing materials again."""
        from . import fleets as fl
        from .shapes import normalize_inventory
        inv = {i.get("location"): i.get("items") or {} for i in normalize_inventory(await self.db.kv_get("inventory", []))}
        dl = fl.deal(m)
        site = dl["location"]
        if dl["kind"] == "trade" and dl.get("controller"):
            from .shop import known_traders
            t = (await known_traders(self.db)).get(dl["controller"]) or {}
            if t.get("location") and t["location"] != site:
                # a newer scan or directory read has the shop elsewhere (Bill's moves about daily)
                why = (f"the shop has moved: last seen at {t['location']} ({t.get('seen_at')}), not {site} — stop this run "
                       "and start the trade again there")
                if m.get("watch_note") != why:
                    self._mlog(m, why)
                    await self.log("fleets", f"{fleet['name']}: {why}", "alert", notify=True)
                return "wait", why
        state, short = fl.deal_check(m, inv, devices)
        if state != "ok" and m.get("contract") and await self._contract_option_ready(m, inv, devices):
            # contracts offer alternative options (seen live 2026-10-08: Famine Assistance at AEMEROTH-2 — 3 orbital farms,
            # or a nutrient synthesizer and resources …): another option is complete at the site, so the price this run
            # set out to bring isn't needed
            state, short = "ok", {}
        what = ", ".join(f"{q} {r}" for r, q in short.items())
        if state == "incoming":
            return "wait", f"{site} is short of {what}: it's on its way there"
        if state in ("redeliver", "regather"):
            m["phase"] = "travel" if state == "redeliver" else None   # next: deliver / load
            if state == "regather":
                m["loaded"] = []
            why = (f"{site} is short of {what}: " + ("depositing the cargo still aboard" if state == "redeliver"
                                                       else "back to gathering it"))
            self._mlog(m, why)
            return "rewind", why
        dev_short = await self._deal_devices_short(m, devices)
        if dev_short:   # devices come by the contract's own delivery (contract_supply_pass), not in the fleet's holds
            if arriving:
                self._mlog(m, f"resources at {site}: waiting for devices there ({dev_short}) — Contracts page")
            return "wait", (f"{site} still needs {dev_short}: they're delivered separately ("
                            + ("spare ones are sent there; " if m.get("trade") else "Contracts page — ")
                            + "printing them needs your authorization)")
        reps = await self.db.kv_get("replicants", {}) or {}
        own = fl.fleet_replicant(fleet, devices, reps)
        by = {d.get("device_code"): d for d in devices}

        def at_site(code: str, r: dict) -> bool:
            host = by.get(r.get("hosted_device_code") or "") or {}
            return site in (r.get("location"), r.get("current_location"), host.get("location"))
        if own:   # the fleet's own replicant fulfills its trades
            code, r = own
            m["fulfiller"] = code
            if not at_site(code, r):
                if arriving:
                    self._mlog(m, f"everything delivered to {site}: waiting for {r.get('name') or code} (rides with the fleet)")
                return "wait", f"materials at {site}: waiting for {r.get('name') or code} (the fleet's replicant) to get there"
            self._mlog(m, f"{r.get('name') or code} at {site}: fulfilling")
            return "go", ""
        here = [c for c, r in reps.items() if at_site(c, r)]
        if not here:
            if arriving:
                self._mlog(m, f"everything delivered to {site}: waiting for a replicant there (none rides with the fleet)")
                await self.log("fleets", f"{fleet['name']}: materials at {site} — no replicant rides with the fleet; send "
                                         f"one there to fulfill {dl['label']}", notify=True)
            return "wait", f"materials at {site}: waiting for a replicant there to fulfill"
        m["fulfiller"] = here[0]
        self._mlog(m, f"{reps[here[0]].get('name') or here[0]} at {site}: fulfilling")
        return "go", ""

    async def next_salvage(self, m: dict) -> str | None:
        """Another salvage in a mining mission's system that it hasn't used up yet (the current one counts as used)."""
        from .salvage import available_salvage
        from .targets import system_resources
        target = (m.get("targets") or [None])[m.get("idx", 0)] if m.get("targets") else None
        if not target:
            return None
        used = set(m.get("salvaged") or []) | {m.get("salvage")}
        left = [x for x in available_salvage(await system_resources(self.db, star_of(target))) if x["code"] not in used]
        return left[0]["code"] if left else None

    async def fleet_phase_steps(self, fleet: dict, m: dict, phase: str, devices: list[dict]) -> tuple[list[dict], list[str]]:
        from . import fleets as fl
        from . import loadouts as lo
        from .shapes import normalize_inventory
        cat = await self.db.kv_get("stars", {}) or {}
        stars = {x.get("designation"): x for x in (cat.get("stars") or []) if isinstance(x, dict)}
        inv = {i.get("location"): i.get("items") or {} for i in normalize_inventory(await self.db.kv_get("inventory", []))}
        target = (m.get("targets") or [None])[m.get("idx", 0)] if m.get("targets") else None
        opts = m.get("opts") or {}
        if fleet["role"] == "mining" and phase in ("travel", "deploy", "work") and target \
                and star_of(target) in await self.warded(devices):
            m["stall"] = True   # warded since the mission started: nothing of ours can mine there, so don't go / unload
            return [], [f"another player's system ward or hub is in {star_of(target)} — nothing of ours can mine there; "
                        "recall the fleet or pick another system"]
        if m.get("relocate") and m.get("solo"):
            # surge-capable members not needed as carriers fly to the new home on their own (solo_pass): the
            # relocation's packing, gathering and travel leave them out
            ftag, solo = fl.fleet_tag(fleet["id"]), set(m["solo"])
            devices = [{**d, "tags": [x for x in d.get("tags") or [] if x != ftag]} if d.get("device_code") in solo else d
                       for d in devices]
        radii, far_au = await self.cruise_radii(), await self.max_cruise_au()
        if phase in ("assemble", "board"):   # board: boarding stragglers before a later leg (next_phase)
            return fl.assemble_steps(fleet, devices, radii, far_au)
        if phase == "gather":
            plan = fl.gather_plan(fleet, devices, stars, self.busy_devices(await self.jobs()), await self.supply_reach())
            if plan["recruit"]:
                self._mlog(m, "recruiting spares: " + ", ".join(f"{d['device_code']} ({d.get('device_type')})" for d in plan["recruit"]))
            if plan["tour"]:
                self._mlog(m, f"{plan['carrier']} collects from " + ", ".join(f"{s_} ({len(ds)})" for s_, ds in plan["tour"]))
            return fl.gather_steps(fleet, plan, stars, radii, far_au), plan["problems"]
        if phase == "travel":
            dark = await self.dark_home(fleet, target, devices) if m.get("relocate") else None
            if dark:
                m["stall"] = True
                return [], [dark]
            return fl.travel_steps(fleet, devices, target, stars), []
        if phase in ("deploy", "work") and target:
            behind = fl.left_behind(fleet, devices, target)
            if behind:
                # the travel leg didn't take the crew there: fly again rather than unload or survey in the wrong system
                where = ", ".join(f"{c['device_code']} in {star_of(c['location'])}" for c in behind)
                tries = m.setdefault("retravel", {})
                key = str(m.get("idx", 0))
                if tries.get(key, 0) >= 2:
                    m["stall"] = True
                    return [], [f"the fleet's carrier is still not in {star_of(target)} ({where}) after two more trips — "
                                "check its route, then retry"]
                tries[key] = tries.get(key, 0) + 1
                self._mlog(m, f"{where} with the crew aboard, not in {star_of(target)}: travelling again")
                m["phase"] = "gather"   # the loop's next phase is travel
                return [], []
        if phase == "deploy":
            if fleet["role"] == "explore" and target and await self.system_surveyed(target):
                # nothing left to survey there: don't unload, move on (the survey phase is skipped too)
                m["skip_survey"] = star_of(target)
                self._mlog(m, f"{star_of(target)} is already surveyed — not deploying, moving on")
                return [], []
            from . import placement as pl
            geo = pl.geography(target or "", devices, await self.system_scan(target) if target else None,
                               (stars.get(target) or {}).get("entry_point"))
            spot = fl.deploy_spot(geo)
            if not spot and fleet["role"] == "explore" and fl.loaded_outer(fleet, devices):
                # never drop a survey crew at the Kuiper belt or Oort cloud (owner 2026-10-21): the work phase takes
                # the carrier inside once the system's bodies are known
                self._mlog(m, f"nothing known inside {target} yet (no scan): the crew stays aboard for now")
                return [], []
            if not spot:
                self._mlog(m, f"nothing known inside {target} yet (no scan): unloading where the carrier is")
            # survey crews no longer drop relays or beacons (2026-10-11): ask for one on the Systems page instead
            return fl.unload_steps(fleet, devices, spot), []
        if phase == "work":
            if fleet["role"] == "explore" and m.get("skip_survey") and m["skip_survey"] == star_of(target or ""):
                return [], []
            if fleet["role"] == "explore":
                from . import placement as pl
                geo = pl.geography(target or "", devices, await self.system_scan(target) if target else None,
                                   (stars.get(target) or {}).get("entry_point"))
                ctrl = next((d for d in fl.members(fleet, devices) if d.get("device_type") == "ami_survey_controller"), None)
                if ctrl:   # it must be in the target system (in a carrier there, or deployed there)
                    by = {d.get("device_code"): d for d in devices}
                    host = by.get(ctrl.get("stowed_in_device_code") or ctrl.get("attached_to_device_code") or "") or {}
                    where = star_of(ctrl.get("location") or host.get("location"))
                    if where and where != star_of(target or ""):
                        # seen live 2026-10-08: it rode in another fleet's vessel, the survey ran in that system instead
                        m["stall"] = True
                        return [], [f"the survey controller {ctrl['device_code']} is in {where}, not in {star_of(target or '')} "
                                    "with the fleet — bring it aboard (Fleets: end mission & board, or move it), then retry"]
                pre, at = [], ctrl.get("location") if ctrl else None
                if fl.loaded_outer(fleet, devices):   # still aboard at the Kuiper belt / Oort cloud (the deploy phase)
                    drop = fl.deploy_spot(geo) or pl.target("ami_survey_controller", geo)
                    pre = fl.unload_steps(fleet, devices, drop)
                    at = at or drop
                spot = pl.target("ami_survey_controller", geo) if not ctrl or not pl.ok("ami_survey_controller", at, geo) \
                    else None   # no controller: where drones off a body or belt go to scan
                steps, problems = fl.explore_work_steps(fleet, devices, spot, radii, far_au)
                for st in steps:
                    if isinstance(st.get("seq0_from"), int):
                        st["seq0_from"] += len(pre)
                return pre + steps, problems
            belt = fl.richest_belt(target, await self.system_scan(target))
            sal = None
            if not belt:   # no belt here: salvage is the only work
                from .salvage import available_salvage
                from .targets import system_resources
                found = [x for x in available_salvage(await system_resources(self.db, target))
                         if x["code"] not in (m.get("salvaged") or [])]   # not one this mission already used up
                if not found:
                    m["stall"] = True
                    return [], [f"{target} has no asteroid belt and no salvage we know of — nothing to mine "
                                "(a survey drone finds salvage on the bodies; then retry)"]
                sal = found[0]
                m["salvage"] = sal["code"]
                self._mlog(m, f"no belt in {target}: salvaging {sal['code']} at {fl.salvage_body(sal['code'])}")
            m["belt"] = belt or fl.salvage_body(sal["code"])
            deliver_to = None
            if opts.get("deliver"):
                fleets = await self.fleets()
                pos = {k: (v or {}).get("position") or {} for k, v in stars.items()}
                # the fleet its materials go to, else (none set, or "supply") the nearest that takes them in from here
                to = fl.materials_target(fleet, fleets) if fleet.get("materials") != "supply" else None
                dests = [to["home"]] if to and to.get("home") and to["home"] != target else \
                    [f["home"] for f in fl.destinations(fleets) if f["home"] != target]
                if dests:
                    near = min(dests, key=lambda d: (lo._dist(target, d, pos), d))
                    deliver_to = lo.drop_point(near, devices, inv, stars)
                    m["deliver_to"] = deliver_to
                else:
                    self._mlog(m, "deliver mode, but no fleet takes materials in (Materials on the Fleets page) — hauling instead")
            return fl.mining_work_steps(fleet, devices, belt, deliver_to, sal)
        if phase == "recall":
            haul = m.get("belt") if fleet["role"] == "mining" and not m.get("deliver_to") and not m.get("end_here") else None
            for c in fl.outside_controllers(fleet, devices):
                self._mlog(m, f"{c['device_code']} ({c.get('device_type')}) runs this fleet's drones but isn't in the fleet "
                              "(no fleet tag): it won't be recalled or taken home — add it under Add / remove devices")
            return fl.recall_steps(fleet, devices, inv, haul, radii, far_au), []
        if phase == "return":
            return fl.travel_steps(fleet, devices, m.get("drop_star") or fleet["home"], stars), []
        if phase == "unload":
            # A stationed fleet's devices work its home system, so they come off the carriers. Any other fleet stays aboard,
            # ready for its next mission: only cargo is deposited (a cargo carrier riding a carrier hops off for it and
            # boards again).
            pile = lo.drop_point(m.get("drop_star") or fleet["home"], devices, inv, stars)
            if fleet.get("station") and fleet.get("role") != "explore":   # a survey crew stays aboard, ready to go again
                steps = fl.unload_steps(fleet, devices)
                self._mlog(m, "home: devices come off the carriers to work the home system (stationed fleet)")
            else:
                steps = []
                self._mlog(m, "home: everyone stays aboard; only cargo is deposited")
            r = fl.roster(fleet, devices)
            carriers = {c["device_code"]: c for c in r["carriers"]}
            for d in fl.members(fleet, devices):
                if int(d.get("cargo_used") or 0) <= 0 or d.get("device_type") not in ("cargo_freighter", "transport_hauler", "transport_drone"):
                    continue
                code = d["device_code"]
                ride = fl.aboard(d, set(carriers)) if not fleet.get("station") else None
                if ride:   # get off, deposit, get back on
                    if d.get("attached_to_device_code") == ride:
                        steps.append(step(f"{ride}: detach {code} (to deposit)", f"/devices/{ride}", {"command": "detach", "device": code}))
                    else:
                        st = step(f"deploy {code} from {ride} (to deposit)", f"/devices/{code}", {"command": "deploy"},
                                  wait=["device.deployed"], timeout=SHORT_TIMEOUT)
                        st["wait_device"] = code
                        steps.append(st)
                st = step(f"{code} → {pile}", f"/devices/{code}", {"command": "travel", "destination": pile},
                          wait=["travel.arrived"], match={"destination": pile})
                st["wait_device"] = code
                steps += [st, step(f"{code}: unload", f"/devices/{code}", {"command": "deposit_resources"})]
                if ride:
                    back = carriers[ride].get("location")
                    if back and back != pile:
                        st = step(f"{code} → {back} (board {ride})", f"/devices/{code}", {"command": "travel", "destination": back},
                                  wait=["travel.arrived"], match={"destination": back})
                        st["wait_device"] = code
                        steps.append(st)
                    steps.append(fl.board_step(ride, code, "attach" if d.get("attached_to_device_code") == ride else "stow"))
            return steps, []
        if fleet["role"] == "trade" and phase in ("load", "deliver", "trade", "collect", "home"):
            return await self.deal_phase_steps(fleet, m, phase, devices, inv, stars)
        return [], []

    async def deal_phase_steps(self, fleet: dict, m: dict, phase: str, devices: list[dict], inv: dict[str, dict],
                               stars: dict[str, dict]) -> tuple[list[dict], list[str]]:
        """A contract / trade mission (fleets.deal): gather the price, deliver it, fulfill, collect the rewards, go home."""
        from . import fleets as fl
        from . import loadouts as lo
        dl = fl.deal(m)
        site = dl["location"]
        pos = {k: (v or {}).get("position") or {} for k, v in stars.items()}

        def dist(a: str, b: str) -> float:
            return lo._dist(a, b, pos)
        if phase == "load":
            need = fl.site_short(dl["price"], inv.get(site) or {})
            m["need"] = need
            if not need:
                self._mlog(m, f"{site} already holds the price: nothing to pick up")
                return [], []
            legs, left, problems = fl.pickup_plan(fleet, devices, need, inv, site, dist)
            m["loaded"] = sorted({x["freighter"] for x in legs})
            for x in legs:
                self._mlog(m, f"{x['freighter']} picks up {', '.join(f'{q} {r}' for r, q in x['take'].items())} at {x['pile']}")
            if left and not legs:
                m["stall"] = True
            return fl.pickup_steps(legs, devices), problems
        if phase == "deliver":
            reps = await self.db.kv_get("replicants", {}) or {}
            hosts = {r.get("hosted_device_code") for r in reps.values() if r.get("hosted_device_code")}
            return fl.site_deliver_steps(fleet, devices, site, hosts, set(m.get("loaded") or [])), []
        if phase == "trade":   # the fleet's replicant (or the one found at the site) fulfills
            settings = await self.db.kv_get("event_settings", {}) or {}
            return [fl.fulfil_step(dl, m.get("fulfiller"), (settings.get("fulfil") or "").strip() or None)], []
        if phase == "collect":
            goods = dl["rewards"] or as_amounts(inv.get(site) or {})
            steps, problems = fl.collect_steps(fleet, devices, site, goods)
            m["drop_star"] = fl.nearest_drop_star(await self.fleets(), dl["star"], fleet["home"], dist)
            return steps, problems
        if phase == "home":
            if m.get("drop_star") and m["drop_star"] != fleet["home"]:
                return fl.travel_steps(fleet, devices, fleet["home"], stars), []
            return [], []
        return [], []

    async def fill_fleets(self, force: bool = False, only: str | None = None) -> list[str]:
        """Rule fleet_fill: idle fleets take spares for the gaps in their loadout (see fleets.fill_plan)."""
        cfg = await self.rule_cfg("fleet_fill")
        if not cfg and not force:
            return []
        if not force:
            last = _ts(await self.db.kv_get("fleet_fill_at", None))
            if last and (_now() - last).total_seconds() < 60 * max(1, int((cfg or {}).get("every_minutes") or 30)):
                return []
            await self.db.kv_set("fleet_fill_at", now_iso())
        from . import fleets as fl
        cat = await self.db.kv_get("stars", {}) or {}
        stars = {x.get("designation"): x for x in (cat.get("stars") or []) if isinstance(x, dict)}
        jobs = await self.jobs()
        busy = self.busy_devices(jobs)
        working = {(j.get("meta") or {}).get("fleet") for j in jobs if j["status"] in ("running", "waiting")}
        devices = await self.devices()
        radii, far_au = await self.cruise_radii(), await self.max_cruise_au()
        hubs = self.hub_stars(devices)
        ignore = set((await self.loadout_cfg()).get("ignore_tags") or [])
        out = []
        for f in await self.fleets():
            # a stalled or stopped mission still holds its devices where they are: don't send the carrier recruiting
            if (only and f["id"] != only) or (f.get("mission") or {}).get("status") in ("running", "stalled", "stopped"):
                continue
            if fl.stationed(f) or f.get("role") == "bootstrap":
                continue   # the loadout pass keeps a stationed fleet filled, at its home
            # a logistics fleet always has jobs going: its extras only need to be out of them (busy)
            if f["id"] in working and f.get("role") != "logistics":
                continue
            drop = self.droppable_extras(f, devices, busy, hubs, ignore)
            if drop:
                codes = [d["device_code"] for d in drop]
                job = await self.create_job("fleet_fill", f"{f['name']}: drops {len(codes)} extra(s) at a hub ({', '.join(codes)})",
                                            None, fl.drop_steps(f, drop), {"devices": codes, "fleet": f["id"]}, force=force)
                busy |= set(codes)
                out.append(f"{f['name']}: drops " + ", ".join(f"{d['device_code']} ({d.get('device_type')})" for d in drop)
                           + " as spares at a hub" + ("" if job else " (dry run)"))
                if job:
                    continue   # one job per fleet at a time: the fill comes next pass
            if f["id"] in working or not fl.short_list(f, devices):
                continue
            steps, recruits, notes = fl.fill_plan(f, devices, stars, busy, radii, far_au, await self.supply_reach())
            for n in notes:
                out.append(f"{f['name']}: {n}")
            if not recruits:
                out.append(f"{f['name']}: short {fl.summarize(fl.short_list(f, devices))}, no idle spares to take")
                continue
            codes = [d["device_code"] for d in recruits]
            job = await self.create_job("fleet_fill", f"{f['name']}: takes {len(codes)} spare(s) ({', '.join(codes)})", None, steps,
                                        {"devices": codes + [d["device_code"] for d in fl.roster(f, devices)["carriers"]],
                                         "fleet": f["id"]}, force=force)
            busy |= set(codes)
            who = ", ".join(f"{d['device_code']} ({d.get('device_type')})" for d in recruits)
            out.append(f"{f['name']}: takes {who}"
                       + ("" if job else " (dry run)"))
        return out

    @staticmethod
    def hub_stars(devices: list[dict]) -> set[str]:
        """Systems with one of our system hubs deployed: where fleets drop their extras."""
        return {star_of(d["location"]) for d in devices if d.get("device_type") == "system_hub" and d.get("location")
                and not str(d.get("status") or "").startswith(("destroyed", "decommission"))}

    @staticmethod
    def droppable_extras(f: dict, devices: list[dict], busy: set[str], hubs: set[str], ignore: set[str]) -> list[dict]:
        """An unstationed fleet's members beyond its loadout (fleets.extras) that can be let go now (owner 2026-10-27:
        when the fleet is next in at a hub, e.g. home for repairs): idle in a hub system, out of every job, in comms
        range, not working, and not riding a carrier that's on its way somewhere."""
        from . import fleets as fl
        by = {d.get("device_code"): d for d in devices}

        def moving(d: dict) -> bool:
            return bool(fl.in_flight(d)) or str(d.get("status") or "").lower().startswith(fl.MOVING)

        out = []
        for d in fl.extras(f, devices, ignore, hubs):
            host = by.get(d.get("stowed_in_device_code") or d.get("attached_to_device_code") or "")
            if (d["device_code"] in busy or fl.where_star(d, by) not in hubs or d.get("in_control_range") is False
                    or str(d.get("status") or "").lower().startswith(fl.BUSY_STATES) or moving(d)
                    or (host and (moving(host) or host.get("device_code") in busy))):
                continue
            out.append(d)
        return out

    async def fleet_owners(self, force: bool = False, only: str | None = None) -> list[str]:
        """Fleets with an owner (and 'keep' on, or `force`): members another replicant owns are handed to the owner
        (change_owner), one job per fleet. A transfer just sent isn't repeated for 15 minutes."""
        from . import fleets as fl
        if not force:
            last = _ts(await self.db.kv_get("fleet_owners_at", None))
            if last and (_now() - last).total_seconds() < 300:
                return []
            await self.db.kv_set("fleet_owners_at", now_iso())
        sent = await self.db.kv_get("owner_sent", {}) or {}
        now = _now()
        sent = {c: v for c, v in sent.items() if _ts(v.get("at")) and (now - _ts(v["at"])).total_seconds() < 900}
        devices = await self.devices()
        busy = self.busy_devices(await self.jobs())
        out = []
        for f in await self.fleets():
            if (only and f["id"] != only) or not f.get("owner") or not (f.get("keep_owner") or force):
                continue
            skip = busy | ({c for c, v in sent.items() if v.get("owner") == f["owner"]} if not force else set())
            steps = fl.owner_steps(f, devices, skip)
            if not steps:
                continue
            codes = [st["path"].split("/")[-1] for st in steps]
            job = await self.create_job("fleets", f"{f['name']}: {len(codes)} device(s) to owner {f['owner']}", None, steps,
                                        {"devices": codes, "fleet": f["id"]}, force=force)
            if job:
                for c in codes:
                    sent[c] = {"owner": f["owner"], "at": now_iso()}
            out.append(f"{f['name']}: {', '.join(codes)} → owner {f['owner']}" + ("" if job else " (dry run)"))
        await self.db.kv_set("owner_sent", sent)
        return out

    async def solo_pass(self, fleet: dict, m: dict, devices: list[dict], jobs: dict) -> bool:
        """A relocation's solo flyers (fleets.solo_flyers): each goes straight to the new home once it has finished
        what it's doing (not working, not in a job, not in flight) — from wherever it is."""
        from . import fleets as fl
        from .loadouts import WORKING
        target = (m.get("targets") or [None])[0]
        if not target:
            return False
        by = {d.get("device_code"): d for d in devices}
        busy = self.busy_devices(list(jobs.values()))
        cat = await self.db.kv_get("stars", {}) or {}
        stars = {x.get("designation"): x for x in (cat.get("stars") or []) if isinstance(x, dict)}
        sent = m.setdefault("solo_sent", [])
        changed = False
        if await self.dark_home(fleet, target, devices):
            return False   # nobody flies there alone into the dark: the travel phase stalls the relocation and says why
        for code in sorted(set(m.get("solo") or []) - set(sent)):
            d = by.get(code)
            if not d or star_of(d.get("location")) == star_of(target):
                sent.append(code)
                changed = True
                continue
            if code in busy or str(d.get("status") or "").startswith(WORKING) or fl.in_flight(d):
                continue   # still on its current task
            steps = fl.travel_steps(fleet, [d], star_of(target), stars)
            if steps and await self.create_job("fleets", f"{code} flies to {fleet['name']}'s new home {star_of(target)} on its own",
                                               code, steps, {"devices": [code]}):
                sent.append(code)
                self._mlog(m, f"{code} ({d.get('device_type')}) flies to {star_of(target)} on its own")
                changed = True
        return changed

    @staticmethod
    def _retry_due(m: dict) -> bool:
        at = _ts((m.get("auto_retry") or {}).get("at"))
        return m.get("status") == "stalled" and not m.get("comms_wait") and bool(at) and _now() >= at

    def _plan_retry(self, m: dict) -> tuple[str, bool]:
        """A mission stalled: schedule its own retry. Returns the note for the logs and whether to skip the
        notification (a retry that stalled again on the same thing: Joe heard about it the first time)."""
        from . import fleets as fl
        again = int((m.get("auto_retry") or {}).get("n") or 0) > 0 \
            and (m.get("auto_retry") or {}).get("spot") == [m.get("idx", 0), m.get("phase")]
        at = fl.plan_retry(m, _now())
        if at:
            return f" — retries by itself at {at[11:16]} UTC (try {m['auto_retry']['n'] + 1} of {fl.RETRY_TRIES})", again
        return f" — no more automatic retries ({fl.RETRY_TRIES} tried): press Retry once it is sorted out", False

    async def run_fleets(self) -> None:
        from . import fleets as fl
        items = await self.fleets()
        if not any((f.get("mission") or {}).get("status") == "running"
                   or (f.get("mission") or {}).get("comms_wait") or self._retry_due(f.get("mission") or {})
                   for f in items):
            return
        jobs = {j["id"]: j for j in await self.jobs()}
        devices = await self.devices()
        changed = False
        for fleet in items:
            if fleet.get("role") == "bootstrap":
                continue   # no missions: bootstrap_pass drives it
            m = fleet.get("mission") or {}
            if m.get("status") == "stalled" and m.get("comms_wait"):
                # stalled because members were out of comms range (live 2026-10-22: Surveyor 2's heaven vessel left
                # ALFOLEN, its controller and drones went dark): resumes by itself once every member is back in range
                if any(d.get("in_control_range") is False for d in fl.members(fleet, devices)):
                    continue
                m.pop("comms_wait")
                fl.rewind(fleet, m)
                self._mlog(m, "back in comms range — resumed")
                changed = True
            elif self._retry_due(m):
                # stalled on something that may clear by itself (a member still on its way, a scan not in yet, a busy
                # device): try the stalled phase again, backing off (fleets.plan_retry)
                m["auto_retry"]["n"] = int(m["auto_retry"].get("n") or 0) + 1
                m["auto_retry"]["at"] = None
                fl.rewind(fleet, m)
                self._mlog(m, f"retrying by itself (try {m['auto_retry']['n']} of {fl.RETRY_TRIES})")
                changed = True
            if m.get("status") != "running":
                continue
            if m.get("relocate") and "solo" not in m and not m.get("departed"):
                # a relocation started before solo flyers existed (1.93): pick them now, so it stops waiting for
                # freighters nobody told to come back
                m["solo"] = sorted(fl.solo_flyers(fleet, devices))
                if m["solo"]:
                    self._mlog(m, f"{', '.join(m['solo'])} fly to the new home on their own after their current task")
                    changed = True
            if m.get("relocate") and set(m.get("solo") or []) - set(m.get("solo_sent") or []):
                changed |= await self.solo_pass(fleet, m, devices, jobs)
            j = jobs.get(m.get("job"))
            if j and j["status"] in ("running", "waiting"):
                continue
            err = next((s.get('error') for s in j['steps'] if s['status'] == 'failed'), '') if j else ''
            if (j and j["status"] == "failed" and m.get("contract") and m.get("phase") == "trade"
                    and "criteria not met" in str(err).lower() and int(m.get("criteria_misses") or 0) < 3):
                # the game says the contract isn't satisfied yet (seen live 2026-10-08: Trader_1 stalled at AEMEROTH-2
                # and stayed stalled after the contract's devices arrived): back to waiting at the site — the gate
                # holds until an option is complete there, then fulfills again
                m["criteria_misses"] = int(m.get("criteria_misses") or 0) + 1
                m["phase"], m["job"] = "wait", None
                self._mlog(m, f"the game says the contract's criteria aren't met yet ({err}) — waiting at the site "
                              f"until they are (try {m['criteria_misses']} of 3)")
                changed = True
                continue
            if j and j["status"] == "failed" and m.get("trade") and m.get("phase") == "trade":
                # a failed trade doesn't prove the shop is gone (owner, 2026-10-20): it stays listed until a scan of
                # its system doesn't show it
                dl = fl.deal(m)
                m["status"] = "stalled"
                again, quiet = self._plan_retry(m)
                self._mlog(m, f"stalled in trade: {err or 'the trade failed'} — the shop may have moved from "
                              f"{dl.get('location')}: scan that system to check, or Fleets › Refresh traders, then Retry"
                              + again)
                await self.log("fleets", f"{fleet['name']}: trade at {dl.get('location')} failed ({err}) — the shop may "
                                         "have moved" + again, "alert", notify=not quiet)
                changed = True
                continue
            if j and j["status"] == "failed":
                m["status"] = "stalled"
                why = next((s.get('error') for s in j['steps'] if s['status'] == 'failed'), 'a step failed')
                dark = "comms range" in str(err or why).lower() \
                    and any(d.get("in_control_range") is False for d in fl.members(fleet, devices))
                again, quiet = "", False
                if dark:
                    m["comms_wait"] = True
                    m.pop("auto_retry", None)
                else:
                    m.pop("comms_wait", None)
                    again, quiet = self._plan_retry(m)
                self._mlog(m, f"stalled in {m.get('phase')}: {why}"
                              + (" — resumes by itself once its members are back in comms range" if dark else again))
                await self.log("fleets", f"{fleet['name']}: stalled in {m.get('phase')}"
                                         + (" (out of comms range; resumes once back in range)" if dark else again),
                               "alert", notify=not quiet)
                changed = True
                continue
            if m.get("end_here"):
                # "End mission & board": one recall job (directives cleared, everyone back aboard a carrier), then stop
                # where the fleet is — no return trip
                if m.get("end_started"):
                    at = ", ".join(fl.roster(fleet, devices)["stars"]) or "?"
                    m["status"], m["job"] = "ended", None
                    m.pop("end_here", None), m.pop("end_started", None)
                    self._mlog(m, f"mission ended early — fleet aboard its carriers in {at}")
                    await self.log("fleets", f"{fleet['name']}: mission ended early, fleet aboard in {at}", notify=True)
                    changed = True
                    continue
                m["phase"], m["phase_at"], m["end_started"] = "recall", now_iso(), True
                steps, problems = await self.fleet_phase_steps(fleet, m, "recall", devices)
                for pr in problems:
                    self._mlog(m, f"recall: {pr}")
                if steps:
                    codes = [d["device_code"] for d in fl.members(fleet, devices)]
                    job = await self.create_job("fleets", f"{fleet['name']}: end mission & board", None, steps,
                                                {"devices": codes, "fleet": fleet["id"]}, force=True)
                    m["job"] = job["id"] if job else None
                    self._mlog(m, f"ending: recall & board, {len(steps)} step(s)")
                else:
                    m["job"] = None
                    self._mlog(m, "ending: everyone is already aboard")
                changed = True
                continue
            if (fleet["role"] == "explore" and m.get("scouting") and m.get("phase") == "recall"
                    and m.get("idx", 0) + 1 >= len(m.get("targets") or []) and not m.get("scout_ended")):
                weak = self.scout_unhealthy(fleet, devices)
                here = star_of((m.get("targets") or [None])[m.get("idx", 0)])
                nxt, why = (None, "") if weak else await self.scout_next(fleet, items, devices, here, set(m.get("targets") or []))
                if nxt:
                    m["targets"].append(nxt)
                    self._mlog(m, f"{here} surveyed — on to {nxt}")
                else:
                    m["scout_ended"] = True
                    self._mlog(m, f"{here} surveyed — returning home " + (f"for repairs ({', '.join(weak)} at or below "
                                                                           f"{self.SCOUT_MIN_CAPACITY:g} %)" if weak else f"({why})"))
                changed = True
            if m.get("phase") == "wait":
                gate, note = await self._deal_gate(fleet, m, devices)
                if gate == "wait":
                    if m.get("watch_note") != note:
                        m["watch_note"] = note
                        changed = True
                    continue
                changed = True   # "go" (fulfill next) or "rewind" (the next phase is deliver / load again)
            if m.get("phase") == "watch":
                others = [j for j in await self.jobs() if (j.get("meta") or {}).get("fleet") != fleet["id"]]
                done, why, upd = fl.watch_done(fleet, m, devices, now_iso(), self.busy_devices(others))
                m.update(upd)
                if m.get("watch_note") != why:
                    m["watch_note"] = why
                    changed = True
                if not done and not m.pop("recall_now", False):
                    continue
                nxt_sal = await self.next_salvage(m) if done and fleet["role"] == "mining" and m.get("salvage") else None
                if nxt_sal:   # salvage used up, more in the system: back to work on the next one before ending
                    m.setdefault("salvaged", []).append(m["salvage"])
                    self._mlog(m, f"salvage {m['salvage']} used up — moving on to {nxt_sal}")
                    m["phase"], m["exhausted_since"] = "deploy", None   # the next phase is work, on the next salvage
                else:
                    self._mlog(m, f"work done ({why})")
            # next phase (skipping any with nothing to do)
            for _ in range(12):
                nxt = fl.next_phase(fleet["role"], m)
                if not nxt:
                    m["status"], m["job"] = "done", None
                    m.pop("auto_retry", None)
                    self._mlog(m, "mission complete")
                    await self.log("fleets", f"{fleet['name']}: mission complete", notify=True)
                    break
                if nxt == "travel" and not m.get("departed"):   # first departure: the loadout (or members) all in one place
                    # all in one place: a relocation waits for every member, not for a complete loadout
                    r = (fl.together if m.get("relocate") else fl.readiness_anywhere)(
                        fleet, devices, prefer=m.get("from") or fleet.get("home"),
                        free=set(m.get("solo") or []) if m.get("relocate") else None)
                    if not r["ready"] and not (m.get("opts") or {}).get("partial"):
                        note = (f"waiting to leave: members still to collect into {r['at']}: "
                                + ", ".join(c for cs in r["elsewhere"].values() for c in cs) if m.get("relocate") else
                                f"waiting to leave: {fl.readiness_text(r)}") + " — Launch anyway sends it short"
                        if m.get("watch_note") != note:
                            m["watch_note"] = note
                            self._mlog(m, note)
                            changed = True
                        # spares can still fill the gaps: gather again (it only takes what's short)
                        steps, _ = await self.fleet_phase_steps(fleet, m, "gather", devices)
                        if steps:
                            m["phase"] = "gather"
                            job = await self.create_job("fleets", f"{fleet['name']}: gather (filling the loadout)", None, steps,
                                                        {"devices": [d["device_code"] for d in fl.members(fleet, devices)],
                                                         "fleet": fleet["id"]}, force=True)
                            m["job"] = job["id"] if job else None
                        break
                if nxt == "travel" or (nxt == "return" and m.get("departed")):
                    # every leg, not only the first (live 2026-10-21: a survey fleet left AINELRAMU for PHEVENIA while
                    # its recall withdrew survey controller 769B37CC, which had not boarded yet: it stayed behind)
                    ros = fl.roster(fleet, devices)
                    loose = [d["device_code"] for d in ros["loose"]]
                    partial = (m.get("opts") or {}).get("partial")
                    board_steps, board_problems = (await self.fleet_phase_steps(fleet, m, "assemble", devices)
                                                   if loose else ([], []))
                    if loose and not board_steps and board_problems and ros["carriers"] and not partial:
                        # nobody can board (live 2026-10-23..25: Surveyor 2 waited 41 h at FALQUORYX with its vessel's
                        # hold full, logging the same problem every pass): stall, so it's heard about and retried
                        note = f"waiting to leave: {'; '.join(board_problems)} — Launch anyway sends it short"
                        m["status"], m["job"], m["watch_note"] = "stalled", None, note
                        again, quiet = self._plan_retry(m)
                        self._mlog(m, note + again)
                        await self.log("fleets", f"{fleet['name']}: stalled before leaving: {'; '.join(board_problems)}"
                                       + again, "alert", notify=not quiet)
                        break
                    if loose and (board_steps or not partial):
                        # Launch anyway sends it short, but still takes along members standing right there
                        # together in one system isn't aboard (seen live 2026-10-11: Surveyor 3's gather failed while its
                        # vessel was still cruising in, a retry then flew the empty vessel to SARKARUS): board them first
                        note = f"waiting to leave: {len(loose)} not aboard yet ({', '.join(loose[:6])}) — boarding them"
                        if m.get("watch_note") != note:
                            m["watch_note"] = note
                            self._mlog(m, note)
                            changed = True
                        steps, problems = board_steps, board_problems
                        for p_ in problems:
                            self._mlog(m, p_)
                        if steps:
                            if m.get("departed"):   # a later leg: board, then this same leg (next_phase)
                                m["phase"], m["board_next"] = "board", nxt
                            else:
                                m["phase"] = "assemble"
                            job = await self.create_job("fleets", f"{fleet['name']}: board before leaving", None, steps,
                                                        {"devices": [d["device_code"] for d in fl.members(fleet, devices)],
                                                         "fleet": fleet["id"]}, force=True)
                            m["job"] = job["id"] if job else None
                        break
                    m["departed"] = True
                    m.pop("watch_note", None)
                m["phase"], m["phase_at"] = nxt, now_iso()
                if nxt == "watch":
                    m.pop("auto_retry", None)   # got past whatever stalled it
                    self._mlog(m, "on station — watching until the work is done")
                    break
                if nxt == "wait":
                    changed = True
                    gate, note = await self._deal_gate(fleet, m, devices, arriving=True)
                    if gate == "rewind":
                        continue   # short at the site: deliver / gather again first
                    if gate == "wait":
                        m["watch_note"] = note
                        break
                    continue   # everything there and the replicant too: fulfill now
                steps, problems = await self.fleet_phase_steps(fleet, m, nxt, devices)
                for pr in problems:
                    self._mlog(m, f"{nxt}: {pr}")
                if m.pop("stall", False):
                    m["status"], m["job"] = "stalled", None
                    again, quiet = self._plan_retry(m)
                    if again:
                        self._mlog(m, again.lstrip(" —"))
                    await self.log("fleets", f"{fleet['name']}: stalled in {nxt}: {'; '.join(problems)}" + again, "alert",
                                   notify=not quiet)
                    break
                if not steps:
                    continue
                codes = [d["device_code"] for d in fl.members(fleet, devices)]
                job = await self.create_job("fleets", f"{fleet['name']}: {nxt}", None, steps, {"devices": codes, "fleet": fleet["id"]}, force=True)
                m["job"] = job["id"] if job else None
                self._mlog(m, f"{nxt}: {len(steps)} step(s)")
                break
            changed = True
        if changed:
            await self.save_fleets(items)

    async def rule_contracts(self, force: bool = False) -> list[str]:
        """See gameevents.py. Fulfill ready events; optionally deliver shortfalls / send a nearby replicant."""
        cfg = await self.rule_cfg("contracts")
        if not cfg:
            return []
        from . import gameevents as gev
        from .shapes import normalize_inventory
        state = await self.db.kv_get("contracts_state", {}) or {}
        last = _ts(state.get("_last"))
        if not force and last and (_now() - last).total_seconds() < 60 * max(1, int(cfg.get("every_minutes") or 5)):
            return []
        state["_last"] = now_iso()
        devices = await self.devices()
        inv = {i.get("location"): i.get("items") or {} for i in normalize_inventory(await self.db.kv_get("inventory", []))}
        reps = await self.db.kv_get("replicants", {}) or {}
        settings = await self.db.kv_get("event_settings", {}) or {}
        tmpl = (settings.get("fulfil") or "").strip() or gev.DEFAULT_FULFIL
        jobs = await self.jobs()
        busy = self.busy_devices(jobs)
        done = []
        from . import wards
        locked = wards.foreign(await self.db.kv_get("stars", {}) or {}, devices)   # wards and hubs lock contracts
        life, approvals = await gev.life_map(self.db), await self.db.kv_get(gev.APPROVALS_KV, {}) or {}
        for des, e in (await gev.load(self.db)).items():
            if e["status"] != "open" or not e.get("location"):
                continue
            if star_of(e["location"]) in locked:
                continue   # another player's ward or hub: its species interaction lock stops us completing this event
            prog = gev.progress(e, inv, devices, reps)
            tried = state.get(des) or {}
            if prog["state"] == "ready" and cfg.get("auto_fulfil", True) \
                    and await gev.approved(self.db, e, life, approvals):   # its species approved on the Contracts page
                t = _ts(tried.get("fulfil"))
                if t and (_now() - t).total_seconds() < 1800:
                    continue  # tried recently; let the event stream catch up (or the error be read)
                rep = prog["present"][0]["code"]
                filled = (tmpl.replace("{designation}", des).replace("{replicant}", rep).replace("{location}", e["location"])
                          .replace("{criteria}", (prog.get("best") or {}).get("name") or "default"))
                method, _, rest = filled.partition(" ")
                path, _, body = rest.partition(" ")
                job = await self.create_job("contracts", f"fulfill {e.get('title')} at {e['location']}", None,
                                            [step(f"fulfill {des}", path, json.loads(body) if body.strip() else None, method=method.upper())],
                                            {"event": des})
                tried["fulfil"] = now_iso()
                done.append(f"fulfill {des}" + ("" if job else " (dry run)"))
            elif prog["state"] == "deliver" and cfg.get("auto_deliver"):
                plan = gev.delivery_plan(e, prog, devices)
                ctrl = plan["controller"]
                t = _ts(tried.get("deliver"))
                if not ctrl or ctrl["device_code"] in busy or not plan["legs"] or (t and (_now() - t).total_seconds() < 3600):
                    continue
                leg = plan["legs"][0]  # one leg per check; the next check sends the next pile
                code = ctrl["device_code"]
                await self.create_job("contracts", f"deliver for {e.get('title')}: {leg['collect']} → {leg['deliver']}", code,
                                      [step(f"{code}: delivery", f"/devices/{code}",
                                            {"command": "set_directive", "directive": "delivery",
                                             "configuration": {"route": {"collect": leg["collect"], "deliver": leg["deliver"]},
                                                               "requirement": leg["requirement"]}}, critical=True),
                                       step(f"{code}: launch", f"/devices/{code}", {"command": "launch"})], {"event": des})
                tried["deliver"] = now_iso()
                done.append(f"deliver {des}")
            elif prog["state"] == "needs replicant" and cfg.get("send_replicant") and prog["nearby"]:
                t = _ts(tried.get("send"))
                if t and (_now() - t).total_seconds() < 3600:
                    continue
                rep = prog["nearby"][0]["code"]
                st = step(f"{rep} → {e['location']}", f"/replicants/{rep}/travel", {"destination": e["location"]},
                          wait=["travel.arrived"], match={"destination": e["location"]})
                await self.create_job("contracts", f"send {prog['nearby'][0]['name']} to {e['location']} for {e.get('title')}",
                                      None, [st], {"event": des})
                tried["send"] = now_iso()
                done.append(f"send {rep} → {e['location']}")
            state[des] = tried
        await self.db.kv_set("contracts_state", state)
        return done

    async def rule_reopen_sites(self) -> list[str]:
        """See sites.py."""
        cfg = await self.rule_cfg("reopen_sites")
        if not cfg:
            return []
        from . import sites
        from .ami_schedule import managed_by
        devices = [d for d in await self.devices() if not _reserved(d)]
        jobs = await self.jobs()
        busy = self.busy_devices(jobs)
        managed = await managed_by(self.db)
        state = await self.db.kv_get("reopen_state", {}) or {}
        cooldown = timedelta(minutes=int(cfg.get("cooldown_minutes") or 30))
        per = max(1, int(cfg.get("drones_per_belt") or 2))
        now = _now()
        done: list[str] = []
        rested = [v["belt"] for v in (await self.db.kv_get("rested", {}) or {}).values() if v.get("belt")]
        for belt, why in sorted(sites.exhausted_belts(devices, await self.exhausted_places() + rested).items()):
            last = _ts(state.get(belt))
            if last and now - last < cooldown:
                continue
            if any(j["rule"] == "reopen_sites" and j["status"] in ("running", "waiting") and j.get("meta", {}).get("belt") == belt
                   for j in jobs):
                continue
            if any(j["rule"] == "reopen_sites" and j.get("meta", {}).get("belt") == belt
                   and (_ts(j.get("finished_at")) or now) > now - sites.FULL_WAIT
                   and any("Maximum active sites" in str(st.get("error") or "") for st in j.get("steps") or [])
                   for j in jobs):
                continue   # the belt holds the game's most sites (searches under way count): wait for them to come up
            star = star_of(belt)
            have = len(sites.survey_at(devices, belt))
            if have >= per:
                continue  # enough drones already opening/holding sites there
            ctrls = [d for d in devices if "survey" in (d.get("device_type") or "") and "controller" in (d.get("device_type") or "")
                     and star_of(d.get("location")) == star and d["device_code"] not in busy and d.get("in_control_range") is not False]
            # belt surveyors first (about 4x faster, owner 2026-10-24); a survey controller adopts them too
            idle = sites.surveyors_first([d for d in devices if sites.is_surveyor(d) and star_of(d.get("location")) == star
                                          and str(d.get("status") or "").startswith("idle") and d["device_code"] not in busy
                                          and d.get("in_control_range") is not False])
            ctrl = None
            if cfg.get("use_ami", True) and ctrls:
                def on_belt_search(c: dict) -> bool:
                    dv = c.get("ami_directive") or {}
                    return dv.get("name") == "belt_search" and c.get("location") == belt and \
                        not str(dv.get("_eval_state") or "").startswith(("no_targets", "idle", "done"))
                if any(on_belt_search(c) for c in ctrls):
                    continue  # already searching this belt
                ctrl = next((c for c in ctrls if c.get("location") == belt), ctrls[0])
            if ctrl:
                mine = [d for d, c in managed.items() if c == ctrl["device_code"]]
                adopt = [d["device_code"] for d in idle if d["device_code"] not in managed][:max(0, per - len(mine))]
                await self.create_job("reopen_sites", f"re-open sites at {belt} with {ctrl['device_code']} ({why})",
                                            ctrl["device_code"], sites.ami_steps(ctrl, belt, adopt), {"devices": adopt, "belt": belt})
                done.append(f"{ctrl['device_code']} → belt_search {belt}")
            else:
                free = [d for d in idle if d["device_code"] not in managed][:per - have]
                if not free:
                    await self.log("reopen_sites", f"{belt} is worked out but there's no idle survey drone in {star} to search it", "alert")
                    state[belt] = now.isoformat(timespec="seconds")
                    continue
                for d in free:
                    await self.create_job("reopen_sites", f"{d['device_code']}: search {belt} ({why})", d["device_code"],
                                          sites.drone_steps(d, belt), {"belt": belt})
                    done.append(f"{d['device_code']} → search {belt}")
            state[belt] = now.isoformat(timespec="seconds")
        # idle survey drones at a belt with fewer open sites than the game allows search there too: the belt isn't
        # worked out, but its sites may lack what's needed (ISOLITHOR-BELT-1 2026-10-23: no conductive or rares at any)
        idle_at: dict[str, list[dict]] = defaultdict(list)
        for d in devices:
            if (sites.is_surveyor(d) and "BELT" in (d.get("location") or "")
                    and str(d.get("status") or "").startswith("idle") and d["device_code"] not in busy
                    and d["device_code"] not in managed and d.get("in_control_range") is not False):
                idle_at[d["location"]].append(d)
        theirs = await self.warded(devices) if idle_at else set()
        for belt, idle in sorted(idle_at.items()):
            key = f"cap:{belt}"
            last = _ts(state.get(key))
            if last and now - last < cooldown or star_of(belt) in theirs:
                continue
            if any(j["rule"] == "reopen_sites" and j.get("meta", {}).get("belt") == belt
                   and (j["status"] in ("running", "waiting") or (_ts(j.get("finished_at")) or now) > now - sites.FULL_WAIT
                        and any("Maximum active sites" in str(st.get("error") or "") for st in j.get("steps") or []))
                   for j in jobs):
                continue
            searching = sum(1 for d in devices if d.get("location") == belt and str(d.get("status") or "").startswith("searching"))
            room = sites.MAX_SITES - await self.open_sites_now(belt) - searching
            for d in sites.surveyors_first(idle)[:max(0, room)]:
                await self.create_job("reopen_sites", f"{d['device_code']}: search {belt} (fewer than {sites.MAX_SITES} open sites)",
                                      d["device_code"], sites.drone_steps(d, belt), {"belt": belt})
                done.append(f"{d['device_code']} → search {belt}")
            state[key] = now.isoformat(timespec="seconds")
        await self.db.kv_set("reopen_state", state)
        return done

    async def belt_open_sites(self, belts: list[str], max_age_minutes: int = 10) -> dict[str, int]:
        """Open-site counts for belts, re-reading each belt's detail if our copy is older than max_age_minutes."""
        from .salvage import open_site_count
        reads = await self.db.kv_get("belt_reads", {}) or {}
        now = _now()
        out: dict[str, int] = {}
        for b in dict.fromkeys(belts):
            t = _ts(reads.get(b))
            detail = await self.db.kv_get(f"loc:{b}", None)
            if detail is None or not t or now - t > timedelta(minutes=max_age_minutes):
                try:
                    detail = await self.api.request("GET", f"/locations/{b}", background=True)
                    await self.db.kv_set(f"loc:{b}", detail or {})
                    reads[b] = now.isoformat(timespec="seconds")
                except ApiError:
                    pass
            out[b] = open_site_count(detail, await self.depleted_since(b, reads.get(b)))
        await self.db.kv_set("belt_reads", reads)
        return out

    async def depleted_since(self, belt: str, since: str | None) -> set[str]:
        """Sites at `belt` a site.depleted event closed after `since` (when we last read the belt). Seen live
        (2026-10-08, LORSELAN): eight drones mined out five small sites in eight minutes; a ten-minute-old read still
        listed all five, and the controller was relaunched onto nothing — and overheated."""
        if not since:
            return set()
        rows = await self.db.fetchall("SELECT payload FROM events WHERE event='site.depleted' AND received_at >= ? "
                                      "AND payload LIKE ?", (since, f'%"{belt}-%'))
        out = set()
        for r in rows:
            try:
                site = (json.loads(r["payload"] or "{}") or {}).get("site")
            except ValueError:
                continue
            if site and str(site).startswith(belt + "-"):
                out.add(site)
        return out

    async def open_sites_now(self, belt: str) -> int:
        """Open sites at a belt from our last read, less what has been mined out since (no request)."""
        from .salvage import open_site_count
        reads = await self.db.kv_get("belt_reads", {}) or {}
        return open_site_count(await self.db.kv_get(f"loc:{belt}", None), await self.depleted_since(belt, reads.get(belt)))

    async def refresh_known_belts(self, every_minutes: int = 20, max_belts: int = 8) -> list[str]:
        """Keep the belts your devices are at current (open sites appear and close all the time), so closed or used-up
        sites drop off the System page and map without a manual refresh. One GET per belt, at most every 20 min."""
        from .salvage import belt_of
        last = _ts(await self.db.kv_get("belts_refreshed_at", None))
        if last and _now() - last < timedelta(minutes=every_minutes):
            return []
        belts = sorted({b for d in await self.devices() for b in [belt_of(d.get("location"))] if b})[:max_belts]
        await self.db.kv_set("belts_refreshed_at", _now().isoformat(timespec="seconds"))
        if belts:
            await self.belt_open_sites(belts, max_age_minutes=every_minutes)
        return belts

    async def consolidate_plan(self, cfg: dict | None = None) -> list[dict]:
        from . import consolidate as co
        from . import gameevents as gev
        from .shapes import normalize_blueprints, normalize_inventory
        cfg = cfg or (await self.settings())["rules"].get("consolidate") or {}
        devices = await self.devices()
        inv = {i.get("location"): i.get("items") or {} for i in normalize_inventory(await self.db.kv_get("inventory", []))}
        bps = {b["device_type"]: b for b in normalize_blueprints(await self.db.kv_get("blueprints", []))}
        protected = {e.get("location") for e in (await gev.load(self.db)).values() if e.get("status") == "open" and e.get("location")}
        protected |= {d.get("location") for d in devices if d.get("device_type") == "system_hub" and d.get("location")}   # its upkeep
        handed = set(await self.db.kv_get("logistics_ami", {}) or {})   # given a delivery by the logistics service: busy
        return co.plan(devices, inv, bps, self.busy_devices(await self.jobs()) | handed, protected, float(cfg.get("min_amount") or 100))

    async def rule_consolidate(self, force: bool = False) -> list[str]:
        from . import consolidate as co
        cfg = await self.rule_cfg("consolidate")
        if not cfg and not force:
            return []
        cfg = cfg or (await self.settings())["rules"].get("consolidate") or {}
        last = _ts(await self.db.kv_get("consolidate_last", None))
        if not force and last and _now() - last < timedelta(minutes=int(cfg.get("every_minutes") or 20)):
            return []
        await self.db.kv_set("consolidate_last", _now().isoformat(timespec="seconds"))
        done = []
        for p in await self.consolidate_plan(cfg):
            if not p.get("controller"):
                continue
            job = await self.create_job("consolidate", f"consolidate: {p['collect']} → {p['deliver']} ({p['total']} units)",
                                        p["controller"], co.steps(p), {"devices": [], "star": p["star"]}, force=force)
            done.append(co.describe(p) + ("" if job else " (dry run)"))
        return done

    async def viability_report(self) -> list[dict]:
        from . import viability as via
        state = await self.db.kv_get("viability", {}) or {}
        stars = await self.star_lookup("by")   # shared: read only
        known: dict[str, int] = {}
        for r in await self.db.fetchall("SELECT key, value FROM kv WHERE key LIKE 'loc:%-BELT-%'"):
            b = via.belt_of(r["key"][4:])
            if b and b == r["key"][4:]:
                idx = [x.get("site_index") for x in (json.loads(r["value"]) or {}).get("resource_sites") or []
                       if isinstance(x, dict) and x.get("site_index") is not None]
                known[b] = max(idx) if idx else 0
        cfg = (await self.settings())["rules"].get("belt_viability") or {}
        rows = via.report(state, stars, (cfg.get("move_at_percent") or 250) / 100, known)
        from . import others as oth   # other players' mining drones on the belt at the last scan of the system
        snaps = oth.normalize(await self.db.kv_get(oth.KV, {}))["stars"]
        for r in rows:
            r["theirs"] = oth.drones_at(snaps.get(r.get("star")), r.get("belt") or "")
        return rows

    async def track_viability(self) -> list[str]:
        """Fold this pass's device list and freshly read belt details into the viability record (no API calls);
        alert once when a belt crosses into 'consider moving'."""
        from . import viability as via
        state = await self.db.kv_get("viability", {}) or {}
        reads = await self.db.kv_get("belt_reads", {}) or {}
        last = state.get("last_obs") or ""
        details = {}
        for b, at in reads.items():
            if at and at > last:
                details[b] = await self.db.kv_get(f"loc:{b}", None)
        now = _now().isoformat(timespec="seconds")
        state, notes = via.observe(state, await self.devices(), details, now)
        state["last_obs"] = now
        alerts: list[str] = []
        cfg = await self.rule_cfg("belt_viability")
        if cfg:
            sent = state.setdefault("alerted", {})
            for r in await self._viability_rows(state):
                if r["verdict"] == "consider moving" and sent.get(r["belt"]) != "consider moving":
                    text = via.alert_text(r)
                    await self.log("belt_viability", text, "alert", notify=True)
                    alerts.append(text)
                sent[r["belt"]] = r["verdict"]
        await self.db.kv_set("viability", state)
        return alerts

    async def _viability_rows(self, state: dict) -> list[dict]:
        await self.db.kv_set("viability", state)   # report() reads the stored state
        return await self.viability_report()

    async def known_open_sites(self, max_age_hours: float = 2) -> dict[str, int]:
        """star → open mining sites on its belts, from belt details read in the last couple of hours (stars whose belts
        weren't read recently are left out = unknown)."""
        from .salvage import open_site_count
        reads = await self.db.kv_get("belt_reads", {}) or {}
        out: dict[str, int] = {}
        for b, at in reads.items():
            t = _ts(at)
            if not t or (_now() - t).total_seconds() > max_age_hours * 3600:
                continue
            out[star_of(b)] = out.get(star_of(b), 0) + open_site_count(await self.db.kv_get(f"loc:{b}", None),
                                                                       await self.depleted_since(b, at))
        return out

    async def system_belts(self, stars: set[str]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for star in stars:
            row = await self.db.fetchone("SELECT data FROM systems WHERE star=?", (star,))
            scan = json.loads(row["data"]) if row else {}
            out[star] = [b["designation"] for b in ((scan.get("asteroid_belt") or {}).get("belts")) or [] if b.get("designation")]
        return out

    async def rule_back_to_belt(self, cfg: dict) -> list[str]:
        """Mining controllers stuck 'exhausted' at a body (drones left at used-up salvage), with a stale 'exhausted' at a
        belt that has re-opened, paused, or done with salvage — while a belt in their system has open sites: bring the
        drones back to the belt, re-adopt them, re-set the directive and launch."""
        from . import salvage as sv
        from .ami_schedule import is_controller, kind_of, managed_by, targets_of
        devices = await self.devices()
        ctrls = [d for d in devices if is_controller(d) and kind_of(d.get("device_type")) == "mining" and not _reserved(d)]
        def candidate(c: dict) -> bool:
            dv = c.get("ami_directive") if isinstance(c.get("ami_directive"), dict) else {}
            st = str(dv.get("_eval_state") or "")
            return (st.startswith("exhausted") or str(c.get("ami_directive_status") or "") == "paused"
                    or (dv.get("name") == "gather_salvage" and st.startswith(sv.FINISHED_STATES)) or sv.parked(c))
        mgd = await managed_by(self.db)
        warded = await self.warded(devices)
        rested = await self.db.kv_get("rested", {}) or {}
        cands = [c for c in ctrls if (candidate(c) or sv.scattered(c, devices, mgd) or c.get("device_code") in rested)
                 and star_of(c.get("location")) not in warded]
        if not cands:
            return []
        stars = {star_of(c.get("location")) for c in cands}
        sysb = await self.system_belts(stars)
        for c in cands:  # the controller's own belt counts even if the scan doesn't list it
            b = sv.belt_of(c.get("location"))
            if b and b not in sysb.setdefault(star_of(c.get("location")), []):
                sysb[star_of(c.get("location"))].append(b)
        open_sites = await self.belt_open_sites([b for bl in sysb.values() for b in bl])
        state = await self.db.kv_get("salvage_state", {}) or {}
        back = state.setdefault("back", {})
        cool = timedelta(minutes=int(cfg.get("back_cooldown_minutes") or 30))
        now = _now()
        skip = self.busy_devices(await self.jobs()) | {k for k, v in back.items() if _ts(v) and now - _ts(v) < cool}
        directive_for = {k: v.get("directive") for k, v in rested.items() if v.get("directive")}   # what it ran before
        for sched in await self.schedules():   # use the directive an AMI schedule gives that controller, if any
            if sched.get("enabled", True):
                for c in targets_of(sched, devices):
                    directive_for.setdefault(c["device_code"], sched["directive"])
        done = []
        busy = self.busy_devices(await self.jobs())
        free = {d["device_code"] for d in devices if not _reserved(d) and d["device_code"] not in busy}
        for p in sv.back_to_belt_plan(cands, devices, mgd, open_sites, sysb, skip, directive_for, free, set(rested)):
            r = rested.get(p["ctrl"]) or {}
            if r.get("directive") == p["directive"] and r.get("config"):
                p["config"] = r["config"]   # the configuration it had before resting (e.g. gather_resources amounts)
            n = len(p["away"]) + len(p["strays"])
            job = await self.create_job("salvage_when_depleted",
                                        f"{p['ctrl']}: back to {p['belt']} ({p['why']})"
                                        + (f", bringing {n} drone(s)" if n else ""),
                                        p["ctrl"], sv.back_to_belt_steps(p), {"devices": p["away"] + p["strays"] + p.get("gone", []), "belt": p["belt"]})
            back[p["ctrl"]] = now.isoformat(timespec="seconds")
            if job and rested.pop(p["ctrl"], None):
                await self.db.kv_set("rested", rested)
            done.append(f"{p['ctrl']} → {p['belt']}" + (" (planned, dry run)" if not job else ""))
        await self.db.kv_set("salvage_state", {**(await self.db.kv_get("salvage_state", {}) or {}), "back": back})
        return done

    async def rest_dry_controllers(self) -> list[str]:
        """A mining controller whose directive is exhausted at its belt while that belt has no open sites (the survey
        drones are still searching) and no salvage took it: clear its directive so it stops coordinating drones with
        nothing to mine — seen live (2026-10-08, LORSELAN) to log ami_overheat every 20 s, heat 2.0, and lose ~9 %
        capacity an hour. Its drones stay at the belt. kv "rested" remembers the directive; back to the belt relaunches
        it once the belt has open sites, and AMI schedules leave it alone until then."""
        cfg = await self.rule_cfg("salvage_when_depleted")
        if not cfg or not cfg.get("rest_when_dry", True):
            return []
        from . import salvage as sv
        from .ami_schedule import is_controller, kind_of
        devices = await self.devices()
        busy = self.busy_devices(await self.jobs())
        rested = await self.db.kv_get("rested", {}) or {}
        out = []
        for c in devices:
            code = c.get("device_code")
            if not (is_controller(c) and kind_of(c.get("device_type")) == "mining") or _reserved(c) or code in busy \
                    or code in rested or c.get("in_control_range") is False:
                continue
            dv = c.get("ami_directive") if isinstance(c.get("ami_directive"), dict) else {}
            state = str(dv.get("_eval_state") or "")
            if not dv.get("name") or dv.get("name") == "gather_salvage" or not state.startswith("exhausted"):
                continue
            place = sv.exhausted_place(state)
            belt = sv.belt_of(place)
            if not belt or belt != place or (await self.belt_open_sites([belt]))[belt] > 0:
                continue   # exhausted at a body (back to the belt handles that), or the belt has sites: not dry
            job = await self.create_job("salvage_when_depleted", f"{code}: rest at {belt} — no open sites, the survey "
                                        "drones are searching (an exhausted controller overheats)", code,
                                        [step(f"{code}: clear_directive", f"/devices/{code}", {"command": "clear_directive"},
                                              critical=True)], {"devices": [], "belt": belt})
            if job:
                rested[code] = {"belt": belt, "directive": dv.get("name"), "config": dv.get("config") or {}, "at": now_iso()}
                out.append(f"{code} rests at {belt}")
        # forget a rest the player ended (a directive set by hand) or a controller that has gone
        by = {d.get("device_code"): d for d in devices}
        for code in list(rested):
            d = by.get(code)
            if d is None or ((d.get("ami_directive") or {}).get("name") and code not in busy
                             and not str((d.get("ami_directive") or {}).get("_eval_state") or "").startswith("exhausted")):
                rested.pop(code)
        await self.db.kv_set("rested", rested)
        return out

    async def rule_salvage(self) -> list[str]:
        """See salvage.py. Returns what it did (for the log / tests)."""
        cfg = await self.rule_cfg("salvage_when_depleted")
        if not cfg:
            return []
        back_done: list[str] = []
        if cfg.get("back_to_belt", True):
            back_done = await self.rule_back_to_belt(cfg)
        from . import salvage as sv
        from .ami_schedule import adoptable, controller_idle, is_controller, kind_of, managed_by
        from .targets import system_resources
        devices = await self.devices()
        busy = self.busy_devices(await self.jobs())
        managed = await managed_by(self.db)
        state = await self.db.kv_get("salvage_state", {}) or {}
        cool, assigned = state.setdefault("cool", {}), state.setdefault("assigned", {})
        cooldown = timedelta(minutes=int(cfg.get("cooldown_minutes") or 10))
        now = _now()

        def cooling(code: str) -> bool:
            t = _ts(cool.get(code))
            return bool(t and now - t < cooldown)

        miners = [d for d in devices if (d.get("device_type") == "mining_drone" or
                  (is_controller(d) and kind_of(d.get("device_type")) == "mining"))
                  and not _reserved(d)]
        done: list[str] = []
        nothing = state.setdefault("nothing", {})   # star -> when we last said there's nothing to mine there
        warded = await self.warded(devices)
        for star in sorted({star_of(d.get("location")) for d in miners} - warded):
            res = await system_resources(self.db, star)
            scanned = await self.db.fetchone("SELECT 1 FROM systems WHERE star=?", (star,))
            no_belt = bool(scanned) and not (await self.system_belts({star}))[star]   # scanned: no belt to mine
            dry = sv.worked_out(res) | {p for p in await self.exhausted_places() if star_of(p) == star}
            sal = sv.available_salvage(res)
            dead_sal = {x["code"] for x in res.get("salvage") or [] if x.get("depleted")}
            for k in [k for k, v in assigned.items() if k in dead_sal]:
                assigned.pop(k)
            ctrls = [d for d in miners if is_controller(d) and star_of(d.get("location")) == star]
            # the controller's own report: "_eval_state": "exhausted:[...]:<place>" = nothing left to mine there
            exhausted = {c["device_code"] for c in ctrls
                         if str(((c.get("ami_directive") or {}).get("_eval_state")) or "").startswith("exhausted")}
            if no_belt and not sal:
                t = _ts(nothing.get(star))
                if not t or now - t > timedelta(hours=24):
                    nothing[star] = now.isoformat(timespec="seconds")
                    await self.log("salvage_when_depleted", f"nothing to mine in {star}: no asteroid belt and no salvage we know "
                                   "of — move its miners, or survey the bodies for salvage", "alert", notify=True)
                continue
            if (not dry and not exhausted and not no_belt) or not sal:
                continue
            if ctrls and cfg.get("use_ami", True):
                reopening = await self.rule_cfg("reopen_sites")
                survey_here = any("survey" in (d.get("device_type") or "") and star_of(d.get("location")) == star for d in devices)
                for c in ctrls:
                    code = c["device_code"]
                    if code in busy or cooling(code):
                        continue
                    if reopening and survey_here and not no_belt:
                        continue  # sites are being re-opened by survey drones; keep mining rather than switch to salvage
                    row = await self.db.fetchone("SELECT event, payload FROM events WHERE device_code=? AND event LIKE 'directive.%' "
                                                 "ORDER BY seq DESC LIMIT 1", (code,))
                    cur = json.loads(row["payload"] or "{}") if row else {}
                    on_salvage = (row and row["event"] == "directive.set" and cur.get("directive") == "gather_salvage"
                                  and (cur.get("configuration") or {}).get("location") not in dead_sal | {sv.body_of(c) for c in dead_sal})
                    if on_salvage:
                        continue
                    idle, _ = await controller_idle(self.db, c)
                    if not (code in exhausted or sv.at_worked_out_place(c.get("location"), dry, dead_sal) or idle or no_belt):
                        continue
                    target = next((x for x in sal if assigned.get(x["code"]) in (None, code)), None)
                    if not target:
                        break
                    adopt = adoptable(devices, c, managed)
                    job = await self.create_job("salvage_when_depleted", f"{code}: salvage {target['code']} (sites in {star} depleted)",
                                                code, sv.ami_steps(code, target["code"], bool(cfg.get("recall")), adopt),
                                                {"devices": adopt, "salvage": target["code"]})
                    cool[code] = now.isoformat(timespec="seconds")
                    if job:
                        assigned[target["code"]] = code
                    done.append(f"{code} → gather_salvage {target['code']}")
                continue  # the AMI handles this system; drones are left to it
            per = int(cfg.get("drones_per_salvage") or 0)
            idle_drones = sorted((d for d in miners if d.get("device_type") == "mining_drone" and star_of(d.get("location")) == star
                                  and str(d.get("status")) == "idle" and d["device_code"] not in busy
                                  and d["device_code"] not in managed and not cooling(d["device_code"])
                                  and (no_belt or sv.at_worked_out_place(d.get("location"), dry, dead_sal))),
                                 key=lambda d: d["device_code"])
            counts: dict[str, int] = defaultdict(int)
            for d in miners:  # drones already at (or heading for) a salvage count toward its share
                for x in sal:
                    if d.get("location") in (x["code"], sv.body_of(x["code"])) and str(d.get("status")) != "idle":
                        counts[x["code"]] += 1
            for j in await self.jobs():
                if j["rule"] == "salvage_when_depleted" and j["status"] in ("running", "waiting") and j.get("meta", {}).get("salvage"):
                    counts[j["meta"]["salvage"]] += 1
            i = 0
            for d in idle_drones:
                while i < len(sal) and per and counts[sal[i]["code"]] >= per:
                    i += 1
                if i >= len(sal):
                    break
                target = sal[i]
                code = d["device_code"]
                await self.create_job("salvage_when_depleted", f"{code}: salvage {target['code']} (sites in {star} depleted)", code,
                                      sv.drone_steps(code, d.get("location"), target), {"salvage": target["code"]})
                cool[code] = now.isoformat(timespec="seconds")
                counts[target["code"]] += 1
                done.append(f"{code} → {target['code']}")
        prev = await self.db.kv_get("salvage_state", {}) or {}
        state["back"] = prev.get("back", state.get("back", {}))   # written by rule_back_to_belt this pass
        state["nothing"] = nothing
        await self.db.kv_set("salvage_state", state)
        return back_done + done

    @staticmethod
    def _fed_stars(d: dict, fleets: list[dict]) -> set[str]:
        """The systems a drone's mining feeds: its own, and the home of the fleet its fleet sends materials to."""
        from . import fleets as fl
        out = {star_of(d.get("location"))}
        tags = set(d.get("tags") or [])
        for f in fleets:
            if fl.fleet_tag(f["id"]) in tags:
                to = fl.materials_target(f, fleets)
                if to and to.get("home"):
                    out.add(to["home"])
        return out

    async def rule_restart_idle_miners(self) -> None:
        cfg = await self.rule_cfg("restart_idle_miners")
        if not cfg:
            return
        cooldown = timedelta(minutes=int(cfg.get("cooldown_minutes") or 10))
        attempts = await self.db.kv_get("miner_restarts", {}) or {}
        busy = self.busy_devices(await self.jobs())
        now = _now()
        dry = (await self.settings())["dry_run"]
        from .ami_schedule import controller_idle, is_controller, kind_of, managed_by
        managed = await managed_by(self.db)
        devices = await self.devices()
        handed: dict[str, list[str]] = {}
        dry_belts: set[str] = set(await self.exhausted_places())
        if await self.rule_cfg("salvage_when_depleted"):
            from .salvage import worked_out
            from .targets import system_resources
            for star in {star_of(d.get("location")) for d in devices if d.get("device_type") == "mining_drone"}:
                dry_belts |= worked_out(await system_resources(self.db, star))
        warded = await self.warded(devices)
        from .bootstrap import no_site, waiting_short
        from .salvage import site_resources
        nosite = await self.db.kv_get("miner_no_site", {}) or {}   # {belt: {resource: at}}, like bootstrap.no_site
        reads = await self.db.kv_get("belt_reads", {}) or {}
        site_res: dict[str, set[str] | None] = {}
        fleets = await self.fleets()
        for d in devices:
            code = d.get("device_code")
            if _reserved(d):
                continue  # a fleet's drones are run by the fleet
            if star_of(d.get("location")) in warded:
                continue  # another player's ward or hub: nothing of ours can mine here
            if (d.get("device_type") != "mining_drone" or str(d.get("status")) != "idle"
                    or "BELT" not in (d.get("location") or "") or code in busy or code in managed
                    or "start_mining" not in (d.get("available_commands") or ["start_mining"])):
                continue
            from . import prospects as pr_
            from .salvage import belt_of
            if belt_of(d.get("location")) in dry_belts:
                continue  # nothing left to mine there: the salvage rule moves it
            last = _ts(attempts.get(code))
            if last and now - last < cooldown:
                continue
            # the drone's own fleet's controller (another fleet's would take it over: live 2026-10-27, Miner_8's
            # drones ran under Miner_9's controller at SOLARES-BELT-1), else a fleetless one
            from .ami_schedule import fleet_of as ctrl_fleet, same_side
            ctrl = min((c for c in devices if is_controller(c) and kind_of(c.get("device_type")) == "mining"
                        and c.get("location") == d.get("location") and same_side(c, d)),
                       key=lambda c: (ctrl_fleet(c) != ctrl_fleet(d), c["device_code"]), default=None) \
                if cfg.get("prefer_ami", True) else None
            if ctrl:
                handed.setdefault(ctrl["device_code"], []).append(code)
                attempts[code] = now.isoformat(timespec="seconds")
                continue
            resource = cfg.get("resource") or "same"
            belt = belt_of(d.get("location")) or d.get("location")
            skip = no_site({"no_site": nosite}, belt, now)
            if belt not in site_res:   # what the open sites hold (our last read, less sites mined out since)
                site_res[belt] = site_resources(await self.db.kv_get(f"loc:{belt}", None),
                                                await self.depleted_since(belt, reads.get(belt)))
            have = site_res[belt]
            if resource == "same":
                row = await self.db.fetchone(
                    "SELECT payload FROM events WHERE device_code=? AND event IN ('mining.started','mining.stopped','mining.retargeted') "
                    "ORDER BY seq DESC LIMIT 1", (code,))
                pl = json.loads(row["payload"]) if row else {}
                resource = pl.get("resource_type") or pl.get("new_resource") or "structural"
                # what waiting prints lack, if its belt has it (owner 2026-10-22) — first what the factories this belt
                # feeds wait for (its own system's, or the system its fleet sends materials to), else any waiting print
                srow = await self.db.fetchone("SELECT data FROM systems WHERE star=?", (star_of(d.get("location")),))
                offered = pr_.belt_resources(json.loads(srow["data"] or "{}") if srow else {}, belt_of(d.get("location")))
                offered = {r for r in (offered if have is None else have) if r not in skip}
                local = waiting_short(devices, self._fed_stars(d, fleets))
                short = {r: q for r, q in local.items() if r in offered} or \
                    {r: q for r, q in (await self.print_needs(devices)).items() if q > 0 and r in offered}
                if short:
                    resource = max(short, key=lambda r: (short[r], r == resource, r))
            if resource in skip or (have is not None and resource not in have):
                # no open site has it (the game said so lately, or our read of the sites): mine something else the
                # open sites have, or wait for the survey drones
                srow = await self.db.fetchone("SELECT data FROM systems WHERE star=?", (star_of(d.get("location")),))
                left = sorted(r for r in (have if have is not None else
                                          pr_.belt_resources(json.loads(srow["data"] or "{}") if srow else {}, belt))
                              if r not in skip)
                if not left:
                    continue
                resource = left[0]
            attempts[code] = now.isoformat(timespec="seconds")
            site = None
            if cfg.get("focus_sites"):   # one site at a time, so sites run out in turn (owner 2026-10-25)
                from .sites import focus_site
                site = focus_site(await self.db.kv_get(f"loc:{belt}", None), resource,
                                  await self.depleted_since(belt, reads.get(belt)))
            on = f"{resource}" + (f" at {site}" if site else "")
            if dry:
                await self.log("restart_idle_miners", f"[dry run] would restart {code} on {on}")
                continue
            body = {"command": "start_mining", "resource_type": resource} | ({"target": site} if site else {})
            ok, _, err = await self.send("POST", f"/devices/{code}", body, f"auto: restart {code}")
            if not ok and site and not NO_SITE_RE.search(err or "") and "exhausted" not in (err or "").lower():
                # the site was refused (closed since our read, or the target not taken): any site will do
                on = resource
                ok, _, err = await self.send("POST", f"/devices/{code}", {"command": "start_mining", "resource_type": resource},
                                             f"auto: restart {code}")
            if not ok and "exhausted" in (err or "").lower():
                from .salvage import belt_of
                ex = await self.db.kv_get("exhausted_places", {}) or {}
                ex[belt_of(d.get("location")) or d.get("location")] = now_iso()
                await self.db.kv_set("exhausted_places", ex)
            quiet = not ok and bool(NO_SITE_RE.search(err or ""))
            if quiet:   # skipped 3 h at this belt (live 2026-10-23: 3 alerts at ISOLITHOR-BELT-1 for conductive)
                nosite.setdefault(belt, {})[resource] = now.isoformat(timespec="seconds")
                await self.db.kv_set("miner_no_site", nosite)
            await self.log("restart_idle_miners", f"restarted {code} on {on}" if ok else f"could not restart {code}: {err}",
                           "info" if ok or quiet else "alert")
        for ccode, drones in handed.items():
            ctrl = next(c for c in devices if c.get("device_code") == ccode)
            idle, _ = await controller_idle(self.db, ctrl)
            steps = [step(f"{ccode}: adopt idle {', '.join(drones)}", f"/devices/{ccode}", {"command": "adopt", "devices": drones})]
            if idle:
                steps.append(step(f"{ccode}: launch", f"/devices/{ccode}", {"command": "launch"}))
            await self.create_job("restart_idle_miners", f"hand {len(drones)} idle miner(s) to {ccode}", ccode, steps,
                                  {"devices": drones})
        await self.db.kv_set("miner_restarts", attempts)
