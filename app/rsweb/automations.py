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
from collections import defaultdict
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import time
from typing import Any

from .api import ApiError
from .db import now_iso
from .shapes import as_amounts  # noqa: F401  (kept for rule authors)
from .ops_rules import OpsRules

log = logging.getLogger("rsweb.auto")

RESOURCES = ["structural", "conductive", "silicates", "carbon", "volatiles", "rares"]



def _reserved(d: dict) -> bool:
    """Fleet members and devices bound for another system are left out of the in-system work rules."""
    from .ami_schedule import reserved
    return reserved(d)

@dataclass
class Option:
    name: str
    kind: str  # bool | int | choice
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
    Rule("census_on_arrival", "Stellar census on arrival",
         "When a vessel that can run a stellar census (heaven and cargo vessels) arrives in a system that hasn't had one, "
         "it runs `stellar_census`: the stars around it, explored or not, with positions and entry points. They're added "
         "to the star catalogue, which only covers ~70 ly around Sol, so the map, routes and the Unexplored stars list know "
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
    Rule("fleet_fill", "Fill unstationed fleets from spares",
         "Every N minutes, a fleet that isn't stationed (stationed ones are kept full by the loadout pass), isn't on a "
         "mission and is short of its loadout (or its template's) takes "
         "idle spare devices of the missing types, nearest first: they get the fleet's tag, the fleet's carrier tours the "
         "systems they're in to pick them up and flies back, and spares that surge themselves fly to the fleet. Missions "
         "still recruit spares in their gather phase whether this is on or not.",
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
         "Every few minutes, for each open event: if what it asks for is at its location and a replicant is there, fulfil it "
         "(POST /locations/<location>/events/<designation>). Optionally have the system's in-system transport controller "
         "deliver the shortfall from other stockpiles, and send a replicant that's already in the same system.",
         [Option("auto_fulfil", "bool", "Fulfil as soon as it's ready", True),
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
         "directive re-set and launched.",
         [Option("use_ami", "bool", "Use the system's AMI mining controller when there is one", True),
          Option("recall", "bool", "AMI: recall its drones when the salvage is used up", True),
          Option("back_to_belt", "bool", "Bring controllers and drones back to a belt once it has open sites again", True),
          Option("back_cooldown_minutes", "int", "Back to the belt: wait before re-trying the same controller (min)", 30),
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
    Rule("deploy_beacon", "Deploy an FTL beacon in new systems",
         "When a vessel arrives in a system with no FTL beacon and it carries one, deploy it (beacons log traffic "
         "through the system). Before deploying it asks the game which beacons are in the system, and it never "
         "deploys twice in the same system.",
         [Option("count_others", "bool", "Also skip systems where another player already has a beacon", True,
                 help="Uses a replicant's view of the system when one is there")]),
    Rule("visitor_alerts", "Visitor alerts (beacon traffic)",
         "Every few minutes the app reads each deployed FTL beacon's traffic log (who arrived and left the system). "
         "When another replicant's devices arrive in one of your systems you get a notification. The full log is on "
         "the Traffic page.",
         [Option("include_npcs", "bool", "Also alert on NPC replicants", True),
          Option("repeat_hours", "int", "Don't repeat an alert for the same replicant and system within (hours)", 6)],
         default_on=True),
    Rule("civ_beacons", "Beacons at civilisation event sites",
         "Civilisations only send their follow-up requests (the daily messages about new events) when an FTL beacon is "
         "deployed AT the planet or moon of one of their events — a beacon in the Kuiper belt or Oort cloud doesn't count. "
         "As soon as a survey discovers an event (and every 10 minutes for older ones) this puts a beacon at that body, so "
         "it's already there when you complete the event. Beacons can't fly, so, cheapest first: a vessel in the system "
         "that carries one flies there and deploys it; a vessel picks up a loose beacon (tagged civ or spare — e.g. one "
         "just printed) and takes it there; a replicant at the body prints one on its vessel; otherwise one is printed on "
         "the system's autofactory (tagged civ) and fetched on a later pass.",
         [Option("print_beacons", "bool", "Print a beacon on the system's autofactory when none is free", True,
                 help="60 structural + 20 conductive each"),
          Option("allow_print", "bool", "Print on a replicant's vessel when the replicant is at the body", True),
          Option("use_replicant_vessel", "bool", "Also use vessels that host your replicant to carry beacons", False,
                 help="Moves your replicant"),
          Option("spare_redundant", "bool", "Mark beacons a system doesn't need as spare", True,
                 help="Once a system has a beacon at a civilisation's body, its other beacons (e.g. Kuiper/Oort) are spare; "
                      "the Loadouts pass gathers spares at the spare depot")]),
    Rule("asteroid_defence", "Asteroid defence",
         "Tracks incoming asteroids (from system.object_detected and by reading the object every 15 minutes): hours to "
         "impact, likelihood, required strength and progress, and estimates how many propulsors it takes to divert it in "
         "time. Alerts when the picture changes. It can activate idle propulsors at the asteroid, send idle ones in the "
         "system there, and print the shortfall on the system's autofactory (sent straight to the asteroid when it can fly).",
         [Option("activate", "bool", "Activate idle propulsors at the asteroid", True),
          Option("send_idle", "bool", "Send idle propulsors in the system to the asteroid", True),
          Option("print_missing", "bool", "Print the shortfall on the system's autofactory", False, help="Spends resources"),
          Option("max_prints", "int", "Most propulsors to print per asteroid at a time", 6)],
         default_on=True),
    Rule("maintenance", "Keep maintenance drones patrolling",
         "A maintenance drone only repairs on the `patrol` directive (it then fixes the most worn device in its system, one "
         "after another). Every N minutes, any maintenance drone sitting in a system without patrol gets it (fleet members "
         "and drones bound for another system are left alone). A system with a device below the critical level and no "
         "maintenance drone raises an alert once a day, or gets one printed on its autofactory.",
         [Option("every_minutes", "int", "Run every (minutes)", 15),
          Option("threshold", "int", "Count a device as worn below (%)", 80),
          Option("critical", "int", "Act when a system's worst device is below (%)", 60),
          Option("print_missing", "bool", "Print a maintenance drone for a system that has none", False, help="Spends resources")],
         default_on=True),
    Rule("restart_idle_miners", "Restart idle mining drones",
         "Every minute, any mining drone sitting idle at a belt gets back to work. Drones an AMI mining controller "
         "already manages are left alone; with a controller at the same location the drone is handed to it "
         "(adopt, and launch if the controller is idle) instead of being told to mine directly.",
         [Option("prefer_ami", "bool", "Hand idle drones to a mining controller at the same location", True),
          Option("resource", "choice", "Resource (when mining directly)", "same", ["same"] + RESOURCES,
                 help="'same' = whatever it mined last (falls back to structural)"),
          Option("cooldown_minutes", "int", "Wait between retries for the same drone (min)", 10)]),
]
RULES_BY_ID = {r.id: r for r in RULES}

STEP_TIMEOUT = 3600          # waiting for travel/scan to finish
SHORT_TIMEOUT = 120          # waiting for deploy/stow confirmation
MAX_LOG = 300


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
        await self._lock.acquire()
        self._owner, self._depth = me, 1
        self.holder = me.get_name() if me is not None else "?"
        self.since = time.monotonic()
        return True

    def release(self) -> None:
        self._depth -= 1
        if self._depth <= 0:
            self._owner, self._depth, self.holder, self.since = None, 0, None, None
            self._lock.release()

    async def __aenter__(self) -> "EngineLock":
        await self.acquire()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.release()


LOCK_ALERT_SECONDS = 600     # the watchdog alerts once the engine lock has been held this long


def _ts(v: str | None) -> datetime | None:
    if not v:
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def step(desc: str, path: str, body: dict | None, wait: list[str] | None = None, timeout: int = STEP_TIMEOUT,
         method: str = "POST", critical: bool = False, match: dict | None = None) -> dict:
    """One command. `wait`: event names that complete it; `match`: payload values the event must carry
    (a key missing from the payload is not held against it)."""
    return {"desc": desc, "method": method, "path": path, "body": body, "wait": wait or [], "timeout": timeout,
            "critical": critical, "match": match or {}, "status": "pending", "started_at": None, "seq0": 0,
            "error": None, "tries": 0}


def event_matches(st: dict, ev: dict, device: str | None) -> bool:
    if ev.get("event") not in st.get("wait", []):
        return False
    if device and ev.get("device_code") != device:
        return False
    p = ev.get("payload") or {}
    for k, v in (st.get("match") or {}).items():
        have = p.get(k, ev.get("location") if k in ("destination", "scan_target", "search_target") else None)
        if have is None:
            continue
        have, want = str(have).upper(), str(v).upper()
        if k == "destination" and "-" not in want:
            # travelling "to a star" lands at one of its locations (entry point, L4 …)
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


class AutomationEngine(OpsRules):
    def __init__(self, db, api, hub, worker):
        self.db, self.api, self.hub, self.worker = db, api, hub, worker
        self.lock = EngineLock()
        self.task: asyncio.Task | None = None
        self.stage: str | None = None          # what the current tick is doing (for the watchdog / snapshot)
        self._lock_alerted = False

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
            cur = await self.db.execute(
                "INSERT INTO notifications(event_id, level, title, body, link, created_at) VALUES(?,?,?,?,?,?)",
                (None, "alert" if level == "alert" else "done", title, None, "/automations", now_iso()))
            self.hub.publish("notify", {"id": cur.lastrowid, "level": "alert" if level == "alert" else "done",
                                        "title": title, "link": "/automations"})

    # --- lifecycle ------------------------------------------------------------------------
    def start(self) -> None:
        self.task = asyncio.create_task(self._tick_loop(), name="automations")

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)

    async def _tick_loop(self) -> None:
        await asyncio.sleep(15)
        while True:
            try:
                await self.watchdog()
                if self.lock.locked() and self.lock.held_for() > LOCK_ALERT_SECONDS:
                    await asyncio.sleep(60)   # still stuck: don't queue up another tick behind it
                    continue
                await self.tick()
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
        from .modular import prepare_moves
        from .shapes import normalize_blueprints
        bps = {b["device_type"]: b for b in normalize_blueprints(await self.db.kv_get("blueprints", []))}
        # drones tracking a site deactivate before moving; large (modular) devices compact (modular.py)
        steps = prepare_moves(steps, await self.devices(), bps)
        jobs = await self.jobs()
        job = {"id": f"{rule}-{int(_now().timestamp() * 1000)}-{len(jobs)}", "rule": rule, "title": title,
               "device": device, "steps": steps, "idx": 0, "status": "running", "created_at": now_iso(),
               "meta": meta or {}}
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

    async def _advance(self, job_id: str) -> None:
        job = await self._get(job_id)
        while job and job["status"] in ("running", "waiting"):
            if job["idx"] >= len(job["steps"]):
                job["status"] = "done"
                job["finished_at"] = now_iso()
                skipped = sum(1 for s in job["steps"] if s["status"] == "skipped")
                await self._update(job)
                await self.log(job["rule"], f"finished: {job['title']}" + (f" ({skipped} step(s) skipped)" if skipped else ""),
                               notify=True)
                return
            st = job["steps"][job["idx"]]
            if st["status"] == "waiting":
                return  # on_event / tick will move it on
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
                ok, resp, err = True, None, None
            else:
                ok, resp, err = await self.send(st["method"], st["path"], st["body"], f"auto: {st['desc']}")
            st["response"] = resp if isinstance(resp, (dict, list)) else None
            if ok and isinstance(resp, dict):
                # long trips: wait at least until the game's own ETA (+30 min) before giving up
                eta = _ts(resp.get("arrives_at")) or _ts(resp.get("completes_at"))
                if eta:
                    st["timeout"] = max(st.get("timeout", STEP_TIMEOUT), (eta - _now()).total_seconds() + 1800)
            if (not ok and (st.get("body") or {}).get("command") == "deploy"
                    and "already deployed" in (err or "").lower()):
                # seen live (2026-10-04, Surveyors fleet): drones already out — that's what the step wanted
                ok, st["note"] = True, err
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
            if (not ok and (st.get("body") or {}).get("command") == "change_owner"
                    and "already belongs to that replicant" in (err or "").lower()):
                # seen live (2026-10-06): a second owner pass for a device the first had already moved
                ok, st["note"] = True, err
                st["wait"] = []
            if not ok:
                st["error"] = err
                if st["tries"] < 2 and "rate" in (err or "").lower():
                    await self._update(job)
                    return  # retry on next tick
                if st["critical"]:
                    st["status"] = "failed"
                    job["status"] = "failed"
                    if job["rule"] == "deploy_beacon" and job.get("meta", {}).get("star"):
                        done = await self.db.kv_get("beacon_systems", {}) or {}
                        done.pop(job["meta"]["star"], None)  # it didn't happen: allow a later retry
                        await self.db.kv_set("beacon_systems", done)
                    await self._update(job)
                    await self.log(job["rule"], f"stopped: {job['title']} — {st['desc']} failed: {err}", "alert", notify=True)
                    return
                st["status"] = "skipped"
                await self.log(job["rule"], f"{job['title']}: skipped '{st['desc']}' ({err})", "alert")
                job["idx"] += 1
                done_steps = [x for x in job["steps"][:job["idx"]] if x["method"] != "WAIT"]
                last3, last6 = done_steps[-3:], done_steps[-6:]
                if ((len(last3) == 3 and all(x["status"] == "skipped" and x.get("error") == err for x in last3))
                        or (len(last6) == 6 and all(x["status"] == "skipped" for x in last6))):
                    job["status"] = "failed"
                    await self._update(job)
                    await self.log(job["rule"], f"stopped: {job['title']} — 3 steps in a row failed with: {err}", "alert", notify=True)
                    return
                await self._update(job)
                continue
            if st["wait"]:
                wait_dev = st.get("wait_device") or job.get("device")
                if await self._already_happened(st, wait_dev):
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

    async def cancel(self, job_id: str) -> None:
        async with self.lock:
            job = await self._get(job_id)
            if job and job["status"] in ("running", "waiting"):
                job["status"] = "cancelled"
                job["finished_at"] = now_iso()
                await self._update(job)
                await self.log(job["rule"], f"cancelled: {job['title']}")

    # --- event & tick handling ------------------------------------------------------------------
    async def on_event(self, ev: dict, late: bool = False) -> None:
        """`late`: a replayed event (see ingest.LATE_EVENT). It still updates what we know and wakes waiting jobs,
        but doesn't fire reactive rules: they'd act on a world that has moved on since."""
        async with self.lock:
            name = ev.get("event") or ""
            p = ev.get("payload") or {}
            # remember what has been surveyed
            if name in ("scan.completed", "search.completed"):
                target = (p.get("scan_target") or p.get("search_target") or p.get("target") or p.get("body")
                          or p.get("designation") or ev.get("location"))
                if target:
                    surveyed = await self.db.kv_get("surveyed", {}) or {}
                    surveyed[target] = now_iso()
                    await self.db.kv_set("surveyed", surveyed)
            # an AMI survey controller finished survey_system: the whole system counts as surveyed
            if name == "directive.completed" and "survey" in (ev.get("device_type") or ""):
                await self.mark_system_surveyed(star_of(ev.get("location")) or ev.get("star") or "", ev.get("device_code"))
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
                    # a cancelled trip never arrives: the device turns back to where it started (the event's
                    # return_time_seconds), so stop waiting for it instead of sitting out the timeout
                    back = int(float(p.get("return_time_seconds") or 0))
                    st["error"] = (f"travel cancelled — {wait_dev} returns to {p.get('origin') or 'where it started'}"
                                   + (f" (≈{back // 60} min)" if back else ""))
                    if st.get("critical"):
                        st["status"], job["status"] = "failed", "failed"
                        await self.log(job["rule"], f"stopped: {job['title']} — {st['error']}", "alert")
                    else:
                        st["status"], job["status"] = "skipped", "running"
                        job["idx"] += 1
                    await self._update(job)
                    if job["status"] == "running":
                        await self._advance(job["id"])
            if name in ("site.depleted", "salvage.depleted") and not late:
                try:
                    await self.rule_salvage()
                except Exception as e:
                    log.exception("salvage rule failed")
                    await self.log("engine", f"salvage rule failed: {e}", "alert")
            if name in ("event.discovered", "event.completed", "print.completed"):
                await self.civ_on_event(ev)
            if name == "system.object_detected":
                asyncio.create_task(self._locked_sync_objects())
            # rules triggered by arrivals
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
            for job in await self.jobs():
                if job["status"] == "running":
                    await self._advance(job["id"])  # e.g. a rate-limited retry
                    continue
                if job["status"] != "waiting":
                    continue
                st = job["steps"][job["idx"]]
                started = _ts(st.get("started_at")) or now
                if (now - started).total_seconds() > st.get("timeout", STEP_TIMEOUT):
                    st["status"] = "skipped"
                    st["error"] = "timed out waiting for " + "/".join(st["wait"])
                    job["idx"] += 1
                    job["status"] = "running"
                    await self._update(job)
                    await self.log(job["rule"], f"{job['title']}: '{st['desc']}' timed out, moving on", "alert")
                    await self._advance(job["id"])
            for stage in ("run_fleets", "fill_fleets", "fleet_owners", "dispatch_new_prints", "rule_contracts", "refresh_known_belts", "track_viability",
                          "rule_consolidate", "rule_reopen_sites", "rule_salvage", "rule_restart_idle_miners",
                          "run_due_schedules", "run_due_loadouts", "civ_beacon_pass", "maintenance_pass"):
                self.stage = stage
                out = await getattr(self, stage)()
                if stage == "civ_beacon_pass":
                    for line in out or []:
                        await self.log("civ_beacons", line)
            self.stage = None
            await self.note_tick()

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
        """Per system: belts, Lagrange points and inner planets (from its scan, the catalogue and device positions)."""
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
        for j in jobs:  # a carrier whose delivery just failed (out of comms, mid-surge …) sits out for 30 minutes
            fin = _ts(j.get("finished_at") or j.get("created_at"))
            if j["rule"] == "loadouts" and j["status"] == "failed" and fin and (_now() - fin).total_seconds() < 1800:
                busy.add(j.get("device"))
                busy.update(j.get("meta", {}).get("devices", []))
        from .loadouts import material_routes
        from .traffic import beacons as _beacons
        civ_locs = {r["location"] for r in await self.civ_coverage() if r.get("completed") or r.get("open")}
        protect = {b["device_code"] for b in _beacons(devices) if b.get("location") in civ_locs}
        p = plan(cfg, devices, bps, inv, stars, hosts, busy, await self.loadout_orders(),
                 await self.db.kv_get("stowed_map", {}) or {}, only, await self.known_open_sites(), protect,
                 await self.geography(devices, stars))
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
        routes, unmet = material_routes(cfg_only, devices, inv, stars, busy, current, await managed_by(self.db))
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
                "loadouts", f"loadouts: {dl['carrier']} carries {len(dl['devices'])} {dl['from']} → {dl['to']}", dl["carrier"],
                lo.delivery_steps(dl, p["by_code"], stars, cfg["settings"]["carriers_return"], p.get("managed"),
                                                  gathering=set(p.get("gathering") or []), assign=p.get("assign"),
                                                  radii=radii, max_cruise_au=far_au),
                {"devices": dl["devices"], "star": dl["to"]}, force=manual))
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
        for code, loc in p.get("pins") or []:
            started += bool(await self.create_job("loadouts", f"loadouts: {code} → {loc} (pinned)", code,
                                                  [lo.pin_step(code, loc)], {"devices": [code]}, force=manual))
        for code, loc in p.get("places") or []:
            started += bool(await self.create_job("loadouts", f"loadouts: {code} → {loc} (placement)", code,
                                                  [lo.pin_step(code, loc, "where its type works")], {"devices": [code]}, force=manual))
        for code in p["arrived"]:
            steps = lo.arrived_steps(code, p["by_code"][code], stowed_in, join=(p.get("assign") or {}).get(code))
            if steps:
                started += bool(await self.create_job("loadouts", f"loadouts: {code} arrived", code, steps,
                                                      {"devices": [code]}, force=manual))
        started += await self._arrival_followups(cfg, p, manual, lines)
        await self.db.kv_set("loadouts_last", {"at": now_iso(), "lines": lines, "jobs": started,
                                               "dry_run": (await self.settings())["dry_run"] and not manual})
        return lines

    async def _arrival_followups(self, cfg: dict, p: dict, manual: bool, lines: list[str]) -> int:
        """After the moves: drones that reached their system join its controller; inactive maintenance drones and
        AMI controllers that reached home are switched on (once per arrival)."""
        from .ami_schedule import handoff_steps, handoffs, managed_by, wakeup_steps, wakeups
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
                if await self.create_job("loadouts", f"loadouts: {dl['carrier']} carries {len(dl['devices'])} {dl['from']} → {dl['to']} "
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
        for ctrl in targets_of(sched, devices):
            code = ctrl["device_code"]
            if code in busy:
                results.append(f"{code}: already running a job")
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
                from .salvage import belt_of, exhausted_place, open_site_count
                place = exhausted_place(state)
                pb = belt_of(place)
                if pb and pb == place and open_site_count(await self.db.kv_get(f"loc:{pb}", None)) > 0:
                    pass  # stale: the belt it ran dry at has open sites again — re-sending restarts it
                elif place and not pb:
                    results.append(f"{code}: exhausted at {place} (its drones are off the belt) — 'Salvage when depleted' "
                                   "brings them back to a belt with open sites")
                    continue
                else:
                    results.append(f"{code}: exhausted on {sched['directive']} at {place or '?'} — re-sending it won't help until sites re-open")
                    continue
            adopt = adoptable(devices, ctrl, managed) if sched.get("adopt", True) else []
            job = await self.create_job("ami_schedules", f"{sched.get('name') or sched['directive']} → {code}", code,
                                        schedule_steps(ctrl, sched, adopt), {"schedule": sched.get("id"), "devices": adopt},
                                        force=manual)
            results.append(f"{code}: {'started' if job else 'planned (dry run)'}"
                           + (f", adopting {len(adopt)}" if adopt else "") + f" ({why})")
        if not results:
            results.append("no matching controllers")
        sched["last_run"] = _now().isoformat(timespec="seconds")
        sched["last_result"] = "; ".join(results)
        return results

    # --- helpers for rules ------------------------------------------------------------------------
    def busy_devices(self, jobs: list[dict]) -> set[str]:
        out = set()
        for j in jobs:
            if j["status"] in ("running", "waiting"):
                out.add(j.get("device"))
                out.update(j.get("meta", {}).get("devices", []))
        return out

    async def devices(self) -> list[dict]:
        return await self.db.kv_get("devices", []) or []

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
        if not vessel or not star:
            return
        if not await self.arrived_from_elsewhere(ev, star):
            return  # moving around inside a system never re-triggers the arrival rules
        s = await self.settings()
        enabled = {rid for rid, cfg in s["rules"].items() if cfg.get("enabled")}
        if not enabled:
            return
        if "scan_on_arrival" in enabled:
            await self.rule_scan_on_arrival(vessel, star)
        if "census_on_arrival" in enabled:
            await self.rule_census_on_arrival(vessel, star)
        stowed = None
        if "deploy_beacon" in enabled:
            stowed = await self.stowed_in(vessel)
            await self.rule_deploy_beacon(vessel, star, stowed, s["rules"]["deploy_beacon"])
        if "auto_survey" in enabled:
            stowed = stowed if stowed is not None else await self.stowed_in(vessel)
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

    async def beacon_in_system(self, star: str, count_others: bool) -> str | None:
        """Why we should NOT deploy a beacon in `star` (None = go ahead)."""
        # 1. one we deployed (or started deploying) ourselves — the device list can lag behind
        done = await self.db.kv_get("beacon_systems", {}) or {}
        if star in done:
            return f"already deployed one there ({done[star]})"
        for j in await self.jobs():
            if j["rule"] == "deploy_beacon" and j.get("meta", {}).get("star") == star and j["status"] in ("running", "waiting"):
                return "a deploy job for this system already ran"
        # 2. ask the game: your own devices filtered to beacons in this system
        try:
            body = await self.api.request("GET", "/devices", params={"device_type": "ftl_beacon", "location": star, "limit": 50},
                                          background=True)
            mine = [d for d in (body or {}).get("devices") or []
                    if "beacon" in (d.get("device_type") or "") and not str(d.get("status", "")).startswith("stowed")
                    and star_of(d.get("location")) == star]
        except ApiError:
            mine = [d for d in await self.devices()
                    if "beacon" in (d.get("device_type") or "") and not str(d.get("status", "")).startswith("stowed")
                    and star_of(d.get("location")) == star]
        if mine:
            return f"your beacon {mine[0].get('device_code')} is there"
        # 3. other players' beacons, seen by a replicant in the system
        if count_others:
            reps = await self.db.kv_get("replicants", {}) or {}
            rep = next((c for c, r in reps.items() if star_of(r.get("location") or r.get("current_location")) == star), None)
            if rep:
                try:
                    body = await self.api.request("GET", f"/replicants/{rep}/scan/devices",
                                                  params={"device_type": "ftl_beacon", "limit": 50}, background=True)
                    theirs = [d for d in (body or {}).get("devices") or [] if "beacon" in (d.get("device_type") or "")]
                    if theirs:
                        return f"{theirs[0].get('owner_name') or 'another player'}'s beacon {theirs[0].get('device_code')} is there"
                except ApiError:
                    pass
        return None

    async def rule_deploy_beacon(self, vessel: str, star: str, stowed: list[dict], cfg: dict | None = None) -> None:
        cfg = cfg or {}
        from .ami_schedule import on_mission
        if on_mission(next((d for d in await self.devices() if d.get("device_code") == vessel), {})):
            return   # a fleet on a mission: a survey crew drops its beacon itself, inside the system (outposts.py)
        beacon = next((i for i in stowed if "beacon" in (i.get("device_type") or "")), None)
        if not beacon:
            return
        why = await self.beacon_in_system(star, bool(cfg.get("count_others", True)))
        if why:
            log.info("deploy_beacon: skipping %s — %s", star, why)
            return
        job = await self.create_job("deploy_beacon", f"deploy FTL beacon {beacon['device_code']} at {star}", beacon["device_code"],
                                    [step(f"deploy beacon {beacon['device_code']}", f"/devices/{beacon['device_code']}",
                                          {"command": "deploy"}, critical=True)], {"star": star})
        if job:  # remember straight away, so the next arrival in this system doesn't deploy another
            done = await self.db.kv_get("beacon_systems", {}) or {}
            done[star] = beacon["device_code"]
            await self.db.kv_set("beacon_systems", done)

    async def rule_auto_survey(self, vessel: str, vessel_loc: str, star: str, stowed: list[dict], cfg: dict) -> None:
        jobs = await self.jobs()
        if any(j["rule"] == "auto_survey" and j["status"] in ("running", "waiting") and j["meta"].get("star") == star
               for j in jobs):
            return
        if star in (await self.db.kv_get("ami_surveyed", {}) or {}):
            return  # an AMI survey_system already covered every body
        for d in await self.devices():
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
        if cfg.get("use_idle", True):
            drones += [d["device_code"] for d in devices if "survey_drone" in (d.get("device_type") or "")
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
        await self.db.kv_set("fleets", keep)
        set_stationed({fleet_tag(f["id"]): f["home"] for f in items if stationed(f)})

    def _mlog(self, m: dict, text: str) -> None:
        m.setdefault("log", []).append({"at": now_iso(), "text": text})
        m["log"] = m["log"][-60:]

    async def fleet_phase_steps(self, fleet: dict, m: dict, phase: str, devices: list[dict]) -> tuple[list[dict], list[str]]:
        from . import fleets as fl
        from . import loadouts as lo
        from .shapes import normalize_inventory
        cat = await self.db.kv_get("stars", {}) or {}
        stars = {x.get("designation"): x for x in (cat.get("stars") or []) if isinstance(x, dict)}
        inv = {i.get("location"): i.get("items") or {} for i in normalize_inventory(await self.db.kv_get("inventory", []))}
        target = (m.get("targets") or [None])[m.get("idx", 0)] if m.get("targets") else None
        opts = m.get("opts") or {}
        radii, far_au = await self.cruise_radii(), await self.max_cruise_au()
        if phase == "assemble":
            return fl.assemble_steps(fleet, devices, radii, far_au)
        if phase == "gather":
            plan = fl.gather_plan(fleet, devices, stars, self.busy_devices(await self.jobs()))
            if plan["recruit"]:
                self._mlog(m, "recruiting spares: " + ", ".join(f"{d['device_code']} ({d.get('device_type')})" for d in plan["recruit"]))
            if plan["tour"]:
                self._mlog(m, f"{plan['carrier']} collects from " + ", ".join(f"{s_} ({len(ds)})" for s_, ds in plan["tour"]))
            return fl.gather_steps(fleet, plan, stars, radii, far_au), plan["problems"]
        if phase == "travel":
            return fl.travel_steps(fleet, devices, target, stars), []
        if phase == "deploy":
            from . import placement as pl
            geo = pl.geography(target or "", devices, await self.system_scan(target) if target else None,
                               (stars.get(target) or {}).get("entry_point"))
            spot = fl.deploy_spot(geo)
            if not spot:
                self._mlog(m, f"nothing known inside {target} yet (no scan): unloading where the carrier is")
            steps = fl.unload_steps(fleet, devices, spot)
            if fleet["role"] == "explore":
                # a survey crew leaves a relay (at an L4/L5 point) and a beacon in each system that has none of yours
                from . import outposts
                carriers = {c["device_code"] for c in fl.roster(fleet, devices)["carriers"]}
                drop, notes = outposts.drop_steps(carriers, devices, target or "", spot,
                                                  await self.db.kv_get("stowed_map", {}) or {}, fl.fleet_tag(fleet["id"]))
                steps += drop
                for n in notes:
                    self._mlog(m, n)
            return steps, []
        if phase == "work":
            if fleet["role"] == "explore":
                from . import placement as pl
                geo = pl.geography(target or "", devices, await self.system_scan(target) if target else None,
                                   (stars.get(target) or {}).get("entry_point"))
                ctrl = next((d for d in fl.members(fleet, devices) if d.get("device_type") == "ami_survey_controller"), None)
                spot = pl.target("ami_survey_controller", geo) if ctrl and not pl.ok("ami_survey_controller",
                                                                                     ctrl.get("location"), geo) else None
                return fl.explore_work_steps(fleet, devices, spot)
            belt = fl.richest_belt(target, await self.system_scan(target))
            sal = None
            if not belt:   # no belt here: salvage is the only work
                from .salvage import available_salvage
                from .targets import system_resources
                found = available_salvage(await system_resources(self.db, target))
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
                to = fl.materials_target(fleet, fleets)   # the fleet its materials go to, else the nearest that takes them in
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
            steps = fl.recall_steps(fleet, devices, inv, haul, radii, far_au)
            if fleet["role"] == "explore" and target:
                # the survey found a civilisation: once everyone is aboard, the beacon goes to that body (civilisations
                # only send follow-up requests to a beacon AT their planet or moon)
                from . import outposts
                carrier = next(iter(fl.roster(fleet, devices)["carriers"]), None)
                civ = outposts.civ_places(await self.civ_coverage(), target)
                if carrier and civ:
                    more, notes = outposts.civ_move_steps(carrier, devices, target, civ,
                                                          await self.db.kv_get("stowed_map", {}) or {})
                    steps += more
                    for n in notes:
                        self._mlog(m, n)
            return steps, []
        if phase == "return":
            return fl.travel_steps(fleet, devices, m.get("drop_star") or fleet["home"], stars), []
        if phase == "unload":
            # A stationed fleet's devices work its home system, so they come off the carriers. Any other fleet stays aboard,
            # ready for its next mission: only cargo is deposited (a cargo carrier riding a carrier hops off for it and
            # boards again).
            pile = lo.drop_point(m.get("drop_star") or fleet["home"], devices, inv, stars)
            if fleet.get("station"):
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
        """A contract / trade mission (fleets.deal): gather the price, deliver it, fulfil, collect the rewards, go home."""
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
        if phase == "trade":
            return [fl.fulfil_step(dl)], []
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
        out = []
        for f in await self.fleets():
            if (only and f["id"] != only) or (f.get("mission") or {}).get("status") == "running" or f["id"] in working:
                continue
            if fl.stationed(f):
                continue   # the loadout pass keeps a stationed fleet filled, at its home
            if not fl.short_list(f, devices):
                continue
            steps, recruits, notes = fl.fill_plan(f, devices, stars, busy, radii, far_au)
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

    async def run_fleets(self) -> None:
        from . import fleets as fl
        items = await self.fleets()
        if not any((f.get("mission") or {}).get("status") == "running" for f in items):
            return
        jobs = {j["id"]: j for j in await self.jobs()}
        devices = await self.devices()
        changed = False
        for fleet in items:
            m = fleet.get("mission") or {}
            if m.get("status") != "running":
                continue
            j = jobs.get(m.get("job"))
            if j and j["status"] in ("running", "waiting"):
                continue
            if j and j["status"] == "failed":
                m["status"] = "stalled"
                self._mlog(m, f"stalled in {m.get('phase')}: {next((s.get('error') for s in j['steps'] if s['status'] == 'failed'), 'a step failed')}")
                await self.log("fleets", f"{fleet['name']}: stalled in {m.get('phase')}", "alert", notify=True)
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
            phases = fl.PHASES[fleet["role"]]
            if m.get("phase") == "wait":
                site = fl.deal(m)["location"]
                reps = await self.db.kv_get("replicants", {}) or {}
                here = [r.get("name") or c for c, r in reps.items() if (r.get("location") or r.get("current_location")) == site]
                if not here:
                    note = f"materials at {site}: waiting for a replicant there to fulfil"
                    if m.get("watch_note") != note:
                        m["watch_note"] = note
                        changed = True
                    continue
                self._mlog(m, f"{', '.join(here)} at {site}: fulfilling")
            if m.get("phase") == "watch":
                done, why, upd = fl.watch_done(fleet, m, devices, now_iso())
                m.update(upd)
                if m.get("watch_note") != why:
                    m["watch_note"] = why
                    changed = True
                if not done and not m.pop("recall_now", False):
                    continue
                self._mlog(m, f"work done ({why})")
            # next phase (skipping any with nothing to do)
            for _ in range(12):
                nxt = fl.next_phase(fleet["role"], m)
                if not nxt:
                    m["status"], m["job"] = "done", None
                    self._mlog(m, "mission complete")
                    await self.log("fleets", f"{fleet['name']}: mission complete", notify=True)
                    break
                m["phase"], m["phase_at"] = nxt, now_iso()
                if nxt == "watch":
                    self._mlog(m, "on station — watching until the work is done")
                    break
                if nxt == "wait":
                    site = fl.deal(m)["location"]
                    self._mlog(m, f"materials delivered to {site}: waiting for a replicant there")
                    await self.log("fleets", f"{fleet['name']}: materials at {site} — send a replicant there to fulfil "
                                             f"{fl.deal(m)['label']}", notify=True)
                    changed = True
                    break
                steps, problems = await self.fleet_phase_steps(fleet, m, nxt, devices)
                for pr in problems:
                    self._mlog(m, f"{nxt}: {pr}")
                if m.pop("stall", False):
                    m["status"], m["job"] = "stalled", None
                    await self.log("fleets", f"{fleet['name']}: stalled in {nxt}: {'; '.join(problems)}", "alert", notify=True)
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
        """See gameevents.py. Fulfil ready events; optionally deliver shortfalls / send a nearby replicant."""
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
        for des, e in (await gev.load(self.db)).items():
            if e["status"] != "open" or not e.get("location"):
                continue
            prog = gev.progress(e, inv, devices, reps)
            tried = state.get(des) or {}
            if prog["state"] == "ready" and cfg.get("auto_fulfil", True):
                t = _ts(tried.get("fulfil"))
                if t and (_now() - t).total_seconds() < 1800:
                    continue  # tried recently; let the event stream catch up (or the error be read)
                rep = prog["present"][0]["code"]
                filled = (tmpl.replace("{designation}", des).replace("{replicant}", rep).replace("{location}", e["location"])
                          .replace("{criteria}", (prog.get("best") or {}).get("name") or "default"))
                method, _, rest = filled.partition(" ")
                path, _, body = rest.partition(" ")
                job = await self.create_job("contracts", f"fulfil {e.get('title')} at {e['location']}", None,
                                            [step(f"fulfil {des}", path, json.loads(body) if body.strip() else None, method=method.upper())],
                                            {"event": des})
                tried["fulfil"] = now_iso()
                done.append(f"fulfil {des}" + ("" if job else " (dry run)"))
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
        for belt, why in sorted(sites.exhausted_belts(devices, await self.exhausted_places()).items()):
            last = _ts(state.get(belt))
            if last and now - last < cooldown:
                continue
            if any(j["rule"] == "reopen_sites" and j["status"] in ("running", "waiting") and j.get("meta", {}).get("belt") == belt
                   for j in jobs):
                continue
            star = star_of(belt)
            have = len(sites.survey_at(devices, belt))
            if have >= per:
                continue  # enough drones already opening/holding sites there
            ctrls = [d for d in devices if "survey" in (d.get("device_type") or "") and "controller" in (d.get("device_type") or "")
                     and star_of(d.get("location")) == star and d["device_code"] not in busy and d.get("in_control_range") is not False]
            idle = [d for d in devices if "survey_drone" in (d.get("device_type") or "") and star_of(d.get("location")) == star
                    and str(d.get("status") or "").startswith("idle") and d["device_code"] not in busy
                    and d.get("in_control_range") is not False]
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
                job = await self.create_job("reopen_sites", f"re-open sites at {belt} with {ctrl['device_code']} ({why})",
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
            out[b] = open_site_count(detail)
        await self.db.kv_set("belt_reads", reads)
        return out

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
        return co.plan(devices, inv, bps, self.busy_devices(await self.jobs()), protected, float(cfg.get("min_amount") or 100))

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
        cat = await self.db.kv_get("stars", {}) or {}
        stars = {s.get("designation"): s for s in (cat.get("stars") or []) if isinstance(s, dict)}
        known: dict[str, int] = {}
        for r in await self.db.fetchall("SELECT key, value FROM kv WHERE key LIKE 'loc:%-BELT-%'"):
            b = via.belt_of(r["key"][4:])
            if b and b == r["key"][4:]:
                idx = [x.get("site_index") for x in (json.loads(r["value"]) or {}).get("resource_sites") or []
                       if isinstance(x, dict) and x.get("site_index") is not None]
                known[b] = max(idx) if idx else 0
        cfg = (await self.settings())["rules"].get("belt_viability") or {}
        return via.report(state, stars, (cfg.get("move_at_percent") or 250) / 100, known)

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
            out[star_of(b)] = out.get(star_of(b), 0) + open_site_count(await self.db.kv_get(f"loc:{b}", None))
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
                    or (dv.get("name") == "gather_salvage" and st.startswith(sv.FINISHED_STATES)))
        cands = [c for c in ctrls if candidate(c)]
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
        directive_for = {}
        for sched in await self.schedules():   # use the directive an AMI schedule gives that controller, if any
            if sched.get("enabled", True):
                for c in targets_of(sched, devices):
                    directive_for.setdefault(c["device_code"], sched["directive"])
        done = []
        for p in sv.back_to_belt_plan(cands, devices, await managed_by(self.db), open_sites, sysb, skip, directive_for):
            job = await self.create_job("salvage_when_depleted",
                                        f"{p['ctrl']}: back to {p['belt']} ({p['why']})"
                                        + (f", bringing {len(p['away'])} drone(s)" if p["away"] else ""),
                                        p["ctrl"], sv.back_to_belt_steps(p), {"devices": p["away"], "belt": p["belt"]})
            back[p["ctrl"]] = now.isoformat(timespec="seconds")
            done.append(f"{p['ctrl']} → {p['belt']}" + (" (planned, dry run)" if not job else ""))
        await self.db.kv_set("salvage_state", {**(await self.db.kv_get("salvage_state", {}) or {}), "back": back})
        return done

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
        for star in sorted({star_of(d.get("location")) for d in miners}):
            res = await system_resources(self.db, star)
            dry = sv.worked_out(res) | {p for p in await self.exhausted_places() if star_of(p) == star}
            sal = sv.available_salvage(res)
            dead_sal = {x["code"] for x in res.get("salvage") or [] if x.get("depleted")}
            for k in [k for k, v in assigned.items() if k in dead_sal]:
                assigned.pop(k)
            ctrls = [d for d in miners if is_controller(d) and star_of(d.get("location")) == star]
            # the controller's own report: "_eval_state": "exhausted:[...]:<place>" = nothing left to mine there
            exhausted = {c["device_code"] for c in ctrls
                         if str(((c.get("ami_directive") or {}).get("_eval_state")) or "").startswith("exhausted")}
            if (not dry and not exhausted) or not sal:
                continue
            if ctrls and cfg.get("use_ami", True):
                reopening = await self.rule_cfg("reopen_sites")
                survey_here = any("survey" in (d.get("device_type") or "") and star_of(d.get("location")) == star for d in devices)
                for c in ctrls:
                    code = c["device_code"]
                    if code in busy or cooling(code):
                        continue
                    if reopening and survey_here:
                        continue  # sites are being re-opened by survey drones; keep mining rather than switch to salvage
                    row = await self.db.fetchone("SELECT event, payload FROM events WHERE device_code=? AND event LIKE 'directive.%' "
                                                 "ORDER BY seq DESC LIMIT 1", (code,))
                    cur = json.loads(row["payload"] or "{}") if row else {}
                    on_salvage = (row and row["event"] == "directive.set" and cur.get("directive") == "gather_salvage"
                                  and (cur.get("configuration") or {}).get("location") not in dead_sal | {sv.body_of(c) for c in dead_sal})
                    if on_salvage:
                        continue
                    idle, _ = await controller_idle(self.db, c)
                    if not (code in exhausted or sv.at_worked_out_place(c.get("location"), dry, dead_sal) or idle):
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
                                  and sv.at_worked_out_place(d.get("location"), dry, dead_sal)),
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
        await self.db.kv_set("salvage_state", state)
        return back_done + done

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
        for d in devices:
            code = d.get("device_code")
            if _reserved(d):
                continue  # a fleet's drones are run by the fleet
            if (d.get("device_type") != "mining_drone" or str(d.get("status")) != "idle"
                    or "BELT" not in (d.get("location") or "") or code in busy or code in managed
                    or "start_mining" not in (d.get("available_commands") or ["start_mining"])):
                continue
            from .salvage import belt_of
            if belt_of(d.get("location")) in dry_belts:
                continue  # nothing left to mine there: the salvage rule moves it
            last = _ts(attempts.get(code))
            if last and now - last < cooldown:
                continue
            ctrl = next((c for c in devices if is_controller(c) and kind_of(c.get("device_type")) == "mining"
                         and c.get("location") == d.get("location")), None) if cfg.get("prefer_ami", True) else None
            if ctrl:
                handed.setdefault(ctrl["device_code"], []).append(code)
                attempts[code] = now.isoformat(timespec="seconds")
                continue
            resource = cfg.get("resource") or "same"
            if resource == "same":
                row = await self.db.fetchone(
                    "SELECT payload FROM events WHERE device_code=? AND event IN ('mining.started','mining.stopped','mining.retargeted') "
                    "ORDER BY seq DESC LIMIT 1", (code,))
                pl = json.loads(row["payload"]) if row else {}
                resource = pl.get("resource_type") or pl.get("new_resource") or "structural"
            attempts[code] = now.isoformat(timespec="seconds")
            if dry:
                await self.log("restart_idle_miners", f"[dry run] would restart {code} on {resource}")
                continue
            ok, _, err = await self.send("POST", f"/devices/{code}", {"command": "start_mining", "resource_type": resource},
                                         f"auto: restart {code}")
            if not ok and "exhausted" in (err or "").lower():
                from .salvage import belt_of
                ex = await self.db.kv_get("exhausted_places", {}) or {}
                ex[belt_of(d.get("location")) or d.get("location")] = now_iso()
                await self.db.kv_set("exhausted_places", ex)
            await self.log("restart_idle_miners", f"restarted {code} on {resource}" if ok else f"could not restart {code}: {err}",
                           "info" if ok else "alert")
        for ccode, drones in handed.items():
            ctrl = next(c for c in devices if c.get("device_code") == ccode)
            idle, _ = await controller_idle(self.db, ctrl)
            steps = [step(f"{ccode}: adopt idle {', '.join(drones)}", f"/devices/{ccode}", {"command": "adopt", "devices": drones})]
            if idle:
                steps.append(step(f"{ccode}: launch", f"/devices/{ccode}", {"command": "launch"}))
            await self.create_job("restart_idle_miners", f"hand {len(drones)} idle miner(s) to {ccode}", ccode, steps,
                                  {"devices": drones})
        await self.db.kv_set("miner_restarts", attempts)
