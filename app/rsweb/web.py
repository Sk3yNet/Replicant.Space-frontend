"""Pages, htmx partials, actions and the live update stream."""
from __future__ import annotations

import asyncio
import html
import logging
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
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates

from .api import ApiError
from .db import now_iso, row_event
from . import notify
from .shapes import as_amounts, normalize_blueprints, normalize_inventory
from . import commands as cmdspec
from . import automations as auto
from .cargo import cargo_context
from .targets import CATEGORY_LABEL, options_for, system_resources, system_targets
from . import carrier as carrier_mod
from .tree import build_tree
from . import production
from . import ami_schedule as amis
from . import printqueue
from .ingest import duplicate_timers

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
from . import version as appver  # noqa: E402
templates.env.globals.update(app_version=appver.label(), APP_VERSION=appver.VERSION, RUN_ID=appver.RUN_ID, entry_age=appver.age_of)
log = logging.getLogger("rsweb.web")
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
    except Exception:  # None, junk, or a template Undefined
        return None
    return n * 100 if n <= 1 else n  # docs show both 0-1 and 0-100


def f_duration(secs: Any) -> str:
    try:
        s = int(float(secs))
    except Exception:
        return "?"
    d, h, m = s // 86400, (s % 86400) // 3600, (s % 3600) // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m {s % 60}s" if m else f"{s}s"


templates.env.filters.update(local=f_local, ago=f_ago, pretty=f_pretty, human=f_human, num=f_num,
                             status_class=status_class, star_of=star_of, capacity=capacity, duration=f_duration)
templates.env.globals.update(RESOURCES=RESOURCES, describe=notify.describe, level_of=notify.level_of,
                             device_options=cmdspec.device_options, target_options=options_for)


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
        "inventory": normalize_inventory(await db.kv_get("inventory", [])),
        "locations": await db.kv_get("locations", {}) or {},
        "totals": await db.kv_get("inventory_totals", {}) or {},
    }


async def active_timers(request: Request) -> list[dict]:
    rows, _ = duplicate_timers(await request.app.state.db.fetchall("SELECT * FROM timers ORDER BY ends_at"))
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
async def call_action(request: Request, user: str, method: str, path: str, body: Any, label: str) -> dict:
    """Send one game command, log it, start any countdown. Returns the outcome without rendering."""
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
    return {"label": label, "method": method, "path": path, "ok": err is None, "status": status,
            "error": err, "response": resp}


def render_action(request: Request, outcome: dict, note: str | None = None) -> HTMLResponse:
    return partial(request, "partials/action_result.html", note=note, **outcome)


async def run_action(request: Request, user: str, method: str, path: str, body: Any, label: str) -> HTMLResponse:
    return render_action(request, await call_action(request, user, method, path, body, label))


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


@router.get("/api/timers.json")
async def timers_json(request: Request, user: str = Depends(current_user)):
    """Raw timer rows, for diagnosing what the In progress list is built from."""
    return JSONResponse(await request.app.state.db.fetchall("SELECT * FROM timers ORDER BY ends_at"))


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
    carrier = await carrier_context(request, dev) if dev else None
    pq = await print_queue_ctx(request, code, dev) if dev and printqueue.has_queue(dev) else None
    return await page(request, user, "device.html", "fleet", carrier=carrier, pq=pq, dev=dev, code=code, err=err, logs=logs, events=events,
                      same_loc=same_loc, commands=order_commands(dev.get("available_commands") or []),
                      dangerous=DANGEROUS)


# --- autofactory print queue ------------------------------------------------------------------
async def print_queue_ctx(request: Request, code: str, dev: dict, outcome: dict | None = None, compact: bool = False) -> dict:
    db = request.app.state.db
    bps = {b["device_type"]: b for b in normalize_blueprints(await db.kv_get("blueprints", []))}
    queue = printqueue.items(dev)
    cur = await printqueue.current(db, dev)
    remaining, exact = printqueue.remaining_seconds(cur, queue, bps)
    cmds = dev.get("available_commands") or []
    cancel_cmd = next((c for c in ("cancel_print", "cancel") if c in cmds), None)
    cat = await db.kv_get("stars", {}) or {}
    st = await load_state(request)
    known = {star_of(d.get("location")) for d in st["devices"] if d.get("location")}
    known |= {r["star"] for r in await db.fetchall("SELECT star FROM systems")}
    return {"code": code, "dev": dev, "queue": queue, "cur": cur, "remaining": remaining, "exact": exact,
            "blueprints": sorted(bps.values(), key=lambda b: b.get("device_type", "")), "outcome": outcome,
            "cancel_cmd": cancel_cmd, "compact": compact, "dest_stars": sorted(k for k in known if k),
            "here_star": star_of(dev.get("location"))}


@router.get("/print-queue/locations", response_class=HTMLResponse)
async def print_queue_locations(request: Request, dest_star: str = "", user: str = Depends(current_user)):
    """<option>s for the 'deliver to' location list of a system: planets, moons, belts, Lagrange points, entry point, outer."""
    star = dest_star.upper()
    if not star:
        return HTMLResponse('<option value="">— system first —</option>')
    t = await system_targets(request.app.state.db, star)
    keep = ("planet", "moon", "belt", "lagrange", "outer", "object")
    opts = ['<option value="">anywhere in ' + star + ' (entry point)</option>']
    for x in t["targets"]:
        if x["category"] in keep or x["category"].startswith(("planet", "moon")):
            text = f'{x["code"]} · {CATEGORY_LABEL.get(x["category"], x["category"])}' + (f' — {x["note"]}' if x.get("note") else "")
            opts.append(f'<option value="{html.escape(x["code"])}">{html.escape(text)}</option>')
    return HTMLResponse("".join(opts))


async def fetch_device(request: Request, code: str) -> dict:
    try:
        return await request.app.state.api.get(f"/devices/{code}")
    except ApiError:
        return next((d for d in (await load_state(request))["devices"] if d.get("device_code") == code), {})


@router.get("/devices/{code}/print-queue", response_class=HTMLResponse)
async def print_queue_panel(request: Request, code: str, compact: int = 0, user: str = Depends(current_user)):
    dev = await fetch_device(request, code)
    return partial(request, "partials/print_queue.html", **await print_queue_ctx(request, code, dev, compact=bool(compact)))


@router.post("/devices/{code}/print-queue", response_class=HTMLResponse)
async def print_queue_action(request: Request, code: str, action: str = Form(...), index: int = Form(0),
                             device_type: str = Form(""), quantity: int = Form(1), dest_star: str = Form(""),
                             dest_loc: str = Form(""), dest_text: str = Form(""), user: str = Depends(current_user)):
    dev = await fetch_device(request, code)
    cmds = dev.get("available_commands") or []
    if action == "remove":
        # `index` is the game's 0-based queue position (the panel shows #1 for index 0)
        body, label = {"command": "dequeue_print", "index": index}, f"remove #{index + 1} from {code}'s queue"
    elif action == "clear":
        body, label = {"command": "clear_queue"}, f"clear {code}'s print queue"
    elif action == "cancel":
        cmd = next((c for c in ("cancel_print", "cancel") if c in cmds), "cancel")
        body, label = {"command": cmd}, f"cancel {code}'s current print"
    elif action == "add" and device_type:
        q = max(1, quantity)
        body, label = {"command": "enqueue_print", "device_type": device_type, "quantity": q}, f"enqueue {q}× {device_type} on {code}"
        loc = (dest_text or dest_loc or "").strip().upper()
        star = (dest_star or "").strip().upper() or star_of(loc)
        if loc and star_of(loc) != star:
            dev2 = await fetch_device(request, code)
            return partial(request, "partials/print_queue.html", **await print_queue_ctx(
                request, code, dev2, {"label": label, "ok": False, "error": f"{loc} isn't in {star}", "method": "POST",
                                      "path": f"/devices/{code}", "status": 400, "response": None}))
        here = star_of(dev.get("location"))
        tags: list[str] = []
        if star and star != here:
            tags.append(lo.to_tag(star))          # the loadout pass delivers it to that system …
        if loc:
            tags.append(lo.at_tag(loc))           # … and on to the exact spot (pinned there)
            if not star or star == here:
                body["oncomplete"] = {"command": "travel", "destination": loc}   # same system: the game sends it on completion
        if tags:
            body["tags"] = tags
        if star or loc:
            label += f" → {loc or star}"
        if star and star != here:
            orders = await request.app.state.db.kv_get("loadout_orders", []) or []
            orders += [{"star": star, "device_type": device_type, "factory": code, "at": now_iso(), "manual": True,
                        **({"location": loc} if loc else {})} for _ in range(q)]
            await request.app.state.db.kv_set("loadout_orders", orders)
    else:
        return HTMLResponse('<div class="result err">Unknown queue action.</div>', status_code=400)
    outcome = await call_action(request, user, "POST", f"/devices/{code}", body, label)
    dev = await fetch_device(request, code)  # re-read so the panel shows the game's view
    return partial(request, "partials/print_queue.html", **await print_queue_ctx(request, code, dev, outcome))


DANGEROUS = {"decommission", "change_owner", "deactivate", "withdraw", "clear_queue", "release", "clear_directive"}


def order_commands(cmds: list[str]) -> list[str]:
    """Everyday commands first (in the game's order), destructive ones last."""
    return [c for c in cmds if c not in DANGEROUS] + [c for c in cmds if c in DANGEROUS]


async def suggestions(request: Request, here: str | None) -> dict:
    db = request.app.state.db
    st = await load_state(request)
    bps = normalize_blueprints(await db.kv_get("blueprints", []))
    systems = [{"star": r["star"], "data": json.loads(r["data"])} for r in await db.fetchall("SELECT star, data FROM systems")]
    sugg = cmdspec.build_suggestions(st, bps, systems, await db.kv_get("stars", {}) or {}, here)
    if here:
        local = await system_targets(db, star_of(here))
        known = {t["code"]: t for t in local["targets"]}
        codes = {o["value"] for o in sugg["locations"]}
        extra = [{"value": t["code"], "label": t["category"]} for t in local["targets"] if t["code"] not in codes]
        sugg["locations"] = extra + sugg["locations"]
        for o in sugg["locations"]:
            t = known.get(o["value"])
            if t:
                o["label"] = f"{t['category']}" + (f" · {t['label']}" if t.get("label") else "")
        sugg["locations"].sort(key=lambda o: (o["value"] not in known, o["value"]))
        sugg["system"] = local
    return sugg


async def _device(request: Request, code: str) -> dict:
    return next((d for d in (await load_state(request))["devices"] if d.get("device_code") == code), {})


CHAIN_ROWS = 3
MOVE_COMMANDS = {"travel"}


async def chain_context(request: Request, traveller: str | None, replicant: str | None = None) -> dict:
    """Device choices for the "then, on arrival" rows: the traveller, what it carries, what's nearby, the rest."""
    st = await load_state(request)
    devs = st["devices"]
    me = next((d for d in devs if d.get("device_code") == traveller), {})
    here = me.get("location")
    carried: list[dict] = []
    if replicant:
        rep = st["replicants"].get(replicant) or {}
        carried = [s for s in rep.get("stowed_devices") or [] if s.get("device_code")]
        here = here or rep.get("location") or rep.get("current_location")
    carried_codes = {c["device_code"] for c in carried}
    groups = [("This " + ("replicant's vessel" if replicant else "device"),
               [{"value": "__self__", "label": f"{f_human(me.get('device_type') or 'vessel')} {traveller or ''}".strip()}])]
    if carried:
        groups.append(("Carried", [{"value": c["device_code"], "label": f"{f_human(c.get('device_type'))} {c['device_code']}"} for c in carried]))
    near = [d for d in devs if d.get("location") == here and d.get("device_code") not in carried_codes | {traveller}]
    if near:
        groups.append((f"At {here}", [{"value": d["device_code"], "label": f"{f_human(d.get('device_type'))} {d['device_code']} · {d.get('status')}"} for d in near]))
    rest = [d for d in devs if d.get("location") != here and d.get("device_code") not in carried_codes | {traveller}]
    if rest:
        groups.append(("Elsewhere", [{"value": d["device_code"], "label": f"{f_human(d.get('device_type'))} {d['device_code']} @ {d.get('location')}"} for d in rest]))
    return {"chain_groups": groups, "chain_commands": auto.CHAIN_COMMANDS, "chain_rows": CHAIN_ROWS,
            "chain_replicant": replicant}


def parse_chain(form, traveller: str | None, replicant: str | None) -> list[dict]:
    steps = []
    for i in range(CHAIN_ROWS):
        cmd = (form.get(f"then_command_{i}") or "").strip()
        if not cmd:
            continue
        dev = (form.get(f"then_device_{i}") or "__self__").strip()
        dev = traveller if dev == "__self__" else dev
        if not dev and cmd != "system_scan":
            raise ValueError("pick a device for each follow-up")
        steps.append(auto.chain_step(dev, cmd, form.get(f"then_arg_{i}"), replicant))
    return steps


async def start_chain(request: Request, user: str, title: str, first: dict, followups: list[dict], device: str | None) -> HTMLResponse:
    eng = request.app.state.worker.automations
    async with eng.lock:
        job = await eng.create_job("chain", title, device, [first, *followups], {"by": user}, force=True)
    job = await eng._get(job["id"]) if job else None
    return partial(request, "partials/chain_result.html", job=job)


@router.get("/devices/{code}/command-form", response_class=HTMLResponse)
async def device_command_form(request: Request, code: str, command: str = "", rid: str = "", user: str = Depends(current_user)):
    dev = await _device(request, code)
    if not command:
        return HTMLResponse('<p class="muted small">Pick a command to see its fields.</p>')
    if command == "set_directive":
        names = await device_directives(request, dev)
        sugg = await suggestions(request, dev.get("location"))
        return partial(request, "partials/directive_picker.html", code=code, names=names,
                       fields=cmdspec.directive_fields(names[0]) if names else [],
                       sugg=sugg, sys_targets=sugg.get("system"), uid=f"d-{code}", self_code=code, rid=rid)
    fields = cmdspec.COMMANDS.get(command)
    chain = await chain_context(request, code) if command in MOVE_COMMANDS else {}
    if command == "collect_resources":
        chain["cargo"] = await cargo_context(request.app.state.db, request.app.state.api, dev,
                                             normalize_blueprints(await request.app.state.db.kv_get("blueprints", [])))
    sugg = await suggestions(request, dev.get("location"))
    return partial(request, "partials/command_form.html", code=code, command=command, fields=fields or [],
                   known=fields is not None, sugg=sugg, sys_targets=sugg.get("system"),
                   uid=f"c-{code}", self_code=code, directives=None, rid=rid, **chain)


@router.post("/devices/{code}/command", response_class=HTMLResponse)
async def device_command(request: Request, code: str, user: str = Depends(current_user)):
    form = await request.form()
    command = (form.get("command") or "").strip()

    def bad(msg: str) -> HTMLResponse:
        return partial(request, "partials/action_result.html", ok=False, status=400, error=msg,
                       label=command or "command", method="POST", path=f"/devices/{code}", response=None)

    if not command:
        return bad("Pick a command")
    if command == "set_directive":
        try:
            body = directive_body(form)
        except ValueError as e:
            return bad(str(e))
        return await run_action(request, user, "POST", f"/devices/{code}", body, f"{code} directive {body['directive']}")
    try:
        body = cmdspec.parse_fields(cmdspec.COMMANDS.get(command, []), form)
        extra = parse_json_field(form.get("args")) or {}
        if not isinstance(extra, dict):
            return bad("Extra arguments must be a JSON object")
    except cmdspec.FormError as e:
        return bad(str(e))
    except ValueError as e:
        return bad(f"Bad JSON: {e}")
    if command in MOVE_COMMANDS:
        try:
            followups = parse_chain(form, code, None)
        except ValueError as e:
            return bad(str(e))
        if followups:
            dest = str(body.get("destination") or "").upper()
            first = auto.step(f"{code} → {dest}", f"/devices/{code}", {"command": command, **body, **extra},
                              wait=["travel.arrived"], match={"destination": dest} if dest else None, critical=True)
            first["wait_device"] = code
            return await start_chain(request, user, f"{code} → {dest}, then {len(followups)} step(s)", first, followups, code)
    return await run_action(request, user, "POST", f"/devices/{code}", {"command": command, **body, **extra},
                            f"{command} on {code}")


async def carrier_context(request: Request, dev: dict) -> dict | None:
    """Rows for the Carrier card, or None if this device can't carry anything."""
    bps = normalize_blueprints(await request.app.state.db.kv_get("blueprints", []))
    if not carrier_mod.is_carrier(dev, bps):
        return None
    st = await load_state(request)
    code = dev.get("device_code")
    rep = next(((c, r) for c, r in st["replicants"].items() if r.get("hosted_device_code") == code), None)
    stowed = [s for s in (dev.get("stowed_devices") or []) if isinstance(s, dict) and s.get("device_code")]
    if not stowed and rep:
        stowed = [s for s in (rep[1].get("stowed_devices") or []) if isinstance(s, dict) and s.get("device_code")]
    rows = carrier_mod.carrier_rows(dev, stowed, st["devices"])
    cap = carrier_mod.stow_capacity(dev, bps)
    if "travel" in (dev.get("available_commands") or []):
        travel_path = f"/devices/{code}"
    elif rep:
        travel_path = f"/replicants/{rep[0]}/travel"
    else:
        travel_path = None
    return {"rows": rows, "capacity": cap, "carried": sum(1 for r in rows if r["carried"]),
            "travel_path": travel_path, "replicant": rep[0] if rep else None, "star": carrier_mod.star_of(dev.get("location"))}


@router.post("/devices/{code}/carrier", response_class=HTMLResponse)
async def device_carrier(request: Request, code: str, user: str = Depends(current_user)):
    form = await request.form()
    try:
        dev = {**await _device(request, code), **(await request.app.state.api.get(f"/devices/{code}") or {})}
    except ApiError:
        dev = await _device(request, code)
    ctx = await carrier_context(request, dev)
    if not ctx:
        return HTMLResponse('<div class="result err">This device can\'t carry anything.</div>')
    launch, stow, recall = set(form.getlist("launch")), set(form.getlist("stow")), set(form.getlist("recall"))
    if not (launch or stow or recall):
        return HTMLResponse('<div class="result err">Tick at least one launch, stow or recall box.</div>')
    needs_move = any(r["code"] in stow and not r["same_loc"] and not r["mobile"] for r in ctx["rows"])
    if needs_move and not ctx["travel_path"]:
        return HTMLResponse('<div class="result err">Some picked devices can\'t travel, and this vessel has no travel command to go and get them.</div>')
    if ctx["capacity"] is not None:
        after = ctx["carried"] - len(launch) + len([c for c in stow if c not in launch])
        if after > ctx["capacity"]:
            return HTMLResponse(f'<div class="result err">That would put {after} devices in a vessel that holds {int(ctx["capacity"])}.</div>')
    steps = carrier_mod.plan(code, dev.get("location"), ctx["rows"], launch, stow, recall,
                             ctx["travel_path"] or f"/devices/{code}", return_after=form.get("return_after") == "on")
    if not steps:
        return HTMLResponse('<div class="result err">Nothing to do for that selection.</div>')
    bits = [f"{len(launch)} launch" if launch else "", f"{len(stow)} pick-up" if stow else "", f"{len(recall - stow)} recall" if recall - stow else ""]
    return await start_chain(request, user, f"{code}: " + ", ".join(b for b in bits if b), steps[0], steps[1:], code)


@router.post("/devices/{code}/collect-all", response_class=HTMLResponse)
async def device_collect_all(request: Request, code: str, user: str = Depends(current_user)):
    """Load as much as fits from the stock where the transport is, in proportion to what's there."""
    dev = await _device(request, code)
    c = await cargo_context(request.app.state.db, request.app.state.api, dev,
                            normalize_blueprints(await request.app.state.db.kv_get("blueprints", [])))
    if not c["plan"]:
        why = ("the hold is full" if c["free"] == 0 else "nothing is stockpiled here" if not c["available"]
               else "the hold capacity is unknown — enter amounts by hand")
        return partial(request, "partials/action_result.html", ok=False, status=0, error=f"Nothing to collect: {why}.",
                       label="collect all", method="POST", path=f"/devices/{code}", response=None)
    return await run_action(request, user, "POST", f"/devices/{code}", {"command": "collect_resources", "resources": c["plan"]},
                            f"collect all ({int(sum(c['plan'].values()))} units) on {code}")


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
    blueprints = normalize_blueprints(await request.app.state.db.kv_get("blueprints", []))
    devices = [d for d in st["devices"] if d.get("replicant_code") == code]
    slingshots = sorted((d for d in st["devices"] if "slingshot" in (d.get("device_type") or "")), key=lambda d: d.get("location") or "")
    by = {d.get("device_code"): d for d in st["devices"]}
    stowed = await request.app.state.db.kv_get("stowed_map", {}) or {}
    sl_locs = {d.get("location") for d in slingshots if d.get("location")}
    matrices = sorted(({**d, "_at": where_is(d, by, stowed)} for d in st["devices"]
                       if "matrix" in (d.get("device_type") or "") and "container" not in (d.get("device_type") or "")),
                      key=lambda d: (d.get("device_type") != "empty_replicant_matrix", d["_at"] or ""))
    matrices = [m for m in matrices if m["_at"] in sl_locs]   # linking needs the matrix at the slingshot
    return await page(request, user, "replicant.html", "fleet", rep=rep, code=code, nearby=nearby,
                      blueprints=blueprints, devices=devices, slingshots=slingshots, matrices=matrices,
                      SLINGSHOT_MIN=SLINGSHOT_MIN_CAPACITY, timers=[t for t in await active_timers(request)
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
        host = ((await load_state(request))["replicants"].get(code) or {}).get("hosted_device_code")
        return partial(request, "partials/route_preview.html", code=code, destination=destination, r=resp,
                       legs=resp.get("route") if isinstance(resp.get("route"), list) else [resp.get("route")] if resp.get("route") else [],
                       sugg=await suggestions(request, destination), uid=f"r-{code}", **await chain_context(request, host, code))
    form = await request.form()
    host = ((await load_state(request))["replicants"].get(code) or {}).get("hosted_device_code")
    try:
        followups = parse_chain(form, host, code)
    except ValueError as e:
        return partial(request, "partials/action_result.html", ok=False, status=400, error=str(e),
                       label="travel", method="POST", path=f"/replicants/{code}/travel", response=None)
    if followups:
        first = auto.step(f"{code} → {destination}", f"/replicants/{code}/travel", {"destination": destination},
                          wait=["travel.arrived"], match={"destination": destination}, critical=True)
        first["wait_device"] = host  # the arrival event is reported for the host vessel
        return await start_chain(request, user, f"{code} → {destination}, then {len(followups)} step(s)", first, followups, host)
    return await run_action(request, user, "POST", f"/replicants/{code}/travel", {"destination": destination},
                            f"{code} → {destination}")


SLINGSHOT_MIN_CAPACITY = 80.0   # docs: a slingshot needs ≥80 % capacity and drops to 5 % per use


def where_is(d: dict, by: dict[str, dict], stowed_map: dict[str, list[str]] | None = None) -> str | None:
    """A device's location, or its carrier's when it's stowed / attached."""
    if d.get("location"):
        return d["location"]
    host = d.get("stowed_in_device_code") or d.get("attached_to_device_code") or next(
        (v for v, kids in (stowed_map or {}).items() if d.get("device_code") in kids), None)
    return (by.get(host) or {}).get("location") if host else None


@router.post("/replicants/{code}/slingshot", response_class=HTMLResponse)
async def replicant_slingshot(request: Request, code: str, slingshot: str = Form(...), user: str = Depends(current_user)):
    """Fire a slingshot (docs /docs/ftl-slingshots/): the replicant must be at a deployed slingshot, which must be linked
    to an empty matrix (wherever that matrix is now) and have ≥80 % capacity. POST /replicants/{code}/teleport
    {"target": <slingshot>} sends the replicant's consciousness to that matrix; the slingshot drops to 5 %."""
    slingshot = slingshot.strip().upper()
    st = await load_state(request)
    sl = next((d for d in st["devices"] if d.get("device_code") == slingshot), {})
    rep = st["replicants"].get(code) or {}
    here = rep.get("location") or rep.get("current_location")
    path = f"/replicants/{code}/teleport"

    def refuse(msg: str) -> HTMLResponse:
        return render_action(request, {"label": f"slingshot {slingshot}", "method": "POST", "path": path, "ok": False,
                                       "status": 400, "error": msg, "response": None})
    if sl and here and sl.get("location") != here:
        return refuse(f"{slingshot} is at {sl.get('location') or sl.get('status')}, the replicant is at {here} — "
                      "go to the slingshot first")
    cap = sl.get("operational_capacity")
    if cap is not None and float(cap) < SLINGSHOT_MIN_CAPACITY:
        return refuse(f"{slingshot} is at {float(cap):.0f}% — it needs at least {SLINGSHOT_MIN_CAPACITY:.0f}% "
                      "(a maintenance drone recharges it)")
    if sl and not sl.get("linked_device"):
        return refuse(f"{slingshot} isn't linked to a matrix yet")
    return await run_action(request, user, "POST", path, {"target": slingshot},
                            f"{code}: slingshot via {slingshot} → {sl.get('linked_device') or '?'}")


@router.post("/slingshots/{slingshot}/link", response_class=HTMLResponse)
async def slingshot_link(request: Request, slingshot: str, matrix: str = Form(...), user: str = Depends(current_user)):
    """Link a slingshot to an empty replicant matrix at the same location (docs: the matrix is stowed in a vessel, which
    then carries it to the destination; the link holds wherever it goes)."""
    slingshot, matrix = slingshot.strip().upper(), matrix.strip().upper()
    st = await load_state(request)
    by = {d.get("device_code"): d for d in st["devices"]}
    stowed = await request.app.state.db.kv_get("stowed_map", {}) or {}
    sl, mx = by.get(slingshot) or {}, by.get(matrix) or {}
    if sl and mx and where_is(mx, by, stowed) != sl.get("location"):
        return render_action(request, {"label": f"link {slingshot} → {matrix}", "method": "PATCH", "path": f"/devices/{slingshot}",
                                       "ok": False, "status": 400, "response": None,
                                       "error": f"{matrix} is at {where_is(mx, by, stowed) or '?'}, {slingshot} at "
                                                f"{sl.get('location') or '?'} — they must be together to link"})
    return await run_action(request, user, "PATCH", f"/devices/{slingshot}", {"configuration": {"linked_device": matrix}},
                            f"link slingshot {slingshot} → {matrix}")


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
                          quantity: int = Form(1), user: str = Depends(current_user)):
    if command:  # cancel the current print (vessels have no queue)
        return await run_action(request, user, "POST", f"/replicants/{code}/print", {"command": command},
                                f"{code} print {command}")
    return await queue_print(request, user, "replicant", code, device_type, quantity)


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
    db = request.app.state.db
    for s, p in rows:
        if s:
            r = await system_resources(db, s)
            p["mineable"], p["salvage"], p["unknown_sites"] = r["mineable"], r["salvageable"], r["unknown_sites"]
            p["top"] = sorted(((k, t["sites"] + t["salvage"]) for k, t in r["totals"].items() if t["sites"] + t["salvage"]),
                              key=lambda kv: -kv[1])[:3]
    return await page(request, user, "systems.html", "systems", rows=[(s, p) for s, p in rows if s], known=known)


def _angle(code: str) -> float:
    return int(hashlib.md5(code.encode()).hexdigest()[:6], 16) % 360 * math.pi / 180


def build_system_view(star: str, scan: dict, devices: list[dict], inventory: list[dict],
                      places: list[dict] | None = None, res: dict | None = None) -> dict:
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
    # every other known location: resource sites, salvage, Lagrange points, objects, outer system
    qty = {x["code"]: x for x in ((res or {}).get("sites", []) + (res or {}).get("salvage", []))}
    hidden = (res or {}).get("hidden") or set()
    drawn = {p.get("designation") for p in planets} | {star}
    shapes["places"] = []
    for t in places or []:
        code = t["code"]
        if code in drawn or code in hidden or t["category"] in ("star", "planet", "belt", "other"):
            continue
        xy = loc_xy(code)
        if not xy:
            continue
        q = qty.get(code) or {}
        rest = code[len(star) + 1:]
        short = rest.split("-", 2)[-1] if t["category"] in ("site", "salvage") else rest.split("-")[-1] if t["category"] == "lagrange" else rest
        shapes["places"].append({"code": code, "category": t["category"], "x": xy[0], "y": xy[1], "label": short,
                                 "note": t.get("note") or "", "total": q.get("total"), "amounts": q.get("amounts") or {},
                                 "depleted": q.get("depleted", False)})
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
    sys_t = await system_targets(db, star)
    res = await system_resources(db, star)
    view = build_system_view(star, scan, st["devices"], st["inventory"], sys_t["targets"], res)
    reps = [r for r in st["replicants"].values() if star_of(r.get("location") or r.get("current_location")) == star]
    game_locs = {k: v for k, v in (st["locations"] or {}).items() if star_of(k) == star}
    qty = {x["code"]: x for x in res["sites_shown"] + res["salvage_shown"]}
    return await page(request, user, "system.html", "systems", star=star, scan=scan, view=view, err=err,
                      updated=row["updated_at"] if row else None, reps=reps, res=res, sys_t=sys_t,
                      CATEGORY_LABEL=CATEGORY_LABEL, game_locs=game_locs, qty=qty,
                      viability=[v for v in await request.app.state.worker.automations.viability_report() if v["star"] == star])


@router.post("/systems/{star}/resources/refresh", response_class=HTMLResponse)
async def system_resources_refresh(request: Request, star: str, user: str = Depends(current_user)):
    """Re-read the system's belts (open mining sites) and the bodies its salvage sits on (at most 15 GETs).
    Belts come from the stored system scan; without one, GET /locations/<STAR> (same body as a scan) supplies them,
    plus any belt our devices or events have been at. Salvage codes (X-1-SAL-2) aren't locations: read the body (X-1)."""
    from .salvage import body_of
    star = star.upper()
    db, api = request.app.state.db, request.app.state.api
    row = await db.fetchone("SELECT data FROM systems WHERE star=?", (star,))
    scan = json.loads(row["data"]) if row else {}
    belts = [b.get("designation") for b in ((scan.get("asteroid_belt") or {}).get("belts")) or [] if b.get("designation")]
    failed: list[str] = []
    if not belts:
        try:
            sysd = await api.get(f"/locations/{star}")
            if isinstance(sysd, dict) and (sysd.get("asteroid_belt") or sysd.get("planets")):
                belts = [b.get("designation") for b in ((sysd.get("asteroid_belt") or {}).get("belts")) or [] if b.get("designation")]
                if not row:
                    await db.execute("INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
                                     (star, json.dumps(sysd), now_iso()))
        except ApiError as e:
            failed.append(f"{star}: {e.message}")
    st = await load_state(request)
    pat = re.compile(rf"\b{re.escape(star)}-BELT-\d+\b")
    for d in st["devices"]:
        belts += pat.findall(d.get("location") or "")
    for r in await db.fetchall("SELECT DISTINCT location FROM events WHERE location LIKE ?", (f"{star}-BELT-%",)):
        belts += pat.findall(r["location"] or "")
    belts = list(dict.fromkeys(b.upper() for b in belts if "-SITE-" not in b))
    res = await system_resources(db, star)
    bodies = list(dict.fromkeys(body_of(x["code"]) for x in res["salvage"] if not x["depleted"]))
    read_belts, read_bodies, open_sites = 0, 0, 0
    for code in (belts + [c for c in bodies if c not in belts])[:15]:
        try:
            data = await api.get(f"/locations/{code}")
        except ApiError as e:
            failed.append(f"{code}: {e.message}")
            continue
        if isinstance(data, dict) and data:
            await db.kv_set(f"loc:{code.upper()}", data)
            if code in belts:
                read_belts += 1
                open_sites += len(data.get("resource_sites") or [])
            else:
                read_bodies += 1
    bits = []
    if read_belts:
        bits.append(f"read {read_belts} belt(s): {open_sites} open mining site(s)")
    elif not belts:
        bits.append("no belts known for this system — run a system scan here first")
    if read_bodies:
        bits.append(f"read {read_bodies} salvage body/bodies")
    msg = "; ".join(bits)
    if read_belts and not open_sites:
        msg += (". Belts only list sites that are open: a survey drone's <code>search</code> at the belt (or an AMI survey "
                "controller on <code>belt_search</code>) opens one, and it stays open while the drone tracks it.")
    err = f'<div class="lv-alert small">Could not read: {"; ".join(failed[:4])}</div>' if failed else ""
    reload = '<script>setTimeout(function(){location.reload()}, 2500)</script>' if (read_belts or read_bodies) else ""
    return HTMLResponse(f'<div class="small">{msg}</div>{err}{reload}')


@router.get("/locations/{code}", response_class=HTMLResponse)
async def location_detail(request: Request, code: str, user: str = Depends(current_user)):
    try:
        data = await request.app.state.api.get(f"/locations/{code}")
        err = None
        if isinstance(data, dict) and data:
            await request.app.state.db.kv_set(f"loc:{code.upper()}", data)
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
    """Everything that can print — autofactories first (they queue and wait for materials), then vessels."""
    out = []
    for d in state["devices"]:
        if "print" in (d.get("features") or []) and d.get("device_type") != "heaven_vessel":
            out.append({"kind": "device", "code": d["device_code"], "device": d["device_code"],
                        "name": f"{f_human(d.get('device_type'))} {d['device_code']}", "location": d.get("location"),
                        "autofactory": "autofactory" in (d.get("device_type") or "")})
    out.sort(key=lambda p: (not p["autofactory"], p["name"]))
    for code, r in state["replicants"].items():
        out.append({"kind": "replicant", "code": code, "name": f"{r.get('name') or code} (vessel)",
                    "host": r.get("hosted_device_code"), "device": r.get("hosted_device_code"), "autofactory": False,
                    "location": r.get("location") or r.get("current_location")})
    return out


def affordable(cost: Any, items: Any) -> int:
    have = as_amounts(items)
    counts = [int(have.get(k, 0.0) // v) for k, v in as_amounts(cost).items() if v > 0]
    return min(counts) if counts else 0


@router.get("/blueprints", response_class=HTMLResponse)
async def blueprints(request: Request, q: str = "", user: str = Depends(current_user)):
    db, worker = request.app.state.db, request.app.state.worker
    refresh_msg = None
    # Opening the page re-reads the list if it is more than a minute old (one cheap GET).
    last = parse_ts(await db.kv_updated("blueprints"))
    if request.app.state.api.configured and (not last or datetime.now(timezone.utc) - last > timedelta(minutes=1)):
        try:
            added = await worker.sync_blueprints()
            if added:
                refresh_msg = "New: " + ", ".join(f_human(t) for t in added)
        except ApiError as e:
            refresh_msg = f"Could not refresh from the game: {e.message}"
    st = await load_state(request)
    bps = normalize_blueprints(await db.kv_get("blueprints", []))
    recent_cut = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    recent = {u["device_type"] for u in await db.kv_get("blueprint_unlocks", []) or [] if u.get("at", "") >= recent_cut}
    if q:
        bps = [b for b in bps if q.lower() in json.dumps(b).lower()]
    inv = {i.get("location"): i.get("items") or {} for i in st["inventory"]}
    prs = printers(st)
    for b in bps:
        b["_afford"] = {p["code"]: affordable(b["resources"], inv.get(p["location"], {})) for p in prs}
    return await page(request, user, "blueprints.html", "blueprints", blueprints=sorted(bps, key=lambda b: b.get("device_type", "")),
                      printers=prs, inv=inv, q=q, recent=recent, refresh_msg=refresh_msg,
                      synced=await db.kv_updated("blueprints"))


@router.post("/blueprints/refresh", response_class=HTMLResponse)
async def blueprints_refresh(request: Request, user: str = Depends(current_user)):
    try:
        added = await request.app.state.worker.sync_blueprints()
    except ApiError as e:
        return HTMLResponse(f'<span class="lv-alert">Refresh failed: {e.message}</span>')
    return HTMLResponse("", headers={"HX-Refresh": "true"}) if added else HTMLResponse('<span class="muted">Up to date.</span>')


async def read_plan(request: Request, form) -> dict:
    """Quantities + chosen printer -> needs, stock at the printer, shortfall, and who could gather it."""
    st = await load_state(request)
    bps = {b["device_type"]: b for b in normalize_blueprints(await request.app.state.db.kv_get("blueprints", []))}
    prs = printers(st)
    pkey = form.get("printer") or ""
    printer = next((p for p in prs if f"{p['kind']}:{p['code']}" == pkey), prs[0] if prs else None)
    location = (form.get("location") if form.get("location") and not pkey else (printer or {}).get("location")) or ""
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
        bp = bps.get(key[4:])
        if n <= 0 or not bp:
            continue
        for r, v in bp["resources"].items():
            need[r] += v * n
        total_time += (bp.get("print_time") or 0) * n
        lines.append((key[4:], n))
    rows = [{"resource": r, "need": need[r], "have": inv.get(r, 0.0),
             "short": max(0.0, need[r] - inv.get(r, 0.0))} for r in sorted(need)]
    star = star_of(location)
    minings = production.controllers_in(st["devices"], star, "mining")
    transports = production.controllers_in(st["devices"], star, "transport")
    mining = next((m for m in minings if m["device_code"] == form.get("mining")), minings[0] if minings else None)
    transport = next((t for t in transports if t["device_code"] == form.get("transport")), transports[0] if transports else None)
    planned = form.get("_planned") == "1"
    # several autofactories at the printer's stockpile: spread the lines over them evenly by print time
    peers = [d for d in st["devices"] if printer and printer.get("autofactory") and d.get("location") == location
             and "autofactory" in (d.get("device_type") or "") and "enqueue_print" in (d.get("available_commands") or [])]
    split_on = (form.get("split") == "on") if planned else True
    parts, split_time = [], None
    if len(peers) > 1 and lines:
        load = {d["device_code"]: printqueue.load_seconds(d, bps) for d in peers}
        start = dict(load)
        for t, n in sorted(lines, key=lambda tn: -float((bps.get(tn[0]) or {}).get("print_time") or 0)):  # longest first
            parts += [(f["device_code"], t, k) for f, k in printqueue.split(peers, t, n, bps, load)]
        split_time = max(load[c] - start[c] for c in load)
    return {"rows": rows, "lines": lines, "location": location, "total_time": total_time, "printer": printer,
            "peers": peers, "split_on": split_on, "parts": parts, "split_time": split_time,
            "gather_on": (form.get("gather") == "on") if planned else True,
            "deliver_on": (form.get("deliver") == "on") if planned else True,
            "short": {r["resource"]: r["short"] for r in rows if r["short"] > 0}, "star": star,
            "minings": minings, "transports": transports, "mining": mining, "transport": transport}


@router.post("/blueprints/plan", response_class=HTMLResponse)
async def blueprint_plan(request: Request, user: str = Depends(current_user)):
    return partial(request, "partials/plan.html", **await read_plan(request, await request.form()))


@router.post("/blueprints/queue-plan", response_class=HTMLResponse)
async def blueprint_queue_plan(request: Request, user: str = Depends(current_user)):
    form = await request.form()
    p = await read_plan(request, form)
    printer = p["printer"]
    if not printer or not printer.get("device"):
        return HTMLResponse('<div class="result err">No printer to queue on.</div>')
    if not p["lines"]:
        return HTMLResponse('<div class="result err">Enter at least one quantity.</div>')
    vessel = printer["kind"] == "replicant"
    if vessel and sum(n for _, n in p["lines"]) > 1:
        return HTMLResponse('<div class="result err">Vessel printers have no queue and print one device at a time. '
                            'Plan 1 device, or pick an autofactory.</div>')
    gather = form.get("gather") == "on"
    assign = p["parts"] if p["parts"] and form.get("split") == "on" else None
    steps = production.production_steps(printer["device"], printer["name"], p["lines"], p["short"],
                                        p["mining"] if gather else None,
                                        p["transport"] if form.get("deliver") == "on" else None,
                                        p["location"], gather, vessel_replicant=printer["code"] if vessel else None,
                                        assign=assign)
    on = f"{len({c for c, _, _ in assign})} autofactories at {p['location']}" if assign else printer["name"]
    title = f"production: {', '.join(f'{n}× {t}' for t, n in p['lines'])} on {on}"
    if gather and p["short"] and p["mining"]:
        title += f" + gather shortfall with {p['mining']['device_code']}"
    return await start_chain(request, user, title, steps[0], steps[1:], printer["device"])


async def queue_print(request: Request, user: str, kind: str, code: str, device_type: str, quantity: int) -> HTMLResponse:
    """Add a print to a printer.

    Autofactories take `enqueue_print` and hold a queue. Heaven vessels have no queue: one print at a
    time through `POST /replicants/{code}/print`, which answers "Printer is busy" while one is running.
    """
    quantity = max(1, quantity)
    if kind != "replicant":
        body = {"command": "enqueue_print", "device_type": device_type, "quantity": quantity}
        out = await call_action(request, user, "POST", f"/devices/{code}", body, f"enqueue {quantity}× {device_type} on {code}")
        resp = render_action(request, out)
        if out["ok"]:
            resp.headers["HX-Trigger"] = "pq-refresh"  # any queue panel on the page re-reads
        return resp
    out = await call_action(request, user, "POST", f"/replicants/{code}/print", {"device_type": device_type},
                            f"print {device_type} on {code}")
    note = None
    if not out["ok"] and "busy" in (out["error"] or "").lower():
        note = ("Vessel printers don't have a queue: wait for the current print to finish, "
                "or queue it on an autofactory.")
    elif out["ok"] and quantity > 1:
        note = f"Vessel printers take one print at a time, so only 1 of {quantity} was started."
    return render_action(request, out, note)


@router.post("/blueprints/print", response_class=HTMLResponse)
async def blueprint_print(request: Request, printer: str = Form(...), device_type: str = Form(...),
                          quantity: int = Form(1), user: str = Depends(current_user)):
    kind, _, code = printer.partition(":")
    return await queue_print(request, user, kind, code, device_type, quantity)


# --- AMI ---------------------------------------------------------------------------------------
DIRECTIVES = cmdspec.DIRECTIVES
controller_kind = cmdspec.controller_kind


async def device_directives(request: Request, dev: dict) -> list[str]:
    bps = normalize_blueprints(await request.app.state.db.kv_get("blueprints", []))
    return cmdspec.directives_for(dev, bps)


@router.get("/ami", response_class=HTMLResponse)
async def ami(request: Request, user: str = Depends(current_user)):
    st = await load_state(request)
    db = request.app.state.db
    ctrls = []
    for d in st["devices"]:
        if "ami" not in (d.get("features") or []) and "set_directive" not in (d.get("available_commands") or []):
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
        names = await device_directives(request, d)
        csugg = await suggestions(request, d.get("location"))
        ctrls.append({"d": d, "kind": kind, "digest": row_event(last) if last else None, "sugg": csugg,
                      "sys": csugg.get("system"),
                      "directive": row_event(last_dir) if last_dir else None,
                      "directives": names, "first_fields": cmdspec.directive_fields(names[0]) if names else [],
                      "candidates": candidates})
    sugg = await suggestions(request, None)
    return await page(request, user, "ami.html", "ami", ctrls=ctrls, sugg=sugg, **await ami_schedules_ctx(request, st))


async def refresh_targets(request: Request, star: str) -> tuple[int, str | None]:
    """Re-read the system scan and every belt's detail (for resource sites). Returns (#details, error)."""
    worker, api, db = request.app.state.worker, request.app.state.api, request.app.state.db
    try:
        scan = await worker.refresh_system(star)
    except ApiError as e:
        return 0, e.message
    n = 0
    for b in ((scan.get("asteroid_belt") or {}).get("belts")) or []:
        code = b.get("designation")
        if not code:
            continue
        try:
            detail = await api.get(f"/locations/{code}")
            await db.kv_set(f"loc:{code}", detail or {})
            n += 1
        except ApiError:
            pass
    return n, None


@router.post("/ami/{code}/refresh-targets", response_class=HTMLResponse)
async def ami_refresh_targets(request: Request, code: str, user: str = Depends(current_user)):
    dev = await _device(request, code)
    star = star_of(dev.get("location"))
    if not star:
        return HTMLResponse('<span class="lv-alert small">Unknown location.</span>')
    n, err = await refresh_targets(request, star)
    if err:
        return HTMLResponse(f'<span class="lv-alert small">Could not read {star}: {err}</span>')
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.get("/ami/{code}/directive-form", response_class=HTMLResponse)
async def ami_directive_form(request: Request, code: str, directive: str = "", user: str = Depends(current_user)):
    dev = await _device(request, code)
    sugg = await suggestions(request, dev.get("location"))
    return partial(request, "partials/fields.html", fields=cmdspec.directive_fields(directive),
                   sugg=sugg, sys_targets=sugg.get("system"), uid=f"d-{code}", self_code=code)


def directive_body(form) -> dict:
    """{"command": "set_directive", "directive": …, "configuration": {…}} from the directive picker's inputs."""
    directive = (form.get("directive") or "").strip()
    if not directive:
        raise cmdspec.FormError("Pick a directive")
    cfg = cmdspec.parse_fields(cmdspec.directive_fields(directive), form)
    extra = parse_json_field(form.get("configuration")) or {}
    if not isinstance(extra, dict):
        raise ValueError("configuration must be a JSON object")
    cfg.update(extra)
    if directive == "gather_salvage" and isinstance(cfg.get("location"), str):
        from .salvage import body_of
        cfg["location"] = body_of(cfg["location"])  # the game takes the body the salvage is at, not the -SAL- code
    body = {"command": "set_directive", "directive": directive}
    if cfg:
        body["configuration"] = cfg
    return body


@router.post("/ami/{code}/adopt", response_class=HTMLResponse)
async def ami_adopt(request: Request, code: str, user: str = Depends(current_user)):
    form = await request.form()
    devs = form.getlist("devices")
    cmd = form.get("command") or "adopt"
    return await run_action(request, user, "POST", f"/devices/{code}", {"command": cmd, "devices": devs},
                            f"{cmd} {len(devs)} device(s) on {code}")


@router.post("/ami/{code}/directive", response_class=HTMLResponse)
async def ami_directive(request: Request, code: str, user: str = Depends(current_user)):
    form = await request.form()
    try:
        body = directive_body(form)
    except ValueError as e:  # FormError is a ValueError
        return partial(request, "partials/action_result.html", ok=False, status=400, error=str(e),
                       label="set_directive", method="POST", path=f"/devices/{code}", response=None)
    return await run_action(request, user, "POST", f"/devices/{code}", body, f"{code} directive {body['directive']}")


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
    runs = list(reversed(await db.kv_get("server_runs", []) or []))
    return await page(request, user, "account.html", "account", runs=runs, changes=appver.CHANGES,
                      account=await db.kv_get("account", {}),
                      achievements=await db.kv_get("achievements", {}), actions=actions, sync=sync,
                      hub_listeners=request.app.state.hub.listeners)


# --- diagnostics: live snapshot + mining diagnosis ----------------------------------------------------
async def _snapshot_run(app, stars: set[str] | None) -> None:
    from . import snapshot as snapmod
    db = app.state.db

    async def progress(done: int, total: int, path: str) -> None:
        await db.kv_set("snapshot_status", {"state": "running", "done": done, "total": total, "path": path, "at": now_iso()})
    try:
        snap = await snapmod.capture(app.state.api, db, app.state.worker.automations, stars, progress=progress)
        await db.kv_set("snapshot_last", snap)
        await db.kv_set("snapshot_status", {"state": "done", "done": snap["requests"], "total": snap["requests"], "at": now_iso()})
    except Exception as e:  # report, never crash the app
        log.exception("snapshot failed")
        import traceback
        tb = traceback.extract_tb(e.__traceback__)[-1]
        await db.kv_set("snapshot_status", {"state": "failed", "at": now_iso(),
                                            "error": f"{type(e).__name__}: {e} ({tb.filename.rsplit('/', 1)[-1]} line {tb.lineno})"})


@router.get("/diagnostics", response_class=HTMLResponse)
async def diagnostics(request: Request, user: str = Depends(current_user)):
    db = request.app.state.db
    snap = await db.kv_get("snapshot_last", None)
    status = await db.kv_get("snapshot_status", {}) or {}
    tmpl = "partials/diagnostics_body.html" if request.headers.get("hx-request") else "diagnostics.html"
    ctx = {"snap": snap, "diag": (snap or {}).get("diagnosis"), "status": status,
           "viability": await request.app.state.worker.automations.viability_report()}
    if tmpl.startswith("partials"):
        return partial(request, tmpl, **ctx)
    return await page(request, user, tmpl, "diagnostics", **ctx)


@router.post("/diagnostics/snapshot", response_class=HTMLResponse)
async def diagnostics_snapshot(request: Request, user: str = Depends(current_user)):
    form = await request.form()
    stars = {x.strip().upper() for x in re.split(r"[,\s]+", form.get("stars") or "") if x.strip()} or None
    db = request.app.state.db
    st = await db.kv_get("snapshot_status", {}) or {}
    t = getattr(request.app.state, "snapshot_task", None)
    if st.get("state") == "running" and t and not t.done():
        return HTMLResponse('<div class="result err">A snapshot is already running.</div>')
    await db.kv_set("snapshot_status", {"state": "running", "done": 0, "total": 60, "at": now_iso()})
    request.app.state.snapshot_task = asyncio.create_task(_snapshot_run(request.app, stars))
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.get("/diagnostics/snapshot.json")
async def diagnostics_download(request: Request, user: str = Depends(current_user)):
    snap = await request.app.state.db.kv_get("snapshot_last", None)
    if not snap:
        return HTMLResponse("No snapshot yet.", status_code=404)
    name = f"replicant-snapshot-{(snap.get('captured_at') or 'x')[:19].replace(':', '')}.json"
    return Response(json.dumps(snap, indent=1, default=str), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


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


# =====================================================================================
# automations
# =====================================================================================


# Where each rule's settings live: the page it works on (it loads them from /automations/rules-panel).
RULE_HOME = {
    "scan_on_arrival": ("Map › Systems", "/systems"), "auto_survey": ("Map › Systems", "/systems"),
    "deploy_beacon": ("Map › Systems", "/systems"),
    "restart_idle_miners": ("Map › Systems", "/systems"), "reopen_sites": ("Map › Systems", "/systems"),
    "salvage_when_depleted": ("Map › Systems", "/systems"), "belt_viability": ("Map › Systems", "/systems"),
    "visitor_alerts": ("Map › Traffic", "/traffic"), "civ_beacons": ("Map › Traffic", "/traffic"),
    "asteroid_defence": ("Map › Defence", "/defence"), "maintenance": ("Map › Upkeep", "/maintenance"),
    "ami_schedules": ("Devices › AMI", "/ami"), "loadouts": ("Fleets › Home fleets", "/loadouts"),
    "consolidate": ("Economy › Blueprints", "/blueprints"), "contracts": ("Economy › Contracts", "/game-events"),
}


@router.get("/automations/rules-panel", response_class=HTMLResponse)
async def automations_rules_panel(request: Request, ids: str = "", open: str = "", user: str = Depends(current_user)):
    """The settings cards for some rules, for the page they work on."""
    want = [i for i in ids.split(",") if i in auto.RULES_BY_ID]
    rules = [r for r in auto.RULES if r.id in want]
    kw: dict = {}
    if "auto_survey" in want:
        st = await load_state(request)
        kw = {"vessels": [d for d in st["devices"] if "vessel" in (d.get("device_type") or "") or d.get("stow_capacity")],
              "surveyed": len(await request.app.state.db.kv_get("surveyed", {}) or {})}
    return partial(request, "partials/rule_cards.html", rules=rules, s=await request.app.state.worker.automations.settings(),
                   open=bool(open), **kw)


@router.post("/automations/rules/{rule_id}/toggle", response_class=HTMLResponse)
async def automations_rule_toggle(request: Request, rule_id: str, user: str = Depends(current_user)):
    """Switch a rule on or off from the overview, leaving its options alone."""
    if rule_id not in auto.RULES_BY_ID:
        return HTMLResponse("unknown rule", status_code=404)
    eng = request.app.state.worker.automations
    s = await eng.settings()
    cfg = s["rules"][rule_id]
    cfg["enabled"] = not cfg.get("enabled")
    await eng.save_settings(s)
    await eng.log(rule_id, f"{'enabled' if cfg['enabled'] else 'disabled'} by {user}")
    return HTMLResponse(f'<span class="{"lv-done" if cfg["enabled"] else "muted"} small">{"on" if cfg["enabled"] else "off"} · saved</span>')


async def ami_schedules_ctx(request: Request, st: dict) -> dict:
    eng = request.app.state.worker.automations
    controllers = [d for d in st["devices"] if amis.is_controller(d)]
    return {"s": await eng.settings(), "schedules": await eng.schedules(), "controllers": controllers,
            "kinds": sorted({amis.kind_of(d.get("device_type")) for d in controllers}),
            "stars": sorted({star_of(d.get("location")) for d in controllers})}


@router.get("/automations", response_class=HTMLResponse)
async def automations_page(request: Request, user: str = Depends(current_user)):
    eng = request.app.state.worker.automations
    jobs = await eng.jobs()
    active = [j for j in jobs if j["status"] in ("running", "waiting")]
    finished = [j for j in reversed(jobs) if j["status"] not in ("running", "waiting")][:15]
    entries = list(reversed(await request.app.state.db.kv_get("automation_log", []) or []))[:60]
    return await page(request, user, "automations.html", "automations", rules=auto.RULES, s=await eng.settings(),
                      rule_home=RULE_HOME, active_jobs=active, finished=finished, entries=entries)


def _schedule_rep(devices: list[dict], target: str) -> dict | None:
    """A representative controller for a schedule target (for its directive list and system targets)."""
    if target.startswith("kind:"):
        return next((d for d in devices if amis.is_controller(d) and amis.kind_of(d.get("device_type")) == target[5:]), None)
    return next((d for d in devices if d.get("device_code") == target), None)


@router.get("/automations/schedule-form", response_class=HTMLResponse)
async def automations_schedule_form(request: Request, target: str = "", user: str = Depends(current_user)):
    rep = _schedule_rep((await load_state(request))["devices"], target)
    if not rep:
        return HTMLResponse('<p class="muted small">Pick a controller.</p>')
    names = await device_directives(request, rep)
    sugg = await suggestions(request, rep.get("location") if not target.startswith("kind:") else None)
    return partial(request, "partials/directive_picker.html", code=rep["device_code"], names=names,
                   fields=cmdspec.directive_fields(names[0]) if names else [], sugg=sugg,
                   sys_targets=sugg.get("system") if not target.startswith("kind:") else None,
                   uid="sched", self_code=rep["device_code"])


@router.post("/automations/schedules", response_class=HTMLResponse)
async def automations_schedule_add(request: Request, user: str = Depends(current_user)):
    form = await request.form()
    target = (form.get("target") or "").strip()
    if not target:
        return HTMLResponse('<span class="lv-alert">Pick a controller.</span>')
    try:
        body = directive_body(form)
    except ValueError as e:
        return HTMLResponse(f'<span class="lv-alert">{e}</span>')
    try:
        every = max(1, int(form.get("every_minutes") or 30))
    except ValueError:
        every = 30
    eng = request.app.state.worker.automations
    items = await eng.schedules()
    items.append({"id": f"s{int(datetime.now(timezone.utc).timestamp() * 1000)}", "name": (form.get("name") or "").strip(),
                  "target": target, "star": (form.get("star") or "").strip().upper() if target.startswith("kind:") else "",
                  "directive": body["directive"], "configuration": body.get("configuration") or {},
                  "every_minutes": every, "only_idle": form.get("only_idle") == "on", "adopt": form.get("adopt") == "on",
                  "launch": form.get("launch") == "on", "enabled": True, "last_run": None, "last_result": None})
    await eng.save_schedules(items)
    await eng.log("ami_schedules", f"schedule added by {user}: {body['directive']} on {target} every {every} min")
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/automations/schedules/{sid}/{action}", response_class=HTMLResponse)
async def automations_schedule_action(request: Request, sid: str, action: str, user: str = Depends(current_user)):
    eng = request.app.state.worker.automations
    async with eng.lock:
        items = await eng.schedules()
        sched = next((x for x in items if x.get("id") == sid), None)
        if not sched:
            return HTMLResponse('<span class="lv-alert">Schedule not found.</span>')
        if action == "toggle":
            sched["enabled"] = not sched.get("enabled", True)
        elif action == "delete":
            items = [x for x in items if x.get("id") != sid]
        elif action == "run":
            results = await eng.run_schedule(sched, manual=True)
            await eng.save_schedules(items)
            return HTMLResponse(f'<span class="small">{"; ".join(results)}</span>', headers={"HX-Trigger": "schedules-changed"})
        await eng.save_schedules(items)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/automations/rules/{rule_id}", response_class=HTMLResponse)
async def automations_rule(request: Request, rule_id: str, user: str = Depends(current_user)):
    rule = auto.RULES_BY_ID.get(rule_id)
    if not rule:
        return HTMLResponse("unknown rule", status_code=404)
    form = await request.form()
    eng = request.app.state.worker.automations
    s = await eng.settings()
    cfg = s["rules"][rule_id]
    was = cfg.get("enabled")
    cfg["enabled"] = form.get("enabled") == "on"
    for o in rule.options:
        raw = form.get(o.name)
        if o.kind == "bool":
            cfg[o.name] = raw == "on"
        elif o.kind == "int":
            try:
                cfg[o.name] = max(0, int(raw or o.default))
            except ValueError:
                pass
        elif raw is not None:
            cfg[o.name] = raw if (not o.options or raw in o.options) else o.default
    await eng.save_settings(s)
    await eng.log(rule_id, (f"{'enabled' if cfg['enabled'] else 'disabled'} by {user}" if was != cfg["enabled"]
                            else f"options changed by {user}"))
    return HTMLResponse(f'<span class="{"lv-done" if cfg["enabled"] else "muted"} small">{"on" if cfg["enabled"] else "off"} · saved</span>')


@router.post("/automations/dry-run", response_class=HTMLResponse)
async def automations_dry_run(request: Request, user: str = Depends(current_user)):
    form = await request.form()
    eng = request.app.state.worker.automations
    s = await eng.settings()
    s["dry_run"] = form.get("dry_run") == "on"
    await eng.save_settings(s)
    await eng.log("engine", f"dry run {'on' if s['dry_run'] else 'off'} ({user})")
    return HTMLResponse('<span class="small lv-done">saved</span>')


@router.post("/automations/jobs/{job_id}/cancel", response_class=HTMLResponse)
async def automations_cancel(request: Request, job_id: str, user: str = Depends(current_user)):
    await request.app.state.worker.automations.cancel(job_id)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/automations/survey-now", response_class=HTMLResponse)
async def automations_survey_now(request: Request, vessel: str = Form(...), user: str = Depends(current_user)):
    """Run the auto-survey rule for a vessel where it is now (as if it had just arrived)."""
    eng = request.app.state.worker.automations
    dev = await _device(request, vessel)
    loc = dev.get("location")
    if not loc:
        return HTMLResponse('<span class="lv-alert">Unknown vessel location — wait for the next device sync.</span>')
    s = await eng.settings()
    async with eng.lock:
        before = len(await eng.jobs())
        await eng.rule_auto_survey(vessel, loc, auto.star_of(loc), await eng.stowed_in(vessel), s["rules"]["auto_survey"])
        made = len(await eng.jobs()) - before
    if s["dry_run"]:
        return HTMLResponse('<span class="muted">Dry run: see the log below for the plan.</span>', headers={"HX-Refresh": "true"})
    if not made:
        return HTMLResponse('<span class="muted">Nothing to do — no un-surveyed bodies, no scan data, or no free survey drones. See the log.</span>',
                            headers={"HX-Refresh": "true"})
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.get("/partials/automation-jobs", response_class=HTMLResponse)
async def p_automation_jobs(request: Request, user: str = Depends(current_user)):
    jobs = await request.app.state.worker.automations.jobs()
    return partial(request, "partials/automation_jobs.html", active=[j for j in jobs if j["status"] in ("running", "waiting")])


# =====================================================================================
# tree view: systems → devices → stowed devices
# =====================================================================================


@router.get("/tree", response_class=HTMLResponse)
async def tree_view(request: Request, user: str = Depends(current_user)):
    st = await load_state(request)
    db = request.app.state.db
    bps = normalize_blueprints(await db.kv_get("blueprints", []))
    carriers = {d["device_code"] for d in st["devices"] if d.get("device_code") and carrier_mod.is_carrier(d, bps)}
    systems = build_tree(st["devices"], st["replicants"], await db.kv_get("stowed_map", {}) or {}, carriers)
    lcfg = await request.app.state.worker.automations.loadout_cfg()
    phases = {star: next((p["name"] for p in lcfg["phases"] if p["id"] == pid), None) for star, pid in lcfg["systems"].items()}
    return await page(request, user, "tree.html", "tree", systems=systems, phases=phases, order_commands=order_commands,
                      dangerous=DANGEROUS, synced=await db.kv_updated("devices"))


# --- loadouts ----------------------------------------------------------------------------------
from . import loadouts as lo  # noqa: E402


async def loadout_ctx(request: Request) -> dict:
    eng = request.app.state.worker.automations
    db = request.app.state.db
    cfg = await eng.loadout_cfg()
    st = await load_state(request)
    bps = normalize_blueprints(await db.kv_get("blueprints", []))
    types = sorted({b["device_type"] for b in bps} | {d.get("device_type") for d in st["devices"] if d.get("device_type")}
                   - {"heaven_vessel"})
    p = await eng.loadout_plan()
    present = Counter(lo.star_of(d.get("location")) for d in st["devices"])
    stars = sorted(set(present) | set(cfg["systems"]) | set(cfg["roles"]),
                   key=lambda s: (s not in cfg["systems"] and s not in cfg["roles"], -present.get(s, 0), s))
    jobs = [j for j in await eng.jobs() if j["rule"] == "loadouts"]
    tagged = defaultdict(list)
    for d in st["devices"]:
        for t in d.get("tags") or []:
            if t == lo.SPARE or t.startswith("to:"):
                tagged[t].append(d)
    rule = (await eng.settings())["rules"].get("loadouts", {})
    from .ami_schedule import handoffs, managed_by
    cat = await db.kv_get("stars", {}) or {}
    star_cat = {s.get("designation"): s for s in (cat.get("stars") or []) if isinstance(s, dict)}
    hosts = {r.get("hosted_device_code"): code for code, r in st["replicants"].items() if r.get("hosted_device_code")}
    pending_handoffs = {h["drone"] for h in handoffs(st["devices"], await managed_by(db), eng.busy_devices(await eng.jobs()),
                                                      set(p.get("moves") or {}), set(cfg.get("ignore_tags") or []), limit=200)}
    audit = lo.audit(cfg, st["devices"], star_cat, p, pending_handoffs, hosts)
    from . import consolidate as co
    consolidation = [co.describe(x) for x in await eng.consolidate_plan()]
    consolidate_on = bool(((await eng.settings())["rules"].get("consolidate") or {}).get("enabled"))
    return {"cfg": cfg, "types": types, "plan": p, "lines": lo.describe(p), "stars": stars, "present": present, "audit": audit,
            "depot": lo.spare_depot(cfg, st["devices"]),
            "consolidation": consolidation, "consolidate_on": consolidate_on,
            "active_jobs": [j for j in jobs if j["status"] in ("running", "waiting")],
            "recent_jobs": [j for j in reversed(jobs) if j["status"] not in ("running", "waiting")][:8],
            "orders": await eng.loadout_orders(), "tagged": dict(tagged), "rule": rule,
            "last": await db.kv_get("loadouts_last", {}) or {},
            "all_tags": sorted({t for d in st["devices"] for t in (d.get("tags") or [])})}


async def save_loadouts(request: Request, cfg: dict) -> None:
    await request.app.state.db.kv_set("loadouts", cfg)


@router.get("/loadouts", response_class=HTMLResponse)
async def loadouts_page(request: Request, user: str = Depends(current_user)):
    based: dict[str, list[dict]] = defaultdict(list)   # mobile fleets by home system
    for f in await request.app.state.worker.automations.fleets():
        based[f.get("home") or ""].append(f)
    return await page(request, user, "loadouts.html", "loadouts", mobile_by_home=based, **await loadout_ctx(request))


@router.post("/loadouts/phases", response_class=HTMLResponse)
async def loadouts_save_phases(request: Request, user: str = Depends(current_user)):
    form = await request.form()
    cfg = await request.app.state.worker.automations.loadout_cfg()
    for ph in cfg["phases"]:
        pid = ph["id"]
        ph["name"] = (form.get(f"name:{pid}") or ph["name"]).strip()
        try:
            ph["order"] = int(form.get(f"order:{pid}") or ph.get("order") or 0)
        except ValueError:
            pass
        wants = {}
        for key, val in form.multi_items():
            if key.startswith(f"want:{pid}:") and str(val).strip() != "":
                try:
                    wants[key[len(f"want:{pid}:"):]] = max(0, int(val))
                except ValueError:
                    pass
        ph["wants"] = wants
    new = (form.get("new_phase") or "").strip()
    if new:
        pid = re.sub(r"[^a-z0-9]+", "-", new.lower()).strip("-") or "phase"
        while any(p["id"] == pid for p in cfg["phases"]):
            pid += "-2"
        copy = next((p for p in cfg["phases"] if p["id"] == form.get("copy_from")), None)
        cfg["phases"].append({"id": pid, "name": new, "order": max([p.get("order", 0) for p in cfg["phases"]] or [0]) + 1,
                              "wants": dict((copy or {}).get("wants") or {})})
    await save_loadouts(request, cfg)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/loadouts/phases/{pid}/delete", response_class=HTMLResponse)
async def loadouts_delete_phase(request: Request, pid: str, user: str = Depends(current_user)):
    cfg = await request.app.state.worker.automations.loadout_cfg()
    cfg["phases"] = [p for p in cfg["phases"] if p["id"] != pid]
    cfg["systems"] = {s: v for s, v in cfg["systems"].items() if v != pid}
    await save_loadouts(request, cfg)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/loadouts/system", response_class=HTMLResponse)
async def loadouts_set_system(request: Request, star: str = Form(...), phase: str = Form(""),
                              user: str = Depends(current_user)):
    cfg = await request.app.state.worker.automations.loadout_cfg()
    if phase:
        cfg["systems"][star] = phase
    else:
        cfg["systems"].pop(star, None)
    await save_loadouts(request, cfg)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/loadouts/role", response_class=HTMLResponse)
async def loadouts_set_role(request: Request, star: str = Form(...), role: str = Form(""), user: str = Depends(current_user)):
    cfg = await request.app.state.worker.automations.loadout_cfg()
    if role in ("source", "destination"):
        cfg["roles"][star] = role
    else:
        cfg["roles"].pop(star, None)
    await save_loadouts(request, cfg)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/loadouts/settings", response_class=HTMLResponse)
async def loadouts_settings(request: Request, user: str = Depends(current_user)):
    form = await request.form()
    cfg = await request.app.state.worker.automations.loadout_cfg()
    cfg["ignore_tags"] = sorted({t.strip().lower() for t in re.split(r"[,\s]+", form.get("ignore_tags") or "") if t.strip()}
                                - {lo.SPARE})
    for k in ("print_missing", "need_stock", "carriers_return", "use_replicant_vessels", "gather_spares"):
        cfg["settings"][k] = form.get(k) == "on"
    cfg["settings"]["spare_depot"] = (form.get("spare_depot") or "").strip().upper()
    await save_loadouts(request, cfg)
    return HTMLResponse('<span class="lv-done small">Saved.</span>', headers={"HX-Refresh": "true"})


@router.post("/loadouts/apply", response_class=HTMLResponse)
async def loadouts_apply(request: Request, star: str = Form(""), user: str = Depends(current_user)):
    eng = request.app.state.worker.automations
    async with eng.lock:
        lines = await eng.apply_loadouts({star} if star else None, manual=True)
    body = "".join(f"<li>{line}</li>" for line in lines) or "<li>Nothing to do: every phased system matches its loadout.</li>"
    return HTMLResponse(f'<div class="result ok"><strong>Applied{" to " + star if star else ""}</strong><ul class="small">{body}</ul>'
                        f'<div class="small muted">Follow the jobs below or on the Automations page.</div></div>',
                        headers={"HX-Trigger": "loadouts-changed"})


@router.post("/loadouts/orders/clear", response_class=HTMLResponse)
async def loadouts_clear_orders(request: Request, user: str = Depends(current_user)):
    await request.app.state.db.kv_set("loadout_orders", [])
    return HTMLResponse("", headers={"HX-Refresh": "true"})


# --- in-game events (contracts) --------------------------------------------------------------
from . import gameevents as gev  # noqa: E402


async def game_events_ctx(request: Request) -> dict:
    db = request.app.state.db
    st = await load_state(request)
    inv = {i.get("location"): i.get("items") or {} for i in st["inventory"]}
    evs = await gev.load(db)
    settings = await db.kv_get("event_settings", {}) or {}
    open_, closed = [], []
    for e in sorted(evs.values(), key=lambda e: e.get("discovered_at") or "", reverse=True):
        if e["status"] == "open":
            e["prog"] = gev.progress(e, inv, st["devices"], st["replicants"])
            e["plan"] = gev.delivery_plan(e, e["prog"], st["devices"])
            open_.append(e)
        else:
            closed.append(e)
    return {"open": open_, "closed": closed[:30], "replicants": st["replicants"], "settings": settings}


@router.get("/game-events", response_class=HTMLResponse)
async def game_events_page(request: Request, user: str = Depends(current_user)):
    return await page(request, user, "game_events.html", "gameevents", **await game_events_ctx(request))


async def _event(request: Request, des: str) -> tuple[dict, dict]:
    ctx = await game_events_ctx(request)
    e = next((x for x in ctx["open"] if x["designation"] == des), None)
    return e, ctx


@router.post("/game-events/{des}/bring", response_class=HTMLResponse)
async def game_event_bring(request: Request, des: str, replicant: str = Form(...), user: str = Depends(current_user)):
    e, _ = await _event(request, des)
    if not e:
        return HTMLResponse('<div class="result err">That event is no longer open.</div>')
    return await run_action(request, user, "POST", f"/replicants/{replicant}/travel", {"destination": e["location"]},
                            f"{replicant} → {e['location']} for {e['title']}")


@router.post("/game-events/{des}/deliver", response_class=HTMLResponse)
async def game_event_deliver(request: Request, des: str, user: str = Depends(current_user)):
    e, _ = await _event(request, des)
    if not e:
        return HTMLResponse('<div class="result err">That event is no longer open.</div>')
    plan = e["plan"]
    ctrl = plan["controller"]
    if not plan["legs"]:
        msg = "Nothing to move: everything needed is already at the location." if not plan["short"] else \
            f"Not enough in {e['star']}: missing {', '.join(f'{int(q)} {r}' for r, q in plan['missing'].items())}."
        return HTMLResponse(f'<div class="result {"ok" if not plan["short"] else "err"}">{msg}</div>')
    if not ctrl:
        return HTMLResponse(f'<div class="result err">No in-system AMI transport controller in {e["star"]} to deliver with.</div>')
    code = ctrl["device_code"]
    steps = []
    for leg in plan["legs"][:3]:
        st = auto.step(f"{code}: deliver {', '.join(f'{q} {r}' for r, q in leg['requirement'].items())} {leg['collect']} → {leg['deliver']}",
                       f"/devices/{code}", {"command": "set_directive", "directive": "delivery",
                                            "configuration": {"route": {"collect": leg["collect"], "deliver": leg["deliver"]},
                                                              "requirement": leg["requirement"]}},
                       wait=["directive.completed"], timeout=6 * 3600)
        steps += [st, auto.step(f"{code}: launch", f"/devices/{code}", {"command": "launch"})]
    # only wait between legs; the last one finishes on its own
    if steps:
        steps[-2]["wait"] = []
    return await start_chain(request, user, f"event {e['title']}: deliver to {e['location']}", steps[0], steps[1:], code)


@router.post("/game-events/{des}/fulfil", response_class=HTMLResponse)
async def game_event_fulfil(request: Request, des: str, replicant: str = Form(""), user: str = Depends(current_user)):
    e, ctx = await _event(request, des)
    if not e:
        return HTMLResponse('<div class="result err">That event is no longer open.</div>')
    tmpl = (ctx["settings"].get("fulfil") or "").strip() or gev.DEFAULT_FULFIL
    rep = replicant or (e["prog"]["present"][0]["code"] if e["prog"]["present"] else "")
    best = (e["prog"].get("best") or {}).get("name") or "default"
    filled = (tmpl.replace("{designation}", des).replace("{replicant}", rep).replace("{location}", e["location"])
              .replace("{criteria}", best))
    method, _, rest = filled.partition(" ")
    path, _, body = rest.partition(" ")
    try:
        payload = json.loads(body) if body.strip() else None
    except ValueError:
        return HTMLResponse('<div class="result err">The fulfil body isn\'t valid JSON.</div>')
    return await run_action(request, user, method.upper(), path, payload, f"fulfil {e['title']} ({des})")


@router.post("/game-events/settings", response_class=HTMLResponse)
async def game_event_settings(request: Request, fulfil: str = Form(""), user: str = Depends(current_user)):
    s = await request.app.state.db.kv_get("event_settings", {}) or {}
    s["fulfil"] = fulfil.strip()
    await request.app.state.db.kv_set("event_settings", s)
    return HTMLResponse('<span class="lv-done small">Saved.</span>')


# --- mobile fleets ----------------------------------------------------------------------------
from . import fleets as fl  # noqa: E402


async def fleets_ctx(request: Request) -> dict:
    eng = request.app.state.worker.automations
    st = await load_state(request)
    items = await eng.fleets()
    bps = normalize_blueprints(await request.app.state.db.kv_get("blueprints", []))
    types = sorted({b["device_type"] for b in bps} | {d.get("device_type") for d in st["devices"] if d.get("device_type")})
    jobs = {j["id"]: j for j in await eng.jobs()}
    bp_by = {b["device_type"]: b for b in bps}
    for f in items:
        f["roster"] = fl.roster(f, st["devices"])
        f["points"] = fl.attach_points(f, st["devices"], bp_by, f["roster"]["rows"])
        m = f.get("mission") or {}
        f["job"] = jobs.get(m.get("job"))
    free = [d for d in st["devices"] if not fl.fleet_of(d) and d.get("device_code") not in
            {r.get("hosted_device_code") for r in st["replicants"].values()}]
    stars_seen = sorted({star_of(d.get("location")) for d in st["devices"] if d.get("location")})
    traders = await request.app.state.db.kv_get("traders_cache", {}) or {}
    profiles = {t: fl.type_profile(t, bp_by, st["devices"]) for t in types}
    from .loadouts import home_fleet_systems
    homes = sorted(home_fleet_systems(await eng.loadout_cfg()))
    return {"profiles": profiles, "home_systems": homes, "fleets": items, "types": types, "free": sorted(free, key=lambda d: (star_of(d.get("location")), d.get("device_type") or "")),
            "stars": stars_seen, "roles": fl.ROLES, "phases": fl.PHASES, "traders": traders}


@router.get("/fleets", response_class=HTMLResponse)
async def fleets_page(request: Request, user: str = Depends(current_user)):
    return await page(request, user, "fleets.html", "fleets", **await fleets_ctx(request))


async def _fleets(request: Request) -> tuple[Any, list[dict]]:
    eng = request.app.state.worker.automations
    return eng, await eng.fleets()


@router.post("/fleets", response_class=HTMLResponse)
async def fleets_create(request: Request, name: str = Form(...), role: str = Form("mining"), home: str = Form(""),
                        user: str = Depends(current_user)):
    eng, items = await _fleets(request)
    fid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:20] or "fleet"
    while any(f["id"] == fid for f in items):
        fid += "-2"
    items.append({"id": fid, "name": name.strip(), "role": role if role in fl.ROLES else "mining", "home": home.upper(), "wants": {}})
    await eng.save_fleets(items)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/fleets/{fid}/edit", response_class=HTMLResponse)
async def fleets_edit(request: Request, fid: str, user: str = Depends(current_user)):
    form = await request.form()
    eng, items = await _fleets(request)
    f = next((x for x in items if x["id"] == fid), None)
    if not f:
        return HTMLResponse("", status_code=404)
    if form.get("delete") == "1":
        items = [x for x in items if x["id"] != fid]
    else:
        f["name"] = (form.get("name") or f["name"]).strip()
        f["home"] = (form.get("home") or f["home"]).upper()
        f["role"] = form.get("role") if form.get("role") in fl.ROLES else f["role"]
        wants = None
        if form.get("lines"):   # the loadout editor: parallel type / qty lists, blank or 0 lines ignored
            wants = {}
            for t, q in zip(form.getlist("type"), form.getlist("qty")):
                t = (t or "").strip()
                try:
                    q = int(str(q).strip() or 0)
                except ValueError:
                    q = 0
                if t and q > 0:
                    wants[t] = wants.get(t, 0) + q
        elif any(k.startswith("want:") for k in form.keys()):   # older bulk form / API use
            wants = {}
            for k, v in form.multi_items():
                if k.startswith("want:") and k[5:] and str(v).strip():
                    try:
                        if int(v) > 0:
                            wants[k[5:]] = int(v)
                    except ValueError:
                        pass
        if wants is not None:
            f["wants"] = wants
            gone = {t for t in form.getlist("release") if t and t not in wants}
            if gone:
                st = await load_state(request)
                tag = fl.fleet_tag(fid)
                steps = [auto.step(f"{d['device_code']} leaves {fid}", f"/devices/{d['device_code']}",
                                   {"configuration": {"remove_tags": [tag]}}, method="PATCH")
                         for d in fl.members(f, st["devices"]) if d.get("device_type") in gone]
                if steps:
                    async with eng.lock:
                        await eng.create_job("fleets", f"fleet {fid}: {', '.join(sorted(gone))} removed ({len(steps)} device(s) released)",
                                             None, steps, {"devices": []}, force=True)
        if form.get("autosave") and form.get("delete") != "1":
            await eng.save_fleets(items)
            n = sum((f.get("wants") or {}).values())
            return HTMLResponse(f'<span class="muted">Loadout saved · {len(f.get("wants") or {})} type(s), {n} device(s)</span>')
    await eng.save_fleets(items)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/fleets/{fid}/want", response_class=HTMLResponse)
async def fleets_want(request: Request, fid: str, user: str = Depends(current_user)):
    """Set one loadout line. A new type defaults to 1; qty 0 removes the type completely — the line goes
    and any member devices of that type leave the fleet (their fleet tag is removed)."""
    form = await request.form()
    eng, items = await _fleets(request)
    f = next((x for x in items if x["id"] == fid), None)
    t = (form.get("type") or "").strip()
    if not f or not t:
        return HTMLResponse("", status_code=404 if not f else 400)
    raw = str(form.get("qty") if form.get("qty") is not None else "1").strip()
    try:
        qty = int(raw or 0)
    except ValueError:
        qty = 1
    wants = dict(f.get("wants") or {})
    if qty > 0:
        wants[t] = qty
    else:
        wants.pop(t, None)
        st = await load_state(request)
        tag = fl.fleet_tag(fid)
        steps = [auto.step(f"{d['device_code']} leaves {fid}", f"/devices/{d['device_code']}",
                           {"configuration": {"remove_tags": [tag]}}, method="PATCH")
                 for d in fl.members(f, st["devices"]) if d.get("device_type") == t]
        if steps:
            async with eng.lock:
                await eng.create_job("fleets", f"fleet {fid}: {t} removed ({len(steps)} device(s) released)",
                                     None, steps, {"devices": []}, force=True)
    f["wants"] = wants
    await eng.save_fleets(items)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/fleets/{fid}/members", response_class=HTMLResponse)
async def fleets_members(request: Request, fid: str, user: str = Depends(current_user)):
    """Add (tag) or remove (untag) devices. Fleet devices drop their home:/spare/to: tags — the fleet owns them."""
    form = await request.form()
    tag = fl.fleet_tag(fid)
    st = await load_state(request)
    by = {d.get("device_code"): d for d in st["devices"]}
    eng = request.app.state.worker.automations
    steps = []
    for code in form.getlist("add"):
        d = by.get(code) or {}
        rem = [t for t in d.get("tags") or [] if t.startswith(("home:", "to:", "fleet:")) or t == "spare"]
        steps.append(auto.step(f"{code} joins {fid}", f"/devices/{code}",
                               {"configuration": {"add_tags": [tag], **({"remove_tags": rem} if rem else {})}}, method="PATCH"))
    for code in form.getlist("remove"):
        steps.append(auto.step(f"{code} leaves {fid}", f"/devices/{code}", {"configuration": {"remove_tags": [tag]}}, method="PATCH"))
    if not steps:
        return HTMLResponse('<div class="result err">Pick at least one device.</div>')
    async with eng.lock:
        await eng.create_job("fleets", f"fleet {fid}: {len(steps)} membership change(s)", None, steps, {"devices": []}, force=True)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/fleets/{fid}/mission", response_class=HTMLResponse)
async def fleets_mission(request: Request, fid: str, user: str = Depends(current_user)):
    form = await request.form()
    eng, items = await _fleets(request)
    f = next((x for x in items if x["id"] == fid), None)
    if not f:
        return HTMLResponse("", status_code=404)
    if (f.get("mission") or {}).get("status") == "running":
        return HTMLResponse('<div class="result err">This fleet is already on a mission — recall or stop it first.</div>')
    targets = [t.strip().upper() for t in re.split(r"[,\s]+", form.get("targets") or "") if t.strip()]
    m = {"status": "running", "phase": None, "idx": 0, "targets": targets, "started_at": now_iso(), "log": [],
         "opts": {"deliver": form.get("deliver") == "on", "exhausted_minutes": int(form.get("exhausted_minutes") or 30)}}
    if f["role"] == "trade":
        try:
            m["trade"] = json.loads(form.get("trade") or "{}")
        except ValueError:
            return HTMLResponse('<div class="result err">Pick a trade.</div>')
        m["targets"] = [m["trade"].get("star")]
    if not m["targets"] or not m["targets"][0]:
        return HTMLResponse('<div class="result err">Give the mission a target system.</div>')
    if f["role"] == "mining":
        m["targets"] = m["targets"][:1]
    if f["role"] in ("mining", "explore"):
        from .loadouts import home_fleet_systems
        homes = home_fleet_systems(await eng.loadout_cfg())
        taken = [t for t in m["targets"] if star_of(t) in homes]
        if taken:
            return HTMLResponse(f'<div class="result err">{html.escape(", ".join(taken))}: '
                                f'{"has" if len(taken) == 1 else "have"} a home fleet, and only the home fleet works its '
                                'system. Pick a system without a loadout phase.</div>')
    f["mission"] = m
    await eng.save_fleets(items)
    async with eng.lock:
        await eng.run_fleets()
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/fleets/{fid}/control", response_class=HTMLResponse)
async def fleets_control(request: Request, fid: str, action: str = Form(...), user: str = Depends(current_user)):
    eng, items = await _fleets(request)
    f = next((x for x in items if x["id"] == fid), None)
    if f and action == "end" and not f.get("mission"):
        f["mission"] = {"status": "ended", "targets": [], "log": []}   # no mission yet: just gather everyone aboard
    m = (f or {}).get("mission") or {}
    if not f or not m:
        return HTMLResponse("", status_code=404)
    async with eng.lock:
        if m.get("job"):
            await eng.cancel(m["job"])
        if action == "end":
            m["status"], m["end_here"] = "running", True
            m.pop("end_started", None)
            eng._mlog(m, "end mission ordered: recall everything aboard the carriers and stay here")
        elif action == "recall":
            m["status"], m["phase"] = "running", ("work" if f["role"] != "trade" else "trade")
            if f["role"] == "explore":
                m["targets"] = (m.get("targets") or [])[:m.get("idx", 0) + 1]  # no further targets
            eng._mlog(m, "recall ordered")
        elif action == "resume" and m.get("end_here"):
            m["status"] = "running"
            m.pop("end_started", None)   # re-run the recall & board
            eng._mlog(m, "resumed (end mission & board)")
        elif action == "resume":
            m["status"] = "running"
            # re-run the phase that stalled
            ph = fl.PHASES[f["role"]]
            cur = m.get("phase")
            m["phase"] = ph[ph.index(cur) - 1] if cur in ph and ph.index(cur) > 0 else None
            eng._mlog(m, "resumed")
        elif action == "stop":
            m["status"] = "stopped"
            eng._mlog(m, "stopped (devices stay where they are)")
        m["job"] = None
        await eng.save_fleets(items)
        await eng.run_fleets()
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/fleets/traders", response_class=HTMLResponse)
async def fleets_traders(request: Request, user: str = Depends(current_user)):
    """Refresh the trader directory and each trader's trades (one request per trader, at most 10)."""
    st = await load_state(request)
    api = request.app.state.api
    rep = next(iter(st["replicants"]), None)
    if not rep:
        return HTMLResponse('<span class="lv-alert small">No replicant to ask.</span>')
    try:
        traders = ((await api.get(f"/replicants/{rep}/traders")) or {}).get("traders") or []
    except ApiError as e:
        return HTMLResponse(f'<span class="lv-alert small">{e.message}</span>')
    out = {}
    for t in traders[:10]:
        code = t.get("controller_code")
        try:
            trades = ((await api.get(f"/devices/{code}/trades")) or {}).get("trades") or []
        except ApiError:
            trades = []
        out[code] = {**t, "trades": trades}
    await request.app.state.db.kv_set("traders_cache", out)
    return HTMLResponse("", headers={"HX-Refresh": "true"})


@router.post("/fleets/{fid}/print", response_class=HTMLResponse)
async def fleets_print(request: Request, fid: str, user: str = Depends(current_user)):
    """Queue the fleet's shortfall on an autofactory (home system first); prints come out already in the fleet."""
    eng, items = await _fleets(request)
    f = next((x for x in items if x["id"] == fid), None)
    st = await load_state(request)
    short = fl.short_list(f, st["devices"]) if f else {}
    if not short:
        return HTMLResponse('<div class="result ok">Nothing missing.</div>')
    facs = [d for d in st["devices"] if "enqueue_print" in (d.get("available_commands") or [])]
    facs.sort(key=lambda d: (star_of(d.get("location")) != f["home"], d.get("device_code")))
    if not facs:
        return HTMLResponse('<div class="result err">No autofactory to print on.</div>')
    # every autofactory in the chosen system shares the prints, evenly by print time
    peers = [d for d in facs if star_of(d.get("location")) == star_of(facs[0].get("location"))]
    bps = {b["device_type"]: b for b in normalize_blueprints(await request.app.state.db.kv_get("blueprints", []))}
    load = {d["device_code"]: printqueue.load_seconds(d, bps) for d in peers}
    steps = []
    for t, n in sorted(short.items(), key=lambda tn: -float((bps.get(tn[0]) or {}).get("print_time") or 0)):
        for fac, k in printqueue.split(peers, t, n, bps, load):
            steps.append(auto.step(f"print {k}× {t} on {fac['device_code']} for {f['name']}", f"/devices/{fac['device_code']}",
                                   {"command": "enqueue_print", "device_type": t, "quantity": k, "tags": [fl.fleet_tag(fid)]}))
    return await start_chain(request, user, f"fleet {f['name']}: print {fl.summarize(short)}", steps[0], steps[1:],
                             facs[0]["device_code"])
