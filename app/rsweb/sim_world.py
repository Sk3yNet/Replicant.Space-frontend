"""One simulation's virtual galaxy, shaped like the real state so the normal pages show it (`?sim=<run id>`: the device
list, the galaxy map). Read live through the run's replicant and cached for a minute in kv `sim:world:<id>`:
{"t", "rep", "devices", "stars", "inventory", "replicants", "totals"}."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from .api import ApiError
from .sim import is_virtual, split_devices

TTL = 60


async def _replicant(app: Any, sid: str) -> str | None:
    eng = app.state.worker.automations
    s = await eng.sim_state()
    a = s.get("active") or {}
    if str(a.get("id")) == sid and a.get("replicant"):
        return a["replicant"]
    iface = (s.get("interface") or {}).get("code")
    if not iface:
        return None
    try:
        body = await app.state.api.get(f"/devices/{iface}/simulate/active", background=True) or {}
    except ApiError:
        return None
    run = next((r for r in body.get("simulations") or [] if str(r.get("simulation_id")) == sid), None)
    if not run:
        return None
    reps = await app.state.db.kv_get("replicants", {}) or {}
    return run.get("replicant_code") or next((c for c, r in reps.items() if isinstance(r, dict)
                                              and r.get("name") == run.get("replicant_name")), None)


async def world(app: Any, sid: str, force: bool = False) -> dict:
    db = app.state.db
    key = f"sim:world:{sid}"
    w = await db.kv_get(key, {}) or {}
    now = datetime.now(timezone.utc).timestamp()
    if not force and w and now - float(w.get("t") or 0) < TTL:
        return w
    rep = w.get("rep") or await _replicant(app, sid)
    devices, stars = w.get("devices") or [], w.get("stars") or []
    if rep:
        try:
            devices = (await app.state.api.get(f"/replicants/{rep}/devices", background=True) or {}).get("devices") or []
            stars = (await app.state.api.get(f"/replicants/{rep}/stars", background=True, per_page=50) or {}).get("stars") or []
        except ApiError:
            pass
    # the game lists the replicant's real devices (and stars) too: only the virtual galaxy's are this run's
    devices = split_devices(devices)[1] or split_devices(await db.kv_get("sim:devices", []) or [])[1]
    stars = [x for x in stars if is_virtual(x.get("designation"))]
    names = {x.get("designation") for x in stars} | {str(d.get("location") or "").split("-")[0] for d in devices}
    inv = [x for x in await db.kv_get("sim:inventory", []) or [] if str(x.get("location") or "").split("-")[0] in names]
    totals: dict[str, float] = defaultdict(float)
    for x in inv:
        for k, v in (x.get("items") or {}).items():
            try:
                totals[k] += float(v)
            except (TypeError, ValueError):
                pass
    reps = await db.kv_get("replicants", {}) or {}
    w = {"t": now, "rep": rep, "devices": devices, "stars": stars, "inventory": inv, "totals": dict(totals),
         "replicants": {rep: reps.get(rep) or {"replicant_code": rep}} if rep else {}}
    await db.kv_set(key, w)
    return w
