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
    Rule("auto_survey", "Auto-survey new systems",
         "When a vessel arrives in a system with un-surveyed bodies, deploy the survey drones it carries and have "
         "them travel to each planet and belt in turn: scan planets, search belts, then move on. Targets are split "
         "across drones. Already-surveyed bodies are skipped.",
         [Option("use_idle", "bool", "Also use idle survey drones already in the system", True),
          Option("include_moons", "bool", "Include moons", False, help="Gas giants can have dozens of moons"),
          Option("include_belts", "bool", "Search asteroid belts", True),
          Option("max_targets", "int", "Max bodies per system", 20),
          Option("return_and_stow", "bool", "Return to the vessel and stow when finished", True)]),
    Rule("deploy_beacon", "Deploy an FTL beacon in new systems",
         "When a vessel arrives in a system where you have no FTL beacon and it carries one, deploy it "
         "(beacons log traffic through the system)."),
    Rule("restart_idle_miners", "Restart idle mining drones",
         "Every minute, any mining drone sitting idle at a belt is told to start mining again.",
         [Option("resource", "choice", "Resource", "same", ["same"] + RESOURCES,
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
        return err is None, resp, err

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
            if not ok:
                st["error"] = err
                if st["tries"] < 2 and "rate" in (err or "").lower():
                    await self._update(job)
                    return  # retry on next tick
                if st["critical"]:
                    st["status"] = "failed"
                    job["status"] = "failed"
                    await self._update(job)
                    await self.log(job["rule"], f"stopped: {job['title']} — {st['desc']} failed: {err}", "alert", notify=True)
                    return
                st["status"] = "skipped"
                await self.log(job["rule"], f"{job['title']}: skipped '{st['desc']}' ({err})", "alert")
                job["idx"] += 1
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
                target = p.get("scan_target") or p.get("search_target") or ev.get("location")
                if target:
                    surveyed = await self.db.kv_get("surveyed", {}) or {}
                    surveyed[target] = now_iso()
                    await self.db.kv_set("surveyed", surveyed)
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
            await self.rule_restart_idle_miners()

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
    async def on_arrival(self, ev: dict) -> None:
        vessel = ev.get("device_code")
        p = ev.get("payload") or {}
        dest = p.get("destination") or ev.get("location")
        star = star_of(dest) or ev.get("star")
        if not vessel or not star:
            return
        s = await self.settings()
        enabled = {rid for rid, cfg in s["rules"].items() if cfg.get("enabled")}
        if not enabled:
            return
        if "scan_on_arrival" in enabled:
            await self.rule_scan_on_arrival(vessel, star)
        stowed = None
        if "deploy_beacon" in enabled:
            stowed = await self.stowed_in(vessel)
            await self.rule_deploy_beacon(vessel, star, stowed)
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

    async def rule_deploy_beacon(self, vessel: str, star: str, stowed: list[dict]) -> None:
        devs = await self.devices()
        if any("beacon" in (d.get("device_type") or "") and d.get("status") != "stowed"
               and star_of(d.get("location")) == star for d in devs):
            return
        beacon = next((i for i in stowed if "beacon" in (i.get("device_type") or "")), None)
        if not beacon:
            return
        await self.create_job("deploy_beacon", f"deploy FTL beacon {beacon['device_code']} at {star}", beacon["device_code"],
                              [step(f"deploy beacon {beacon['device_code']}", f"/devices/{beacon['device_code']}",
                                    {"command": "deploy"}, critical=True)])

    async def rule_auto_survey(self, vessel: str, vessel_loc: str, star: str, stowed: list[dict], cfg: dict) -> None:
        jobs = await self.jobs()
        if any(j["rule"] == "auto_survey" and j["status"] in ("running", "waiting") and j["meta"].get("star") == star
               for j in jobs):
            return
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
        drones = [(i["device_code"], True) for i in stowed
                  if "survey" in (i.get("device_type") or "") and i["device_code"] not in busy]
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

    async def rule_restart_idle_miners(self) -> None:
        cfg = await self.rule_cfg("restart_idle_miners")
        if not cfg:
            return
        cooldown = timedelta(minutes=int(cfg.get("cooldown_minutes") or 10))
        attempts = await self.db.kv_get("miner_restarts", {}) or {}
        busy = self.busy_devices(await self.jobs())
        now = _now()
        dry = (await self.settings())["dry_run"]
        for d in await self.devices():
            code = d.get("device_code")
            if (d.get("device_type") != "mining_drone" or str(d.get("status")) != "idle"
                    or "BELT" not in (d.get("location") or "") or code in busy
                    or "start_mining" not in (d.get("available_commands") or ["start_mining"])):
                continue
            last = _ts(attempts.get(code))
            if last and now - last < cooldown:
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
            await self.log("restart_idle_miners", f"restarted {code} on {resource}" if ok else f"could not restart {code}: {err}",
                           "info" if ok else "alert")
        await self.db.kv_set("miner_restarts", attempts)
