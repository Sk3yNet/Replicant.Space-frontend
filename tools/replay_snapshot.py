"""Replay one automation tick against a Diagnostics snapshot, with a stub API that answers GETs from the snapshot
and records commands instead of sending them. Approximate: snapshots lack the star catalogue, blueprints and replicants.
Usage: python tools/replay_snapshot.py <snapshot.json>"""
import asyncio, json, sys, tempfile, time, traceback
from datetime import datetime, timezone
from unittest import mock

import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
from rsweb import snapshot as snapmod
from rsweb.api import ApiError
from rsweb.db import DB
from rsweb import automations as au

SNAP = sys.argv[1]
snap = json.load(open(SNAP))
app = snap["app"]
NOW = datetime.fromisoformat(snap["captured_at"])

GET = {}
for c in snap["calls"]:
    if c.get("status") == 200:
        GET[c["path"]] = c.get("body")
SENT = []


class Api:
    configured = True

    async def request(self, method, path, params=None, json_body=None, background=False, retries=2):
        path = "/" + path.lstrip("/")
        if method == "GET":
            if path in GET:
                return GET[path]
            raise ApiError(404, f"not in snapshot: {path}")
        SENT.append((method, path, json_body))
        return {}

    async def get(self, path, background=False, **params):
        return await self.request("GET", path, params=params, background=background)

    async def post(self, path, body=None, background=False):
        return await self.request("POST", path, json_body=body, background=background)

    async def paged(self, path, key, **kw):
        b = GET.get(path) or {}
        return b.get(key) or []


class Hub:
    def publish(self, *a, **k):
        pass


async def main():
    tmp = tempfile.mktemp(suffix=".db")
    db = DB(tmp)
    await db.open()
    devices = snapmod.devices_of(snap)
    await db.kv_set("devices", devices)
    await db.kv_set("automation_settings", {"dry_run": app["dry_run"], "rules": app["rules"]})
    # jobs: the snapshot drops idx etc; rebuild minimal
    jobs = []
    for j in app["jobs"]:
        j = dict(j)
        steps = [dict(s) for s in j["steps"]]
        idx = next((i for i, s in enumerate(steps) if s.get("status") not in ("done", "skipped")), len(steps))
        j.update(steps=steps, idx=idx)
        jobs.append(j)
    await db.kv_set("automation_jobs", jobs)
    await db.kv_set("ami_schedules", app["schedules"])
    for k in ("miner_restarts", "exhausted_places", "reopen_state", "salvage_state", "belt_reads", "loadouts",
              "loadout_orders", "stowed_map", "loadouts_last"):
        await db.kv_set(k, app[k])
    await db.kv_set("automation_log", app["log"])
    inv = GET.get("/inventory") or {}
    await db.kv_set("inventory", inv.get("inventory") or inv.get("items") or [])
    # fleets as they were (snapshots before 1.18.0 carry only id/role/mission; their loadouts config is converted on load)
    await db.kv_set("fleets", [{"id": f["id"], "name": f.get("name") or f["id"], "role": f["role"], "home": f.get("home") or "",
                                "station": bool(f.get("station")), "template": f.get("template"), "wants": f.get("wants") or {},
                                "materials": f.get("materials") or "",
                                "mission": {"status": f["mission"]} if f.get("mission") in ("running", "stalled", "ended", "stopped")
                                else None}
                               for f in app["fleets"]])
    eng = au.AutomationEngine(db, Api(), Hub(), None)
    with mock.patch.object(au, "_now", lambda: NOW):
        order = ["run_fleets", "dispatch_new_prints", "rule_contracts", "refresh_known_belts", "track_viability",
                 "rule_consolidate", "rule_reopen_sites", "rule_salvage", "rule_restart_idle_miners",
                 "run_due_schedules", "run_due_loadouts", "civ_beacon_pass", "maintenance_pass"]
        for name in order:
            t = time.time()
            n = len(SENT)
            try:
                r = await asyncio.wait_for(getattr(eng, name)(), 30)
                print(f"OK   {name} {time.time()-t:.1f}s sent={len(SENT)-n} -> {str(r)[:300]}")
            except Exception as e:
                print(f"FAIL {name}: {type(e).__name__}: {e}")
                traceback.print_exc(limit=-4)
    print("\n--- new log ---")
    for e in (await db.kv_get("automation_log", []))[len(app["log"]):]:
        print(e["level"], e["text"][:300])
    print("\n--- loadouts_last ---")
    print(json.dumps(await db.kv_get("loadouts_last", {}), indent=1)[:3000])
    print("\n--- sent ---")
    for s in SENT[:80]:
        print(s[0], s[1], json.dumps(s[2])[:150])
    await db.close()


asyncio.run(main())
