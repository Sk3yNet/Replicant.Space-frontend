"""A small fake Replicant Space API for local development and tests.

Run:  uvicorn rsweb.mock:app --port 9000
(RS_MOCK_DEMO=1 serves the demo galaxy in demo_galaxy.json instead of the small Sol one: tools/make_demo_galaxy.py)
then point the client at it:  RS_API_BASE=http://localhost:9000/v1 RS_API_TOKEN=dev
It emits a plausible event every few seconds on /v1/events/stream.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse


def iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).isoformat(timespec="seconds")


REP = "77F75255"
HOST = "11ADA230"

STARS = [
    {"designation": "SOL", "name": "Sol", "spectral_type": "G2", "color": "Yellow", "position": {"x": 0, "y": 0, "z": 0},
     "estimated_planets": 8, "entry_point": "SOL-5-L4", "region": "solzone"},   # no has_hub: a hub there with no device of ours would count as warding SOL
] + [
    {"designation": n, "spectral_type": st, "color": c, "position": {"x": x, "y": y, "z": z}, "estimated_planets": p,
     "entry_point": f"{n}-3-L4", "region": "solzone"}
    for n, st, c, x, y, z, p in [
        ("ABOTEIN", "K3", "Orange", 4.2, -2.1, 1.0, 6), ("TARAZEDAR", "M5", "Red", -6.3, 3.3, -2.2, 4),
        ("MENKENTAR", "G4", "Yellow", -4.66, -0.13, 4.34, 9), ("CHAMAKUY", "M5", "Red", 9.1, -8.9, -3.9, 5),
        ("REGULUZ", "A1", "White", 12.4, 6.0, 1.2, 7), ("POLIBUS", "F6", "White", -11.0, -9.4, 5.5, 6),
        ("IMPOLLA", "K9", "Orange", 15.3, -2.2, -7.7, 3), ("LERNA", "B8", "Blue", -18.2, 12.1, 3.0, 10),
    ]
] + [
    {"designation": f"STAR{i:03d}", "spectral_type": "M2", "color": random.choice(["Red", "Orange", "Yellow", "White"]),
     "position": {"x": random.uniform(-60, 60), "y": random.uniform(-60, 60), "z": random.uniform(-15, 15)},
     "estimated_planets": random.randint(0, 9), "entry_point": f"STAR{i:03d}-2-L4", "region": "outer"}
    for i in range(160)
]

SOL_SCAN = {
    "asteroid_belt": {"present": True, "belts": [{"designation": "SOL-BELT-1", "density": "moderate", "inner_radius_au": 2.2,
        "outer_radius_au": 3.3, "resources": {"carbon": "moderate", "conductive": "high", "rares": "low", "silicates": "moderate",
        "structural": "rich", "volatiles": "low"}}]},
    "entry_point": "SOL-5-L4",
    "outer_system": {"kuiper": {"designation": "SOL-KUIPER", "distance_au": 40.0}, "oort": {"designation": "SOL-OORT", "distance_au": 2000.0}},
    "planets": [
        {"designation": "SOL-1", "type": "Barren", "orbital_distance_au": 0.39, "moon_count": 0, "in_habitable_zone": False},
        {"designation": "SOL-2", "type": "Terrestrial", "orbital_distance_au": 0.72, "moon_count": 0, "in_habitable_zone": False},
        {"designation": "SOL-3", "type": "Ocean World", "orbital_distance_au": 1.0, "moon_count": 1, "in_habitable_zone": True},
        {"designation": "SOL-4", "type": "Barren", "orbital_distance_au": 1.52, "moon_count": 2, "in_habitable_zone": False},
        {"designation": "SOL-5", "type": "Gas Giant", "orbital_distance_au": 5.2, "moon_count": 79, "in_habitable_zone": False},
        {"designation": "SOL-6", "type": "Gas Giant", "orbital_distance_au": 9.5, "moon_count": 83, "in_habitable_zone": False},
        {"designation": "SOL-7", "type": "Ice Giant", "orbital_distance_au": 19.2, "moon_count": 27, "in_habitable_zone": False},
        {"designation": "SOL-8", "type": "Ice Giant", "orbital_distance_au": 30.1, "moon_count": 14, "in_habitable_zone": False},
    ],
    "replicants": {"bob-1": {"replicant_code": REP, "location": "SOL-BELT-1", "last_active": iso()}},
    "star": {"designation": "SOL", "color": "Yellow", "spectral_type": "G2", "mass_solar": 1.0, "luminosity_solar": 1.0,
             "temperature_k": 5772, "age_my": 4600, "habitable_zone": {"inner_au": 0.95, "outer_au": 1.37},
             "position": {"x": 0, "y": 0, "z": 0}},
    "system_tags": [],
}

BLUEPRINTS = [
    {"device_type": "mining_drone", "short_description": "Mines one resource at a belt site.", "features": ["cruise", "mine", "stow"],
     "print_time": 180, "resources": {"carbon": 25, "conductive": 50, "silicates": 25, "structural": 100}},
    {"device_type": "survey_drone", "short_description": "Scans bodies and searches belts.", "features": ["cruise", "survey", "stow"],
     "print_time": 240, "resources": {"conductive": 60, "silicates": 40, "structural": 80, "rares": 5}},
    {"device_type": "transport_drone", "short_description": "Moves 20 units of cargo.", "features": ["cruise", "transport"],
     "print_time": 200, "resources": {"structural": 120, "conductive": 30}},
    {"device_type": "ami_mining_controller", "short_description": "Coordinates mining drones.", "features": ["cruise", "ami", "stow"],
     "directives": ["gather_resources", "gather_evenly", "maintain_ratios"], "print_time": 600,
     "resources": {"conductive": 200, "rares": 40, "silicates": 100, "structural": 150}},
    {"device_type": "ftl_beacon", "short_description": "Monitors traffic in a system.", "features": ["monitor"], "print_time": 100,
     "resources": {"structural": 60, "conductive": 20}},
    {"device_type": "cargo_vessel", "short_description": "Hauls devices: 50 in the hold, 3 attached.", "features": ["surge", "cruise", "attach"],
     "stow_capacity": 50, "attach_capacity": 3, "cargo_capacity": 200, "print_time": 2400,
     "resources": {"structural": 900, "conductive": 250, "silicates": 200}},
    {"device_type": "propulsor_plate", "short_description": "Pushes an asteroid off course.", "features": ["cruise", "divert", "stow"],
     "print_time": 300, "resources": {"structural": 150, "conductive": 60}},
    {"device_type": "autofactory", "short_description": "Queued printing.", "features": ["print", "modular"], "print_time": 1800,
     "resources": {"structural": 800, "conductive": 300, "silicates": 200, "rares": 30}},
]


class World:
    def __init__(self) -> None:
        self.seq = int(time.time() * 1000)
        self.events: list[dict] = []
        self.inventory = {"SOL-BELT-1": {"structural": 1200.0, "conductive": 340.0, "silicates": 260.0, "carbon": 180.0},
                          "SOL-3-L4": {"structural": 400.0, "rares": 12.0}}
        self.devices = [
            {"device_code": HOST, "device_type": "heaven_vessel", "location": "SOL-BELT-1", "status": "stationary",
             "features": ["surge", "cruise", "system_scan", "mine", "cradle", "print", "census"], "operational_capacity": 98.0,
             "available_commands": ["travel", "deactivate", "stellar_census"], "stow_capacity": 10},
        ]
        for i, (res, st) in enumerate([("structural", "mining (structural)"), ("conductive", "mining (conductive)"),
                                       ("silicates", "idle"), ("carbon", "mining (carbon)")]):
            self.devices.append({"device_code": f"2AC6121{i}", "device_type": "mining_drone", "location": "SOL-BELT-1",
                                 "status": st, "features": ["cruise", "mine", "stow"], "operational_capacity": 92.0 - i * 15,
                                 "available_commands": ["change_owner", "deactivate", "decommission", "deploy", "recall", "retarget", "start_mining", "stow", "travel"]})
        self.devices += [
            {"device_code": "D8C2A140", "device_type": "survey_drone", "location": "SOL-3", "status": "scanning",
             "features": ["cruise", "survey", "stow"], "operational_capacity": 88.0, "available_commands": ["scan", "search", "travel", "recall", "stow"]},
            {"device_code": "MC91FF22", "device_type": "ami_mining_controller", "location": "SOL-BELT-1", "status": "coordinating",
             "features": ["cruise", "ami", "stow"], "operational_capacity": 100.0,
             "available_commands": ["adopt", "release", "set_directive", "clear_directive", "launch", "withdraw"]},
            {"device_code": "AF00BEEF", "device_type": "autofactory", "location": "SOL-3-L4", "status": "idle",
             "features": ["print", "modular"], "operational_capacity": 100.0,
             "available_commands": ["enqueue_print", "dequeue_print", "clear_queue", "compact", "decommission"]},
            {"device_code": "BCN00001", "device_type": "ftl_relay", "location": "SOL-5-L4", "status": "relaying",
             "features": ["relay"], "operational_capacity": 100.0, "available_commands": ["activate"]},
            # carried in the heaven vessel
            {"device_code": "SV000001", "device_type": "survey_drone", "location": "SOL-BELT-1", "status": "stowed",
             "features": ["cruise", "survey", "stow"], "operational_capacity": 100.0,
             "available_commands": ["deploy", "travel", "scan", "search", "stow", "recall"]},
            {"device_code": "TC000001", "device_type": "ami_transport_controller", "location": "SOL-BELT-1", "status": "idle",
             "features": ["cruise", "ami", "stow"], "operational_capacity": 100.0,
             "available_commands": ["adopt", "release", "set_directive", "clear_directive", "launch", "withdraw"]},
            {"device_code": "TR000001", "device_type": "transport_drone", "location": "SOL-BELT-1", "status": "idle",
             "features": ["cruise", "transport", "stow"], "operational_capacity": 100.0, "cargo_capacity": 20,
             "cargo": {"carbon": 5}, "available_commands": ["collect_resources", "deposit_resources", "travel", "stow"]},
            {"device_code": "SP000001", "device_type": "surge_plate", "location": "SOL-3", "status": "idle",
             "features": ["stow"], "operational_capacity": 100.0, "available_commands": ["stow", "deploy"]},
            {"device_code": "FB000001", "device_type": "ftl_beacon", "location": "SOL-BELT-1", "status": "stowed",
             "features": ["monitor", "stow"], "operational_capacity": 100.0, "available_commands": ["deploy"]},
        ]
        self.teleports: list[tuple[str, str]] = []
        self.move_seconds = 2.0
        self.channels = ["#general", "#trade"]
        for d in self.devices:
            d["replicant_code"] = REP
        self.location = "SOL-BELT-1"
        self.vessel_busy_until = 0.0
        self.foreign_devices: list[dict] = []
        self.queues: dict[str, list] = {}
        self.af_print_seconds = 45.0
        self.audit: dict[str, list[dict]] = {}       # beacon -> audit rows (newest last)
        self.trades: dict[str, list[dict]] = {}      # trade controller -> trades
        self.feedback: list[dict] = []
        self.trips: dict[str, Any] = {}              # device -> pending arrival (asyncio handle), for canceling
        self.objects: dict[str, dict] = {}           # STAR-OBJ-n -> object
        self.profiles: dict[str, dict] = {}          # other replicants' public profiles
        self.profile_patches: list[dict] = []         # PATCH /replicants/{code} bodies
        self.evict_next: list = []                    # what the next ward activate reports as evicted_miners
        self.xp = 87340
        self.replicants: list[dict] = []             # the demo galaxy's replicants (load_demo)

    def af_next(self, af: dict) -> None:
        """Autofactory: if idle, start the head of its queue (the printing item leaves the queue)."""
        q = self.queues.get(af["device_code"]) or []
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # seeding history at import time: leave it queued
        if af["status"] != "idle" or not q:
            return
        item = q.pop(0)
        secs = self.af_print_seconds
        af["status"] = f"printing ({item['device_type']})"
        self.emit("print.started", af, device_type=item["device_type"], print_mode="standard",
                  completes_at=iso(datetime.now(timezone.utc) + timedelta(seconds=secs)), tags=[])

        def done():
            af["status"] = "idle"
            self.emit("print.completed", af, device_type=item["device_type"], print_mode="standard",
                      new_device_code=f"{random.randint(0, 0xFFFFFFFF):08X}", tags=[])
            self.af_next(af)
        loop.call_later(secs, done)

    def emit(self, event: str, device: dict | None = None, **payload) -> dict:
        self.seq += 1
        ev = {"id": f"{self.seq}-0", "version": 1, "category": event.split(".")[0], "event": event,
              "replicant_code": REP, "device_code": device["device_code"] if device else None,
              "device_type": device["device_type"] if device else None,
              "star": ((device or {}).get("location") or "SOL").split("-")[0], "location": (device or {}).get("location"),
              "payload": payload, "created_at": iso()}
        self.events.append(ev)
        self.events = self.events[-10000:]
        return ev

    def random_event(self) -> dict:
        if self.replicants:   # demo galaxy: the events below assume the Sol world's devices and stockpile
            if random.random() < 0.5:
                return self.emit("experience.gained", None, source="mining", amount=random.choice([5, 10, 25]))
            return self.emit("bobnet.new", None, id=self.seq, replicant_name="Sylphrena", replicant_code="30B93F2F",
                             current_star="SOL", channel="#general", message=random.choice(["anyone out past the rim?", "o7"]))
        d = random.choice(self.devices[1:5])
        roll = random.random()
        if roll < 0.35:
            res = random.choice(["structural", "conductive", "silicates", "carbon"])
            q = random.randint(5, 40)
            self.inventory["SOL-BELT-1"][res] = self.inventory["SOL-BELT-1"].get(res, 0) + q
            return self.emit("mining.stopped", d, location=d["location"], resource_type=res, quantity_mined=q)
        if roll < 0.5:
            return self.emit("mining.started", d, location=d["location"], site="SOL-BELT-1-SITE-2",
                             resource_type="structural", availability="high", density="moderate", cycle_time_seconds=52)
        if roll < 0.62:
            return self.emit("experience.gained", None, source="mining", amount=random.choice([5, 10, 25]))
        if roll < 0.72:
            ctrl = self.devices[6]
            return self.emit("ami.mining.digest", ctrl, directive="gather_evenly", report={"stockpile": dict(self.inventory["SOL-BELT-1"])},
                             activity={"event_count": 12, "counts": {"mining.started": 4, "mining.stopped": 8}, "window": [iso(), iso()]},
                             devices=[{"device_code": x["device_code"], "status": x["status"], "events": 3, "last_event": "mining.stopped"}
                                      for x in self.devices[1:5]])
        if roll < 0.8:
            sd = self.devices[5]
            return self.emit("scan.completed", sd, scan_target=random.choice(["SOL-3", "SOL-4", "SOL-3-1"]), scan_type="body", report={})
        if roll < 0.86:
            return self.emit("bobnet.new", None, id=self.seq, replicant_name="Sylphrena", replicant_code="30B93F2F",
                             current_star="SOL", channel="#general", message=random.choice(["anyone near LERNA?", "hub up at SOL", "o7"]))
        if roll < 0.9:
            af = self.devices[7]
            if not self.queues.get(af["device_code"]) and af["status"] == "idle":
                self.queues[af["device_code"]] = [{"device_type": t, "notify": {"device": None}}
                                                  for t in ("ftl_relay", "ftl_beacon", "ftl_beacon", "mining_drone")]
            self.af_next(af)
            return None
        if roll < 0.93:
            return self.emit("hub.warning", {"device_code": "HUB00001", "device_type": "system_hub", "location": "SOL-5-L4"},
                             capacity=72, warning_type="maintenance_due")
        d2 = self.devices[5]
        arrive = datetime.now(timezone.utc) + timedelta(seconds=random.randint(30, 300))
        return self.emit("travel.departed", d2, travel_type="cruise", origin=d2["location"], destination="SOL-4",
                         distance_au=0.6, travel_time_seconds=(arrive - datetime.now(timezone.utc)).seconds, arrives_at=iso(arrive))


def load_demo(world: World, path: str) -> None:
    """Swap the world for the demo galaxy: its stars, devices, replicants and stockpiles, every timestamp moved by the
    time since it was captured so trips and scans are still under way."""
    doc = json.load(open(path))
    shift = datetime.now(timezone.utc) - datetime.fromisoformat(doc["captured_at"])
    stamp = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")

    def move(v):
        if isinstance(v, dict):
            return {k: move(x) for k, x in v.items()}
        if isinstance(v, list):
            return [move(x) for x in v]
        if isinstance(v, str) and stamp.match(v):
            try:
                return (datetime.fromisoformat(v) + shift).isoformat(timespec="seconds")
            except ValueError:
                return v
        return v

    doc = move(doc)
    STARS[:] = [s for s in STARS if s["designation"] == "SOL"] + [s for s in doc["stars"] if s["designation"] != "SOL"]
    world.devices = doc["devices"]
    world.replicants = doc["replicants"]
    world.inventory = {i["location"]: {it["resource_type"]: float(it["quantity"]) for it in i["items"]} for i in doc["inventory"]}
    world.location = next((d["location"] for d in world.devices if d["device_code"] == HOST), None) or world.location


def create_mock(event_interval: float = 4.0) -> FastAPI:
    world = World()
    if demo := os.environ.get("RS_MOCK_DEMO"):
        load_demo(world, demo if demo.endswith(".json") else os.path.join(os.path.dirname(__file__), "demo_galaxy.json"))
    app = FastAPI(title="mock replicant space")
    app.state.world = world

    def rl(resp: JSONResponse) -> JSONResponse:
        resp.headers["X-RateLimit-Limit"] = "120"
        resp.headers["X-RateLimit-Remaining"] = str(random.randint(80, 119))
        resp.headers["X-Replicant-Space-Unread-Count"] = "1"
        return resp

    def ok(body, status=200):
        return rl(JSONResponse(body, status_code=status))

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if not request.headers.get("authorization", "").startswith("Bearer "):
            return JSONResponse({"error": "missing bearer token"}, status_code=401)
        return await call_next(request)

    TUTORIALS = [("bootstrap", "Bootstrap", "Learn the basics of commanding your replicant.",
                  [("check_messages", "Check your messages", "GET /messages to view your inbox."),
                   ("check_events", "Check your events feed", "GET /events to see your game activity."),
                   ("scan_system", "Scan your new star system.", "POST /replicants/:code/scan to scan the system."),
                   ("check_vessel", "Check your vessel and list of stowed devices.", "GET /replicants/:code to see your replicant information.")]),
                 ("exploring_belt", "Exploring the Belt", "Search for more mining sites and scan the belt.",
                  [("scan_belt", "Scan the belt.", "POST /devices/:code scan at the belt."),
                   ("search_sites", "Search for more sites.", "POST /devices/:code search."),
                   ("mine_site", "Mine a new site.", "POST /devices/:code mine.")])]
    tut_done = {"bootstrap": 3, "exploring_belt": 0}   # steps completed

    def _tut(i, slug, name, desc, steps):
        n = tut_done[slug]
        return {"slug": slug, "name": name, "description": desc, "order": i + 1, "completed": n >= len(steps),
                "current_step": n, "total_steps": len(steps), "graduation": f"{name} graduate: 100 XP"}   # v3.5.2

    sims: dict = {"active": None, "history": []}

    @app.get("/v1/devices/{code}/research")
    async def research_list(code: str):
        return ok({"research": [{"topic": "precursor_signal", "title": "Builder's Echo",
                                 "description": "Analyse a precursor signal pattern.", "completed": False}]})

    @app.post("/v1/devices/{code}/research/{topic}")
    async def research_do(code: str, topic: str):
        return ok({"topic": topic, "achievement": f"{topic}_discovered", "unlocked_blueprints": ["sensor_array"]})

    @app.get("/v1/leaderboards/simulations")
    async def sim_boards():
        return ok({"scenarios": [{"scenario_code": "mining_rush", "scenario_name": "Mining Rush", "completions": 87,
                                  "best_time_seconds": 1842}]})

    @app.get("/v1/leaderboards/simulations/{scen}")
    async def sim_board(scen: str):
        return ok({"scenario_code": scen, "scenario_name": "Mining Rush", "entries": [
            {"rank": 1, "replicant_code": "7A303E2A", "name": "Bob", "score_seconds": 1842, "resources_mined": 1031,
             "devices_printed": 2}]})

    @app.post("/v1/devices/{code}/retrieve")
    async def locker_retrieve(code: str):
        return ok({"status": "retrieved", "vessel_device_code": "HV2E8F3A"})

    @app.get("/v1/devices/{code}/simulate")
    async def sim_scenarios(code: str):
        return ok({"scenarios": [{"code": "mining_rush", "name": "Mining Rush", "description": "Mine 3,000 total resources",
                                  "objective_type": "yield_quota", "objective_target": 3000,
                                  "entry_cost": {"device_type": "compute_core", "quantity": 1}, "timeout_hours": 4,
                                  "long_description": "A small cluster of ~10 stars with you in the middle.", "version": 1},
                                 {"code": "terraforming_training_1", "name": "Terraforming Training",
                                  "description": "Transform a hostile world into a habitable home for humanity.",
                                  "objective_type": "terraform_training", "objective_target": None, "entry_cost": None,
                                  "timeout_hours": None, "version": 1}]})

    @app.delete("/v1/devices/{code}/simulate/{sim_id}")
    async def sim_end(code: str, sim_id: int):
        sims["active"] = None
        return ok({"simulation_id": sim_id, "status": "abandoned"})

    @app.get("/v1/devices/{code}/simulate/active")
    async def sim_active(code: str):
        a = sims["active"]
        return ok({"simulations": [{"is_mine": True, "replicant_name": "Mellifont_Gabbleworth", "scenario_code": a.get("scenario"),
                                    "scenario_name": "Mining Rush", "simulation_id": a.get("id"), "replicant_code": a.get("replicant"),
                                    "started_at": iso(), "timeout_hours": 4}] if a else []})

    @app.post("/v1/devices/{code}/simulate")
    async def sim_start(code: str, request: Request):
        body = await request.json()
        sims["active"] = {"id": 42, "scenario": body.get("scenario"), "replicant": body.get("replicant_code")}
        return ok({"simulation_id": 42, "scenario_code": body.get("scenario"), "scenario_name": "Mining Rush",
                   "starting_star": "VIRTUHOMAM", "starting_location": "VIRTUHOMAM-3-L4", "seed": 2043,
                   "devices": [{"device_code": "SIM8F2A1", "device_type": "heaven_vessel"},
                               {"device_code": "SIM3B7C2", "device_type": "mining_drone"}], "timeout_hours": 4})

    @app.get("/v1/replicants/{code}/devices")
    async def rep_devices(code: str):
        if sims["active"]:
            return ok({"devices": [{"device_code": "REAL0001", "device_type": "mining_drone", "location": "SOL-BELT-1",
                                    "status": "mining"},   # live: the replicant's real devices are listed too
                                   {"device_code": "SIM8F2A1", "device_type": "heaven_vessel", "location": "VIRTUHOMAM-3-L4",
                                    "status": "idle"},
                                   {"device_code": "SIM3B7C2", "device_type": "mining_drone", "location": None,
                                    "status": "stowed", "stowed_in_device_code": "SIM8F2A1"}]})
        return ok({"devices": []})

    @app.get("/v1/accounts/simulations")
    async def sim_history():
        return ok({"simulations": sims["history"]})

    @app.get("/v1/leaderboards")
    async def boards():
        return ok({"boards": [{"key": "xp", "name": "Experience", "description": "Top 10 by XP", "type": "value"},
                              {"key": "distance", "name": "Distance", "description": "Light years traveled", "type": "value"}]})

    @app.get("/v1/leaderboards/xp")
    async def board_xp():
        return ok({"board": "xp", "entries": [{"rank": 1, "replicant_code": "77F75255", "name": "Bob", "value": 101050}]})

    @app.get("/v1/leaderboards/distance")
    async def board_distance():
        return ok({"board": "distance", "entries": [{"rank": 1, "replicant_code": "ZZZZ0001", "name": "Riker", "value": 9000.5}]})

    @app.get("/v1/health")
    async def health():
        return ok({"status": "ok", "database": "ok", "redis": "ok"})

    @app.get("/v1/locations/{star}/events")
    async def loc_events(star: str):
        return ok({"events": [{"designation": "EV1", "location": f"{star}-3", "event_type": "famine", "title": "Famine Signal",
                               "status": "active", "tier": 2, "category": "humanitarian", "discovered_at": iso()}]})

    @app.get("/v1/locations/{star}/inventory")
    async def loc_inventory(star: str):
        return ok({"star": star, "locations": [{"location": f"{star}-BELT-1", "items": [{"resource_type": "structural", "quantity": 120}]}]})

    @app.get("/v1/locations/{star}/stars")
    async def loc_stars(star: str):
        return ok({"stars": [{"designation": "FARAWAYX", "color": "Red", "spectral_type": "M2", "distance_from_replicant": 4.2,
                              "position": {"x": 900, "y": 0, "z": 0}, "explored": False}], "total": 1, "total_pages": 1})

    @app.get("/v1/replicants/{code}/events")
    async def rep_events(code: str):
        return ok({"events": [{"id": 1, "device_code": "HV2E8F3A", "event_type": "travel.arrived", "message": "Arrived at SOL-3",
                               "created_at": iso()}], "next_cursor": None})

    perms: list = []

    @app.get("/v1/devices/{code}/permissions")
    async def perms_get(code: str):
        return ok({"permissions": perms})

    @app.post("/v1/devices/{code}/permissions")
    async def perms_add(code: str, request: Request):
        b = await request.json()
        perms.append({"id": len(perms) + 1, "permission_type": b.get("permission_type"), "granted_account_id": b.get("account_id"),
                      "granted_at": iso()})
        return ok({"status": "granted", "permission_type": b.get("permission_type"), "granted_account_id": b.get("account_id")})

    posts: list = [{"id": 1, "replicant_name": "Bill", "subject": "New shop location", "content": "NOSOXA is in the rear-view mirror",
                    "is_player_content": True, "published_at": iso(), "created_at": iso()}]

    @app.get("/v1/blog")
    async def blog_list():
        return ok({"posts": list(reversed(posts)), "next_cursor": None})

    @app.post("/v1/blog")
    async def blog_add(request: Request):
        b = await request.json()
        posts.append({"id": len(posts) + 1, **b, "is_player_content": True, "published_at": iso(), "created_at": iso()})
        return ok(posts[-1], 201)

    @app.get("/v1/leaderboards/megastructure")
    async def mega_board():
        return ok({"leaderboard": [{"rank": 1, "replicant": "Bob-19", "devices": 214}]})

    @app.post("/v1/locations/{code}/contribute")
    async def contribute(code: str, request: Request):
        body = await request.json()
        return ok({"location": code, "accepted": body.get("devices") or [], "rejected": [], "progress": 0.46,
                   "status": "contribution_recorded"})

    @app.get("/v1/devices/{code}/network")
    async def relay_network(code: str):
        return ok({"connections": [{"device_code": "75B06535", "distance_ly": 2.4, "star": "MUROPE"}],
                   "range_ly": 7.5, "status": "relaying"})

    @app.post("/v1/replicants/{code}/transfer")
    async def transfer(code: str, request: Request):
        body = await request.json()
        return ok({"status": "transferred", "old_host": "11ADA230", "new_host": body.get("target")})

    @app.get("/v1/tutorials")
    async def tutorials():
        return ok({"tutorials": [_tut(i, *t_) for i, t_ in enumerate(TUTORIALS)]})

    @app.get("/v1/tutorials/{slug}")
    async def tutorial(slug: str):
        i, t_ = next(((i, t_) for i, t_ in enumerate(TUTORIALS) if t_[0] == slug), (None, None))
        if t_ is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        n = tut_done[slug]
        d = _tut(i, *t_)
        d.pop("order"), d.pop("total_steps")
        d["steps"] = [{"key": k, "description": ds, "hint": h, "completed": j < n, "current": j == n}
                      for j, (k, ds, h) in enumerate(t_[3])]
        return ok(d)

    @app.get("/v1/accounts/me")
    async def me():
        return ok({"name": "bob", "email": "bob@example.com", "email_verified": True, "created_at": iso(),
                   "experience_points_total": world.xp, "status": "active", "timezone": "America/New_York",
                   "unread_message_count": 1, "bobnet_channels": list(world.channels),
                   "replicants": [{**r, "experience_points": 1245, "created_at": iso()} for r in world.replicants] or
                                 [{"replicant_code": REP, "name": "bob-1", "current_location": world.location, "current_star": "SOL",
                                   "hosted_device_code": HOST, "device_count": len(world.devices), "experience_points": 1245,
                                   "created_at": iso()}]})

    @app.patch("/v1/accounts/me")
    async def me_patch(request: Request):
        body = await request.json()
        if "bobnet_channels" in body:
            chans = body["bobnet_channels"]
            if not isinstance(chans, list) or any(not str(c).startswith("#") for c in chans):
                return ok({"error": "bobnet_channels must be a list of #channel names"}, 400)
            world.channels = list(chans)
        return ok({"bobnet_channels": world.channels})

    @app.get("/v1/devices/{code}/channels")
    async def relay_channels(code: str):
        d = next((d for d in world.devices if d["device_code"] == code), None)
        if not d or "relay" not in d["device_type"]:
            return ok({"error": "Not a relay"}, 400)
        return ok({"channels": [{"name": "#general", "last_active": iso()}, {"name": "#trade", "last_active": iso()},
                                {"name": "#explorers", "last_active": iso()}, {"name": "#sol-local", "last_active": None}]})

    @app.get("/v1/devices/{code}/messages")
    async def relay_messages(code: str, limit: int = 20):
        return ok({"messages": [{"id": 1, "channel": "#explorers", "current_star": "ABOTEIN", "message": "anyone near ZALDANAL?",
                                 "replicant_code": "FF000001", "replicant_name": "riker-2", "time": iso()},
                                {"id": 2, "channel": "#general", "current_star": "SOL", "message": "hello", "replicant_code": REP,
                                 "replicant_name": "bob-1", "time": iso()}][:limit], "next_cursor": None, "total": 2})

    @app.get("/v1/replicants/{code}")
    async def replicant(code: str):
        if code in world.profiles:
            return ok(world.profiles[code])
        if r := next((r for r in world.replicants if r["replicant_code"] == code and code != REP), None):
            return ok({"name": r["name"], "replicant_code": code, "hosted_device_code": r["hosted_device_code"],
                       "location": r["current_location"], "status": "stationary", "experience_points": 1245})
        return ok({"name": "bob-1", "replicant_code": code, "hosted_device_code": HOST, "location": world.location,
                   "position": {"x": 0.0, "y": 0.0, "z": 0.0}, "status": "stationary", "experience_points": 1245,
                   "stowed_devices": [{"device_code": "3CA5D7E4", "device_type": "replicant_matrix"}]})

    @app.patch("/v1/replicants/{code}")
    async def replicant_patch(code: str, request: Request):
        body = await request.json()
        if body.get("name") == "Bill":
            return ok({"error": "Name already taken"}, 409)
        world.profile_patches.append({"code": code, **body})
        return ok({"replicant_code": code, **body})

    @app.get("/v1/replicants/{code}/reputation")
    async def replicant_reputation(code: str):
        return ok({"reputation": [{"species": "Veth", "standing": 12, "tier": "acquainted"}]})

    @app.get("/v1/accounts/reputation")
    async def account_reputation():
        return ok({"reputation": [{"species": "Veth", "standing": 12, "tier": "acquainted", "events_completed": 2}]})

    @app.get("/v1/species")
    async def species():
        return ok({"species": [   # live shape 2026-10-17: environment = each species' habitable ranges
            {"name": "Humans", "species_key": "humans", "homeworld_type": "terrestrial", "trait": "curious",
             "environment": {"biosphere": [30, 70], "gravity": [0.7, 1.3], "hydrosphere": [40, 70], "oxygen": [18, 25],
                             "pressure": [0.8, 1.2], "tectonic": [5, 30], "temperature": [270, 300], "toxicity": [0, 5]}},
            {"name": "Branthar", "species_key": "branthar", "homeworld_type": "desert_world", "trait": "nomadic",
             "environment": {"biosphere": [5, 25], "gravity": [0.8, 1.5], "hydrosphere": [0, 10], "oxygen": [12, 18],
                             "pressure": [0.3, 0.7], "tectonic": [0, 15], "temperature": [300, 340], "toxicity": [0, 15]}},
            {"name": "Veth", "home": "LERNA-3", "stage": "intelligent", "traits": ["curious", "aquatic"]}]})

    @app.get("/v1/replicants")
    async def directory(name: str | None = None, limit: int = 20):
        people = [{"name": "Bill", "replicant_code": "B1LL0001", "last_location": "SOL-3", "is_npc": True},
                  {"name": "Billy-2", "replicant_code": "B1LLY002", "last_location": "LERNA", "is_npc": False},
                  {"name": "Sylphrena", "replicant_code": "30B93F2F", "last_location": "IMPOLLA", "is_npc": True}]
        hits = [p for p in people if not name or name.lower() in p["name"].lower()]
        return ok({"replicants": hits[:limit], "next_cursor": None})

    @app.get("/v1/replicants/{code}/scan/devices")
    async def scan_devices(code: str, device_type: str | None = None):
        devs = [d for d in world.foreign_devices if not device_type or d["device_type"] == device_type]
        return ok({"star": "SOL", "device_count": len(devs), "devices": devs, "next_cursor": None})

    @app.get("/v1/replicants/{code}/stars")
    async def nearby(code: str, per_page: int = 10):
        out = []
        for s in STARS[1:]:
            p = s["position"]
            dist = round((p["x"] ** 2 + p["y"] ** 2 + p["z"] ** 2) ** 0.5, 2)
            out.append({**s, "distance_from_replicant": dist, "estimated_travel_time": int(dist * 45)})
        out.sort(key=lambda s: s["distance_from_replicant"])
        return ok({"page": 1, "per_page": per_page, "replicant_position": {"x": 0, "y": 0, "z": 0}, "stars": out[:per_page]})

    @app.get("/v1/replicants/{code}/stars/{star}")
    async def nearby_one(code: str, star: str):
        s = next((s for s in STARS if s["designation"] == star), None)
        if not s:
            return ok({"error": "Star not found"}, 404)
        p = s["position"]
        dist = round((p["x"] ** 2 + p["y"] ** 2 + p["z"] ** 2) ** 0.5, 2)
        return ok({"replicant_position": {"x": 0, "y": 0, "z": 0},
                   "star": {**s, "distance_from_replicant": dist, "estimated_travel_time": int(dist * 45), "explored": star == "SOL", "has_life": None}})

    @app.post("/v1/replicants/{code}/travel")
    async def travel(code: str, request: Request):
        body = await request.json()
        dest = body.get("destination", "")
        if not dest:
            return ok({"error": "destination is required"}, 400)
        legs = [{"leg": 1, "from": world.location, "to": "SOL-OORT", "type": "cruise", "time_seconds": 30, "distance_ly": 0},
                {"leg": 2, "from": "SOL-OORT", "to": f"{dest.split('-')[0]}-3-L4", "type": "surge", "time_seconds": 600, "distance_ly": 6.4}]
        if body.get("dry_run"):
            return ok({"status": "preview", "final_destination": dest, "total_distance_ly": 6.4, "total_time_seconds": 630, "route": legs})
        secs = world.move_seconds
        arrive = datetime.now(timezone.utc) + timedelta(seconds=secs)
        host = world.devices[0]
        origin = world.location
        world.emit("travel.departed", host, travel_type="surge", origin=origin, destination=dest, arrives_at=iso(arrive),
                   travel_time_seconds=secs, legs=legs)
        # the game lands a star-level trip at the star's entry point
        landing = dest if "-" in dest else f"{dest}-3-L4"

        def arrived():
            world.location = landing
            host["location"] = landing
            for x in world.devices:
                if x["status"] == "stowed":
                    x["location"] = landing
            world.emit("travel.arrived", host, destination=landing, origin=origin, travel_type="surge", attached_devices=[])
        asyncio.get_running_loop().call_later(secs, arrived)
        return ok({"status": "travel_initiated", "origin": world.location, "destination": dest, "departed_at": iso(),
                   "arrives_at": iso(arrive), "total_time_seconds": secs, "route": legs})

    @app.post("/v1/replicants/{code}/teleport")
    async def teleport(code: str, request: Request):
        body = await request.json()
        t = next((d for d in world.devices if d["device_code"] == body.get("target")), None)
        if t is None:
            return ok({"error": "Target not found"}, 404)
        if "slingshot" in t["device_type"]:
            if float(t.get("operational_capacity") or 0) < 80:
                return ok({"error": "Slingshot capacity too low"}, 400)
            t["operational_capacity"] = 5.0
        world.teleports.append((code, body.get("target")))
        return ok({"status": "teleporting", "target": body.get("target"), "completes_at": iso()}, 202)

    @app.post("/v1/replicants/{code}/scan")
    async def scan(code: str):
        return ok(SOL_SCAN)

    @app.post("/v1/replicants/{code}/mine")
    async def mine(code: str, request: Request):
        body = await request.json()
        return ok({"status": "mining", "resource_type": body.get("resource_type")}, 202)

    @app.delete("/v1/replicants/{code}/mine")
    async def stop_mine(code: str):
        return ok({"status": "stopped"})

    @app.post("/v1/replicants/{code}/print")
    async def rprint(code: str, request: Request):
        body = await request.json()
        if body.get("command"):
            return ok({"status": "queue_cleared", "queue": [], "queue_length": 0})
        bp = next((b for b in BLUEPRINTS if b["device_type"] == body.get("device_type")), None)
        if not bp:
            return ok({"error": "Unknown blueprint"}, 400)
        if time.time() < world.vessel_busy_until:
            return ok({"error": "Printer is busy"}, 409)
        world.vessel_busy_until = time.time() + bp["print_time"]
        world.emit("print.started", world.devices[0], device_type=bp["device_type"], print_mode="standard",
                   completes_at=iso(datetime.now(timezone.utc) + timedelta(seconds=bp["print_time"])), tags=[])
        return ok({"status": "enqueued"}, 202)

    @app.post("/v1/replicants/{code}/message")
    async def msg(code: str, request: Request):
        body = await request.json()
        world.emit("bobnet.new", None, id=world.seq, replicant_name="bob-1", replicant_code=REP, current_star="SOL",
                   channel=body.get("channel"), message=body.get("text"))
        return ok({"status": "sent"})

    @app.get("/v1/devices")
    async def devices(limit: int = 20, cursor: int | None = None):
        start = cursor or 0
        page = world.devices[start:start + limit]
        nxt = start + limit if start + limit < len(world.devices) else None
        return ok({"devices": page, "next_cursor": nxt})

    @app.get("/v1/devices/{code}")
    async def device(code: str):
        d = next((d for d in world.devices if d["device_code"] == code), None)
        if d and "autofactory" in d["device_type"]:
            d = {**d, "print_queue": list(world.queues.get(code, []))}
        if d and d["device_code"] == HOST:
            d = {**d, "stowed_devices": [{"device_code": x["device_code"], "device_type": x["device_type"]}
                                         for x in world.devices if x["status"] == "stowed"]}
        return ok(d) if d else ok({"error": "Device not found"}, 404)

    @app.get("/v1/devices/{code}/logs")
    async def logs(code: str):
        return ok({"events": [{"id": 1, "created_at": iso(), "device_code": code, "device_type": "mining_drone",
                               "event_type": "device_deployed", "message": "Deployed at SOL-BELT-1", "payload": {}}]})

    @app.post("/v1/devices/{code}")
    async def command(code: str, request: Request):
        body = await request.json()
        d = next((d for d in world.devices if d["device_code"] == code), None)
        if not d:
            return ok({"error": "Device not found"}, 404)
        cmd = body.get("command")
        if cmd not in d["available_commands"]:
            return ok({"error": f"Command '{cmd}' not available for this device"}, 400)
        if cmd == "stellar_census":   # synchronous: the stars around the vessel (live shape, 2026-10-06)
            here = (d.get("location") or "SOL").split("-")[0]
            return ok({"page": 1, "per_page": 20, "total": 2, "total_pages": 1, "total_stars": 2,
                       "replicant_position": {"x": 0.0, "y": 0.0, "z": 0.0},
                       "stars": [{"designation": here, "color": "Yellow", "spectral_type": "G2", "distance_from_replicant": 0.0,
                                  "entry_point": f"{here}-3-L4", "estimated_planets": 8, "estimated_travel_time": 0,
                                  "explored": True, "has_hub": False, "has_life": True, "has_ward": False,
                                  "position": {"x": 0.0, "y": 0.0, "z": 0.0}, "region": "solzone"},
                                 {"designation": "ZALDANAL", "color": "Red", "spectral_type": "M1", "distance_from_replicant": 5.83,
                                  "entry_point": None, "estimated_planets": 3, "estimated_travel_time": 291,
                                  "explored": False, "has_hub": False, "has_life": None, "has_ward": True,
                                  "position": {"x": 5.0, "y": 3.0, "z": 0.0}, "region": "solzone"}]})
        if cmd == "change_owner":
            if d.get("replicant_code") == body.get("target"):
                return ok({"error": "Device already belongs to that replicant"}, 400)
            d["replicant_code"] = body.get("target")
            return ok({"ok": True, "device_code": code, "replicant_code": d["replicant_code"]})
        if cmd == "start_mining":
            d["status"] = f"mining ({body.get('resource_type')})"
            world.emit("mining.started", d, location=d["location"], resource_type=body.get("resource_type"), site="SOL-BELT-1-SITE-1")
        if cmd == "enqueue_print":
            q = world.queues.setdefault(code, [])
            q.extend({"device_type": body.get("device_type"), "notify": {"device": None}, "tags": list(body.get("tags") or []),
                      **({"oncomplete": body["oncomplete"]} if body.get("oncomplete") else {})}
                     for _ in range(int(body.get("quantity") or 1)))
            world.af_next(d)
            return ok({"status": "enqueued", "queue": list(q), "queue_length": len(q)})
        if cmd == "dequeue_print":
            q = world.queues.setdefault(code, [])
            i = int(body.get("index") or 0)       # 0-based, like the game (confirmed by the player)
            if not 0 <= i < len(q):
                return ok({"error": "Invalid queue index"}, 400)
            removed = q.pop(i)
            return ok({"status": "dequeued", "removed": removed, "queue": list(q), "queue_length": len(q)})
        if cmd == "clear_queue":
            world.queues[code] = []
            return ok({"status": "queue_cleared", "queue": [], "queue_length": 0})
        if cmd == "set_directive":
            world.emit("directive.set", d, directive=body.get("directive"), configuration=body.get("configuration"))
            if any(k in d["device_type"] for k in ("maintenance", "trade")):
                d["ami_directive"] = {"name": body.get("directive"), "config": body.get("configuration") or {}, "_eval_state": "idle"}
                d["ami_directive_status"] = "active"
        if cmd in ("activate", "deactivate") and d["device_type"] == "system_ward":
            d["status"] = "warding" if cmd == "activate" else "inactive"
            d["available_commands"] = [c for c in d["available_commands"] if c not in ("activate", "deactivate")] + [
                "deactivate" if cmd == "activate" else "activate"]
            world.emit(f"ward.{cmd}d", d)
            if cmd == "activate":
                return ok({"device_code": code, "status": "warding", "evicted_miners": world.evict_next})
            return ok({"device_code": code, "status": "inactive"})
        if cmd == "activate" and "propulsor" in d["device_type"]:
            d["status"] = "diverting"
            obj = world.objects.get(d["location"])
            if obj:
                obj["active_propulsors"] = obj.get("active_propulsors", 0) + 1
                obj["current_thrust_per_hour"] = 4.0 * obj["active_propulsors"]
            world.emit("diversion.activated", d, object_designation=d["location"], size_class="large")
        loop = asyncio.get_running_loop()
        if cmd == "travel" and body.get("dry_run"):
            # a preview, like the live game: 50 s/ly between systems, 25 % off a leg into a system with a hub of ours
            # (live 2026-10-18); `via` adds waypoints. Nothing moves
            at = {x["designation"]: x["position"] for x in STARS}
            hubs = {(x.get("location") or "").split("-")[0] for x in world.devices
                    if x.get("device_type") == "system_hub" and x.get("status") not in ("inactive", "stowed")}
            stops = [(d.get("location") or "").split("-")[0]] + [str(v).split("-")[0] for v in body.get("via") or []
                                                                 if body.get("via") != "direct"] + [str(body.get("destination") or "").split("-")[0]]
            legs = []
            for a, b in zip(stops, stops[1:]):
                pa, pb = at.get(a) or {}, at.get(b) or {}
                ly = sum((float(pa.get(k) or 0) - float(pb.get(k) or 0)) ** 2 for k in "xyz") ** .5
                legs.append({"leg": len(legs) + 1, "from": a, "to": b, "type": "surge", "distance_ly": round(ly, 4),
                             "time_seconds": round(ly * 50 * (.75 if b in hubs else 1))})
            return ok({"status": "preview", "final_destination": body.get("destination"), "route": legs,
                       "total_distance_ly": sum(x["distance_ly"] for x in legs),
                       "total_time_seconds": sum(x["time_seconds"] for x in legs)})
        if cmd == "travel":
            secs = world.move_seconds
            arrive = datetime.now(timezone.utc) + timedelta(seconds=secs)
            origin, dest = d["location"], body.get("destination")
            d["status"] = "cruising"
            # the live device list shows a `travel` record while moving
            o_star, d_star = (origin or "").split("-")[0], (dest or "").split("-")[0]
            if o_star != d_star:
                legs = [(origin, f"{o_star}-OORT", "cruise", secs * .1), (f"{o_star}-OORT", f"{d_star}-3-L4", "surge", secs * .8),
                        (f"{d_star}-3-L4", dest, "cruise", secs * .1)]
            else:
                legs = [(origin, dest, "cruise", secs)]
            d["travel"] = {"origin": origin, "destination": dest, "final_destination": dest, "type": legs[0][2],
                           "departed_at": iso(datetime.now(timezone.utc)), "arrives_at": iso(arrive), "final_arrives_at": iso(arrive),
                           "route": [{"leg": i + 1, "from": f, "to": t, "type": ty, "time_seconds": ts}
                                     for i, (f, t, ty, ts) in enumerate(legs)]}
            world.emit("travel.departed", d, travel_type="cruise", origin=origin, destination=dest,
                       arrives_at=iso(arrive), travel_time_seconds=secs)

            def arrived():
                d["location"], d["status"] = dest, "idle"
                d.pop("travel", None)
                if code == HOST:
                    world.location = dest
                    for x in world.devices:
                        if x["status"] == "stowed":
                            x["location"] = dest
                world.emit("travel.arrived", d, destination=dest, origin=origin, travel_type="cruise", attached_devices=[])
            world.trips[code] = loop.call_later(secs, arrived)
            # like the live game (2026-10-08): the answer is the trip itself
            return ok({**d["travel"], "device_code": code, "status": "travelling", "attached_devices": [],
                       "progress_percent": 0.0})
        if cmd == "deploy":
            d["status"] = "idle"
            world.emit("device.deployed", d, deployed_from_device_code=HOST)
            return ok({"device_code": code, "status": "deployed"})
        if cmd == "stow":
            tgt = next((x for x in world.devices if x["device_code"] == body.get("target")), None)
            if tgt and tgt["location"] != d["location"]:
                return ok({"error": "Target must be at the same location"}, 400)
            d["status"] = "stowed"
            world.emit("device.stowed", d, stowed_in_device_code=body.get("target"))
            return ok({"device_code": code, "status": "stowed"})
        if cmd == "collect_resources":
            stock = world.inventory.setdefault(d["location"], {})
            want = body.get("resources") or {}
            hold = d.setdefault("cargo", {})
            room = d.get("cargo_capacity", 0) - sum(hold.values())
            if sum(want.values()) > room:
                return ok({"error": f"Not enough cargo space ({room} free)"}, 400)
            for r, q in want.items():
                if stock.get(r, 0) < q:
                    return ok({"error": f"Not enough {r} here"}, 400)
            for r, q in want.items():
                stock[r] -= q
                hold[r] = hold.get(r, 0) + q
            world.emit("transport.collected", d, resources=want, total=sum(want.values()),
                       cargo_after=sum(hold.values()), cargo_capacity=d.get("cargo_capacity"))
            return ok({"device_code": code, "status": "collected", "cargo": dict(hold)})
        if cmd in ("scan", "search"):
            target = d["location"]
            d["status"] = "scanning"
            world.emit(f"{cmd}.started", d, **{f"{cmd}_target": target, f"{cmd}_type": "body" if cmd == "scan" else "belt",
                                               "eta_seconds": world.move_seconds})

            def done():
                d["status"] = "idle"
                world.emit(f"{cmd}.completed", d, **{f"{cmd}_target": target, f"{cmd}_type": "body" if cmd == "scan" else "belt",
                                                     "report": {}})
            loop.call_later(world.move_seconds, done)
            return ok({"device_code": code, "status": "scanning"}, 202)
        return ok({"device_code": code, "status": d["status"], "command": cmd})

    @app.get("/v1/devices/{code}/audit")
    async def audit(code: str, latest: bool = False, limit: int = 20):
        rows = list(world.audit.get(code, []))
        if latest:
            rows = list(reversed(rows))
        return ok({"audit": rows[:limit]})

    def cancel_trip(code: str):
        d = next((x for x in world.devices if x["device_code"] == code), None)
        h = world.trips.pop(code, None)
        if not d or not h or not d.get("travel"):
            return ok({"error": "Device is not traveling"}, 400)
        h.cancel()
        tr = d.pop("travel")
        d["location"], d["status"] = tr["origin"], "idle"    # it turns back (instantly here)
        world.emit("travel.cancelled", d, travel_type=tr["type"], origin=tr["origin"], destination=tr["destination"],
                   return_time_seconds=0, attached_devices=[])
        return ok({"status": "travel_cancelled", "device_code": code, "return_time_seconds": 0})

    @app.delete("/v1/devices/{code}/travel")
    async def cancel_device_travel(code: str):
        return cancel_trip(code)

    @app.delete("/v1/replicants/{code}/travel")
    async def cancel_replicant_travel(code: str):
        return cancel_trip(HOST)

    @app.post("/v1/feedback")
    async def feedback(request: Request):
        body = await request.json()
        if body.get("type") not in ("typo", "bug", "idea") or not body.get("body"):
            return ok({"error": "type: Must be one of: typo, bug, idea."}, 400)
        world.feedback.append(body)
        return ok({"status": "feedback_received"})

    @app.get("/v1/devices/{code}/trades")
    async def trades(code: str):
        return ok({"trades": world.trades.get(code, [])})

    @app.post("/v1/devices/{code}/trades")
    async def trade_create(code: str, request: Request):
        body = await request.json()
        tr = {"name": body["name"], "trade_code": f"TRD-{len(world.trades.get(code, [])) + 1:06X}", "current_stock": body["stock"],
              "initial_stock": body["stock"], "criteria": body["criteria"], "rewards": body["rewards"], "created_at": iso()}
        world.trades.setdefault(code, []).append(tr)
        return ok(tr, 201)

    @app.delete("/v1/devices/{code}/trades/{trade}")
    async def trade_delete(code: str, trade: str):
        world.trades[code] = [t for t in world.trades.get(code, []) if t["trade_code"] != trade]
        return Response(status_code=204)

    @app.post("/v1/devices/{code}/trades/{trade}")
    async def trade_execute(code: str, trade: str):
        t = next((t for t in world.trades.get(code, []) if t["trade_code"] == trade), None)
        if not t:
            return ok({"error": "Trade not found"}, 404)
        t["current_stock"] -= 1
        return ok({"status": "trade_completed", "trade_code": trade})

    @app.patch("/v1/devices/{code}")
    async def patch_device(code: str, request: Request):
        body = await request.json()
        d = next((d for d in world.devices if d["device_code"] == code), None)
        if d is None:
            return ok({"error": "Device not found"}, 404)
        cfg = body.get("configuration") or {}
        if "linked_device" in cfg:   # FTL slingshot
            d["linked_device"] = cfg["linked_device"]
            return ok({"device_code": code, "linked_device": d["linked_device"]})
        tags = set(d.get("tags") or []) | set(cfg.get("add_tags") or [])
        tags -= set(cfg.get("remove_tags") or [])
        d["tags"] = sorted(tags)
        return ok({"device_code": code, "tags": d["tags"]})

    @app.get("/v1/inventory")
    async def inventory():
        return ok({"locations": [{"location": k, "items": [{"resource": r, "quantity": int(v)} for r, v in items.items()]} for k, items in sorted(world.inventory.items())],
                   "next_cursor": None})

    @app.get("/v1/locations")
    async def locations():
        return ok({"locations": {"SOL-BELT-1": {"devices": 6, "replicants": 1, "resource_sites": 4, "resources": 1980},
                                 "SOL-3-L4": {"devices": 1, "replicants": 0, "resource_sites": 0, "resources": 412}}})

    @app.post("/v1/locations/{code}/events/{des}")
    async def fulfil_event(code: str, des: str):
        done = world.__dict__.setdefault("events_done", set())
        if des in done:
            return ok({"error": "Event already completed by this account"}, 400)
        done.add(des)
        return ok({"status": "completed", "designation": des})

    @app.get("/v1/locations/{code}")
    async def location(code: str):
        if code.endswith("-KUIPER"):
            return ok({"location": code, "kuiper_objects": [{"designation": f"{code}-001", "mass_class": "large",
                                                             "composition": "ice"}]})
        if code == "SOL":
            return ok(SOL_SCAN)
        if "BELT" in code:
            return ok({"location_type": "belt", "location": code, "belt": SOL_SCAN["asteroid_belt"]["belts"][0],
                       "devices": [], "inventory": [], "resource_sites": [{"designation": f"{code}-SITE-1", "resource_type": "structural", "availability": "high", "quantity": 5200},
                                          {"designation": f"{code}-SITE-2", "resource_type": "rares", "availability": "low", "quantity": 340}]})
        if code in world.objects:
            return ok({"location": code, "location_type": "object", "object": world.objects[code], "devices": [], "inventory": []})
        if "-SAL-" in code:   # like the game: salvage codes aren't locations
            return ok({"error": "Planet not found"}, 404)
        if code == "SOL-3-1":  # the body the salvage sits on — shape as seen live (GET /locations/KELMONENT-1)
            return ok({"location": code, "location_type": "rocky", "moon": {"designation": code, "type": "rocky", "scanned": True},
                       "resource_sites": [{"designation": "SOL-3-1-SAL-1", "name": "Derelict hauler", "site_type": "salvage",
                                           "site_index": 0, "resources_remaining_pct": {"structural": 86.67, "conductive": 93.75}}],
                       "devices": [], "inventory": []})
        if code.count("-") >= 1 and code.split("-")[0] == "SOL":
            return ok({"location": code, "planet": {"designation": code, "type": "rocky", "surface_temp_c": 14.0, "mass_earth": 1.0,
                                                   "scanned": True, "tags": ["rocky"]}, "devices": [], "inventory": [], "resource_sites": []})
        return ok({"error": "No scan data for this system; you need presence or a relay"}, 403)

    @app.get("/v1/overview")
    async def overview():   # the docs' example (2026-10-19)
        return ok({"server_time": iso(), "season": {"number": 3, "title": "Alteration Protocol",
                                                     "tagline": "The Exodus Ark departs to distribute humanity...", "in_game_year": 2160},
                   "storylines": [{"title": "Terraforming the Colony Worlds", "summary": "Humanity is living on space stations...",
                                   "status": "active", "action": "Conduct terraforming training at a simulation interface...",
                                   "locations": ["ROSALIATE", "TRYNLLYR"], "visible_from": "2026-10-01"},
                                  {"title": "The Ark Journey", "summary": "The Exodus Ark is distributing humanity...", "status": "complete",
                                   "location": "ROSALIATE-3", "schedule": {"current_stop": 12, "total_stops": 12},
                                   "visible_from": "2026-09-02", "visible_until": "2026-10-31"}],
                   "events": [{"title": "The Shattering of Ethirel", "summary": "The Drafel are blowing up a moon...",
                               "location": "ETHIREL-2", "action": "Print or purchase a vector charge and deliver it...",
                               "reward": "Blueprint for a mining_rig.", "visible_from": "2026-09-26", "visible_until": "2026-10-31"}],
                   "opportunities": [{"title": "Training Simulations", "summary": "Practice gameplay scenarios...",
                                      "locations": [{"name": "Deep Space Tracking Array", "system": "MIRFAKA"}],
                                      "action": "Travel to a datacenter and start a simulation..."}], "npcs": {"Riker": "Earth's caretaker...", "Bill": "Chief engineer at the Skunkworks..."}})

    @app.get("/v1/stars")
    async def stars():
        return ok({"total": len(STARS), "generated_at": iso(), "stars": STARS})

    @app.get("/v1/blueprints")
    async def blueprints():
        return ok({"blueprints": BLUEPRINTS})

    @app.get("/v1/achievements")
    async def all_achievements():
        return ok({"achievements": [
            {"achievement_key": "first_scan", "title": "First Scan", "description": "Completed your first system scan.",
             "category": "exploration", "xp_reward": 100, "player_count": 42, "last_achieved_at": iso()},
            {"achievement_key": "signal_fire", "title": "Signal Fire", "description": "Deploy your first FTL beacon.",
             "category": "infrastructure", "xp_reward": 300, "player_count": 17, "last_achieved_at": iso()}]})

    @app.get("/v1/accounts/achievements")
    async def achievements():
        return ok({"achievements": [{"achievement_key": "first_scan", "title": "First Scan", "description": "Completed your first system scan.",
                                     "category": "exploration", "xp_reward": 100, "achieved_at": iso()}]})

    @app.get("/v1/messages")
    async def messages():
        return ok({"messages": [{"id": 1, "message_type": "alert", "title": "Welcome to the galaxy", "body": "Your matrix is online.",
                                 "created_at": iso(), "read": False}], "next_cursor": None})

    @app.post("/v1/messages/read")
    async def messages_read():
        return ok({"status": "ok"})

    @app.get("/v1/events")
    async def events(limit: int = 100, cursor: str | None = None):
        evs = world.events
        if cursor:
            def key(i):   # "<ms>-<n>": compare as numbers, not text ("…-9" < "…-10")
                a, _, b = str(i).partition("-")
                return (int(a or 0), int(b or 0)) if a.isdigit() and (b or "0").isdigit() else (0, 0)
            evs = [e for e in evs if key(e["id"]) > key(cursor)]
        page = evs[:limit]
        return ok({"events": page, "next_cursor": page[-1]["id"] if len(evs) > limit else None})

    @app.get("/v1/events/stream")
    async def stream(request: Request, cursor: str | None = None):
        last = request.headers.get("last-event-id") or cursor

        async def gen():
            sent = last
            ticks = 0
            while True:
                for ev in [e for e in world.events if not sent or e["id"] > sent]:
                    data = {k: v for k, v in ev.items() if k != "id"}
                    yield f"id: {ev['id']}\nevent: {ev['event']}\ndata: {json.dumps(data)}\n\n"
                    sent = ev["id"]
                await asyncio.sleep(1)
                ticks += 1
                if ticks % max(1, int(event_interval)) == 0:
                    world.random_event()
                if ticks % 15 == 0:
                    yield ": keepalive\n\n"
                if await request.is_disconnected():
                    break

        return StreamingResponse(gen(), media_type="text/event-stream")

    # seed some history
    world.emit("salvage.discovered", {"device_code": "D8C2A140", "device_type": "survey_drone", "location": "SOL-3"},
               designation="SOL-3-1-SAL-1", location="SOL-3-1-SAL-1", salvage_type="wreck", name="Derelict hauler",
               resources={"structural": 300, "conductive": 80})
    for _ in range(40):
        world.random_event()
    return app


app = create_mock()
