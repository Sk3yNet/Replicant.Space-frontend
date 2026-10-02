"""A small fake Replicant Space API for local development and tests.

Run:  uvicorn rsweb.mock:app --port 9000
then point the client at it:  RS_API_BASE=http://localhost:9000/v1 RS_API_TOKEN=dev
It emits a plausible event every few seconds on /v1/events/stream.
"""
from __future__ import annotations

import asyncio
import json
import random
import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


def iso(dt: datetime | None = None) -> str:
    return (dt or datetime.now(timezone.utc)).isoformat(timespec="seconds")


REP = "77F75255"
HOST = "11ADA230"

STARS = [
    {"designation": "SOL", "name": "Sol", "spectral_type": "G2", "color": "Yellow", "position": {"x": 0, "y": 0, "z": 0},
     "estimated_planets": 8, "entry_point": "SOL-5-L4", "region": "solzone", "has_hub": True},
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
    {"device_type": "transport_drone", "short_description": "Moves 20 units of cargo.", "features": ["cruise", "transport", "stow"],
     "print_time": 200, "resources": {"structural": 120, "conductive": 30}},
    {"device_type": "ami_mining_controller", "short_description": "Coordinates mining drones.", "features": ["cruise", "ami", "stow"],
     "directives": ["gather_resources", "gather_evenly", "maintain_ratios"], "print_time": 600,
     "resources": {"conductive": 200, "rares": 40, "silicates": 100, "structural": 150}},
    {"device_type": "ftl_beacon", "short_description": "Monitors traffic in a system.", "features": ["monitor"], "print_time": 100,
     "resources": {"structural": 60, "conductive": 20}},
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
             "available_commands": ["travel", "deactivate"], "stow_capacity": 10},
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
        self.move_seconds = 2.0
        for d in self.devices:
            d["replicant_code"] = REP
        self.location = "SOL-BELT-1"
        self.vessel_busy_until = 0.0
        self.foreign_devices: list[dict] = []
        self.queues: dict[str, list] = {}
        self.af_print_seconds = 45.0
        self.xp = 87340

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
              "star": (device or {}).get("location", "SOL").split("-")[0], "location": (device or {}).get("location"),
              "payload": payload, "created_at": iso()}
        self.events.append(ev)
        self.events = self.events[-10000:]
        return ev

    def random_event(self) -> dict:
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


def create_mock(event_interval: float = 4.0) -> FastAPI:
    world = World()
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

    @app.get("/v1/accounts/me")
    async def me():
        return ok({"name": "bob", "email": "bob@example.com", "email_verified": True, "created_at": iso(),
                   "experience_points_total": world.xp, "status": "active", "timezone": "America/New_York",
                   "unread_message_count": 1, "bobnet_channels": ["#general", "#trade"],
                   "replicants": [{"replicant_code": REP, "name": "bob-1", "current_location": world.location, "current_star": "SOL",
                                   "hosted_device_code": HOST, "device_count": len(world.devices), "experience_points": 1245,
                                   "created_at": iso()}]})

    @app.get("/v1/replicants/{code}")
    async def replicant(code: str):
        return ok({"name": "bob-1", "replicant_code": code, "hosted_device_code": HOST, "location": world.location,
                   "position": {"x": 0.0, "y": 0.0, "z": 0.0}, "status": "stationary", "experience_points": 1245,
                   "stowed_devices": [{"device_code": "3CA5D7E4", "device_type": "replicant_matrix"}]})

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
        if cmd == "start_mining":
            d["status"] = f"mining ({body.get('resource_type')})"
            world.emit("mining.started", d, location=d["location"], resource_type=body.get("resource_type"), site="SOL-BELT-1-SITE-1")
        if cmd == "enqueue_print":
            q = world.queues.setdefault(code, [])
            q.extend({"device_type": body.get("device_type"), "notify": {"device": None}}
                     for _ in range(int(body.get("quantity") or 1)))
            world.af_next(d)
            return ok({"status": "enqueued", "queue": list(q), "queue_length": len(q)})
        if cmd == "dequeue_print":
            q = world.queues.setdefault(code, [])
            i = int(body.get("index") or 0)
            if not 1 <= i <= len(q):
                return ok({"error": "Invalid queue index"}, 400)
            removed = q.pop(i - 1)
            return ok({"status": "dequeued", "removed": removed, "queue": list(q), "queue_length": len(q)})
        if cmd == "clear_queue":
            world.queues[code] = []
            return ok({"status": "queue_cleared", "queue": [], "queue_length": 0})
        if cmd == "set_directive":
            world.emit("directive.set", d, directive=body.get("directive"), configuration=body.get("configuration"))
        loop = asyncio.get_running_loop()
        if cmd == "travel":
            secs = world.move_seconds
            arrive = datetime.now(timezone.utc) + timedelta(seconds=secs)
            origin, dest = d["location"], body.get("destination")
            d["status"] = "cruising"
            world.emit("travel.departed", d, travel_type="cruise", origin=origin, destination=dest,
                       arrives_at=iso(arrive), travel_time_seconds=secs)

            def arrived():
                d["location"], d["status"] = dest, "idle"
                if code == HOST:
                    world.location = dest
                    for x in world.devices:
                        if x["status"] == "stowed":
                            x["location"] = dest
                world.emit("travel.arrived", d, destination=dest, origin=origin, travel_type="cruise", attached_devices=[])
            loop.call_later(secs, arrived)
            return ok({"device_code": code, "status": "travelling", "arrives_at": iso(arrive)})
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

    @app.patch("/v1/devices/{code}")
    async def patch_device(code: str, request: Request):
        body = await request.json()
        d = next((d for d in world.devices if d["device_code"] == code), None)
        cfg = body.get("configuration") or {}
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

    @app.get("/v1/locations/{code}")
    async def location(code: str):
        if code == "SOL":
            return ok(SOL_SCAN)
        if "BELT" in code:
            return ok({"location_type": "belt", "location": code, "belt": SOL_SCAN["asteroid_belt"]["belts"][0],
                       "devices": [], "inventory": [], "resource_sites": [{"designation": f"{code}-SITE-1", "resource_type": "structural", "availability": "high", "quantity": 5200},
                                          {"designation": f"{code}-SITE-2", "resource_type": "rares", "availability": "low", "quantity": 340}]})
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

    @app.get("/v1/stars")
    async def stars():
        return ok({"total": len(STARS), "generated_at": iso(), "stars": STARS})

    @app.get("/v1/blueprints")
    async def blueprints():
        return ok({"blueprints": BLUEPRINTS})

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
            evs = [e for e in evs if e["id"] > cursor]
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
