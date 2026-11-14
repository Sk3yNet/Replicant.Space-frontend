"""What the engine's parts share: the rule table, the job step helpers, time and text helpers and the engine lock.
automations.py and the engine_*.py mixins all start from here (from .engine_base import *)."""
from __future__ import annotations

import asyncio
import contextvars
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
         "When a replicant's vessel arrives in a system we have no scan for (or only one from before the last "
         "season reset), run a system scan so the planets, belts and stations are known (other rules, the Systems "
         "page and the Galaxy map use it)."),
    Rule("devices_on_arrival", "Device scan on arrival",
         "When a replicant's vessel arrives in another system, it scans the other devices there at once (GET "
         "/scan/devices; every arrival, since devices come and go). Other players' fixed devices and drones go to the "
         "System page and the Galaxy map, any AMI trade controller to the shops list and any equipment locker to the Lockers page: system scans don't list "
         "shops. Replicant interfaces it sees go to Simulations. Read-only. Without it the traffic poll does the same "
         "scan, minutes later; the poll also rescans once where a replicant has sat since before the last season reset.",
         default_on=True),
    Rule("census_on_arrival", "Stellar census on arrival",
         "When a vessel that can run a stellar census (heaven and cargo vessels) arrives in a system that hasn't had one, "
         "it runs `stellar_census`: the stars around it, explored or not, with positions and entry points. They're added "
         "to the star catalog, which only covers ~70 ly around Sol, so the map, routes and the Unexplored stars list know "
         "them. One action per new system (again after a season reset).", default_on=True),
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
          Option("quiet_in_range", "bool", "Also civilizations with no event yet, in systems your relay network covers", True,
                 help="A beacon goes to every planet or moon with intelligent or spacefaring life in a relay-covered system, "
                      "so their first requests reach you too"),
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
SPREAD = {"station_swap_pass": (5, 3), "dark_relay_pass": (5, 4), "season_rescan_pass": (5, 2), "rule_salvage": (3, 0), "rule_restart_idle_miners": (3, 1), "rule_reopen_sites": (3, 2)}
STEP_TIMEOUT = 3600          # waiting for travel/scan to finish
TRIP_GRACE_S = 1800          # past a trip's ETA, a wait (or a refused step) keeps going this long before giving up
NO_ETA_EXTRA_S = 3600        # still moving and no ETA known: this much longer, once
BUSY_RE = re.compile(r"while (the )?(replicant|device|carrier|vessel)? ?is (still )?(travel|cruis|surg|in motion)|"
                     r"already in motion|is (currently )?(travel|surg|cruis)|"
                     r"not in a star system|in transit", re.I)   # mid-trip between systems (seen live 2026-10-11, Surveyor 3's gather)
# "No conductive remaining at any active site" (live 2026-10-23, Delta Force): the belt's open sites lack that resource.
# Nothing to fix by hand: the step is skipped quietly and doesn't count toward stopping the job.
GONE_OBJECT_RE = re.compile(r"no system object at this location", re.I)   # a pin to a STAR-OBJ-n that is gone
NO_SITE_RE = re.compile(r"\bno \w+ remaining at any active site", re.I)
# Refusals the engine sorts out by itself (live 2026-10-30): a belt surveyor re-activated after a move with no site left to
# track ("Nothing to activate"), a search at a belt already holding the game's most sites (reopen_sites waits for them).
# Skipped at info level, never counted toward the 3-in-a-row stop.
# "No conductive remaining at salvage site" (live 2026-11-12, FALQUORYX-2-2): our copy of the salvage's amounts is stale;
# the resource is noted in kv salvage_gone {body: [resource]} and never asked for there again
SALVAGE_GONE_RE = re.compile(r"\bno (\w+) remaining at salvage site", re.I)
QUIET_SKIP_RE = re.compile(NO_SITE_RE.pattern + r"|nothing to activate|maximum active sites|" + SALVAGE_GONE_RE.pattern, re.I)
BUSY_RETRY_S, BUSY_TRIES = 300, 36   # retry such a refusal every 5 min, for up to 3 h
# the game server itself failing (a 502/503/504 answer, or no answer): nothing was done, so send the same step again
# (snapshot 2026-10-31: two logistics trips answered "Service Unavailable" were skipped and their jobs then failed waiting
# for carriers that had never left)
SERVER_RE = re.compile(r"service unavailable|bad gateway|gateway time-?out|network error", re.I)
SERVER_RETRY_S, SERVER_TRIES = 120, 6
HOP_SETTLE_S, HOP_SETTLE_TRIES = 15, 8   # a trip after a surge hop waits for the device to show at the spot (2 min at most)
SHORT_TIMEOUT = 120          # waiting for deploy/stow confirmation
MAX_LOG = 300


# An alert whose text says only Joe can move it on (a fleet mission out of automatic retries).
NEEDS_YOU_RE = re.compile(r"no more automatic retries", re.I)


def severity(entry: dict) -> str:
    """error / warning / info for the Automations log toggles and notifications. An error is something no automation
    will fix: it needs Joe (owner 2026-10-30), and the caller logs it as "error" (a Confirm, a hand-started job that
    stopped, an evacuation that can't be done). Every other alert is a warning: a stopped automated job, a skipped step,
    a stage that failed on odd data — the engine plans again, or it's for Claude to look into."""
    level = str(entry.get("level") or "info")
    if level in ("error", "warning"):
        return level
    if level != "alert":
        return "info"
    return "error" if NEEDS_YOU_RE.search(str(entry.get("text") or "")) else "warning"


KV_SECTOR_FAR = "sector_far"   # {"FROM>TO" star leg: iso} trips the job runner found beyond the sector range (1.418.0)


def stop_level(job: dict) -> str:
    """How a stopped job is logged: one you started by hand (a chain, a forced run) isn't tried again by anything, so you
    hear about it as an error; an automated one is planned again by its rule on a later pass (a warning)."""
    return "error" if job.get("rule") == "chain" or job.get("forced") else "alert"


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
    """An unfurl refused because the device isn't folded up (already unfurled, or unfolding: "Device is unfurling")."""
    e = (err or "").lower()
    return ("unfurl" in e and ("already" in e or "is unfurling" in e)) or bool(re.search(r"\bnot compact", e))


VESSEL_PRINT_RE = re.compile(r"^/replicants/[^/]+/print$")
HAND_JOB = contextvars.ContextVar("hand_job", default=False)   # set while a job you started yourself sends its steps


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


__all__ = [n for n in dir() if not n.startswith("__")]   # everything, underscore helpers too
