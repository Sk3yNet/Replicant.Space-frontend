"""Live snapshot + mining diagnosis.

`capture()` makes read-only GETs (devices, inventory, mining controllers, the belts miners are at, recent mining/AMI
events, logs of idle drones), capped at `max_requests` and run on the background budget, and bundles them with the
app's own automation state (rules, schedules, recent jobs, log). Every response is kept with its path and time, so the
file doubles as test fixtures. The API token is never included.

`diagnose()` reads that bundle and says, per mining drone, what it's doing, what its controller is doing, how many sites
are open where it is, and which of the app's rules would act on it (or why each one skips it).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from .ami_schedule import is_controller, kind_of, reserved
from .api import ApiError

SNAPSHOT_VERSION = 1


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def belt_of(loc: str | None) -> str | None:
    m = re.match(r"^([A-Z0-9]+-BELT-\d+)", (loc or "").upper())
    return m.group(1) if m else None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(v: Any) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def is_miner(d: dict) -> bool:
    return (d.get("device_type") or "") == "mining_drone"


def is_mining_ctrl(d: dict) -> bool:
    return is_controller(d) and kind_of(d.get("device_type")) == "mining"


async def capture(api, db, eng, stars: set[str] | None = None, max_requests: int = 60, progress=None) -> dict:
    calls: list[dict] = []
    skipped: list[str] = []

    async def get(path: str, **params: Any) -> Any:
        if len(calls) >= max_requests:
            skipped.append(path)
            return None
        rec: dict = {"path": path, "params": {k: v for k, v in params.items() if v is not None},
                     "at": _now().isoformat(timespec="seconds")}
        calls.append(rec)
        try:
            body = await api.get(path, background=True, **params)
            rec["status"], rec["body"] = 200, body
            return body
        except ApiError as e:
            rec["status"], rec["error"], rec["body"] = e.status, e.message, e.body
            return None
        finally:
            if progress:
                await progress(len(calls), max_requests, path)

    # 1. every device (page by hand, like the sync: an empty first page with a cursor = partial snapshot)
    devices, cursor, partial = [], None, False
    for _ in range(10):
        body = await get("/devices", limit=50, cursor=cursor) or {}
        page = body.get("devices") or []
        cursor = body.get("next_cursor")
        partial = partial or (not page and bool(cursor))
        devices.extend(page)
        if not cursor:
            break
    if not devices:  # partial / failed: fall back on the app's cached list for choosing what else to read
        devices = await db.kv_get("devices", []) or []
    in_scope = [d for d in devices if not stars or star_of(d.get("location")) in stars]

    # 2. stockpiles
    cur = None
    for _ in range(4):
        body = await get("/inventory", limit=50, cursor=cur) or {}
        cur = body.get("next_cursor")
        if not cur:
            break

    # 3. mining controllers (full detail: directive, eval state, managed devices)
    ctrls = [d for d in in_scope if is_mining_ctrl(d)]
    for c in ctrls[:12]:
        await get(f"/devices/{c['device_code']}")
        await get(f"/devices/{c['device_code']}/logs", latest="true", limit=15)   # e.g. ami_overheat

    # 4. the belts miners and controllers are at (open resource sites)
    belts = sorted({b for d in in_scope if (is_miner(d) or is_mining_ctrl(d)) for b in [belt_of(d.get("location"))] if b})
    import json as _json
    for star in sorted({star_of(c.get("location")) for c in ctrls if c.get("location")}):  # every belt in a mining system
        row = await db.fetchone("SELECT data FROM systems WHERE star=?", (star,))
        scan = _json.loads(row["data"]) if row else {}
        belts += [b["designation"] for b in ((scan.get("asteroid_belt") or {}).get("belts")) or []
                  if b.get("designation") and b["designation"] not in belts]
    for b in belts[:12]:
        await get(f"/locations/{b}")

    # 5. recent mining + AMI events
    # /events returns oldest-first from the start of the window: ask for the last few hours, not the whole day
    since = (_now() - timedelta(hours=3)).isoformat(timespec="seconds")
    await get("/events", after=since, limit=100)
    await get("/events", category="ami", after=since, limit=100)

    # 6. idle (not mining) drones: detail + their latest log lines
    idle = [d for d in in_scope if is_miner(d) and not str(d.get("status") or "").startswith("mining")]
    for d in idle[:6]:
        await get(f"/devices/{d['device_code']}")
        await get(f"/devices/{d['device_code']}/logs", latest="true", limit=15)

    # app-side state: why the automations did / didn't act
    from .ami_schedule import managed_by
    settings = await eng.settings()
    jobs = await eng.jobs()
    mine_rules = ("restart_idle_miners", "ami_schedules", "reopen_sites", "salvage_when_depleted", "loadouts", "fleets", "engine")
    app = {
        "rules": {k: v for k, v in (settings.get("rules") or {}).items()},
        "dry_run": settings.get("dry_run"),
        "schedules": await eng.schedules(),
        "jobs": [{k: j.get(k) for k in ("id", "rule", "title", "status", "device", "created_at", "finished_at", "error", "meta", "v", "run")}
                 | {"steps": [{"desc": s.get("desc"), "status": s.get("status"), "error": s.get("error")} for s in j.get("steps") or []]}
                 for j in jobs if j.get("rule") in mine_rules][-40:],
        "log": [e for e in (await db.kv_get("automation_log", []) or []) if e.get("rule") in mine_rules][-150:],
        "miner_restarts": await db.kv_get("miner_restarts", {}) or {},
        "exhausted_places": await db.kv_get("exhausted_places", {}) or {},
        "reopen_state": await db.kv_get("reopen_state", {}) or {},
        "managed_by": await managed_by(db),
        "salvage_state": await db.kv_get("salvage_state", {}) or {},
        "belt_reads": await db.kv_get("belt_reads", {}) or {},
        "viability": await eng.viability_report(),
        "loadouts_last": await db.kv_get("loadouts_last", {}) or {},
        "loadouts": await db.kv_get("loadouts", {}) or {},              # templates, settings, ignore tags — to replay a pass
        "loadout_orders": await db.kv_get("loadout_orders", []) or [],
        "stowed_map": await db.kv_get("stowed_map", {}) or {},
        "engine": {**eng.engine_status(), **(await db.kv_get("engine_tick", {}) or {})},   # is the tick loop alive?
        "fleets": [{"id": f.get("id"), "name": f.get("name"), "role": f.get("role"), "home": f.get("home"),
                    "station": bool(f.get("station")), "template": f.get("template"), "wants": f.get("wants") or {},
                    "materials": f.get("materials") or "", "mission": (f.get("mission") or {}).get("status")}
                   for f in await eng.fleets()],
    }
    from . import version as ver
    app["server"] = {"version": ver.VERSION, "fingerprint": ver.FINGERPRINT, "build": ver.BUILD, "run": ver.RUN_ID,
                     "runs": (await db.kv_get("server_runs", []) or [])[-15:],
                     "changes": [{"version": v, "date": d, "summary": t} for v, d, t in ver.CHANGES]}
    snap = {"version": SNAPSHOT_VERSION, "captured_at": _now().isoformat(timespec="seconds"),
            "scope": sorted(stars) if stars else "all", "partial_device_list": partial,
            "requests": len(calls), "skipped": skipped, "calls": calls, "app": app}
    try:
        snap["diagnosis"] = diagnose(snap)
    except Exception as e:  # keep the raw capture even if the diagnosis trips over unexpected data
        import traceback
        tb = traceback.extract_tb(e.__traceback__)[-1]
        snap["diagnosis"] = {"headline": [f"diagnosis failed: {type(e).__name__}: {e} (snapshot.py line {tb.lineno}) — "
                                          "the raw snapshot is still complete; download it"],
                             "drones": [], "controllers": [], "rules_on": {}, "recent_alerts": [], "failed_jobs": []}
    return snap


def _bodies(snap: dict, prefix: str) -> dict[str, Any]:
    """path -> body for successful calls whose path starts with prefix (latest wins)."""
    return {c["path"]: c.get("body") for c in snap.get("calls") or [] if c["path"].startswith(prefix) and c.get("status") == 200}


def devices_of(snap: dict) -> list[dict]:
    out: list[dict] = []
    for c in snap.get("calls") or []:
        if c["path"] == "/devices" and c.get("status") == 200:
            out.extend((c.get("body") or {}).get("devices") or [])
    details = {p.split("/")[2]: b for p, b in _bodies(snap, "/devices/").items() if p.count("/") == 2 and isinstance(b, dict)}
    return [{**d, **details.get(d.get("device_code"), {})} for d in out]


def engine_headline(snap: dict) -> list[str]:
    """The automation engine is stuck (lock held for long) or its ticks stopped finishing."""
    eng = (snap.get("app") or {}).get("engine") or {}
    cap = _ts(snap.get("captured_at"))
    out = []
    if eng.get("lock_held") and (eng.get("held_seconds") or 0) > 600:
        out.append(f"AUTOMATIONS STALLED: engine busy for {eng['held_seconds'] // 60} min (held by {eng.get('held_by') or '?'}, "
                   f"stage {eng.get('stage') or '-'}) — restart the app")
    fin = _ts(eng.get("finished_at"))
    if cap and fin and (cap - fin).total_seconds() > 900 and not out:
        out.append(f"automation ticks haven't finished since {fin.isoformat(timespec='seconds')}"
                   + (f" — last error: {eng['error']}" if eng.get("error") else ""))
    return out


def diagnose(snap: dict) -> dict:
    devices = devices_of(snap)
    app = snap.get("app") or {}
    rules = app.get("rules") or {}
    on = {k: bool((v or {}).get("enabled")) for k, v in rules.items()}
    managed = dict(app.get("managed_by") or {})
    for d in devices:
        if d.get("controller_device_code"):
            managed[d["device_code"]] = d["controller_device_code"]
    by = {d.get("device_code"): d for d in devices}
    belts = {p.split("/")[2]: b for p, b in _bodies(snap, "/locations/").items() if isinstance(b, dict)}
    exhausted = set((app.get("exhausted_places") or {}).keys())
    busy = {j.get("device") for j in app.get("jobs") or [] if j.get("status") in ("running", "waiting")} | {
        x for j in app.get("jobs") or [] if j.get("status") in ("running", "waiting") for x in (j.get("meta") or {}).get("devices") or []}
    restarts = app.get("miner_restarts") or {}
    cooldown = int(((rules.get("restart_idle_miners") or {}).get("cooldown_minutes")) or 10)
    logs = {p.split("/")[2]: (b or {}).get("events") or [] for p, b in _bodies(snap, "/devices/").items() if p.endswith("/logs")}
    schedules = app.get("schedules") or []

    def sched_for(c: dict) -> list[dict]:
        out = []
        for s in schedules:
            t = s.get("target") or ""
            if t == c.get("device_code") or (t == "kind:mining" and not reserved(c)
                                              and (not s.get("star") or s.get("star") == star_of(c.get("location")))):
                out.append({"id": s.get("id"), "directive": s.get("directive"), "enabled": s.get("enabled", True),
                            "every_minutes": s.get("every_minutes"), "last_run": s.get("last_run")})
        return out

    def directive(c: dict) -> dict:
        dv = c.get("ami_directive") if isinstance(c.get("ami_directive"), dict) else {}
        return {"name": dv.get("name"), "config": dv.get("config"), "state": dv.get("_eval_state"),
                "status": c.get("ami_directive_status")}

    def belt_info(b: str | None) -> dict:
        if not b:
            return {}
        det = belts.get(b)
        sites = (det or {}).get("resource_sites") if det else None
        trackers = [d["device_code"] for d in devices if (d.get("location") or "").startswith(b) and
                    str(d.get("status") or "").startswith(("tracking", "searching"))]
        from .salvage import open_site_count
        return {"belt": b, "read": det is not None, "open_sites": open_site_count(det) if det is not None else None,
                "sites": [{"code": s.get("designation") or s.get("site"), "resource": s.get("resource_type") or s.get("resource"),
                           "level": s.get("availability"), "qty": s.get("quantity") or s.get("remaining")}
                          for s in (sites or []) if isinstance(s, dict)],
                "trackers": trackers, "marked_exhausted": b in exhausted, "searches": searches_at(b)}

    def searches_at(b: str) -> dict | None:
        """Survey drones searching this belt: how many, how far along, when the first new site is due."""
        runs = [d.get("scan") or {} for d in devices if str(d.get("status") or "").startswith("searching")
                and ((d.get("scan") or {}).get("target") == b or (d.get("location") or "") == b)]
        if not runs:
            return None
        ends = sorted(x.get("completes_at") for x in runs if x.get("completes_at"))
        pct = [float(x["progress_percent"]) for x in runs if x.get("progress_percent") is not None]
        return {"drones": len(runs), "progress": round(min(pct), 1) if pct else None, "first_due": ends[0] if ends else None,
                "last_due": ends[-1] if ends else None}

    def search_note(b: str | None) -> str | None:
        sr = searches_at(b) if b else None
        if not sr:
            return None
        return (f"{sr['drones']} survey drone(s) searching {b}" + (f", {sr['progress']}%+ done" if sr["progress"] is not None else "")
                + (f" — new site(s) due {sr['first_due']}" if sr["first_due"] else "")
                + "; mining resumes once they open (back to the belt re-launches the controller)")

    ctrl_rows = []
    for c in [d for d in devices if is_mining_ctrl(d)]:
        dv = directive(c)
        kids = [k for k, v in managed.items() if v == c["device_code"]]
        mining = [k for k in kids if str((by.get(k) or {}).get("status") or "").startswith("mining")]
        here_idle = [d["device_code"] for d in devices if is_miner(d) and d.get("location") == c.get("location")
                     and d["device_code"] not in managed and str(d.get("status") or "").startswith("idle")]
        notes = []
        st = str(dv["state"] or "")
        fleet = next((t[6:] for t in c.get("tags") or [] if t.startswith("fleet:")), None)
        if not dv["name"] and fleet and reserved(c):
            notes.append(f"fleet {fleet}: away from its station (or not stationed) — the in-system rules leave its devices "
                         "alone until a mission puts it to work")
        elif not dv["name"]:
            notes.append("no directive: it won't do anything until one is set" +
                         (" (an AMI schedule targets it)" if sched_for(c) else " and no AMI schedule targets it"))
        elif st.startswith("exhausted"):
            from .salvage import exhausted_place
            place = exhausted_place(st)
            sb = [b for b in belts if star_of(b) == star_of(c.get("location"))]
            open_b = {b: belt_info(b).get("open_sites") for b in sb}
            best = max((b for b, n in open_b.items() if n), key=lambda b: open_b[b], default=None)
            if place and belt_of(place) is None and best:
                notes.append(f"exhausted at {place}, where its drones are (not a belt) — {best} has {open_b[best]} open site(s): "
                             "the drones need to come back to the belt (Salvage when depleted → back to the belt does this)")
            elif mining:
                notes.append(f"partly exhausted ({st.split(':')[1] if st.count(':') >= 2 else st}) — its drones are mining "
                             "the other resources, which is fine")
            elif place and place == best:
                notes.append(f"stale 'exhausted' at {place}: the belt has {open_b[best]} open site(s) again — re-set the directive and launch")
            else:
                notes.append(f"its directive reports exhausted at {place or '?'}"
                             + ("; no belt in this system has open sites" + ("" if any(searches_at(b) for b in sb) else " — survey drones must search")
                                if sb and not best
                                else "" if best else "; its system's belts weren't read"))
        elif st.startswith("idle"):
            notes.append(f"directive {dv['name']} is idle ({st})")
        if dv["name"] and str(dv["status"] or "active") != "active":
            notes.append(f"directive status is {dv['status']}, not active — launch it")
        if kids and not mining:
            notes.append(f"runs {len(kids)} drone(s) but none are mining")
        if here_idle:
            notes.append(f"{len(here_idle)} idle unmanaged drone(s) at its location it could adopt: {', '.join(here_idle)}")
        bi = belt_info(belt_of(c.get("location")))
        if bi.get("open_sites") == 0:
            notes.append(search_note(bi["belt"]) or f"{bi['belt']} lists no open sites — mining needs a survey drone to search and keep tracking one")
        ctrl_rows.append({"code": c["device_code"], "location": c.get("location"), "status": c.get("status"),
                          "in_control_range": c.get("in_control_range"), "directive": dv, "drones": kids, "mining": mining,
                          "schedules": sched_for(c), "belt": bi, "notes": notes, "tags": c.get("tags") or []})

    drone_rows = []
    for d in [x for x in devices if is_miner(x)]:
        code, st, loc = d["device_code"], str(d.get("status") or ""), d.get("location")
        b = belt_of(loc)
        bi = belt_info(b)
        ctrl = by.get(managed.get(code)) if managed.get(code) else None
        why: list[str] = []
        fix: list[str] = []
        state = "mining" if st.startswith("mining") else ("moving" if st.startswith(("travel", "cruis", "surg", "recall")) else
                                                           "stowed" if (st.startswith("stowed") or d.get("stowed_in_device_code")
                                                                         or d.get("attached_to_device_code")) else st or "?")
        if state == "mining":
            drone_rows.append({"code": code, "location": loc, "status": st, "state": "mining", "controller": managed.get(code),
                               "belt": bi, "why": [], "fix": [], "last_log": None})
            continue
        if d.get("in_control_range") is False:
            why.append("out of comms range: it can't be commanded")
            fix.append("put an FTL relay in range, or bring a replicant into the system")
        if state == "stowed":
            why.append(f"stowed/attached on {d.get('stowed_in_device_code') or d.get('attached_to_device_code') or '?'}")
            fix.append("deploy/detach it at a belt")
        elif state == "moving":
            why.append(f"{st}: on its way somewhere")
        elif not b:
            why.append(f"{st} at {loc}, which isn't a belt")
            fix.append("send it to a belt in its system (the arrival hand-off does this when a mining controller is there)")
        if reserved(d):
            why.append("reserved: a fleet's device away from its station, spare, or tagged to: another system — the "
                       "in-system rules leave it alone")
        if b and bi.get("open_sites") == 0:
            why.append(f"{b} has no open resource sites" + (" (marked exhausted by the app)" if bi.get("marked_exhausted") else ""))
            sn = search_note(b)
            if sn:
                fix.append(f"nothing to do — {sn}")
            else:
                fix.append("open a site: survey drone `search` at the belt and leave it tracking (Re-open sites rule: "
                           + ("on" if on.get("reopen_sites") else "OFF") + ")")
        elif b and bi.get("open_sites") is None:
            why.append(f"{b} wasn't read in this snapshot (request cap)")
        if ctrl:
            dv = directive(ctrl)
            s2 = str(dv["state"] or "")
            if not dv["name"]:
                why.append(f"its controller {ctrl['device_code']} has no directive")
                fix.append(f"set a mining directive on {ctrl['device_code']} (e.g. gather_evenly) and launch — or add an AMI schedule")
            elif s2.startswith("exhausted"):
                why.append(f"its controller {ctrl['device_code']} reports {s2}")
                fix.append("open new sites at that belt, or switch the controller to gather_salvage / another resource")
            elif str(dv["status"] or "active") != "active":
                why.append(f"controller directive {dv['name']} is {dv['status']}")
                fix.append(f"launch {ctrl['device_code']}")
            if ctrl.get("location") != loc and state not in ("moving",):
                why.append(f"it's at {loc}, its controller is at {ctrl.get('location')}")
            why.append(f"run by controller {ctrl['device_code']}: the app's restart rule leaves managed drones to it")
        else:
            rr = rules.get("restart_idle_miners") or {}
            if not on.get("restart_idle_miners"):
                why.append("no controller runs it, and the 'Restart idle miners' rule is OFF")
                fix.append("turn on Restart idle miners, or let a mining controller adopt it")
            elif st != "idle":
                why.append(f"restart rule only acts on status exactly 'idle' (this is '{st}')")
            elif b is None:
                pass
            elif code in busy:
                why.append("a running app job is using it")
            elif b and (b in exhausted):
                why.append("the app marked this belt exhausted, so the restart rule hands it to the salvage rule instead "
                           + ("(salvage rule on)" if on.get("salvage_when_depleted") else "(salvage rule OFF — nothing happens)"))
            else:
                last = _ts(restarts.get(code))
                if last and _now() - last < timedelta(minutes=cooldown):
                    why.append(f"restart tried at {restarts.get(code)} — in its {cooldown}-min cooldown")
                ctrl_here = next((c for c in devices if is_mining_ctrl(c) and c.get("location") == loc), None)
                if ctrl_here and rr.get("prefer_ami", True):
                    why.append(f"restart rule hands it to {ctrl_here['device_code']} at the same belt (adopt)")
        lg = logs.get(code) or []
        last_log = (lg[0].get("message") if lg and isinstance(lg[0], dict) else None)
        drone_rows.append({"code": code, "location": loc, "status": st, "state": state, "controller": managed.get(code),
                           "belt": bi, "why": why, "fix": list(dict.fromkeys(fix)), "last_log": last_log, "tags": d.get("tags") or []})

    srv = app.get("server") or {}
    cur_run, cur_v = srv.get("run"), srv.get("version")

    def age(x: dict) -> str:  # same rules as version.age_of, but relative to the snapshot's own run
        if not x.get("run"):
            return "unversioned"
        if x.get("run") == cur_run:
            return "current"
        return "earlier run" if x.get("v") == cur_v else "older version"
    errors = [{"text": e.get("text"), "at": e.get("at"), "age": age(e), "v": e.get("v")}
              for e in app.get("log") or [] if e.get("level") == "alert" and e.get("text")][-15:]
    failed_jobs = [{"title": j.get("title"), "age": age(j), "v": j.get("v"),
                    "error": j.get("error") or next((s.get("error") for s in j.get("steps") or [] if s.get("error")), None)}
                   for j in app.get("jobs") or [] if j.get("status") in ("failed", "stalled")][-10:]
    n_idle = sum(1 for r in drone_rows if r["state"] != "mining")
    headline = engine_headline(snap)
    if drone_rows:
        headline.append(f"{len(drone_rows) - n_idle} of {len(drone_rows)} mining drones are mining")
    no_sites = sorted({r["belt"]["belt"] for r in drone_rows if r["state"] != "mining" and r["belt"].get("open_sites") == 0})
    if no_sites:
        bits = []
        for b in no_sites:
            sr = searches_at(b)
            bits.append(f"{b} (searching, new sites due {sr['first_due']})" if sr and sr.get("first_due") else b)
        headline.append(f"belts with no open sites: {', '.join(bits)}")
    no_dir = [c["code"] for c in ctrl_rows if not c["directive"]["name"] and not any(t.startswith("fleet:") for t in c["tags"])]
    if no_dir:
        headline.append(f"controllers with no directive: {', '.join(no_dir)}")
    ex = [c["code"] for c in ctrl_rows if str(c["directive"]["state"] or "").startswith("exhausted") and not c["mining"]]
    if ex:
        headline.append(f"controllers reporting exhausted: {', '.join(ex)}")
    hot = sorted(code for code, lines in logs.items() if any("overheat" in str(x.get("event") or x.get("type") or x)
                                                             for x in lines or []))
    if hot:
        headline.append(f"controllers logging ami_overheat: {', '.join(hot)}")
    gated = [f"{c['code']} ({c['directive']['state']})" for c in ctrl_rows if str(c["directive"]["state"] or "").startswith("gated")]
    if gated:
        headline.append(f"controllers gated by the game: {', '.join(gated)}")
    off = [r for r in ("restart_idle_miners", "ami_schedules", "reopen_sites") if not on.get(r)]
    if off:
        headline.append("rules off: " + ", ".join(off))
    if app.get("dry_run"):
        headline.append("DRY RUN is on — automations only log what they would do")
    return {"headline": headline, "drones": sorted(drone_rows, key=lambda r: (r["state"] == "mining", r["location"] or "", r["code"])),
            "controllers": ctrl_rows, "rules_on": on, "recent_alerts": errors, "failed_jobs": failed_jobs}
