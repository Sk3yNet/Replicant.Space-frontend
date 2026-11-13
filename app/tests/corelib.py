"""Helpers, fixtures and worlds shared by the test_core*.py files (they were the top of test_core.py)."""
import asyncio
from collections import Counter
from datetime import datetime, timedelta, timezone

import httpx
import json
import pytest
from fastapi.testclient import TestClient

from rsweb import notify
from rsweb.api import ApiError, Bucket, RSClient, parse_sse
from rsweb.config import Settings
from rsweb.db import DB
from rsweb.hub import Hub
from rsweb.ingest import Worker
from rsweb.main import create_app
from rsweb.mock import create_mock


def iso(dt):
    return dt.isoformat(timespec="seconds")


async def _lines(text):
    for line in text.split("\n"):
        yield line


def run(coro):
    return asyncio.run(coro)


def _ev(i, event, device="AAAA0001", created=None, **payload):
    return {"id": f"{1000 + i}-0", "event": event, "category": event.split(".")[0], "device_code": device,
            "device_type": "mining_drone", "location": "SOL-BELT-1", "payload": payload,
            "created_at": iso(created or datetime.now(timezone.utc))}


# --- HTTP level, against the mock game API ------------------------------------------------------
@pytest.fixture()
def client(tmp_path):
    mock = create_mock()
    s = Settings(api_token="dev", api_base="http://mock/v1", db_path=str(tmp_path / "app.sqlite"),
                 allowed_emails=["joe@example.com"], disable_background=True, virtual_queue=False)
    app = create_app(s, transport=httpx.ASGITransport(app=mock))
    with TestClient(app) as c:
        w = app.state.worker

        async def sync():
            await w.sync_account(); await w.sync_devices(); await w.sync_inventory(); await w.sync_blueprints(); await w.sync_catalogue()
        c.portal.call(sync)
        yield c


H = {"X-Auth-Request-Email": "joe@example.com"}
HX = {**H, "HX-Request": "true"}


class _Form(dict):
    def getlist(self, k):
        v = self.get(k)
        return v if isinstance(v, list) else ([v] if v else [])


def _pump(client, until, timeout=10.0):
    """Feed new mock-world events to the worker (the event stream isn't running in tests)."""
    import asyncio as _a
    world = client.app.state.api.http._transport.app.state.world
    w = client.app.state.worker

    async def go():
        seen = getattr(client, "_seen", len(world.events))
        loop = _a.get_running_loop()
        end = loop.time() + timeout
        while loop.time() < end:
            new = world.events[seen:]
            seen = len(world.events)
            for ev in new:
                await w.handle_event(dict(ev))
            if await until():
                break
            await _a.sleep(0.02)
        client._seen = seen
    client.portal.call(go)


def _arrive(client, n):
    client.portal.call(client.app.state.worker.handle_event, {
        "id": f"66666666666{n:02d}-0", "event": "travel.arrived", "device_code": "11ADA230", "device_type": "heaven_vessel",
        "location": "SOL-BELT-1", "payload": {"destination": "SOL-BELT-1", "origin": "ABOTEIN-OORT", "travel_type": "surge"}, "created_at": iso(datetime.now(timezone.utc))})


def _rep(p, star):
    """The loadout report of the fleet stationed in `star` (reports are per fleet)."""
    return next(r for r in p["report"].values() if r["star"] == star)


def _lo_world():
    def dev(code, t, loc, status="idle", **kw):
        return {"device_code": code, "device_type": t, "location": loc, "status": status,
                "features": kw.pop("features", ["cruise", "stow"]), "available_commands": kw.pop("cmds", ["travel", "stow", "deploy"]),
                "operational_capacity": 100.0, **kw}
    devices = [
        dev("A1", "mining_drone", "AAA-BELT-1"), dev("A2", "mining_drone", "AAA-BELT-1", "mining (carbon)"),
        dev("A3", "mining_drone", "AAA-BELT-1"), dev("A4", "mining_drone", "AAA-BELT-1", "mining (rares)"),
        dev("AC", "ami_mining_controller", "AAA-BELT-1", "coordinating"),
        dev("CAR", "surge_carrier", "AAA-OORT", features=["surge", "cruise"], cmds=["travel", "deploy"], stow_capacity=9),
        dev("AF", "autofactory", "AAA-3-L4", cmds=["enqueue_print", "dequeue_print", "clear_queue"], features=["print"]),
        dev("BS", "survey_drone", "BBB-2"),
        dev("CC", "ami_mining_controller", "CCC-BELT-1", tags=["spare"]),
        dev("CK", "mining_drone", "CCC-BELT-1", tags=["spare", "keep"]),
        dev("HV", "heaven_vessel", "AAA-BELT-1", features=["surge", "cruise", "print"], stow_capacity=10),
    ]
    cfg = {"phases": [{"id": "outpost", "name": "Outpost", "order": 1,
                       "wants": {"mining_drone": 2, "ami_mining_controller": 1, "survey_drone": 2}}],
           "systems": {"AAA": "outpost", "BBB": "outpost"}, "ignore_tags": ["keep"]}
    bps = [{"device_type": "survey_drone", "resources": {"structural": 100}, "print_time": 240},
           {"device_type": "mining_drone", "resources": {"structural": 100}, "print_time": 180},
           {"device_type": "ami_mining_controller", "resources": {"rares": 500}, "print_time": 600}]
    inv = {"AAA-3-L4": {"structural": 250.0}}
    stars = {"AAA": {"designation": "AAA", "position": {"x": 0, "y": 0, "z": 0}, "entry_point": "AAA-OORT"},
             "BBB": {"designation": "BBB", "position": {"x": 3, "y": 0, "z": 0}, "entry_point": "BBB-5-L4"},
             "CCC": {"designation": "CCC", "position": {"x": 9, "y": 0, "z": 0}}}
    return cfg, devices, bps, inv, stars


def _salvage_setup(client, drop_controller=False):
    world = client.app.state.api.http._transport.app.state.world
    if drop_controller:
        world.devices = [d for d in world.devices if "controller" not in d["device_type"]]
        client.portal.call(client.app.state.db.kv_set, "devices", [])   # (a device just missing from the list is kept as unlisted)
    client.portal.call(client.app.state.worker.sync_devices)
    eng = client.app.state.worker.automations

    async def enable():
        s = await eng.settings()
        s["rules"]["salvage_when_depleted"]["enabled"] = True
        s["rules"]["restart_idle_miners"]["enabled"] = True
        await eng.save_settings(s)
    client.portal.call(enable)
    client.get("/systems/SOL", headers=H)
    client.post("/systems/SOL/resources/refresh", headers=HX)   # site + salvage quantities
    for ev in [e for e in world.events if e["event"] == "salvage.discovered"]:
        client.portal.call(client.app.state.worker.handle_event, dict(ev))
    return world, eng


def _real_devices():
    """A trimmed copy of the player's real GET /devices output (2026-10-01)."""
    return [
        {"device_code": "84EE1EF1", "device_type": "ami_mining_controller", "location": "AEMEROTH-BELT-1", "status": "coordinating",
         "features": ["ami", "cruise", "stow"], "available_commands": ["adopt", "release", "set_directive", "launch"],
         "ami_directive": {"_eval_state": "exhausted:['carbon', 'rares']:AEMEROTH-4-2", "config": {}, "name": "gather_evenly"},
         "ami_directive_status": "active", "controller_device_code": None, "tags": ["home:aemeroth"], "in_control_range": True},
        {"device_code": "512FE0F9", "device_type": "mining_drone", "location": "AEMEROTH-5-L4", "status": "idle",
         "controller_device_code": "84EE1EF1", "tags": ["home:falquoryx"], "available_commands": ["travel", "stow"]},
        {"device_code": "DF451241", "device_type": "ami_transport_controller", "location": "FALQUORYX-BELT-1", "status": "coordinating",
         "features": ["ami"], "available_commands": ["adopt", "release", "set_directive", "launch"],
         "ami_directive": {"_eval_state": "idle:no_sources", "config": {"deliver": "FALQUORYX-BELT-1"}, "name": "consolidate"},
         "ami_directive_status": "active", "controller_device_code": None, "tags": ["home:falquoryx"]},
        {"device_code": "374C62A7", "device_type": "transport_drone", "location": "FALQUORYX-1-L4", "status": "idle",
         "controller_device_code": "DF451241", "tags": ["home:falquoryx", "spare"]},
        {"device_code": "57C506F0", "device_type": "cargo_freighter", "location": "FALQUORYX-BELT-1", "status": "idle",
         "features": ["surge", "cruise", "transport"], "cargo_capacity": 500, "cargo_used": 500,
         "controller_device_code": "DF451241", "tags": []},
        {"device_code": "F32E05A7", "device_type": "ami_mining_controller", "location": "FALQUORYX-BELT-1", "status": "coordinating",
         "features": ["ami"], "available_commands": ["adopt", "release", "set_directive", "launch"], "tags": ["home:falquoryx"],
         "ami_directive": {"_eval_state": "exhausted:['rares']:FALQUORYX-BELT-1", "config": {}, "name": "gather_evenly"},
         "ami_directive_status": "active"},
        {"device_code": "926637CA", "device_type": "mining_drone", "location": "ITHVALAI-2-L4", "status": "idle",
         "controller_device_code": "F32E05A7", "tags": ["home:falquoryx", "home:ithvalai"]},
    ]


def _enable(client, *rules):
    eng = client.app.state.worker.automations

    async def go():
        s = await eng.settings()
        for r in rules:
            s["rules"][r]["enabled"] = True
        await eng.save_settings(s)
    client.portal.call(go)
    return eng


def _fleet_world():
    T = ["fleet:prospector-1"]
    def dev(code, t, loc, **kw):
        return {"device_code": code, "device_type": t, "location": loc, "status": kw.pop("status", "idle"),
                "tags": kw.pop("tags", T), "available_commands": kw.pop("cmds", ["travel"]), **kw}
    devices = [
        dev("MF000001", "mobile_fleet", "AEM-5-L4", features=["surge", "cruise", "attach"], attach_capacity=36, attached_devices=[]),
        dev("MC000001", "ami_mining_controller", "AEM-5-L4"),
        dev("SC000001", "ami_survey_controller", "AEM-BELT-1"),
        dev("TC000001", "ami_transport_controller", "AEM-5-L4"),
        dev("MD000001", "mining_drone", "AEM-5-L4"), dev("MD000002", "mining_drone", "AEM-5-L4", attached_to_device_code="MF000001"),
        dev("SD000001", "survey_drone", "AEM-BELT-1"),
        dev("CF000001", "cargo_freighter", "AEM-5-L4", features=["surge", "cruise", "transport"], cargo_capacity=500, cargo_used=0),
        dev("LOST0001", "mining_drone", "FAL-BELT-1"),                       # a member stranded elsewhere
        dev("LOCAL001", "mining_drone", "AEM-5-L4", tags=["home:aem"]),      # not in the fleet
    ]
    fleet = {"id": "prospector-1", "name": "Prospector-1", "role": "mining", "home": "AEM",
             "wants": {"mobile_fleet": 1, "mining_drone": 4, "ami_mining_controller": 1}}
    return fleet, devices


def _bound_controller_world():
    return [
        {"device_code": "PL000001", "device_type": "surge_platform", "location": "FAL-1-L4", "status": "idle", "attach_capacity": 4,
         "features": ["surge", "cruise", "attach"], "available_commands": ["attach", "detach", "travel"], "tags": ["home:fal"]},
        # printed in FAL for ITHVALAI, but an "all survey controllers" schedule put it to work there
        {"device_code": "SC000001", "device_type": "ami_survey_controller", "location": "FAL-BELT-1", "status": "coordinating",
         "features": ["cruise", "ami"], "available_commands": ["adopt", "release", "set_directive", "clear_directive", "launch", "travel"],
         "ami_directive": {"name": "belt_search", "config": {}, "_eval_state": "searching:1:0"}, "ami_directive_status": "active",
         "tags": ["home:ithvalai", "to:ithvalai"]},
        {"device_code": "SD000001", "device_type": "survey_drone", "location": "FAL-BELT-1", "status": "idle",
         "controller_device_code": "SC000001", "tags": ["home:fal"]},
        {"device_code": "SD000002", "device_type": "survey_drone", "location": "FAL-BELT-1", "status": "idle", "tags": ["to:ithvalai"]},
    ]

def _arrival_world():
    A = ["adopt", "release", "set_directive", "launch", "activate"]
    return [
        {"device_code": "MC1", "device_type": "ami_mining_controller", "location": "ITH-BELT-1", "status": "coordinating",
         "features": ["ami"], "available_commands": A, "ami_directive": {"name": "gather_evenly"}, "tags": ["home:ith"]},
        {"device_code": "SC1", "device_type": "ami_survey_controller", "location": "ITH-3", "status": "idle",
         "features": ["ami"], "available_commands": A, "tags": ["home:ith"]},
        {"device_code": "TC1", "device_type": "ami_transport_controller", "location": "ITH-2-L4", "status": "idle",
         "features": ["ami"], "available_commands": A, "tags": ["home:ith", "ferry"]},          # ferry: never takes drones
        # delivered to the entry point: not where the controllers work
        {"device_code": "MD1", "device_type": "mining_drone", "location": "ITH-2-L4", "status": "idle", "tags": ["home:ith"]},
        {"device_code": "SD1", "device_type": "survey_drone", "location": "ITH-2-L4", "status": "idle", "tags": ["home:ith"]},
        {"device_code": "TD1", "device_type": "transport_drone", "location": "ITH-2-L4", "status": "idle", "tags": ["home:ith"]},
        {"device_code": "MD2", "device_type": "mining_drone", "location": "ITH-BELT-1", "status": "mining (carbon)",
         "controller_device_code": "MC1", "tags": ["home:ith"]},                                  # already run
        {"device_code": "MD3", "device_type": "mining_drone", "location": "ITH-2-L4", "status": "idle", "tags": ["home:fal"]},  # passing through
        {"device_code": "MD4", "device_type": "mining_drone", "location": "ITH-2-L4", "status": "idle", "tags": ["to:fal"]},    # leaving
        {"device_code": "MD5", "device_type": "mining_drone", "location": "ITH-2-L4", "status": "idle", "tags": ["spare"]},     # spare
        {"device_code": "MT1", "device_type": "maintenance_drone", "location": "ITH-2-L4", "status": "inactive",
         "available_commands": ["activate", "travel"], "tags": ["home:ith"]},
        {"device_code": "MT2", "device_type": "maintenance_drone", "location": "FAL-2-L4", "status": "inactive",
         "available_commands": ["activate", "travel"], "tags": ["home:ith"]},                     # not home yet
    ]


def _live():
    import pathlib
    return json.loads((pathlib.Path(__file__).parent / "fixtures" / "live_2026-10-02.json").read_text())


def _live_b():
    import pathlib
    return json.loads((pathlib.Path(__file__).parent / "fixtures" / "live_2026-10-02b.json").read_text())


def _belt(*sites):
    return {"location_type": "belt", "resource_sites": [
        {"designation": f"FAL-BELT-1-SITE-{i}", "site_index": i, "resources_remaining_pct": {"carbon": pc}} for i, pc in sites]}


def _live_c_inventory():
    import pathlib
    s = json.loads((pathlib.Path(__file__).parent / "fixtures" / "live_2026-10-02c.json").read_text())
    return s


# --- stationed fleets (1.18.0) ------------------------------------------------------------------------------
def _st(fid, home, wants, **kw):
    return {"id": fid, "name": fid.title(), "role": "mining", "home": home, "station": True, "wants": wants, **kw}


# --- stellar census (1.19.0) -------------------------------------------------------------------------------------
CENSUS = {"page": 1, "per_page": 20, "total_pages": 1, "replicant_position": {"x": -458.9, "y": -223.75, "z": 3.3},
          "stars": [{"designation": "OTHILETH", "distance_from_replicant": 0.0, "entry_point": "OTHILETH-1-L4", "explored": True,
                     "estimated_travel_time": 0, "position": {"x": -458.9, "y": -223.75, "z": 3.3}, "has_ward": True},
                    {"designation": "ZALDANAL", "distance_from_replicant": 5.83, "entry_point": None, "explored": False,
                     "estimated_travel_time": 291, "estimated_planets": 3, "position": {"x": -463.461, "y": -220.8789, "z": 1.1055}},
                    {"designation": "LORQELYR", "distance_from_replicant": 14.55, "entry_point": None, "explored": False,
                     "estimated_travel_time": 727, "position": {"x": -466.2084, "y": -212.8483, "z": 9.5955}}]}


# --- devices in transit on the maps (1.20.0) ---------------------------------------------------------------------
def _moving(code, origin, dest, legs, start, end, dtype="survey_drone"):
    return {"device_code": code, "device_type": dtype, "status": "recalling", "location": None,
            "travel": {"origin": origin, "destination": dest, "final_destination": dest,
                       "departed_at": start.isoformat(), "arrives_at": end.isoformat(), "final_arrives_at": end.isoformat(),
                       "route": [{"from": f, "to": t, "type": ty, "time_seconds": s} for f, t, ty, s in legs]}}


def _survey_world():
    devices = [
        {"device_code": "HV", "device_type": "heaven_vessel", "location": "LOR-1-L4", "status": "idle", "features": ["surge"],
         "stow_capacity": 10, "tags": ["fleet:s"]},
        {"device_code": "SC", "device_type": "ami_survey_controller", "location": None, "stowed_in_device_code": "HV",
         "status": "stowed", "tags": ["fleet:s"]},
        {"device_code": "R1", "device_type": "ftl_relay", "location": None, "stowed_in_device_code": "HV", "status": "stowed", "tags": []},
        {"device_code": "B1", "device_type": "ftl_beacon", "location": None, "stowed_in_device_code": "HV", "status": "stowed", "tags": []},
        {"device_code": "R0", "device_type": "ftl_relay", "location": "FAL-1-L4", "status": "relaying", "tags": []},
        {"device_code": "B0", "device_type": "ftl_beacon", "location": "FAL-5", "status": "monitoring", "tags": []},
    ]
    fleet = {"id": "s", "name": "Surveyors", "role": "explore", "home": "FAL", "wants": {}}
    return fleet, devices


def _ward(code, loc, status="warding"):
    return {"device_code": code, "device_type": "system_ward", "location": loc, "status": status, "features": ["ward"],
            "operational_capacity": 100.0, "available_commands": ["deactivate" if status == "warding" else "activate", "travel"]}


def fleet_member_code(devices):
    return next(d["device_code"] for d in devices if any(t.startswith("fleet:prospector") for t in d.get("tags") or []))


__all__ = [n for n in dir() if not n.startswith("__")]   # every helper, underscore ones too
