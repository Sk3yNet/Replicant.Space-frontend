"""Station jobs (game v4.0.0, Season 4 "Resonance Accord"; docs /docs/stations/, /factions/, /influence/, /jobs/).

Twelve colony stations sit at STAR-OBJ-n locations, each with three factions (role keys crew / leader / trader, local
names per station) and a job board. You hold one accepted job at a time across all stations; tier 1 needs no
reputation (+10 on completion, -5 on abandon, 24 h), tier 2 needs 50 with the posting faction (+20 / -15, 48 h). The
first 3 jobs have no cooldown, then 2/4/8/14/24 h over a rolling 5-day window (GET /jobs `stats`). Job types: gather
resources, deploy devices, scan a body, deliver resources, courier a device handed over on acceptance. Completing needs
a replicant at the station (deliveries and couriers to another station: at either end).

What the app keeps (kv):
  stations         {"at", "stations": [...]}               GET /stations, every BOARD_MIN
  station_boards   {slug: {"at", "jobs": [...], "error", "away"}}  GET /stations/{slug}/jobs, read only where a
                                                           replicant of yours is (live 2026-11-09: 400 "No replicant at
                                                           New Havana Station"); elsewhere the last board read is kept
  station_mine     GET /jobs body + "at"                   your jobs, stats, reputation; every MINE_MIN
  station_detail   {slug: {..., "at"}}                     GET /stations/{slug} (contributors), on demand
  job_runner       settings and state, see DEFAULT_RUNNER

The job runner (Economy › Jobs): for a job you hand it (Run) or one it accepted itself (Auto accept, off by default) it
brings what the objective needs: resources by logistics to the station's pile (gather) or the destination (deliver),
devices pinned or printed to the location (deploy), the courier device pinned to its destination. Once the objective
looks met and a replicant of yours is at a place the job completes, it calls complete. Scan jobs are by hand (Scan and
return on the page). An hour before a deadline it warns once.

Objective field names other than the docs' example (type, station_location, resource, quantity) are guesses, read
widely (KEYS_*); unverified live.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

KV_STATIONS, KV_BOARDS, KV_MINE, KV_DETAIL, KV_RUNNER = "stations", "station_boards", "station_mine", "station_detail", "job_runner"
BOARD_MIN, MINE_MIN = 15, 5
ROLES = ("crew", "leader", "trader")
KINDS = {"gather_resources": "gather", "deliver_resources": "deliver", "deploy_devices": "deploy", "deploy_device": "deploy",
         "scan_body": "scan", "courier": "courier"}
RUNNABLE = ("gather", "deliver", "deploy", "courier")   # what the runner can do by itself
DEFAULT_RUNNER = {"auto": False, "stations": [], "factions": list(ROLES), "kinds": list(RUNNABLE), "tier2": True,
                  "run": [], "warned": {}, "notes": {}, "supplied": {}, "tries": {}, "accept_fail_at": None}
KEYS_DEST = ("destination_location", "destination", "target_location", "to_location", "deliver_to", "delivery_location")
KEYS_DEST_STATION = ("destination_station", "destination_station_slug", "target_station", "to_station")
KEYS_TYPE = ("device_type", "device", "type_needed")
KEYS_QTY = ("quantity", "count", "amount", "number")
KEYS_TARGET = ("target", "body", "target_body", "scan_target", "location")
RETRY_MIN = 15
WARN_MIN = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(s: Any) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def star_of(loc: str | None) -> str:
    return (loc or "").split("-")[0]


def factions(st: dict) -> dict[str, dict]:
    """{role: {name, summary, influence}}: the docs show a mapping keyed by role, the spec a list with `type`."""
    f = (st or {}).get("factions")
    if isinstance(f, dict):
        return {k: v for k, v in f.items() if isinstance(v, dict)}
    out = {}
    for x in f or []:
        if isinstance(x, dict) and x.get("type"):
            out[str(x["type"])] = x
    return out


def role(key: Any) -> str:
    """crew / leader / trader (reputation is keyed leader, some texts say leaders, traders)."""
    k = str(key or "").lower()
    return {"leaders": "leader", "traders": "trader"}.get(k, k)


def kind_of(job: dict) -> str:
    """gather / deliver / deploy / scan / courier, from the objectives' type, else from the board's summary text."""
    t = str(((job or {}).get("objectives") or {}).get("type") or "").lower()
    if t in KINDS:
        return KINDS[t]
    for k, v in KINDS.items():
        if t and (t.startswith(v) or v in t):
            return v
    s = f"{job.get('objectives_summary') or ''} {job.get('title') or ''}".lower()
    for word, k in (("transport", "courier"), ("courier", "courier"), ("carry", "courier"), ("deliver", "deliver"),
                    ("scan", "scan"), ("survey", "scan"), ("deploy", "deploy"), ("gather", "gather"), ("collect", "gather"),
                    ("mine", "gather"), ("supply", "gather")):
        if word in s:
            return k
    return "other"


def _first(d: dict, keys: tuple[str, ...]) -> Any:
    return next((d[k] for k in keys if d.get(k) not in (None, "", [], {})), None)


def station_by_slug(stations: list[dict], slug: str | None) -> dict:
    return next((s for s in stations if s.get("slug") == slug), {})


def station_loc(stations: list[dict], slug: str | None) -> str | None:
    return station_by_slug(stations, slug).get("location")


def objective(job: dict, stations: list[dict]) -> dict:
    """What the job asks, read widely: {kind, station, at (where it's done), resource, qty, device_type, device_code,
    dest (deliver / courier destination), target (scan), complete_at [locations where complete works]}."""
    o = (job or {}).get("objectives") or {}
    st_loc = o.get("station_location") or station_loc(stations, job.get("station_slug") or job.get("station"))
    k = kind_of(job)
    dest = _first(o, KEYS_DEST)
    if isinstance(dest, dict):
        dest = dest.get("location") or dest.get("designation")
    if not dest:
        ds = _first(o, KEYS_DEST_STATION)
        dest = station_loc(stations, ds) if ds else None
    prov = (job or {}).get("provided") or o.get("provided") or {}
    if isinstance(prov, list):
        prov = prov[0] if prov else {}
    dcode = prov.get("device_code") if isinstance(prov, dict) else (prov if isinstance(prov, str) else None)
    qty = _first(o, KEYS_QTY)
    try:
        qty = float(qty) if qty is not None else None
    except (TypeError, ValueError):
        qty = None
    res = o.get("resource") or o.get("resource_type")
    if not res and isinstance(o.get("resources"), dict) and o["resources"]:
        res, qty = next(iter(o["resources"].items()))
    target = None
    if k == "scan":
        target = _first(o, KEYS_TARGET)
        if target == st_loc:
            target = o.get("target") or o.get("body")
    at = (dest if k in ("deliver", "courier") and dest else
          (_first(o, ("location", "target_location", "deploy_location")) or st_loc) if k == "deploy" else st_loc)
    done_at = [x for x in ([st_loc, dest] if k in ("deliver", "courier") else [st_loc]) if x]
    return {"kind": k, "station": st_loc, "at": at, "resource": res, "qty": qty,
            "device_type": _first(o, KEYS_TYPE) or (prov.get("device_type") if isinstance(prov, dict) else None),
            "device_code": dcode, "dest": dest, "target": target, "complete_at": done_at}


def reps_at(replicants: dict, devices: list[dict], loc: str | None) -> list[str]:
    """Your replicants at that exact location: their own record, or the device hosting them."""
    if not loc:
        return []
    out = [c for c, r in (replicants or {}).items() if isinstance(r, dict) and (r.get("location") or r.get("current_location")) == loc]
    out += [h["replicant_code"] for d in devices or [] if d.get("location") == loc
            for h in [d.get("hosting_replicant") or {}] if h.get("replicant_code") and h["replicant_code"] not in out]
    return out


def pile(inventory: dict[str, dict], loc: str | None) -> dict[str, float]:
    from .shapes import as_amounts
    return as_amounts((inventory or {}).get(loc or "") or {})


def deployed_at(devices: list[dict], t: str | None, loc: str | None) -> list[dict]:
    return [d for d in devices or [] if d.get("device_type") == t and d.get("location") == loc
            and not d.get("stowed_in_device_code") and not d.get("attached_to_device_code")]


def progress(ob: dict, inventory: dict, devices: list[dict]) -> tuple[bool | None, str]:
    """(objective met: True / False, or None when the app can't tell; a short note)."""
    k = ob["kind"]
    if k in ("gather", "deliver") and ob["resource"] and ob["qty"]:
        have = pile(inventory, ob["at"]).get(ob["resource"], 0.0)
        return have >= ob["qty"], f"{int(have)} of {int(ob['qty'])} {ob['resource']} at {ob['at']}"
    if k == "deploy" and ob["device_type"]:
        n = int(ob["qty"] or 1)
        have = len(deployed_at(devices, ob["device_type"], ob["at"]))
        return have >= n, f"{have} of {n} {str(ob['device_type']).replace('_', ' ')} deployed at {ob['at']}"
    if k == "courier" and ob["device_code"] and ob["dest"]:
        d = next((x for x in devices or [] if x.get("device_code") == ob["device_code"]), None)
        where = (d or {}).get("location")
        return where == ob["dest"], f"{ob['device_code']} at {where or '?'}, bound for {ob['dest']}"
    if k == "scan":
        return None, f"scan {ob['target'] or 'the body'}, then come back to {ob['station']}"
    return None, "objective not read: complete by hand"


def accepted(mine: dict | None) -> list[dict]:
    return [j for j in (mine or {}).get("jobs") or [] if isinstance(j, dict) and str(j.get("status") or "accepted") == "accepted"]


def cooldown_until(mine: dict | None) -> datetime | None:
    t = _ts(((mine or {}).get("stats") or {}).get("next_available_at"))
    return t if t and t > _now() else None


def reputation(mine: dict | None, slug: str, r: str) -> float:
    rep = ((mine or {}).get("reputation") or {}).get(slug) or {}
    f = (rep.get("factions") or {}).get(r) or (rep.get("factions") or {}).get(r + "s") or {}
    try:
        return float(f.get("reputation") or 0)
    except (TypeError, ValueError):
        return 0.0


TIER_REP = {1: 0, 2: 50}


def board_rows(stations: list[dict], boards: dict, mine: dict | None) -> list[dict]:
    """Every posted job on every board, with its station, faction's local name, kind and whether you may take it."""
    out = []
    busy = bool(accepted(mine))
    cool = cooldown_until(mine)
    for s in stations:
        slug = s.get("slug")
        b = (boards or {}).get(slug) or {}
        fs = factions(s)
        for j in b.get("jobs") or []:
            if not isinstance(j, dict):
                continue
            r = role(j.get("faction"))
            tier = int(j.get("tier") or 1)
            need = TIER_REP.get(tier, 0)
            have = reputation(mine, slug, r)
            why = ("you hold a job already" if busy else f"cooldown until {cool:%H:%M} UTC" if cool else
                   f"needs {need} reputation (you have {int(have)})" if have < need else
                   "the game says you can't take it" if j.get("can_accept") is False else "")
            out.append({**j, "station_slug": slug, "read_at": b.get("at"), "away": bool(b.get("away")), "station_name": s.get("name") or slug, "location": s.get("location"),
                        "role": r, "faction_name": (fs.get(r) or fs.get(r + "s") or {}).get("name") or r,
                        "kind": kind_of(j), "tier": tier, "blocked": why})
    return out


def pick(rows: list[dict], cfg: dict) -> dict | None:
    """The job Auto accept takes: an open one at a chosen station (all when none chosen), from a chosen faction, of a
    kind the runner does, tier 2 only when allowed; best reputation first, then faction order, then the shortest."""
    sts = set(cfg.get("stations") or [])
    fac = [role(f) for f in cfg.get("factions") or ROLES]
    kinds = set(cfg.get("kinds") or RUNNABLE) & set(RUNNABLE)
    ok = [r for r in rows if not r["blocked"] and (not sts or r["station_slug"] in sts) and r["role"] in fac
          and r["kind"] in kinds and (cfg.get("tier2", True) or r["tier"] == 1)]
    ok.sort(key=lambda r: (-(float(r.get("reputation_reward") or 0)), fac.index(r["role"]), int(r.get("deadline_hours") or 99), r.get("id") or 0))
    return ok[0] if ok else None


def logistics_rows(mine: dict | None, cfg: dict, stations: list[dict]) -> list[dict]:
    """The resource needs the runner hands to logistics, shaped like hub upkeep rows (logistics.requests)."""
    out = []
    run = set(str(x) for x in cfg.get("run") or [])
    for j in accepted(mine):
        if str(j.get("id")) not in run:
            continue
        ob = objective(j, stations)
        if ob["kind"] in ("gather", "deliver") and ob["resource"] and ob["qty"] and ob["at"]:
            out.append({"code": f"job{j.get('id')}", "location": ob["at"], "target": {ob["resource"]: ob["qty"]},
                        "kind": "job", "urgent": False,
                        "label": f"station job {j.get('title') or j.get('id')}: {int(ob['qty'])} {ob['resource']} to {ob['at']}"})
    return out


def kind_label(k: str) -> str:
    return {"gather": "gather resources", "deliver": "deliver resources", "deploy": "deploy devices", "scan": "scan a body",
            "courier": "courier", "other": "other"}.get(k, k)


_SLUG = re.compile(r"^[a-z0-9_\-]{1,40}$")


def valid_slug(s: str) -> bool:
    return bool(_SLUG.match(s or ""))


async def refresh(api, db, boards: bool = True, background: bool = True) -> list[str]:
    """Read the stations, every board and your jobs from the game; returns errors (one line each)."""
    from .api import ApiError
    errs = []
    now = _now().isoformat(timespec="seconds")
    try:
        mine = await api.get("/jobs", background=background) or {}
        await db.kv_set(KV_MINE, {**(mine if isinstance(mine, dict) else {}), "at": now})
    except ApiError as e:
        errs.append(f"your jobs: {e.message}")
    if not boards:
        return errs
    try:
        body = await api.get("/stations", background=background) or {}
        sts = body.get("stations") if isinstance(body, dict) else body
        sts = [s for s in sts or [] if isinstance(s, dict) and s.get("slug")]
        await db.kv_set(KV_STATIONS, {"at": now, "stations": sts})
    except ApiError as e:
        errs.append(f"stations: {e.message}")
        sts = ((await db.kv_get(KV_STATIONS, {})) or {}).get("stations") or []
    old = await db.kv_get(KV_BOARDS, {}) or {}
    reps = await db.kv_get("replicants", {}) or {}
    devices = await db.kv_get("devices", []) or []
    new = {}
    for s in sts:
        slug = s["slug"]
        if not reps_at(reps, devices, s.get("location")):   # the game shows a board only to a replicant at the station
            new[slug] = {**(old.get(slug) or {}), "error": None, "away": True}
            continue
        try:
            b = await api.get(f"/stations/{slug}/jobs", background=background) or {}
            new[slug] = {"at": now, "jobs": [j for j in (b.get("jobs") if isinstance(b, dict) else b) or [] if isinstance(j, dict)]}
        except ApiError as e:
            new[slug] = {**(old.get(slug) or {}), "error": e.message}
    await db.kv_set(KV_BOARDS, new)
    return errs


async def runner_cfg(db) -> dict:
    return {**DEFAULT_RUNNER, **(await db.kv_get(KV_RUNNER, {}) or {})}


async def run_pass(eng) -> list[str]:
    """The engine stage: refresh what's due, then work the accepted job you handed the runner (or Auto accept took)."""
    from .shapes import normalize_inventory
    db, api = eng.db, eng.api
    cfg = await runner_cfg(db)
    mine0 = await db.kv_get(KV_MINE, {}) or {}
    boards_at = _ts(((await db.kv_get(KV_STATIONS, {})) or {}).get("at"))
    mine_at = _ts(mine0.get("at"))
    now = _now()
    due_boards = not boards_at or (now - boards_at).total_seconds() > BOARD_MIN * 60
    if due_boards or not mine_at or (now - mine_at).total_seconds() > MINE_MIN * 60:
        await refresh(api, db, boards=due_boards)
    mine = await db.kv_get(KV_MINE, {}) or {}
    stations = ((await db.kv_get(KV_STATIONS, {})) or {}).get("stations") or []
    out: list[str] = []
    jobs = accepted(mine)
    live = {str(j.get("id")) for j in jobs}
    changed = False
    for k in ("run",):
        keep = [x for x in cfg.get(k) or [] if str(x) in live]
        if keep != (cfg.get(k) or []):
            cfg[k], changed = keep, True
    for k in ("warned", "notes", "supplied", "tries"):
        keep = {x: v for x, v in (cfg.get(k) or {}).items() if x in live}
        if keep != (cfg.get(k) or {}):
            cfg[k], changed = keep, True
    if not jobs and cfg.get("auto") and not cooldown_until(mine):
        failed = _ts(cfg.get("accept_fail_at"))
        if not failed or (now - failed).total_seconds() > 30 * 60:
            j = pick(board_rows(stations, await db.kv_get(KV_BOARDS, {}) or {}, mine), cfg)
            if j:
                ok, body, err = await eng.send("POST", f"/jobs/{j['id']}/accept", None, f"accept station job {j.get('title')}")
                if ok:
                    cfg["run"] = [str(j["id"])]
                    cfg["accept_fail_at"] = None
                    await eng.log("station_jobs", f"accepted {j.get('title')} at {j['station_name']} ({j['faction_name']}, "
                                                  f"tier {j['tier']}): {j.get('objectives_summary') or ''}", "info", notify=True)
                    await refresh(api, db, boards=False)
                    mine = await db.kv_get(KV_MINE, {}) or {}
                    jobs = accepted(mine)
                    out.append(f"accepted job {j['id']}")
                else:
                    cfg["accept_fail_at"] = now.isoformat(timespec="seconds")
                    await eng.log("station_jobs", f"couldn't accept {j.get('title')} at {j['station_name']}: {err}", "warning")
                changed = True
    devices = await eng.devices()
    reps = await db.kv_get("replicants", {}) or {}
    inv = {i.get("location"): i.get("items") or {} for i in normalize_inventory(await db.kv_get("inventory", []))}
    for j in jobs:
        jid = str(j.get("id"))
        exp = _ts(j.get("expires_at"))
        if exp and 0 < (exp - now).total_seconds() < WARN_MIN * 60 and jid not in (cfg.get("warned") or {}):
            cfg.setdefault("warned", {})[jid] = now.isoformat(timespec="seconds")
            changed = True
            await eng.log("station_jobs", f"station job {j.get('title')} ends at {exp:%H:%M} UTC: finish or abandon it "
                                          "(abandoning costs reputation)", "warning", notify=True)
        if jid not in set(cfg.get("run") or []):
            continue
        ob = objective(j, stations)
        if ob["kind"] == "deploy" and ob["device_type"] and ob["at"] and jid not in (cfg.get("supplied") or {}):
            from .loadouts import at_tag
            n = int(ob["qty"] or 1)
            have = [d for d in devices if d.get("device_type") == ob["device_type"] and
                    (d.get("location") == ob["at"] or at_tag(ob["at"]) in (d.get("tags") or []))]
            short = n - len(have)
            note = "already there or on the way"
            if short > 0:
                pinned, printed, err = await eng.pin_or_print(ob["device_type"], short, ob["at"], devices,
                                                              eng.busy_devices(await eng.jobs()), f"station job {jid}", have)
                note = f"{len(pinned)} spare(s) sent, {printed} queued to print" + (f" ({err})" if err else "")
            cfg.setdefault("supplied", {})[jid] = note
            changed = True
            await eng.log("station_jobs", f"{j.get('title')}: {str(ob['device_type']).replace('_', ' ')} to {ob['at']}: {note}")
        if ob["kind"] == "courier" and ob["device_code"] and ob["dest"] and jid not in (cfg.get("supplied") or {}):
            from .loadouts import at_tag, to_tag
            d = next((x for x in devices if x.get("device_code") == ob["device_code"]), None)
            if d:
                ok, _, err = await eng.send("PATCH", f"/devices/{ob['device_code']}",
                                            {"configuration": {"add_tags": [to_tag(star_of(ob["dest"])), at_tag(ob["dest"])],
                                                               "remove_tags": [t for t in d.get("tags") or [] if t == "spare"]}},
                                            f"pin courier {ob['device_code']} to {ob['dest']}")
                cfg.setdefault("supplied", {})[jid] = "pinned" if ok else f"pin failed: {err}"
                changed = True
                if ok:
                    await eng.log("station_jobs", f"{j.get('title')}: {ob['device_code']} pinned to {ob['dest']}; the loadout pass takes it there")
        met, note = progress(ob, inv, devices)
        if cfg.get("notes", {}).get(jid) != note:
            cfg.setdefault("notes", {})[jid] = note
            changed = True
        if not met:
            continue
        here = [r for loc in ob["complete_at"] for r in reps_at(reps, devices, loc)]
        last = _ts((cfg.get("tries") or {}).get(jid))
        if last and (now - last).total_seconds() < RETRY_MIN * 60:
            continue
        cfg.setdefault("tries", {})[jid] = now.isoformat(timespec="seconds")
        changed = True
        if not here:
            await eng.log("station_jobs", f"{j.get('title')} is ready: send a replicant to {' or '.join(ob['complete_at'])} "
                                          "to complete it", "warning", notify=not last)
            continue
        ok, body, err = await eng.send("POST", f"/jobs/{jid}/complete", None, f"complete station job {j.get('title')}")
        if ok:
            got = (body or {}).get("rewards_summary") or j.get("rewards_summary") or ""
            await eng.log("station_jobs", f"completed {j.get('title')}" + (f": {got}" if got else ""), "info", notify=True)
            await refresh(api, db, boards=False)
            out.append(f"completed job {jid}")
        else:
            await eng.log("station_jobs", f"complete {j.get('title')}: {err}", "warning")
    if changed:
        await db.kv_set(KV_RUNNER, cfg)
    return out
