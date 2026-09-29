"""Pages, htmx partials, actions and the live update stream."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from .api import ApiError
from .db import now_iso, row_event
from . import notify

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
router = APIRouter()

RESOURCES = ["structural", "conductive", "silicates", "carbon", "volatiles", "rares"]


# =====================================================================================
# auth
# =====================================================================================
class AuthError(Exception):
    def __init__(self, status: int, msg: str):
        self.status, self.msg = status, msg


async def auth_error_handler(request: Request, exc: AuthError):
    return PlainTextResponse(exc.msg, status_code=exc.status)


def current_user(request: Request) -> str:
    s = request.app.state.settings
    email = (request.headers.get(s.auth_header) or "").strip().lower()
    if not email and s.dev_user:
        email = s.dev_user.lower()
    if not email:
        raise AuthError(401, "Not signed in (no identity header from the auth proxy)")
    if s.allowed_emails and email not in s.allowed_emails:
        raise AuthError(403, f"{email} is not allowed to use this client")
    if request.method not in ("GET", "HEAD") and not request.headers.get("hx-request"):
        # All state-changing requests come from htmx; a cross-site form cannot set this header.
        raise AuthError(403, "Missing HX-Request header")
    return email


# =====================================================================================
# template helpers
# =====================================================================================
def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(os.getenv("TZ") or "America/New_York")
    except Exception:
        return ZoneInfo("UTC")


def parse_ts(v: Any) -> datetime | None:
    if not v or not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def f_local(v: Any, fmt: str = "%b %d %H:%M") -> str:
    dt = parse_ts(v)
    return dt.astimezone(_tz()).strftime(fmt) if dt else (v or "")


def f_ago(v: Any) -> str:
    dt = parse_ts(v)
    if not dt:
        return ""
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    future = secs < 0
    secs = abs(secs)
    if secs < 60:
        txt = f"{int(secs)}s"
    elif secs < 3600:
        txt = f"{int(secs // 60)}m"
    elif secs < 86400:
        txt = f"{secs / 3600:.1f}h"
    else:
        txt = f"{secs / 86400:.1f}d"
    return f"in {txt}" if future else f"{txt} ago"


def f_pretty(v: Any) -> str:
    return json.dumps(v, indent=2, sort_keys=True, default=str)


def f_human(v: Any) -> str:
    return str(v or "").replace("_", " ")


def f_num(v: Any) -> str:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return str(v)
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs(n) >= 10_000:
        return f"{n / 1000:.1f}k"
    return f"{int(n):,}" if n == int(n) else f"{n:,.2f}"


def status_class(status: Any) -> str:
    s = str(status or "").lower()
    if s.startswith(("idle", "inactive", "waiting")):
        return "st-idle"
    if s.startswith(("mining", "printing", "collecting", "depositing", "prospecting", "scanning", "coordinating",
                     "patrolling", "relaying", "monitoring", "tracking", "repairing")):
        return "st-work"
    if s.startswith(("travel", "cruis", "surg", "recall", "divert")):
        return "st-move"
    if s.startswith(("decommission", "compact")):
        return "st-off"
    return "st-other"


def star_of(location: str | None) -> str:
    return (location or "").split("-")[0]


def capacity(v: Any) -> float | None:
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return n * 100 if n <= 1 else n  # docs show both 0-1 and 0-100


templates.env.filters.update(local=f_local, ago=f_ago, pretty=f_pretty, human=f_human, num=f_num,
                             status_class=status_class, star_of=star_of, capacity=capacity)
templates.env.globals.update(RESOURCES=RESOURCES, describe=notify.describe, level_of=notify.level_of)


async def base_ctx(request: Request, user: str, active: str, **kw) -> dict:
    st = request.app.state
    unread = await st.db.fetchone("SELECT COUNT(*) AS n FROM notifications WHERE read=0")
    return {
        "request": request, "user": user, "active": active,
        "rate": st.api.rate_status(), "stream_state": st.worker.stream_state,
        "notif_unread": unread["n"] if unread else 0,
        "configured": st.api.configured,
        **kw,
    }


async def page(request: Request, user: str, template: str, active: str, **kw) -> HTMLResponse:
    visit = None
    if not request.headers.get("hx-request"):
        visit = await notify.touch_visit(request.app.state.db, user, request.app.state.settings.visit_gap_minutes)
    ctx = await base_ctx(request, user, active, visit=visit, **kw)
    return templates.TemplateResponse(request, template, ctx)


def partial(request: Request, template: str, **kw) -> HTMLResponse:
    return templates.TemplateResponse(request, template, {"request": request, **kw})


# =====================================================================================
# state helpers
# =====================================================================================
async def load_state(request: Request) -> dict:
    db = request.app.state.db
    return {
        "account": await db.kv_get("account", {}) or {},
        "replicants": await db.kv_get("replicants", {}) or {},
        "devices": await db.kv_get("devices", []) or [],
        "inventory": await db.kv_get("inventory", []) or [],
        "locations": await db.kv_get("locations", {}) or {},
        "totals": await db.kv_get("inventory_totals", {}) or {},
    }


async def active_timers(request: Request) -> list[dict]:
    rows = await request.app.state.db.fetchall("SELECT * FROM timers ORDER BY ends_at")
    now = datetime.now(timezone.utc)
    out = []
    for r in rows:
        end = parse_ts(r["ends_at"])
        start = parse_ts(r["started_at"]) or now
        if not end:
            continue
        total = max((end - start).total_seconds(), 1)
        r["pct"] = max(0, min(100, 100 * (now - start).total_seconds() / total))
        r["done"] = end <= now
        out.append(r)
    return out


def fleet_summary(devices: list[dict]) -> dict:
    by_type = Counter(d.get("device_type") or "?" for d in devices)
    by_class = Counter(status_class(d.get("status")) for d in devices)
    idle = [d for d in devices if status_class(d.get("status")) == "st-idle"]
    low = [d for d in devices if (capacity(d.get("operational_capacity")) or 100) < 50]
    by_star: dict[str, int] = Counter(star_of(d.get("location")) for d in devices)
    return {"total": len(devices), "by_type": by_type.most_common(), "by_class": by_class,
            "idle": idle, "low": low, "by_star": by_star.most_common()}


def sparkline(values: list[float], w: int = 120, h: int = 28) -> str:
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1
    pts = [f"{i * w / (len(values) - 1):.1f},{h - 2 - (v - lo) * (h - 4) / span:.1f}" for i, v in enumerate(values)]
    return " ".join(pts)


async def resource_series(request: Request, hours: int = 48) -> dict[str, list[float]]:
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = await request.app.state.db.fetchall(
        "SELECT ts, resource, qty FROM inventory_history WHERE ts >= ? ORDER BY ts", (since,))
    series: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        series[r["resource"]].append(r["qty"])
    return series


# =====================================================================================
# actions (everything that changes game state goes through here)
# =====================================================================================
async def run_action(request: Request, user: str, method: str, path: str, body: Any, label: str) -> HTMLResponse:
    st = request.app.state
    status, resp, err = 200, None, None
    try:
        resp = await st.api.request(method, path, json_body=body if method != "GET" else None)
        await st.worker.timers_from_response(path, resp, label)
    except ApiError as e:
        status, err, resp = e.status, e.message, e.body
    await st.db.execute(
        "INSERT INTO actions(at, user, method, path, body, status, response) VALUES(?,?,?,?,?,?,?)",
        (now_iso(), user, method, path, json.dumps(body) if body is not None else None, status,
         json.dumps(resp, default=str)[:20000] if resp is not None else None),
    )
    request.state.action_response = resp if err is None else None
    if not err:
        st.hub.publish("state", "action")
    return partial(request, "partials/action_result.html", label=label, method=method, path=path,
                   ok=err is None, status=status, error=err, response=resp)


def parse_json_field(text: str | None) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    return json.loads(text)


# =====================================================================================
# pages
# =====================================================================================
@router.get("/healthz")
async def healthz():
    return {"ok": True}


@router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, user: str = Depends(current_user)):
    st = await load_state(request)
    visit = await notify.touch_visit(request.app.state.db, user, request.app.state.settings.visit_gap_minutes) \
        if not request.headers.get("hx-request") else None
    digest = None
    if visit and not visit.get("digest_dismissed"):
        digest = await notify.build_digest(request.app.state.db, visit["baseline_at"])
    alerts = await request.app.state.db.fetchall(
        "SELECT * FROM notifications WHERE read=0 AND level='alert' ORDER BY id DESC LIMIT 10")
    recent = [row_event(r) for r in await request.app.state.db.fetchall(
        "SELECT * FROM events WHERE event NOT LIKE 'ami.%.digest' AND event != 'bobnet.new' ORDER BY seq DESC LIMIT 25")]
    series = await resource_series(request)
    sync = {k: await request.app.state.db.kv_get(f"sync:{k}") for k in ("account", "devices", "inventory")}
    ctx = await base_ctx(request, user, "dashboard", visit=visit, digest=digest, alerts=alerts, recent=recent,
                         fleet=fleet_summary(st["devices"]), timers=await active_timers(request),
                         spark={k: sparkline(v) for k, v in series.items()}, sync=sync, **st)
    return templates.TemplateResponse(request, "dashboard.html", ctx)


@router.post("/digest/dismiss", response_class=HTMLResponse)
async def digest_dismiss(request: Request, user: str = Depends(current_user)):
    await request.app.state.db.execute("UPDATE visitors SET digest_dismissed=1 WHERE email=?", (user,))
    return HTMLResponse("")


@router.get("/digest", response_class=HTMLResponse)
async def digest_view(request: Request, hours: int = 24, user: str = Depends(current_user)):
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    digest = await notify.build_digest(request.app.state.db, since)
    return await page(request, user, "digest.html", "dashboard", digest=digest, hours=hours)


@router.get("/partials/timers", response_class=HTMLResponse)
async def p_timers(request: Request, user: str = Depends(current_user)):
    return partial(request, "partials/timers.html", timers=await active_timers(request))


@router.get("/partials/fleet-summary", response_class=HTMLResponse)
async def p_fleet_summary(request: Request, user: str = Depends(current_user)):
    st = await load_state(request)
    return partial(request, "partials/fleet_summary.html", fleet=fleet_summary(st["devices"]), **st)


@router.get("/partials/rate", response_class=HTMLResponse)
async def p_rate(request: Request, user: str = Depends(current_user)):
    return partial(request, "partials/rate.html", rate=request.app.state.api.rate_status(),
                   stream_state=request.app.state.worker.stream_state)


# --- fleet ------------------------------------------------------------------------------
def filter_devices(devices: list[dict], q: str, dtype: str, status: str, star: str, tag: str) -> list[dict]:
    out = []
    ql = q.lower().strip()
    for d in devices:
        if dtype and d.get("device_type") != dtype:
            continue
        if status and status_class(d.get("status")) != status:
            continue
        if star and star_of(d.get("location")) != star:
            continue
        if tag and tag not in (d.get("tags") or []):
            continue
        if ql and ql not in json.dumps(d).lower():
            continue
        out.append(d)
    return sorted(out, key=lambda d: (d.get("location") or "", d.get("device_type") or "", d.get("device_code") or ""))


@router.get("/fleet", response_class=HTMLResponse)
async def fleet(request: Request, q: str = "", type: str = "", status: str = "", star: str = "", tag: str = "",
                user: str = Depends(current_user)):
    st = await load_state(request)
    devs = filter_devices(st["devices"], q, type, status, star, tag)
    types = sorted({d.get("device_type") or "?" for d in st["devices"]})
    stars = sorted({star_of(d.get("location")) for d in st["devices"]})
    tags = sorted({t for d in st["devices"] for t in (d.get("tags") or [])})
    kw = dict(devices=devs, types=types, stars=stars, tags=tags,
              f={"q": q, "type": type, "status": status, "star": star, "tag": tag}, **{k: v for k, v in st.items() if k != "devices"})
    if request.headers.get("hx-request") and request.headers.get("hx-target") == "fleet-table":
        return partial(request, "partials/fleet_table.html", **kw)
    return await page(request, user, "fleet.html", "fleet", **kw)


@router.post("/fleet/refresh", response_class=HTMLResponse)
async def fleet_refresh(request: Request, user: str = Depends(current_user)):
    try:
        await request.app.state.worker.sync_devices()
        msg = "Device list refreshed."
    except ApiError as e:
        msg = f"Refresh failed: {e.message}"
    return HTMLResponse(f'<span class="muted">{msg}</span>', headers={"HX-Trigger": "fleet-refreshed"})


@router.get("/devices/{code}", response_class=HTMLResponse)
async def device_detail(request: Request, code: str, user: str = Depends(current_user)):
    api = request.app.state.api
    err = None
    dev, logs = {}, []
    try:
        dev = await api.get(f"/devices/{code}")
    except ApiError as e:
        err = e.message
        dev = next((d for d in (await load_state(request))["devices"] if d.get("device_code") == code), {})
    try:
        logs = ((await api.get(f"/devices/{code}/logs", latest="true", limit=20)) or {}).get("events") or []
    except ApiError:
        pass
    events = [row_event(r) for r in await request.app.state.db.fetchall(
        "SELECT * FROM events WHERE device_code=? ORDER BY seq DESC LIMIT 50", (code,))]
    st = await load_state(request)
    same_loc = [d for d in st["devices"] if d.get("location") == dev.get("location") and d.get("device_code") != code]
    return await page(request, user, "device.html", "fleet", dev=dev, code=code, err=err, logs=logs, events=events,
                      same_loc=same_loc, command_help=COMMAND_HELP)


# Argument templates for device commands (pre-fills the JSON box on the device page).
COMMAND_HELP: dict[str, dict] = {
    "travel": {"destination": "STAR-BELT-1"},
    "stow": {"target": "DEVICE_CODE"},
    "deploy": {},
    "start_mining": {"resource_type": "structural"},
    "retarget": {"resource_type": "conductive"},
    "collect_resources": {"resources": {"structural": 20}},
    "deposit_resources": {},
    "attach": {"device": "DEVICE_CODE"},
    "enqueue_print": {"device_type": "mining_drone", "quantity": 1},
    "dequeue_print": {"index": 1},
    "prospect": {"direction": [0.0, 1.0, 0.0]},
    "replicate": {"target": "EMPTY_MATRIX_CODE"},
    "adopt": {"devices": ["DEVICE_CODE"]},
    "release": {"devices": ["DEVICE_CODE"]},
    "set_directive": {"directive": "gather_evenly", "configuration": {}},
    "change_owner": {"replicant_code": "REPLICANT_CODE"},
    "set_welcome_message": {"message": "Welcome!"},
    "message": {"channel": "#general", "text": "hello"},
}
DANGEROUS = {"decommission", "change_owner", "deactivate"}


@router.post("/devices/{code}/command", response_class=HTMLResponse)
async def device_command(request: Request, code: str, command: str = Form(...), args: str = Form(""),
                         user: str = Depends(current_user)):
    try:
        extra = parse_json_field(args) or {}
        if not isinstance(extra, dict):
            raise ValueError("arguments must be a JSON object")
    except ValueError as e:
        return partial(request, "partials/action_result.html", ok=False, status=400, error=f"Bad JSON: {e}",
                       label=command, method="POST", path=f"/devices/{code}", response=None)
    return await run_action(request, user, "POST", f"/devices/{code}", {"command": command, **extra},
                            f"{command} on {code}")


@router.post("/devices/{code}/tags", response_class=HTMLResponse)
async def device_tags(request: Request, code: str, add: str = Form(""), remove: str = Form(""),
                      user: str = Depends(current_user)):
    cfg: dict = {}
    if add.strip():
        cfg["add_tags"] = [t.strip() for t in add.split(",") if t.strip()]
    if remove.strip():
        cfg["remove_tags"] = [t.strip() for t in remove.split(",") if t.strip()]
    return await run_action(request, user, "PATCH", f"/devices/{code}", {"configuration": cfg}, f"tags on {code}")


# --- replicants -------------------------------------------------------------------------
@router.get("/replicants/{code}", response_class=HTMLResponse)
async def replicant_detail(request: Request, code: str, user: str = Depends(current_user)):
    st = await load_state(request)
    rep = st["replicants"].get(code, {})
    try:
        rep = {**rep, **(await request.app.state.api.get(f"/replicants/{code}") or {})}
    except ApiError as e:
        rep["_error"] = e.message
    nearby = []
    try:
        nearby = ((await request.app.state.api.get(f"/replicants/{code}/stars", per_page=15)) or {}).get("stars") or []
    except ApiError:
        pass
    blueprints = await request.app.state.db.kv_get("blueprints", []) or []
    devices = [d for d in st["devices"] if d.get("replicant_code") == code]
    return await page(request, user, "replicant.html", "fleet", rep=rep, code=code, nearby=nearby,
                      blueprints=blueprints, devices=devices, timers=[t for t in await active_timers(request)
                                                                      if code in (t.get("device_code"), t.get("replicant_code"))])


@router.post("/replicants/{code}/travel", response_class=HTMLResponse)
async def replicant_travel(request: Request, code: str, destination: str = Form(...), dry_run: str = Form(""),
                           user: str = Depends(current_user)):
    destination = destination.strip().upper()
    if dry_run:
        try:
            resp = await request.app.state.api.post(f"/replicants/{code}/travel", {"destination": destination, "dry_run": True})
        except ApiError as e:
            return partial(request, "partials/action_result.html", ok=False, status=e.status, error=e.message,
                           label="route preview", method="POST", path=f"/replicants/{code}/travel", response=e.body)
        return partial(request, "partials/route_preview.html", code=code, destination=destination, r=resp,
                       legs=resp.get("route") if isinstance(resp.get("route"), list) else [resp.get("route")] if resp.get("route") else [])
    return await run_action(request, user, "POST", f"/replicants/{code}/travel", {"destination": destination},
                            f"{code} → {destination}")


@router.post("/replicants/{code}/scan", response_class=HTMLResponse)
async def replicant_scan(request: Request, code: str, user: str = Depends(current_user)):
    resp = await run_action(request, user, "POST", f"/replicants/{code}/scan", {}, f"system scan by {code}")
    # Cache the scan for the system view if it succeeded.
    data = getattr(request.state, "action_response", None)
    if isinstance(data, dict) and data.get("planets") is not None:
        star = (data.get("star") or {}).get("designation")
        if not star:
            rep = (await load_state(request))["replicants"].get(code, {})
            star = star_of(rep.get("location") or rep.get("current_location"))
        if star:
            await request.app.state.db.execute(
                "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)", (star, json.dumps(data), now_iso()))
    return resp


@router.post("/replicants/{code}/mine", response_class=HTMLResponse)
async def replicant_mine(request: Request, code: str, resource_type: str = Form(""), stop: str = Form(""),
                         user: str = Depends(current_user)):
    if stop:
        return await run_action(request, user, "DELETE", f"/replicants/{code}/mine", None, f"stop mining {code}")
    return await run_action(request, user, "POST", f"/replicants/{code}/mine", {"resource_type": resource_type},
                            f"{code} mine {resource_type}")


@router.post("/replicants/{code}/print", response_class=HTMLResponse)
async def replicant_print(request: Request, code: str, device_type: str = Form(""), command: str = Form(""),
                          user: str = Depends(current_user)):
    body = {"command": command} if command else {"device_type": device_type}
    return await run_action(request, user, "POST", f"/replicants/{code}/print", body,
                            f"{code} print {device_type or command}")


@router.post("/replicants/{code}/message", response_class=HTMLResponse)
async def replicant_message(request: Request, code: str, channel: str = Form("#general"), text: str = Form(...),
                            user: str = Depends(current_user)):
    return await run_action(request, user, "POST", f"/replicants/{code}/message", {"channel": channel, "text": text},
                            f"BobNet {channel}")


# --- systems ----------------------------------------------------------------------------
@router.get("/systems", response_class=HTMLResponse)
async def systems(request: Request, user: str = Depends(current_user)):
    st = await load_state(request)
    known = {r["star"]: r for r in await request.app.state.db.fetchall("SELECT star, updated_at FROM systems")}
    presence: dict[str, dict] = defaultdict(lambda: {"devices": 0, "replicants": [], "locations": 0, "resources": 0})
    for d in st["devices"]:
        presence[star_of(d.get("location"))]["devices"] += 1
    for code, r in st["replicants"].items():
        presence[star_of(r.get("location") or r.get("current_location"))]["replicants"].append(r.get("name") or code)
    for loc, info in (st["locations"] or {}).items():
        p = presence[star_of(loc)]
        p["locations"] += 1
        p["resources"] += (info or {}).get("resources") or 0
    for s in known:
        presence[s]
    rows = sorted(presence.items(), key=lambda kv: (-kv[1]["devices"], kv[0]))
    return await page(request, user, "systems.html", "systems", rows=[(s, p) for s, p in rows if s], known=known)


def _angle(code: str) -> float:
    return int(hashlib.md5(code.encode()).hexdigest()[:6], 16) % 360 * math.pi / 180


def build_system_view(star: str, scan: dict, devices: list[dict], inventory: list[dict]) -> dict:
    """Lay out a top-down, log-scaled diagram of a star system as SVG primitives."""
    size, c = 760, 380
    planets = scan.get("planets") or []
    belts = ((scan.get("asteroid_belt") or {}).get("belts")) or []
    outer = scan.get("outer_system") or {}
    kuiper = (outer.get("kuiper") or {}).get("distance_au")
    oort = (outer.get("oort") or {}).get("distance_au")
    dists = [p.get("orbital_distance_au") or 0 for p in planets] + [b.get("outer_radius_au") or 0 for b in belts]
    max_au = max([kuiper or 0, *(d * 1.25 for d in dists), 1.0])

    def r(au: float) -> float:
        return 28 + (c - 50) * math.log10(1 + 9 * (au or 0) / max_au)

    shapes = {"planets": [], "belts": [], "hz": None, "kuiper": None, "markers": [], "size": size, "c": c}
    hz = (scan.get("star") or {}).get("habitable_zone") or {}
    if hz.get("inner_au") is not None and hz.get("outer_au") is not None:
        ri, ro = r(hz["inner_au"]), r(hz["outer_au"])
        shapes["hz"] = {"r": (ri + ro) / 2, "w": max(ro - ri, 1.5)}
    pos: dict[str, tuple[float, float]] = {}
    for p in planets:
        rad, a = r(p.get("orbital_distance_au")), _angle(p.get("designation", ""))
        x, y = c + rad * math.cos(a), c + rad * math.sin(a)
        pos[p.get("designation", "")] = (x, y)
        shapes["planets"].append({**p, "r": rad, "x": x, "y": y,
                                  "size": 9 if "Giant" in (p.get("type") or "") else 6})
    for b in belts:
        ri, ro = r(b.get("inner_radius_au")), r(b.get("outer_radius_au"))
        shapes["belts"].append({**b, "r": (ri + ro) / 2, "w": max(ro - ri, 3)})
    if kuiper:
        shapes["kuiper"] = {"r": r(kuiper), "au": kuiper, "designation": (outer.get("kuiper") or {}).get("designation")}
    shapes["oort"] = {"au": oort, "designation": (outer.get("oort") or {}).get("designation")} if oort else None

    def loc_xy(loc: str) -> tuple[float, float] | None:
        parts = loc.split("-")
        if len(parts) < 2:
            return (c, c)
        if parts[1] == "BELT" and shapes["belts"]:
            belt = next((b for b in shapes["belts"] if loc.startswith(b.get("designation", "~"))), shapes["belts"][0])
            a = _angle(loc)
            return (c + belt["r"] * math.cos(a), c + belt["r"] * math.sin(a))
        if parts[1] == "KUIPER" and shapes["kuiper"]:
            a = _angle(loc)
            return (c + shapes["kuiper"]["r"] * math.cos(a), c + shapes["kuiper"]["r"] * math.sin(a))
        if parts[1] == "OORT":
            return (size - 30, 30)
        planet = "-".join(parts[:2])
        if planet in pos:
            px, py = pos[planet]
            if len(parts) >= 3 and parts[2] in ("L4", "L5"):
                p = next(p for p in shapes["planets"] if p["designation"] == planet)
                a = math.atan2(py - c, px - c) + (math.pi / 3 if parts[2] == "L4" else -math.pi / 3)
                return (c + p["r"] * math.cos(a), c + p["r"] * math.sin(a))
            if len(parts) >= 3:
                a = _angle(loc)
                return (px + 14 * math.cos(a), py + 14 * math.sin(a))
            return (px, py)
        a = _angle(loc)
        return (c + (c - 60) * 0.5 * math.cos(a), c + (c - 60) * 0.5 * math.sin(a))

    by_loc: dict[str, list[dict]] = defaultdict(list)
    for d in devices:
        if star_of(d.get("location")) == star:
            by_loc[d.get("location")].append(d)
    inv = {i.get("location"): i.get("items") or {} for i in inventory if star_of(i.get("location")) == star}
    for loc in sorted(set(by_loc) | set(inv)):
        xy = loc_xy(loc)
        if xy:
            shapes["markers"].append({"loc": loc, "x": xy[0], "y": xy[1], "devices": by_loc.get(loc, []),
                                      "stock": inv.get(loc, {})})
    return shapes


@router.get("/systems/{star}", response_class=HTMLResponse)
async def system_view(request: Request, star: str, refresh: int = 0, user: str = Depends(current_user)):
    star = star.upper()
    db = request.app.state.db
    err = None
    row = await db.fetchone("SELECT data, updated_at FROM systems WHERE star=?", (star,))
    if refresh or not row:
        try:
            await request.app.state.worker.refresh_system(star)
            row = await db.fetchone("SELECT data, updated_at FROM systems WHERE star=?", (star,))
        except ApiError as e:
            err = e.message
    scan = json.loads(row["data"]) if row else {}
    st = await load_state(request)
    view = build_system_view(star, scan, st["devices"], st["inventory"])
    reps = [r for r in st["replicants"].values() if star_of(r.get("location") or r.get("current_location")) == star]
    return await page(request, user, "system.html", "systems", star=star, scan=scan, view=view, err=err,
                      updated=row["updated_at"] if row else None, reps=reps)


@router.get("/locations/{code}", response_class=HTMLResponse)
async def location_detail(request: Request, code: str, user: str = Depends(current_user)):
    try:
        data = await request.app.state.api.get(f"/locations/{code}")
        err = None
    except ApiError as e:
        data, err = {}, e.message
    st = await load_state(request)
    devices = [d for d in st["devices"] if d.get("location") == code]
    tmpl = "partials/location.html" if request.headers.get("hx-request") else "location_page.html"
    if tmpl.startswith("partials"):
        return partial(request, tmpl, code=code, data=data, err=err, devices=devices)
    return await page(request, user, tmpl, "systems", code=code, data=data, err=err, devices=devices)


# --- galaxy map ---------------------------------------------------------------------------
@router.get("/map", response_class=HTMLResponse)
async def galaxy_map(request: Request, user: str = Depends(current_user)):
    st = await load_state(request)
    return await page(request, user, "map.html", "map", replicants=st["replicants"])


@router.get("/api/map.json")
async def map_data(request: Request, user: str = Depends(current_user)):
    db = request.app.state.db
    cat = await db.kv_get("stars", {}) or {}
    st = await load_state(request)
    presence = Counter(star_of(d.get("location")) for d in st["devices"])
    infra: dict[str, list[str]] = defaultdict(list)
    for d in st["devices"]:
        t = d.get("device_type") or ""
        if any(k in t for k in ("relay", "hub", "beacon", "ward", "slingshot", "observatory")):
            infra[star_of(d.get("location"))].append(t)
    scanned = {r["star"] for r in await db.fetchall("SELECT star FROM systems")}
    reps = [{"code": c, "name": r.get("name"), "star": star_of(r.get("location") or r.get("current_location")),
             "position": r.get("position")} for c, r in st["replicants"].items()]
    stars = []
    for s in cat.get("stars") or []:
        d = s.get("designation")
        stars.append({**s, "devices": presence.get(d, 0), "infra": infra.get(d, []), "scanned": d in scanned})
    return JSONResponse({"stars": stars, "replicants": reps, "generated_at": cat.get("generated_at"),
                         "catalogue_updated": await db.kv_updated("stars")})


@router.get("/api/route", response_class=HTMLResponse)
async def route_estimate(request: Request, replicant: str, star: str, user: str = Depends(current_user)):
    try:
        body = await request.app.state.api.get(f"/replicants/{replicant}/stars/{star}")
        err = None
    except ApiError as e:
        body, err = {}, e.message
    return partial(request, "partials/route_estimate.html", s=(body or {}).get("star") or {}, err=err,
                   replicant=replicant, star=star)


@router.post("/map/refresh", response_class=HTMLResponse)
async def map_refresh(request: Request, user: str = Depends(current_user)):
    try:
        stars = await request.app.state.api.get("/stars")
        await request.app.state.db.kv_set("stars", stars or {})
        return HTMLResponse(f"Catalogue refreshed ({len((stars or {}).get('stars') or [])} stars). Reload the map.")
    except ApiError as e:
        return HTMLResponse(f"Refresh failed: {e.message} (the catalogue allows 1 request/minute)")


# --- blueprints & planning -------------------------------------------------------------------
def printers(state: dict) -> list[dict]:
    """Everything that can print: replicant vessels and autofactories."""
    out = []
    for code, r in state["replicants"].items():
        out.append({"kind": "replicant", "code": code, "name": r.get("name") or code,
                    "location": r.get("location") or r.get("current_location")})
    for d in state["devices"]:
        if "print" in (d.get("features") or []) and d.get("device_type") != "heaven_vessel":
            out.append({"kind": "device", "code": d["device_code"], "name": f"{f_human(d.get('device_type'))} {d['device_code']}",
                        "location": d.get("location")})
    return out


def affordable(cost: dict, items: dict) -> int:
    counts = []
    for k, v in (cost or {}).items():
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v > 0:
            counts.append(int(float(items.get(k, 0) or 0) // v))
    return min(counts) if counts else 0


@router.get("/blueprints", response_class=HTMLResponse)
async def blueprints(request: Request, q: str = "", user: str = Depends(current_user)):
    st = await load_state(request)
    bps = await request.app.state.db.kv_get("blueprints", []) or []
    if q:
        bps = [b for b in bps if q.lower() in json.dumps(b).lower()]
    inv = {i.get("location"): i.get("items") or {} for i in st["inventory"]}
    prs = printers(st)
    for b in bps:
        b["_afford"] = {p["code"]: affordable(b.get("resources") or {}, inv.get(p["location"], {})) for p in prs}
    return await page(request, user, "blueprints.html", "blueprints", blueprints=sorted(bps, key=lambda b: b.get("device_type", "")),
                      printers=prs, inv=inv, q=q)


@router.post("/blueprints/plan", response_class=HTMLResponse)
async def blueprint_plan(request: Request, user: str = Depends(current_user)):
    form = await request.form()
    st = await load_state(request)
    bps = {b.get("device_type"): b for b in await request.app.state.db.kv_get("blueprints", []) or []}
    location = form.get("location") or ""
    inv = {i.get("location"): i.get("items") or {} for i in st["inventory"]}.get(location, {})
    need: Counter = Counter()
    lines = []
    total_time = 0
    for key, val in form.multi_items():
        if not key.startswith("qty:"):
            continue
        try:
            n = int(val or 0)
        except ValueError:
            n = 0
        if n <= 0:
            continue
        bp = bps.get(key[4:])
        if not bp:
            continue
        for r, v in (bp.get("resources") or {}).items():
            need[r] += float(v) * n
        total_time += (bp.get("print_time") or 0) * n
        lines.append((key[4:], n))
    rows = [{"resource": r, "need": need[r], "have": float(inv.get(r, 0) or 0),
             "short": max(0.0, need[r] - float(inv.get(r, 0) or 0))} for r in sorted(need)]
    return partial(request, "partials/plan.html", rows=rows, lines=lines, location=location, total_time=total_time)


@router.post("/blueprints/print", response_class=HTMLResponse)
async def blueprint_print(request: Request, printer: str = Form(...), device_type: str = Form(...),
                          quantity: int = Form(1), user: str = Depends(current_user)):
    kind, _, code = printer.partition(":")
    if kind == "replicant":
        return await run_action(request, user, "POST", f"/replicants/{code}/print", {"device_type": device_type},
                                f"print {device_type} on {code}")
    return await run_action(request, user, "POST", f"/devices/{code}",
                            {"command": "enqueue_print", "device_type": device_type, "quantity": max(1, quantity)},
                            f"enqueue {quantity}× {device_type} on {code}")


# --- AMI ---------------------------------------------------------------------------------------
DIRECTIVES = {
    "mining": {"gather_resources": {"structural": 500, "conductive": 200},
               "gather_evenly": {}, "maintain_ratios": {"structural": 0.5, "conductive": 0.3, "silicates": 0.2},
               "deplete_smallest": {}, "gather_salvage": {"location": "STAR-1-3-SAL-1", "recall": True}},
    "survey": {"survey_system": {"planets": "all", "moons": "all", "recall": True}, "belt_search": {}},
    "transport": {"delivery": {"route": {"collect": "STAR-BELT-1", "deliver": "STAR-3-L4"}, "requirement": {"structural": 100}},
                  "shuttle": {"collect": "STAR-BELT-1", "deliver": "STAR-3-L4", "priority": ["structural"]},
                  "ferry": {"collect": "STAR-BELT-1", "deliver": "OTHER-3-L4", "priority": []},
                  "consolidate": {"deliver": "STAR-3-L4", "priority": []}},
    "maintenance": {"patrol": {}},
    "trade": {"trade": {"name": "My shop", "description": "", "announcement": ""}},
    "fleet": {},
}


def controller_kind(dtype: str) -> str:
    for k in DIRECTIVES:
        if k in (dtype or ""):
            return k
    return "fleet"


@router.get("/ami", response_class=HTMLResponse)
async def ami(request: Request, user: str = Depends(current_user)):
    st = await load_state(request)
    db = request.app.state.db
    ctrls = []
    for d in st["devices"]:
        if "ami" not in (d.get("features") or []):
            continue
        kind = controller_kind(d.get("device_type"))
        last = await db.fetchone(
            "SELECT * FROM events WHERE device_code=? AND event LIKE 'ami.%.digest' ORDER BY seq DESC LIMIT 1",
            (d["device_code"],))
        last_dir = await db.fetchone(
            "SELECT * FROM events WHERE device_code=? AND event LIKE 'directive.%' ORDER BY seq DESC LIMIT 1",
            (d["device_code"],))
        candidates = [x for x in st["devices"] if x.get("location") == d.get("location")
                      and "ami" not in (x.get("features") or [])]
        ctrls.append({"d": d, "kind": kind, "digest": row_event(last) if last else None,
                      "directive": row_event(last_dir) if last_dir else None,
                      "directives": DIRECTIVES.get(kind, {}), "candidates": candidates})
    return await page(request, user, "ami.html", "ami", ctrls=ctrls, directives_json=json.dumps(DIRECTIVES))


@router.post("/ami/{code}/adopt", response_class=HTMLResponse)
async def ami_adopt(request: Request, code: str, user: str = Depends(current_user)):
    form = await request.form()
    devs = form.getlist("devices")
    cmd = form.get("command") or "adopt"
    return await run_action(request, user, "POST", f"/devices/{code}", {"command": cmd, "devices": devs},
                            f"{cmd} {len(devs)} device(s) on {code}")


@router.post("/ami/{code}/directive", response_class=HTMLResponse)
async def ami_directive(request: Request, code: str, directive: str = Form(...), configuration: str = Form(""),
                        user: str = Depends(current_user)):
    try:
        cfg = parse_json_field(configuration) or {}
    except ValueError as e:
        return partial(request, "partials/action_result.html", ok=False, status=400, error=f"Bad JSON: {e}",
                       label="set_directive", method="POST", path=f"/devices/{code}", response=None)
    body = {"command": "set_directive", "directive": directive}
    if cfg:
        body["configuration"] = cfg
    return await run_action(request, user, "POST", f"/devices/{code}", body, f"{code} directive {directive}")


# --- events -----------------------------------------------------------------------------------
async def query_events(request: Request, category: str, event: str, device: str, q: str, before: int | None,
                       limit: int = 100) -> list[dict]:
    sql, params = "SELECT * FROM events WHERE 1=1", []
    if category:
        sql += " AND category=?"; params.append(category)
    if event:
        sql += " AND event LIKE ?"; params.append(event.replace("*", "%"))
    if device:
        sql += " AND device_code=?"; params.append(device.upper())
    if q:
        sql += " AND (payload LIKE ? OR location LIKE ? OR event LIKE ?)"; params += [f"%{q}%"] * 3
    if before:
        sql += " AND seq < ?"; params.append(before)
    sql += " ORDER BY seq DESC LIMIT ?"; params.append(limit)
    return [row_event(r) for r in await request.app.state.db.fetchall(sql, params)]


@router.get("/events", response_class=HTMLResponse)
async def events_page(request: Request, category: str = "", event: str = "", device: str = "", q: str = "",
                      before: int | None = None, user: str = Depends(current_user)):
    evs = await query_events(request, category, event, device, q, before)
    kw = dict(events=evs, f={"category": category, "event": event, "device": device, "q": q},
              last_seq=evs[-1]["seq"] if evs else None)
    if request.headers.get("hx-request") and request.headers.get("hx-target") in ("event-rows", "more-row"):
        return partial(request, "partials/event_rows.html", **kw)
    cats = [r["category"] for r in await request.app.state.db.fetchall(
        "SELECT DISTINCT category FROM events WHERE category IS NOT NULL ORDER BY category")]
    total = await request.app.state.db.fetchone("SELECT COUNT(*) AS n FROM events")
    return await page(request, user, "events.html", "events", categories=cats, total=total["n"], **kw)


@router.post("/events/backfill", response_class=HTMLResponse)
async def events_backfill(request: Request, user: str = Depends(current_user)):
    try:
        n = await request.app.state.worker.backfill()
        return HTMLResponse(f"Backfilled {n} event(s).")
    except ApiError as e:
        return HTMLResponse(f"Backfill failed: {e.message}")


# --- notifications & messages ----------------------------------------------------------------
@router.get("/notifications", response_class=HTMLResponse)
async def notifications(request: Request, show: str = "unread", user: str = Depends(current_user)):
    where = "WHERE read=0" if show == "unread" else ""
    rows = await request.app.state.db.fetchall(f"SELECT * FROM notifications {where} ORDER BY id DESC LIMIT 300")
    return await page(request, user, "notifications.html", "notifications", rows=rows, show=show)


@router.post("/notifications/read", response_class=HTMLResponse)
async def notifications_read(request: Request, id: int | None = Form(None), user: str = Depends(current_user)):
    if id:
        await request.app.state.db.execute("UPDATE notifications SET read=1 WHERE id=?", (id,))
    else:
        await request.app.state.db.execute("UPDATE notifications SET read=1 WHERE read=0")
    return HTMLResponse("", headers={"HX-Refresh": "true"} if not id else {})


@router.get("/messages", response_class=HTMLResponse)
async def messages(request: Request, user: str = Depends(current_user)):
    err = None
    try:
        await request.app.state.worker.sync_messages()
    except ApiError as e:
        err = e.message
    msgs = await request.app.state.db.kv_get("messages", []) or []
    bobnet = [row_event(r) for r in await request.app.state.db.fetchall(
        "SELECT * FROM events WHERE event='bobnet.new' ORDER BY seq DESC LIMIT 50")]
    st = await load_state(request)
    return await page(request, user, "messages.html", "messages", msgs=msgs, err=err, bobnet=bobnet,
                      replicants=st["replicants"])


@router.post("/messages/read", response_class=HTMLResponse)
async def messages_read(request: Request, id: int | None = Form(None), user: str = Depends(current_user)):
    body = {"ids": [id]} if id else {"mark_all": True}
    return await run_action(request, user, "POST", "/messages/read", body, "mark messages read")


# --- account & console ------------------------------------------------------------------------
@router.get("/account", response_class=HTMLResponse)
async def account(request: Request, user: str = Depends(current_user)):
    db = request.app.state.db
    actions = await db.fetchall("SELECT * FROM actions ORDER BY id DESC LIMIT 50")
    sync = {k: await db.kv_get(f"sync:{k}") for k in ("account", "devices", "inventory", "messages", "catalogue")}
    return await page(request, user, "account.html", "account", account=await db.kv_get("account", {}),
                      achievements=await db.kv_get("achievements", {}), actions=actions, sync=sync,
                      hub_listeners=request.app.state.hub.listeners)


CONSOLE_PRESETS = [
    ("GET", "/accounts/me", ""), ("GET", "/devices?limit=50", ""), ("GET", "/inventory", ""),
    ("GET", "/locations", ""), ("GET", "/blueprints", ""), ("GET", "/events?limit=20", ""),
    ("GET", "/accounts/achievements", ""), ("GET", "/accounts/reputation", ""),
    ("GET", "/replicants/{code}/stars?per_page=20", ""), ("GET", "/replicants/{code}/scan/devices", ""),
    ("GET", "/replicants/{code}/traders", ""),
    ("POST", "/replicants/{code}/travel", '{"destination": "SOL-BELT-1", "dry_run": true}'),
    ("PATCH", "/accounts/me", '{"events": {"ami_digest_interval": 3}}'),
]


@router.get("/console", response_class=HTMLResponse)
async def console(request: Request, user: str = Depends(current_user)):
    st = await load_state(request)
    first = next(iter(st["replicants"]), "{code}")
    presets = [(m, p.replace("{code}", first), b) for m, p, b in CONSOLE_PRESETS]
    return await page(request, user, "console.html", "console", presets=presets)


@router.post("/console", response_class=HTMLResponse)
async def console_run(request: Request, method: str = Form("GET"), path: str = Form(...), body: str = Form(""),
                      user: str = Depends(current_user)):
    method = method.upper()
    if method not in ("GET", "POST", "PATCH", "DELETE", "PUT"):
        method = "GET"
    try:
        parsed = parse_json_field(body)
    except ValueError as e:
        return partial(request, "partials/action_result.html", ok=False, status=400, error=f"Bad JSON: {e}",
                       label="console", method=method, path=path, response=None)
    path = path.strip()
    params = None
    if "?" in path:
        path, qs = path.split("?", 1)
        params = dict(p.split("=", 1) if "=" in p else (p, "") for p in qs.split("&") if p)
    if method == "GET":
        try:
            resp = await request.app.state.api.request("GET", path, params=params)
            return partial(request, "partials/action_result.html", ok=True, status=200, error=None, label="console",
                           method=method, path=path, response=resp)
        except ApiError as e:
            return partial(request, "partials/action_result.html", ok=False, status=e.status, error=e.message,
                           label="console", method=method, path=path, response=e.body)
    return await run_action(request, user, method, path, parsed, f"console {method} {path}")


# =====================================================================================
# live updates (SSE to the browser)
# =====================================================================================
def _sse(event: str, html: str) -> str:
    data = "\n".join(f"data: {line}" for line in (html.splitlines() or [""]))
    return f"event: {event}\n{data}\n\n"


@router.get("/live/stream")
async def live(request: Request, user: str = Depends(current_user)):
    st = request.app.state
    q = st.hub.subscribe()
    env = templates.env

    async def gen():
        try:
            yield "retry: 5000\n\n"
            last_rate = 0.0
            while True:
                if await request.is_disconnected():
                    break
                try:
                    kind, data = await asyncio.wait_for(q.get(), timeout=10)
                except asyncio.TimeoutError:
                    kind, data = None, None
                if kind == "event":
                    yield _sse("event", env.get_template("partials/event_row.html").render(e=data, live=True))
                elif kind == "notify":
                    yield _sse("notify", env.get_template("partials/toast.html").render(n=data))
                    unread = await st.db.fetchone("SELECT COUNT(*) AS n FROM notifications WHERE read=0")
                    yield _sse("badge", env.get_template("partials/badge.html").render(n=unread["n"]))
                elif kind == "state":
                    yield _sse("state", str(data))
                now = asyncio.get_running_loop().time()
                if now - last_rate > 10:
                    last_rate = now
                    yield _sse("rate", env.get_template("partials/rate.html").render(
                        rate=st.api.rate_status(), stream_state=st.worker.stream_state))
                    yield ": keepalive\n\n"
        finally:
            st.hub.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
