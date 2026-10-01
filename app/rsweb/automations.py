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
from typing import Any

from .api import ApiError
from .db import now_iso
from .shapes import as_amounts  # noqa: F401  (kept for rule authors)

log = logging.getLogger("rsweb.auto")

RESOURCES = ["structural", "conductive", "silicates", "carbon", "volatiles", "rares"]


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


RULES: list[Rule] = [
    Rule("scan_on_arrival", "System scan on arrival",
         "When a replicant's vessel arrives in a system we have no scan for, run a system scan so the "
         "planets and belts are known (other rules and the Systems page use it)."),
    Rule("ami_schedules", "Run AMI schedules",
         "Master switch for the AMI schedules below: every N minutes each schedule checks its controller(s); "
         "if one is idle (or its directive finished) it adopts idle drones of the right kind at its location, "
         "sets the directive and launches it. The controllers do the actual work."),
    Rule("loadouts", "Keep systems at their loadout",
         "Every N minutes, apply the Loadouts page: mark devices above a system's loadout as spare, send spares to "
         "systems that are short, print what's still missing on an autofactory that has the materials, and carry "
         "it there. Devices with an ignored tag are never touched.",
         [Option("every_minutes", "int", "Run every (minutes)", 15)]),
    Rule("salvage_when_depleted", "Salvage when mining sites run out",
         "When every known resource site at a belt is depleted and the system has salvage: an AMI mining controller "
         "in the system is switched to gather_salvage on the biggest salvage (adopting idle drones there) and launched. "
         "With no mining controller in the system, idle mining drones at the worked-out belt fly to the salvage and "
         "mine it. When a salvage runs out, the next one is picked.",
         [Option("use_ami", "bool", "Use the system's AMI mining controller when there is one", True),
          Option("recall", "bool", "AMI: recall its drones when the salvage is used up", False),
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


class AutomationEngine:
    def __init__(self, db, api, hub, worker):
        self.db, self.api, self.hub, self.worker = db, api, hub, worker
        self.lock = asyncio.Lock()
        self.task: asyncio.Task | None = None

    # --- settings & persistence ---------------------------------------------------------
    async def settings(self) -> dict:
        s = await self.db.kv_get("automation_settings", None) or {}
        s.setdefault("dry_run", False)
        rules = s.setdefault("rules", {})
        for r in RULES:
            cfg = rules.setdefault(r.id, {})
            cfg.setdefault("enabled", False)
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
        entries.append({"at": now_iso(), "rule": rule, "level": level, "text": text})
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
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("automation tick failed")
            await asyncio.sleep(60)

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
        jobs = await self.jobs()
        job = {"id": f"{rule}-{int(_now().timestamp() * 1000)}-{len(jobs)}", "rule": rule, "title": title,
               "device": device, "steps": steps, "idx": 0, "status": "running", "created_at": now_iso(),
               "meta": meta or {}}
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
            if not ok and "already at destination" in (err or "").lower():
                ok, st["note"] = True, err  # nothing to do: count it as done and move on
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
    async def on_event(self, ev: dict) -> None:
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
                from .loadouts import to_tag
                orders = await self.db.kv_get("loadout_orders", []) or []
                tags = [str(t) for t in p.get("tags") or []]
                hit = next((o for o in orders if not o.get("device_code") and o["device_type"] == p.get("device_type")
                            and (to_tag(o["star"]) in tags or o.get("factory") == ev.get("device_code"))), None)
                if hit:
                    new = p.get("new_device_code")
                    hit["device_code"] = new or "?"
                    await self.db.kv_set("loadout_orders", orders)
                    if new and to_tag(hit["star"]) not in tags:
                        # the game didn't carry the print's tags over: tag it ourselves so it is routed, not re-printed
                        await self.send("PATCH", f"/devices/{new}", {"configuration": {"add_tags": [to_tag(hit["star"])]}},
                                        f"auto: tag new {p.get('device_type')} {new} for {hit['star']}")
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
            if name in ("site.depleted", "salvage.depleted"):
                try:
                    await self.rule_salvage()
                except Exception as e:
                    log.exception("salvage rule failed")
                    await self.log("engine", f"salvage rule failed: {e}", "alert")
            # rules triggered by arrivals
            if name == "travel.arrived":
                try:
                    await self.on_arrival(ev)
                except Exception as e:
                    log.exception("arrival rules failed")
                    await self.log("engine", f"arrival handling failed: {e}", "alert")

    async def tick(self) -> None:
        async with self.lock:
            now = _now()
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
            await self.rule_salvage()
            await self.rule_restart_idle_miners()
            await self.run_due_schedules()
            await self.run_due_loadouts()

    # --- loadouts --------------------------------------------------------------------------------------
    async def loadout_cfg(self) -> dict:
        from .loadouts import normalize
        return normalize(await self.db.kv_get("loadouts", {}) or {})

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
        p = plan(cfg, devices, bps, inv, stars, hosts, busy, await self.loadout_orders(),
                 await self.db.kv_get("stowed_map", {}) or {}, only)
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
        cfg_only = {**cfg, "roles": {k: v for k, v in cfg["roles"].items() if not only or k in only}}
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
        for ctrl, codes in sorted((p.get("releases") or {}).items()):
            started += bool(await self.create_job("loadouts", f"loadouts: {ctrl} releases {len(codes)} device(s) in another system", ctrl,
                                                  [lo.step(f"{ctrl}: release {', '.join(codes)}", f"/devices/{ctrl}",
                                                           {"command": "release", "devices": codes})],
                                                  {"devices": codes}, force=manual))
        tags = lo.tag_steps(p)
        if tags:
            started += bool(await self.create_job("loadouts", f"loadouts: spare tags ({len(tags)})", None, tags,
                                                  {"devices": []}, force=manual))
        orders = await self.db.kv_get("loadout_orders", []) or []
        for pr in p["prints"]:
            job = await self.create_job("loadouts", f"loadouts: print {pr['n']}× {pr['device_type']} for {pr['star']}",
                                        pr["factory"], lo.print_steps(pr), {"devices": [], "star": pr["star"]}, force=manual)
            if job:
                started += 1
                orders += [{"star": pr["star"], "device_type": pr["device_type"], "factory": pr["factory"],
                            "at": now_iso(), "job": job["id"]} for _ in range(pr["n"])]
        await self.db.kv_set("loadout_orders", orders)
        for code, dest in p["self_moves"]:
            started += bool(await self.create_job("loadouts", f"loadouts: {code} → {dest}", code,
                                                  lo.self_move_steps(code, dest, stars, p["by_code"][code]),
                                                  {"devices": [code], "star": dest}, force=manual))
        for dl in p["deliveries"]:
            started += bool(await self.create_job(
                "loadouts", f"loadouts: {dl['carrier']} carries {len(dl['devices'])} {dl['from']} → {dl['to']}", dl["carrier"],
                lo.delivery_steps(dl, p["by_code"], stars, cfg["settings"]["carriers_return"]),
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
        for code in p["arrived"]:
            steps = lo.arrived_steps(code, p["by_code"][code], stowed_in)
            if steps:
                started += bool(await self.create_job("loadouts", f"loadouts: {code} arrived", code, steps,
                                                      {"devices": [code]}, force=manual))
        await self.db.kv_set("loadouts_last", {"at": now_iso(), "lines": lines, "jobs": started,
                                               "dry_run": (await self.settings())["dry_run"] and not manual})
        return lines

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
        devices = await self.devices()
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
        if cfg.get("use_idle", True):
            drones += [d["device_code"] for d in devices if "survey_drone" in (d.get("device_type") or "")
                       and d.get("location") == (ctrl.get("location") if not carried_ctrl else vessel_loc)
                       and str(d.get("status", "")).startswith("idle") and d["device_code"] not in busy | set(drones)]
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

    async def rule_salvage(self) -> list[str]:
        """See salvage.py. Returns what it did (for the log / tests)."""
        cfg = await self.rule_cfg("salvage_when_depleted")
        if not cfg:
            return []
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

        miners = [d for d in devices if d.get("device_type") == "mining_drone" or
                  (is_controller(d) and kind_of(d.get("device_type")) == "mining")]
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
                for c in ctrls:
                    code = c["device_code"]
                    if code in busy or cooling(code):
                        continue
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
        await self.db.kv_set("salvage_state", state)
        return done

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
