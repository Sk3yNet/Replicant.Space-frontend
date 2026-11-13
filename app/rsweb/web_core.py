"""What every page module shares: the router, templates and filters, sign-in, page rendering, state loading and
the helpers the routes call. web.py and the web_*.py route modules start from here (from .web_core import *)."""
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

from markupsafe import Markup
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates

from .api import ApiError
from .db import now_iso, row_event
from .ingest import auto_read_cfg, message_unread
from . import locread
from . import notify
from .shapes import as_amounts, normalize_blueprints, normalize_inventory
from . import commands as cmdspec
from . import automations as auto
from . import hubroute
from .cargo import cargo_context
from .targets import CATEGORY_LABEL, options_for, system_resources, system_targets
from . import carrier as carrier_mod
from .tree import build_tree
from . import production
from . import ami_schedule as amis
from . import printqueue
from . import snapshot as snapmod
from .ingest import duplicate_timers

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
from . import version as appver  # noqa: E402
templates.env.globals.update(TENANT_MODE=os.getenv("TENANT_MODE") == "1", app_version=appver.label(), APP_VERSION=appver.VERSION, RUN_ID=appver.RUN_ID, entry_age=appver.age_of, command_help=cmdspec.DESCRIPTIONS)
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
    return json.dumps(v, indent=2, sort_keys=True, default=str, ensure_ascii=False)


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


DRONE_KINDS = (("mining_drone", "mining", "M"), ("survey_drone", "survey", "S"), ("belt_surveyor", "survey", "S"),
               ("transport_drone", "transport", "T"),
               ("transport_hauler", "transport", "T"), ("maintenance_drone", "maintenance", "R"))


def drone_summary(devices: list[dict]) -> list[dict]:
    """Drones by kind: [{kind, letter, n, working, idle, moving, other, state, devices: [(code, status)]}].
    `state` is what most of them are doing (working / idle / moving / other), for the colour."""
    out: dict[str, dict] = {}
    for d in devices:
        t = d.get("device_type") or ""
        k = next(((kind, letter) for key, kind, letter in DRONE_KINDS if key == t), None)
        if not k:
            continue
        g = out.setdefault(k[0], {"kind": k[0], "letter": k[1], "n": 0, "working": 0, "idle": 0, "moving": 0, "other": 0,
                                  "devices": []})
        g["n"] += 1
        cls = status_class(d.get("status"))
        g[{"st-work": "working", "st-idle": "idle", "st-move": "moving"}.get(cls, "other")] += 1
        g["devices"].append((d.get("device_code"), d.get("status") or "?"))
    for g in out.values():
        g["state"] = max(("working", "idle", "moving", "other"), key=lambda x: (g[x], x == "working"))
    order = [kind for _, kind, _ in DRONE_KINDS]
    return sorted(out.values(), key=lambda g: order.index(g["kind"]))


def drone_orbits(devices: list[dict], r0: float) -> list[dict]:
    """Working drones at a location as orbiting dots around its marker (radius r0): workers clockwise on an inner
    ring, survey drones counterclockwise farther out (with a scanning beam). Evenly phased, at most 10 per ring;
    `delay` is a negative animation delay that sets each dot's starting angle."""
    rings: dict[bool, list[dict]] = {False: [], True: []}
    for d in devices:
        k = next((kind for key, kind, _ in DRONE_KINDS if key == (d.get("device_type") or "")), None)
        if k and status_class(d.get("status")) == "st-work":
            rings[k == "survey"].append(d)
    out = []
    for survey, ds in rings.items():
        ds = sorted(ds, key=lambda d: d.get("device_code") or "")[:10]
        period = 16.0 if survey else 9.0
        for i, d in enumerate(ds):
            out.append({"code": d.get("device_code"), "kind": "survey" if survey else "work", "r": r0 + (13 if survey else 5), "r0": r0,
                        "dur": period, "delay": -round(period * i / len(ds), 2),
                        "title": f"{(d.get('device_type') or 'drone').replace('_', ' ')} {d.get('device_code')} — {d.get('status')}"})
    return out


def status_class(status: Any) -> str:
    s = str(status or "").lower()
    if s.startswith(("idle", "inactive", "waiting")):
        return "st-idle"
    if s.startswith(("mining", "printing", "collecting", "depositing", "prospecting", "scanning", "coordinating",
                     "patrolling", "relaying", "monitoring", "tracking", "repairing", "searching", "salvaging")):
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


_CONTROL_RE = re.compile(r'<(input|select)\b([^>]*)>', re.I)


def f_no_restore(html: Any) -> Markup:
    """Checkboxes, radios and selects on a full page show the server's state: a reload must not let the browser put back
    ticks by position (Joe 2026-10-31: Contracts and device pages showed ticks on the wrong rows)."""
    def mark(m: re.Match) -> str:
        tag, attrs = m.group(1), m.group(2)
        if "autocomplete" in attrs.lower() or (tag.lower() == "input" and not re.search(r'type="?(checkbox|radio)', attrs, re.I)):
            return m.group(0)
        return f'<{tag} autocomplete="off"{attrs}>'
    return Markup(_CONTROL_RE.sub(mark, str(html)))


templates.env.filters.update(no_restore=f_no_restore, local=f_local, ago=f_ago, pretty=f_pretty, human=f_human, num=f_num,
                             status_class=status_class, star_of=star_of, capacity=capacity, duration=f_duration)
from .modular import modular_type as _modular_type


def unconfirmed_mark(d: Any) -> Markup:
    """A pill next to a device's status while it shows the game's answer to an order but no re-read yet (1.317.0)."""
    u = (d or {}).get("_unconfirmed") if isinstance(d, dict) else None
    if not isinstance(u, dict):
        return Markup("")
    return Markup(' <span class="pill muted small" title="{} sent {}: the game answered, waiting for its device list or an '
                  'event to confirm">unconfirmed</span>').format(Markup.escape(u.get("what") or "order"), f_ago(u.get("at")))


def fold_mark(d: Any) -> Markup:
    """A "compacted" pill next to the status of a large (modular) device that is folded up but whose status doesn't say
    so (printed compacted, or compacted and idle since): every device with the modular feature or a compact command,
    fleet tenders too (1.376.0)."""
    if not isinstance(d, dict):
        return Markup("")
    from .modular import folded, is_modular
    st = str(d.get("status") or "")
    if not is_modular(d) or st.startswith(("compact", "unfurl")) or not folded(d):
        return Markup("")
    return Markup(' <span class="pill st-off small" title="folded up for transport: unfurl it to put it to work">compacted</span>')


templates.env.globals.update(unconfirmed=unconfirmed_mark, fold_mark=fold_mark)
# The menu: (group, landing URL, [(page key, URL, label)]); the classic header shows it as tabs, the command screen as its
# icon rail with a fly-out per group.
NAV = [
    ("Dashboard", "/", [("dashboard", "/", "Dashboard")]),
    ("Devices", "/tree", [("tree", "/tree", "Tree"), ("fleet", "/fleet", "Device list"), ("ami", "/ami", "AMI"), ("lost", "/lost", "Lost"), ("cockpit", "/cockpit", "Cockpit")]),
    ("Map", "/map", [("map", "/map", "Galaxy"), ("stars", "/stars", "Stars"), ("systems", "/systems", "Systems"), ("prospects", "/systems/prospects", "Prospects"), ("relays", "/map/relays", "Relays"), ("traffic", "/traffic", "Traffic"), ("trail", "/trail", "Trail"), ("wards", "/wards", "Wards & hubs"),
           ("defence", "/defence", "Defense"), ("terraforming", "/terraforming", "Terraforming"), ("tfcandidates", "/terraforming/candidates", "Terraforming candidates"), ("maintenance", "/maintenance", "Upkeep")]),
    ("Fleets", "/fleets", [("fleets", "/fleets", "Fleets"), ("fleet_details", "/fleets/details", "Fleet details"), ("logistics", "/fleets/logistics", "Logistics"), ("reform", "/fleets/reform", "Reset & reform")]),
    ("Economy", "/blueprints", [("blueprints", "/blueprints", "Blueprints"), ("gameevents", "/game-events", "Contracts"), ("jobs", "/jobs", "Jobs"), ("reputation", "/reputation", "Reputation"), ("shop", "/shop", "Shop"), ("simulations", "/simulations", "Simulations"), ("lockers", "/lockers", "Lockers")]),
    ("Activity", "/automations", [("automations", "/automations", "Automations"), ("events", "/events", "Events"),
                        ("messages", "/messages", "Messages"), ("contacts", "/contacts", "Contacts"), ("chatter", "/chatter", "Chatter"), ("notifications", "/notifications", "Notifications"),
                        ("leaderboards", "/leaderboards", "Leaderboards"), ("blog", "/blog", "Blog")]),
  ]

UI_COOKIE, ACCENT_COOKIE = "ui", "ui_accent"
_HEX = __import__("re").compile(r"#[0-9a-fA-F]{6}")


def ui_mode(request: Request | None) -> str:
    """How a page is drawn: "win" (only its content, for a window of the command screen: header X-Window), "bridge" (the
    command screen around it: cookie ui=bridge) or "classic" (the tabbed header). The simulation's galaxy map stays classic."""
    if request is None:
        return "classic"
    if request.headers.get("x-window"):
        return "win"
    if request.cookies.get(UI_COOKIE) == "bridge" and not (request.url.path == "/map" and sim_view(request)):
        return "bridge"
    return "classic"


def ui_accent(request: Request | None) -> str:
    """The command screen's accent color (cookie ui_accent, set on the Account page); red by default."""
    v = (request.cookies.get(ACCENT_COOKIE) or "") if request is not None else ""
    v = v.replace("%23", "#")
    return v if _HEX.fullmatch(v) else "#e0332f"


templates.env.globals.update(NAV=NAV, ui_mode=ui_mode, ui_accent=ui_accent)
from . import tradepost as _tp  # noqa: E402
templates.env.globals.update(price_text=_tp.price_text, hours_text=_tp.hours_text)
templates.env.globals.update(is_large=lambda b: _modular_type((b or {}).get("device_type"), {(b or {}).get("device_type"): b or {}}))
templates.env.globals.update(RESOURCES=RESOURCES, describe=notify.describe, level_of=notify.level_of,
                             device_options=cmdspec.device_options, field_applies=cmdspec.field_applies,
                             target_options=options_for)


async def base_ctx(request: Request, user: str, active: str, **kw) -> dict:
    st = request.app.state
    return {
        "request": request, "user": user, "active": active, "sim_id": sim_view(request),
        "rate": st.api.rate_status(), "stream_state": st.worker.stream_state,
        "notif_unread": await notify.unread_errors(st.db),   # the badge counts errors only
        "configured": st.api.configured,
        "season_hold": await st.db.kv_get("season_hold", None),
        **kw,
    }


async def page(request: Request, user: str, template: str, active: str, **kw) -> HTMLResponse:
    visit = None
    if not request.headers.get("hx-request") and not request.headers.get("x-live"):   # a live redraw isn't a visit
        visit = await notify.touch_visit(request.app.state.db, user, request.app.state.settings.visit_gap_minutes)
    ctx = await base_ctx(request, user, active, visit=visit, **kw)
    return templates.TemplateResponse(request, template, ctx)


def partial(request: Request, template: str, **kw) -> HTMLResponse:
    return templates.TemplateResponse(request, template, {"request": request, **kw})


# =====================================================================================
# state helpers
# =====================================================================================
def sim_view(request: Request) -> str | None:
    """`?sim=<run id>`: the page shows that simulation's virtual galaxy instead of the real one (sim_world)."""
    qp = getattr(request, "query_params", None)
    v = str((qp.get("sim") if qp is not None else "") or "").strip()
    return v if v.isdigit() else None


async def load_state(request: Request) -> dict:
    db = request.app.state.db
    sid = sim_view(request)
    if sid:
        from .sim_world import world
        w = await world(request.app, sid, force=bool(request.query_params.get("refresh")))
        return {"account": await db.kv_get("account", {}) or {}, "replicants": w["replicants"], "devices": w["devices"],
                "inventory": normalize_inventory(w["inventory"]), "locations": {}, "totals": w["totals"]}
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


# A device's status blocks that carry a finish time: (key, timer kind, label from the block)
_STATUS_TIMERS = (("printing", "print", lambda b: f"printing {(b.get('device_type') or '?').replace('_', ' ')}"),
                  ("scan", "scan", lambda b: f"scanning {b.get('target') or ''}".strip()),
                  ("prospect", "prospect", lambda b: f"prospecting {b.get('direction') or ''}".strip()),
                  ("repair", "repair", lambda b: f"repairing {b.get('target_device_code') or ''}".strip()))


def device_timers(dev: dict, rows: list[dict], now: datetime | None = None) -> list[dict]:
    """Every running command of one device with its finish time, for the device page: the ETAs in the device's own
    status (printing, scan, prospect, repair, the current leg of a multi-leg trip) and the app's timers for it
    (compacting, unfurling, triangulation, or a command answer the status doesn't show yet). The whole trip has its
    own card; a kind the status reports replaces the app's timer of that kind."""
    now = now or datetime.now(timezone.utc)
    out: dict[str, dict] = {}

    def add(kind, label, end, start):
        if not end:
            return
        start = start if start and start <= end else min(now, end)
        total = max((end - start).total_seconds(), 1)
        out[kind] = {"kind": kind, "label": label, "ends_at": end.isoformat(timespec="seconds"),
                     "started_at": start.isoformat(timespec="seconds"), "done": end <= now,
                     "pct": max(0, min(100, 100 * (now - start).total_seconds() / total))}

    def finish(b, done_key="completes_at"):
        end = parse_ts(b.get(done_key))
        if not end and b.get("eta_seconds") is not None:
            try:
                end = now + timedelta(seconds=float(b["eta_seconds"]))
            except (TypeError, ValueError):
                pass
        return end

    for r in rows:
        if r.get("device_code") == dev.get("device_code") and r.get("kind") != "travel":
            add(r.get("kind") or "timer", r.get("label") or "", parse_ts(r.get("ends_at")), parse_ts(r.get("started_at")))
    for key, kind, label in _STATUS_TIMERS:
        b = dev.get(key)
        if isinstance(b, dict) and b:
            add(kind, label(b), finish(b), parse_ts(b.get("started_at")))
    if "prospect" not in out and dev.get("is_prospecting"):
        add("prospect", f"prospecting {dev.get('prospect_direction') or ''}".strip(),
            parse_ts(dev.get("prospect_completes_at")), parse_ts(dev.get("prospect_started_at")))
    tr = dev.get("travel") or {}
    if isinstance(tr, dict) and tr.get("final_destination") and tr.get("destination") \
            and tr["final_destination"] != tr["destination"]:
        add("leg", f"{str(tr.get('stage') or tr.get('type') or 'leg').replace('_', ' ')} to {tr['destination']}",
            finish(tr, "arrives_at"), parse_ts(tr.get("departed_at")))
    return sorted(out.values(), key=lambda t: t["ends_at"])


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
    body = fl.with_renamed_tags(method, path, body, await st.db.kv_get(fl.RENAMES_KV, {}) or {})
    status, resp, err = 200, None, None
    from .vqueue import HAND
    hand = HAND.set(isinstance(body, dict) and body.get("command") == "enqueue_print")   # your prints go first
    try:
        resp = await st.api.request(method, path, json_body=body if method != "GET" else None)
        await st.worker.timers_from_response(path, resp, label)
    except ApiError as e:
        status, err, resp = e.status, e.message, e.body
    finally:
        HAND.reset(hand)
    await st.db.execute(
        "INSERT INTO actions(at, user, method, path, body, status, response) VALUES(?,?,?,?,?,?,?)",
        (now_iso(), user, method, path, json.dumps(body, ensure_ascii=False) if body is not None else None, status,
         json.dumps(resp, default=str, ensure_ascii=False)[:20000] if resp is not None else None),
    )
    request.state.action_response = resp if err is None else None
    if not err:
        st.hub.publish("state", "action")
        await record_evictions(st, path, resp)
        if method != "GET":
            st.worker.after_order(path, body, resp)
    return {"label": label, "method": method, "path": path, "ok": err is None, "status": status,
            "error": err, "response": resp}


async def record_evictions(st, path: str, resp: Any) -> None:
    """A ward's activate can evict other players' miners (`evicted_miners`): keep a log and say so in the bell."""
    from . import wardhub as wh
    ev = wh.evicted(resp)
    if not ev:
        return
    code = path.rstrip("/").split("/")[-1]
    log_ = await st.db.kv_get(wh.EVICTIONS_KV, []) or []
    log_.append({"at": now_iso(), "ward": code, "evicted": ev})
    await st.db.kv_set(wh.EVICTIONS_KV, log_[-200:])
    title = f"Ward {code} evicted {len(ev)} miner(s): {wh.eviction_text(ev)}"
    cur = await st.db.execute("INSERT INTO notifications(event_id, level, title, body, link, created_at) VALUES(?,?,?,?,?,?)",
                              (None, "info", title, None, "/wards", now_iso()))
    st.hub.publish("notify", {"id": cur.lastrowid, "level": "info", "title": title, "link": "/wards"})


def render_action(request: Request, outcome: dict, note: str | None = None) -> HTMLResponse:
    from .modular import ownership_hint
    hint = ownership_hint(outcome.get("error")).removeprefix(" — hint: ")
    if hint:
        note = (note + " " if note else "") + hint[0].upper() + hint[1:] + "."
    return partial(request, "partials/action_result.html", note=note, **outcome)


async def run_action(request: Request, user: str, method: str, path: str, body: Any, label: str) -> HTMLResponse:
    return render_action(request, await call_action(request, user, method, path, body, label))


def parse_json_field(text: str | None) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    return json.loads(text)


def stock_piles(inventory: Any, cat: dict) -> list[dict]:
    """Stockpiles by location rows: each location's sector (the star's catalog region) and resource total,
    sorted by sector (unknown last), then by total, largest first."""
    region = {x.get("designation"): x.get("region") for x in (cat or {}).get("stars") or [] if isinstance(x, dict)}
    rows = []
    for loc in inventory or []:
        items = (loc or {}).get("items") or {}
        total = sum(v for v in items.values() if isinstance(v, (int, float)) and v > 0)
        if not total:
            continue
        rows.append({"location": loc.get("location"), "items": items, "total": total,
                     "region": region.get(star_of(loc.get("location"))) or ""})
    rows.sort(key=lambda r: (not r["region"], r["region"], -r["total"], r["location"] or ""))
    return rows


from . import cargo as cargo_mod   # noqa: E402  (materials in holds: dashboard + logistics)


TUTORIALS_KV, TUTORIALS_TTL = "tutorials", 600   # seconds the list (and the current tutorial's steps) is reused


async def tutorials_status(request: Request, force: bool = False) -> dict:
    """GET /tutorials (each one's progress) and GET /tutorials/{slug} for the first unfinished one (its steps, the
    current one and its hint), cached for ten minutes on the background budget."""
    db = request.app.state.db
    c = await db.kv_get(TUTORIALS_KV, {}) or {}
    now = datetime.now(timezone.utc).timestamp()
    if force or now - float(c.get("t") or 0) > TUTORIALS_TTL:
        api = request.app.state.api
        try:
            body = await api.get("/tutorials", background=True) or {}
            tuts = sorted(body.get("tutorials") or [], key=lambda x: x.get("order") or 0)
            cur = next((x for x in tuts if not x.get("completed")), None)
            detail = (await api.get(f"/tutorials/{cur['slug']}", background=True) or {}) if cur else None
            c = {"t": now, "at": now_iso(), "tutorials": tuts, "detail": detail}
        except ApiError as e:
            c = {**c, "t": now, "error": str(e)}
        await db.kv_set(TUTORIALS_KV, c)
    return c


ACHIEVEMENTS_KV, ACHIEVEMENTS_TTL = "achievements", 1800


async def achievements_status(request: Request, force: bool = False) -> dict:
    """GET /achievements (every achievement in the game) and GET /accounts/achievements (yours), cached for half an
    hour on the background budget. Earned: matched by achievement_key (else title), newest first; the rest by category."""
    db = request.app.state.db
    c = await db.kv_get(ACHIEVEMENTS_KV, {}) or {}
    now = datetime.now(timezone.utc).timestamp()
    if force or now - float(c.get("t") or 0) > ACHIEVEMENTS_TTL:
        api = request.app.state.api
        try:
            mine = (await api.get("/accounts/achievements", background=True) or {}).get("achievements") or []
            try:
                every = (await api.get("/achievements", background=True) or {}).get("achievements") or []
            except ApiError:
                every = []   # the public list is a nice-to-have: yours still shows
            key = lambda a: str(a.get("achievement_key") or a.get("key") or a.get("title") or "").lower()
            got = {key(a): a for a in mine}
            earned = [{**next((x for x in every if key(x) == k), {}), **a} for k, a in got.items()]
            earned.sort(key=lambda a: str(a.get("achieved_at") or a.get("unlocked_at") or ""), reverse=True)
            left = sorted((a for a in every if key(a) not in got),
                          key=lambda a: (str(a.get("category") or ""), float(a.get("xp_reward") or 0), str(a.get("title") or "")))
            c = {"t": now, "at": now_iso(), "earned": earned, "left": left, "total": len(every) or None,
                 "xp": sum(float(a.get("xp_reward") or 0) for a in earned)}
        except ApiError as e:
            c = {**c, "t": now, "error": str(e)}
        await db.kv_set(ACHIEVEMENTS_KV, c)
    return c


async def _stock_card(request: Request, hours: int) -> HTMLResponse:
    """Dashboard › Stockpiles in the charts' style (owner 2026-11-02): one chart per resource, its own color, totals across
    every location from inventory_history (a row per resource each inventory poll), thinned to about 300 points."""
    from . import metrics
    db = request.app.state.db
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    raw: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for r in await db.fetchall("SELECT ts, resource, qty FROM inventory_history WHERE ts >= ? ORDER BY ts", (since,)):
        raw[r["resource"]].append((r["ts"], r["qty"]))
    totals = await db.kv_get("inventory_totals", {}) or {}
    st = await load_state(request)
    transit = (await cargo_mod.in_transit(db, st["devices"], await request.app.state.worker.automations.jobs())).get("totals") or {}
    names = [r for r in RESOURCES if r in totals or r in raw] + sorted((set(totals) | set(raw)) - set(RESOURCES))
    charts = []
    for i, r in enumerate(names):
        pts = raw.get(r) or []
        step = max(1, len(pts) // 300)
        pts = pts[::step] + ([pts[-1]] if pts and (len(pts) - 1) % step else [])
        title = r + (f" · + {f_num(transit[r])} in transit" if transit.get(r) else "")
        # a resource keeps its color whichever others are shown: the RESOURCES order, then the rest
        charts.append((title, metrics.chart({"in stockpiles": pts}, hours), RESOURCES.index(r) if r in RESOURCES else i))
    return partial(request, "partials/backlog.html", hours=hours, card="stock", title="Stockpiles", charts=charts,
                   synced=(await db.kv_get("sync:inventory") or {}).get("at"))


# --- fleet ------------------------------------------------------------------------------
# Tag kinds the Device list's tag filter folds into one "any" entry each (one tag per pinned place, contract or trip:
# 31 civ-beacon pins alone on 2026-10-28). A single one is still found by typing it in the search box.
TAG_GROUPS = (("at:", "pinned at a place"), ("to:", "bound for a system"), ("contract:", "for a contract"),
              ("home:", "home system"), ("boot:", "bootstrap"))


def tag_matches(tag: str, tags: list) -> bool:
    """A Device list tag filter: one tag, or a kind (`at:`, ending in a colon) matching every tag of that kind."""
    return any(str(t).startswith(tag) for t in tags) if tag.endswith(":") else tag in tags


def tag_options(devices: list[dict], chosen: str = "") -> dict:
    """The tag filter's choices: plain tags, fleets, and one entry per folded kind, each with its device count."""
    count = Counter(t for d in devices for t in set(d.get("tags") or []))
    plain, fleets, kinds = [], [], {}
    for t, n in sorted(count.items()):
        kind = next((k for k, _ in TAG_GROUPS if t.startswith(k)), None)
        if kind:
            kinds.setdefault(kind, [0, 0])[0] += 1
        elif t.startswith("fleet:"):
            fleets.append((t, n))
        else:
            plain.append((t, n))
    for d in devices:
        for k in {k for t in d.get("tags") or [] for k, _ in TAG_GROUPS if str(t).startswith(k)}:
            kinds[k][1] += 1
    groups = [(k, label, *kinds[k]) for k, label in TAG_GROUPS if k in kinds]
    extra = chosen if chosen and not chosen.endswith(":") and chosen not in {t for t, _ in plain + fleets} else ""
    return {"plain": plain, "fleets": fleets, "groups": groups, "extra": extra}


def filter_devices(devices: list[dict], q: str, dtype: str, status: str, star: str, tag: str, owner: str = "") -> list[dict]:
    out = []
    ql = q.lower().strip()
    for d in devices:
        if dtype and d.get("device_type") != dtype:
            continue
        if status and status_class(d.get("status")) != status:
            continue
        if star and star_of(d.get("location")) != star:
            continue
        if tag and not tag_matches(tag, d.get("tags") or []):
            continue
        if owner and d.get("replicant_code") != owner:
            continue
        if ql and ql not in json.dumps(d).lower():
            continue
        out.append(d)
    return sorted(out, key=lambda d: (d.get("location") or "", d.get("device_type") or "", d.get("device_code") or ""))


async def fleet_shown(request: Request, st: dict) -> list[dict]:
    """The device list's devices; a stowed / attached device has no location of its own: show where its carrier is
    ("in <carrier>")."""
    by = {d.get("device_code"): d for d in st["devices"]}
    stowed = await request.app.state.db.kv_get("stowed_map", {}) or {}
    held_by = {c: k for k, kids in stowed.items() for c in kids}
    shown = []
    for d in st["devices"]:
        if not d.get("location"):
            carrier = d.get("stowed_in_device_code") or d.get("attached_to_device_code") or held_by.get(d.get("device_code"))
            for _ in range(3):   # a carrier may itself ride in another
                c = by.get(carrier or "") or {}
                if c.get("location") or not c:
                    break
                carrier = c.get("stowed_in_device_code") or c.get("attached_to_device_code") or held_by.get(carrier)
            if (by.get(carrier or "") or {}).get("location"):
                d = {**d, "location": by[carrier]["location"], "carried_by": carrier}
        shown.append(d)
    return shown


# --- autofactory print queue ------------------------------------------------------------------
async def print_queue_ctx(request: Request, code: str, dev: dict, outcome: dict | None = None, compact: bool = False) -> dict:
    db = request.app.state.db
    bps = {b["device_type"]: b for b in normalize_blueprints(await db.kv_get("blueprints", []))}
    queue = printqueue.items(dev)
    cur = await printqueue.current(db, dev)
    remaining, exact = printqueue.remaining_seconds(cur, queue, bps)
    cmds = dev.get("available_commands") or []
    # docs (autofactories): cancel the current print with {"command": "deactivate"}
    cancel_cmd = next((c for c in ("deactivate", "cancel_print", "cancel") if c in cmds), None)
    st = await load_state(request)
    known = {star_of(d.get("location")) for d in st["devices"] if d.get("location")}
    known |= {r["star"] for r in await db.fetchall("SELECT star FROM systems")}
    edits = (await db.kv_get(printqueue.EDITS_KV, {}) or {}).get(code) or []
    if cur:
        cur = {**cur, "edit": printqueue.edit_for(edits, cur.get("device_type"), cur.get("tags") or [])}
    queue = [{**it, "edit": printqueue.edit_for(edits, it["device_type"], it["tags"])} for it in queue]
    return {"code": code, "dev": dev, "queue": queue, "cur": cur, "remaining": remaining, "exact": exact,
            "blueprints": sorted(bps.values(), key=lambda b: b.get("device_type", "")), "outcome": outcome,
            "cancel_cmd": cancel_cmd, "compact": compact, "dest_stars": sorted(k for k in known if k),
            "here_star": star_of(dev.get("location"))}


async def fetch_device(request: Request, code: str) -> dict:
    try:
        return await request.app.state.api.get(f"/devices/{code}")
    except ApiError:
        return next((d for d in (await load_state(request))["devices"] if d.get("device_code") == code), {})


async def render_print_queue_group(request: Request, codes: str, error: str | None = None) -> HTMLResponse:
    from . import vqueue as vqm
    db = request.app.state.db
    bps = {b["device_type"]: b for b in normalize_blueprints(await db.kv_get("blueprints", []))}
    facs, own, shared = [], [], []
    for code in [c.strip().upper() for c in codes.split(",") if c.strip()][:20]:
        dev = await fetch_device(request, code)
        if not dev:
            continue
        q = printqueue.items(dev)
        real = [x for x in q if not x["extra"].get("virtual")]
        cur = await printqueue.current(db, dev)
        facs.append({"code": code, "dev": dev, "cur": cur, "queue": real})
        own.append((cur, real))
        shared += [{**x, "factory": code} for x in q if x["extra"].get("virtual")]
    rank = {v: k for k, v in vqm.LABELS.items()}
    shared.sort(key=lambda x: rank.get(x["extra"].get("priority"), 9))
    remaining, exact = printqueue.group_seconds(own, shared, bps)
    first = (facs[0]["dev"] if facs else {})
    return partial(request, "partials/print_queue_group.html", facs=facs, shared=shared, remaining=remaining, exact=exact,
                   codes=",".join(f["code"] for f in facs), location=first.get("location"), dtype=first.get("device_type"),
                   waiting=sum(len(f["queue"]) for f in facs) + len(shared), printing=sum(1 for f in facs if f["cur"]),
                   error=error)


DANGEROUS = {"decommission", "change_owner", "deactivate", "withdraw", "clear_queue", "release", "clear_directive"}


def with_attach(dev: dict, same_loc: list[dict]) -> list[str]:
    """The device's commands, plus attach when a carrier with attach points is here: the carrier attaches it (any device,
    stowable ones too: an FTL relay attached to a mobile fleet, live 2026-10-20), though the game doesn't list attach
    among the cargo's own commands. Nothing else is added: the game's command list is the word (a fleet tender lists no
    repair command yet, 2026-11-12: not implemented in the game, Joe)."""
    cmds = list(dev.get("available_commands") or [])
    if "attach" in cmds or dev.get("attached_to_device_code") or dev.get("stowed_in_device_code") \
            or dev.get("device_type") == "cargo_freighter":
        return cmds
    if any(fl.capacity(d) > 0 and "attach" in (d.get("available_commands") or ["attach"]) for d in same_loc):
        cmds.append("attach")
    return cmds


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
    """Device choices for the "then, on arrival" rows: the traveler, what it carries, what's nearby, the rest."""
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


def saving_note(via: list[str], direct: Any, planned: Any) -> str:
    """The planned waypoints and the time they save on the game's own route (two travel previews)."""
    d_s, v_s = hubroute.total_seconds(direct), hubroute.total_seconds(planned)
    note = f"Re-routed via {', '.join(via)}"
    if d_s and v_s:
        note += (f": saves {hubroute.hm(d_s - v_s)} ({round(100 * (d_s - v_s) / d_s)} %) on the game's direct route of "
                 f"{hubroute.hm(d_s)}" if d_s > v_s else f": no faster than the game's direct route ({hubroute.hm(d_s)})")
    return note + "."


def loop_refused(err: Any) -> bool:
    """The game refuses a surge spot inside the system as a waypoint from the entry point ("Loop detected: already at
    ISOLITHOR"); the job runner then hops there first and cruises the rest (Automations._hop_legs)."""
    return "loop detected" in str(err or "").lower()


HOP_FIRST_NOTE = "Go hops to {via} first (a 30 s surge hop), then cruises the rest: the game refuses that waypoint from here."


async def hop_first_chain(request: Request, user: str, path: str, body: dict, device: str, via: list[str]) -> HTMLResponse:
    """A hand-sent trip inside one system whose surge spot the game refused as a loop (live 2026-10-29: 5DF3161B
    ISOLITHOR-1-L4 → ISOLITHOR-BELT-1 then cruised 499 AU for 57 min): a job that hops to the spot, then goes on."""
    dest = str(body.get("destination") or "").upper()
    first = auto.step(f"{device} → {dest}", path, {**body, "via": via}, wait=["travel.arrived"],
                      match={"destination": dest}, critical=True)
    first.update(wait_device=device, exit_checked=True, hub_checked=True)
    return await start_chain(request, user, f"{device} → {dest} (surge hop to {', '.join(via)} first)", first, [], device)


RAMP_NOTE = ("Go lands at {entry}, hops to {ramp} (a 30 s surge hop), then cruises the rest: surges into {star} land "
             "at its entry point, and one trip from there would cruise the whole way.")


async def ramp_note(request: Request, ramp: str) -> str:
    eng = request.app.state.worker.automations
    return RAMP_NOTE.format(entry=await eng._hub_entry(star_of(ramp)) or "the entry point", ramp=ramp, star=star_of(ramp))


def ramp_step(path: str, body: dict, device: str) -> dict:
    """A hand-sent trip into a system whose surges land at its entry point, with an off-ramp there (trip_plan): the job
    runner plans it like its own trips and splits it (Automations._ramp_legs: the entry point, the hop, the rest)."""
    dest = str(body.get("destination") or "").upper()
    first = auto.step(f"{device} → {dest}", path, {k: v for k, v in body.items() if k != "via"}, wait=["travel.arrived"],
                      match={"destination": dest}, critical=True)
    first.update(wait_device=device, sector_ok=True)
    return first


async def planned_device_trip(request: Request, user: str, code: str, body: dict) -> HTMLResponse:
    """A device's trip from its command form, planned like automated trips (surge hops, a hub, the off-ramp: the game's
    own route takes none of them). A preview says what the plan saves; a refused plan goes the game's way, except a
    surge spot refused as a loop, which the device hops to first."""
    path, label = f"/devices/{code}", f"travel on {code}"
    via, ramp = await request.app.state.worker.automations.trip_plan(code, str(body["destination"]).upper())
    if ramp and not body.get("dry_run"):
        dest = str(body["destination"]).upper()
        return await start_chain(request, user, f"{code} → {dest} (hops to {ramp} at arrival)", ramp_step(path, body, code),
                                 [], code)
    if not via:
        out = await call_action(request, user, "POST", path, body, label)
        return render_action(request, out, note=await ramp_note(request, ramp) if ramp else None)
    out = await call_action(request, user, "POST", path, {**body, "via": via}, f"{label} via {', '.join(via)}")
    if not out["ok"] and loop_refused(out["error"]):
        if not body.get("dry_run"):
            return await hop_first_chain(request, user, path, body, code, via)
        return render_action(request, await call_action(request, user, "POST", path, body, label),
                             note=HOP_FIRST_NOTE.format(via=", ".join(via)))
    if not out["ok"]:
        return render_action(request, await call_action(request, user, "POST", path, body, label),
                             note=f"The planned waypoints ({', '.join(via)}) were refused ({out['error']}): the game's own route.")
    note = f"Planned route via {', '.join(via)}."
    if ramp:
        note += " " + await ramp_note(request, ramp)
    elif body.get("dry_run"):
        try:
            note = saving_note(via, await request.app.state.api.request("POST", path, json_body=body), out["response"])
        except ApiError:
            pass
    return render_action(request, out, note=note)


async def long_cruise_warning(request: Request, code: str, dest: str) -> str | None:
    """A manual trip inside one system longer than the max cruise (Fleets › settings): the warning to show, else None."""
    dev = await _device(request, code) or {}
    here = dev.get("location")
    if not here or not dest or star_of(here) != star_of(dest.upper()) or "surge" in (dev.get("features") or []):
        return None   # a surge-capable device isn't held to it
    eng = request.app.state.worker.automations
    au, limit = fl.cruise_au(here, dest.upper(), await eng.cruise_radii()), await eng.max_cruise_au()
    if au <= limit:
        return None
    return (f"{code} would cruise ≈{au:.0f} AU from {here} to {dest.upper()} — over the {limit:g} AU limit "
            "(Fleets › settings), likely hours. A carrier can fetch it instead.")


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
    cap = carrier_mod.stow_capacity(dev, bps)
    points = carrier_mod.attach_points(dev)
    can_hold = bool(cap) or (not points and carrier_mod.is_carrier(dev, bps))
    rows = carrier_mod.carrier_rows(dev, stowed, st["devices"], can_hold=can_hold, can_attach=points > 0)
    if "travel" in (dev.get("available_commands") or []):
        travel_path = f"/devices/{code}"
    elif rep:
        travel_path = f"/replicants/{rep[0]}/travel"
    else:
        travel_path = None
    return {"rows": rows, "capacity": cap if can_hold else None, "carried": sum(1 for r in rows if r["carried"] and r["mode"] == "stow"),
            "points": points or None, "attached": sum(1 for r in rows if r["carried"] and r["mode"] == "attach"),
            "can_hold": can_hold, "can_attach": points > 0,
            "travel_path": travel_path, "replicant": rep[0] if rep else None, "star": carrier_mod.star_of(dev.get("location"))}


SLINGSHOT_MIN_CAPACITY = 80.0   # docs: a slingshot needs ≥80 % capacity and drops to 5 % per use


def where_is(d: dict, by: dict[str, dict], stowed_map: dict[str, list[str]] | None = None) -> str | None:
    """A device's location, or its carrier's when it's stowed / attached."""
    if d.get("location"):
        return d["location"]
    host = d.get("stowed_in_device_code") or d.get("attached_to_device_code") or next(
        (v for v, kids in (stowed_map or {}).items() if d.get("device_code") in kids), None)
    return (by.get(host) or {}).get("location") if host else None


BOBNET_KV = "bobnet_sent"     # [{channel, text, at}]: what we posted lately, to refuse a repeat
BOBNET_REPEAT_MINUTES = 10


def _bobnet_key(channel: str, text: str) -> tuple[str, str]:
    return ((channel or "").strip().lower(), " ".join((text or "").split()).lower())


async def bobnet_repeat(db, channel: str, text: str) -> str | None:
    """When the same text went to the same channel in the last few minutes: when (to refuse a double send)."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=BOBNET_REPEAT_MINUTES)
    for m in await db.kv_get(BOBNET_KV, []) or []:
        at = parse_ts(m.get("at"))
        if at and at > cutoff and _bobnet_key(m.get("channel"), m.get("text")) == _bobnet_key(channel, text):
            return m["at"]
    return None


async def bobnet_note_sent(db, channel: str, text: str) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=BOBNET_REPEAT_MINUTES)
    sent = [m for m in await db.kv_get(BOBNET_KV, []) or [] if (parse_ts(m.get("at")) or cutoff) > cutoff]
    await db.kv_set(BOBNET_KV, sent[-50:] + [{"channel": channel, "text": text, "at": now_iso()}])


def scan_tags(rows) -> list[tuple[str, list[str]]]:
    """[(tag, [stars])]: every system tag in the cached scans (system_tags), with the systems carrying it, most common
    first."""
    by: dict[str, set[str]] = defaultdict(set)
    for r in rows:
        try:
            tags = json.loads(r["tags"]) if r["tags"] else []
        except ValueError:
            continue
        for t in tags if isinstance(tags, list) else []:
            by[str(t)].add(r["star"])
    return sorted(((t, sorted(v)) for t, v in by.items()), key=lambda x: (-len(x[1]), x[0]))


def _angle(code: str) -> float:
    return int(hashlib.md5(code.encode()).hexdigest()[:6], 16) % 360 * math.pi / 180


def build_system_view(star: str, scan: dict, devices: list[dict], inventory: list[dict],
                      places: list[dict] | None = None, res: dict | None = None,
                      groups: list[dict] | None = None, star_pos: dict[str, dict] | None = None,
                      others: list[dict] | None = None, drone_sites: dict[str, str] | None = None) -> dict:
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
    # the star's disc: 12 px for the Sun, growing gently with its radius, inside the innermost orbit (28 px)
    rad, measured = star_radius({"spectral_type": (scan.get("star") or {}).get("spectral_type")},
                                (scan.get("star") or {}).get("radius_solar"))
    shapes["star_r"] = round(min(22.0, max(5.0, 12 * rad ** 0.4)), 1) if rad else 12
    shapes["star_radius"] = {"r": rad, "measured": measured} if rad else None
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
                                      "stock": inv.get(loc, {}), "drones": drone_summary(by_loc.get(loc, []))})
    # other players' fixed devices (others.py snapshot): hollow coral rings beside ours
    theirs: dict[str, list[dict]] = defaultdict(list)
    for d in others or []:
        if star_of(d.get("location")) == star:
            theirs[d["location"]].append(d)
    shapes["others"] = []
    for loc, ds in sorted(theirs.items()):
        xy = loc_xy(loc)
        if xy:
            shapes["others"].append({"loc": loc, "x": xy[0] + 7, "y": xy[1] - 7, "devices": ds})
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

    # working drones circle the site they work (a survey drone's tracking_site, a miner's last mining event), else
    # their location's marker
    at_site = {pl["code"]: (pl["x"], pl["y"]) for pl in shapes["places"]}
    at_loc = {m["loc"]: (m["x"], m["y"], 5 + min(len(m["devices"]), 12)) for m in shapes["markers"]}
    rings: dict[str, dict] = {}
    for loc, ds in by_loc.items():
        for d in ds:
            site = d.get("tracking_site") or (drone_sites or {}).get(d.get("device_code"))
            if site in at_site and site.startswith(loc + "-"):
                key, (x, y), r0 = site, at_site[site], 5
            elif loc in at_loc:
                key, (x, y, r0) = loc, at_loc[loc]
            else:
                continue
            rings.setdefault(key, {"x": x, "y": y, "r0": r0, "devices": []})["devices"].append(d)
    shapes["orbits"] = [{**g, "orbits": o} for _, g in sorted(rings.items()) if (o := drone_orbits(g["devices"], g["r0"]))]

    # devices in transit: cruising inside the system along their leg, surging in or out at the rim (in the direction
    # of the other star). segs: [x1, y1, x2, y2, t0 ms, t1 ms] — the page moves the arrow along them as time passes.
    me = (star_pos or {}).get(star)

    def rim(other: str) -> tuple[float, float]:
        o = (star_pos or {}).get(other)
        if me and o:
            dx, dy = (o.get("x") or 0) - (me.get("x") or 0), -((o.get("y") or 0) - (me.get("y") or 0))
        else:
            a = _angle(other)
            dx, dy = math.cos(a), math.sin(a)
        n = math.hypot(dx, dy) or 1.0
        return (c + (c - 16) * dx / n, c + (c - 16) * dy / n)

    shapes["movers"] = []
    for g in groups or []:
        segs, way = [], set()
        for x in g["legs"]:
            fs, ts = star_of(x["from"]), star_of(x["to"])
            if fs == ts == star:
                a, b = loc_xy(x["from"]), loc_xy(x["to"])
                way.add("local")
            elif fs == star:
                a, b = loc_xy(x["from"]), rim(ts)
                way.add("out")
            elif ts == star:
                a, b = rim(fs), loc_xy(x["to"])
                way.add("in")
            else:
                continue
            if a and b:
                segs.append([round(a[0], 1), round(a[1], 1), round(b[0], 1), round(b[1], 1), int(x["t0"] * 1000), int(x["t1"] * 1000)])
        if segs:
            other = star_of(g["destination"]) if "out" in way else star_of(g["origin"]) if "in" in way else None
            shapes["movers"].append({"label": g["label"], "codes": g["codes"], "destination": g["destination"],
                                     "origin": g["origin"], "segs": segs, "t0": int(g["t0"] * 1000), "t1": int(g["t1"] * 1000),
                                     "way": "out" if "out" in way else "in" if "in" in way else "local", "other": other})
    return shapes


async def system_view_model(request: Request, star: str, scan: dict, st: dict, sys_t: dict | None = None,
                            res: dict | None = None) -> dict:
    """build_system_view from what's stored (no game calls): the System page and the desktop wallpaper."""
    db = request.app.state.db
    sys_t = sys_t if sys_t is not None else await system_targets(db, star)
    res = res if res is not None else await system_resources(db, star)
    from . import transit
    cat = await db.kv_get("stars", {}) or {}
    star_pos = {x.get("designation"): x.get("position") for x in cat.get("stars") or [] if isinstance(x, dict) and x.get("position")}
    from . import others as oth
    snap = oth.normalize(await db.kv_get(oth.KV, {}))["stars"].get(star) or {}
    return build_system_view(star, scan, st["devices"], st["inventory"], sys_t["targets"], res,
                             transit.trips(st["devices"]), star_pos, others=snap.get("devices"),
                             drone_sites=await mining_sites(db, star))


async def mining_sites(db, star: str) -> dict[str, str]:
    """{device code: the site its last mining.started / retargeted / relocated event names} in a system."""
    out: dict[str, str] = {}
    for r in await db.fetchall("SELECT device_code, event, payload FROM events WHERE event IN ('mining.started', "
                               "'mining.retargeted', 'mining.relocated') AND (star = ? OR location LIKE ?) ORDER BY seq",
                               (star, f"{star}-%")):
        try:
            p = json.loads(r["payload"] or "{}") or {}
        except ValueError:
            continue
        if site := p.get("new_site") if r["event"] == "mining.relocated" else p.get("site"):
            out[r["device_code"]] = site
    return out


def devices_in_system(devices: list[dict], star: str) -> dict:
    """Your devices in a system for the System page: those located there, plus those riding aboard a carrier that is,
    sorted by place then type; with a per-type summary (working / moving / idle)."""
    by = {d.get("device_code"): d for d in devices}
    rows = []
    for d in devices:
        ride = d.get("stowed_in_device_code") or d.get("attached_to_device_code")
        loc = d.get("location") or (by.get(ride) or {}).get("location")
        if star_of(loc) != star:
            continue
        fleet = next((t[6:] for t in d.get("tags") or [] if t.startswith("fleet:")), None)
        rows.append({**d, "_where": d.get("location") or f"aboard {ride}", "_sort": loc or "", "_fleet": fleet,
                     "_class": status_class(d.get("status"))})
    rows.sort(key=lambda d: (d["_sort"], d["_where"], d.get("device_type") or "", d["device_code"]))
    summary: dict[str, Counter] = defaultdict(Counter)
    for d in rows:
        summary[d.get("device_type") or "device"][d["_class"]] += 1
    return {"rows": rows, "summary": sorted(((t, sum(c.values()), c) for t, c in summary.items()), key=lambda x: (-x[1], x[0]))}


async def survey_state(db, star: str, scan: dict) -> tuple[dict, str | None]:
    """({body: when surveyed} for this system's planets and belts, when the whole system counts as surveyed or None).
    The whole system: an AMI survey controller finished it (ami_surveyed), or every planet and belt is marked."""
    from .automations import survey_targets
    marks = await db.kv_get("surveyed", {}) or {}
    bodies = [t["target"] for t in survey_targets(scan or {}, {}, False, True, 10_000)]
    mine = {b: marks[b] for b in bodies if b in marks}
    full = (await db.kv_get("ami_surveyed", {}) or {}).get(star)
    if not full and bodies and len(mine) == len(bodies):
        full = max(mine.values())
    return mine, full


async def system_layout(db, star: str, scan: dict, st: dict, sys_t: dict, res: dict, viability: list[dict]):
    """The system page's three cards: belts (richness, viability, open sites, ≈ units left), one row per resource across
    them, and every other body (planets, moons, L-points … with their salvage)."""
    from .targets import site_yields
    yb, yl = await site_yields(db)
    vrow = {v["belt"]: v for v in viability}
    devs_at = Counter(d.get("location") for d in st["devices"])
    belts = []
    for b in ((scan.get("asteroid_belt") or {}).get("belts")) or []:
        code = b.get("designation")
        if not code:
            continue
        sites = [x for x in res["sites_shown"] if x.get("belt") == code]
        left, basis = {}, {}
        for r, lvl in (b.get("resources") or {}).items():
            avg, n = yb.get((code, r)) or yl.get((lvl, r)) or (None, 0)
            if avg is None:
                continue
            pct = sum(float((x.get("remaining_pct") or {}).get(r, 0)) for x in sites) / 100.0
            left[r] = round(pct * avg)
            basis[r] = {"avg": round(avg), "n": n, "own": (code, r) in yb}
        belts.append({**b, "code": code, "sites": sites, "viability": vrow.get(code), "devices": devs_at.get(code, 0),
                      "left": left, "basis": basis})
    resources = []
    for r, t in res["totals"].items():
        pcts = [float((x.get("remaining_pct") or {})[r]) for x in res["sites_shown"] if r in (x.get("remaining_pct") or {})]
        est = [b["left"][r] for b in belts if r in b["left"]]
        resources.append({"resource": r, "levels": {b["code"]: (b.get("resources") or {}).get(r) for b in belts
                                                   if (b.get("resources") or {}).get(r)},
                          "open_sites": len(pcts), "pct_left": round(sum(pcts) / len(pcts)) if pcts else None,
                          "left": sum(est) if est else None,
                          "guess": any(not b["basis"].get(r, {}).get("own", True) for b in belts if r in b["left"]),
                          "salvage": t.get("salvage") or 0, "stock": t.get("stock") or 0})
    planets = {p.get("designation"): p for p in scan.get("planets") or []}
    salvage_on: dict[str, list] = defaultdict(list)
    for x in res["salvage_shown"]:
        salvage_on[x.get("body")].append(x)
    bodies = [{**t, "planet": planets.get(t["code"]), "salvage": salvage_on.get(t["code"], [])}
              for t in sys_t["targets"] if t["category"] not in ("belt", "site", "salvage")]
    known = {t["code"] for t in bodies}
    bodies += [{"code": c, "category": "planet", "planet": p, "salvage": salvage_on.get(c, []), "devices": 0}
               for c, p in planets.items() if c and c not in known]
    order = {"star": 0, "planet": 1, "moon": 2, "lagrange": 3}
    def au(t: dict) -> float:   # moons and L-points sort with their planet (STAR-3-1, STAR-3-L4 → STAR-3)
        parent = planets.get("-".join(t["code"].split("-")[:2])) or {}
        return float(parent.get("orbital_distance_au") or (0 if t["category"] == "star" else 99))
    bodies.sort(key=lambda t: (au(t), order.get(t["category"], 9), t["code"]))
    return belts, resources, bodies


async def _bp_map(request: Request) -> dict[str, dict]:
    from .shapes import normalize_blueprints
    return {b["device_type"]: b for b in normalize_blueprints(await request.app.state.db.kv_get("blueprints", []))}


async def relays_on(db, devices: list[dict]) -> list[dict]:
    """The devices, with `_on` set on relays the game reports out_of_range that are switched on. Their status reads the
    same on or off (F6A3A357 at KAMUYIS, 2026-10-15), so the last word is the latest of: an activate / deactivate
    command that went through, the game's relay.activated event, and the last time the device list showed it
    relaying or inactive (e.g. with a replicant there: kv relay_state)."""
    import json as _json
    codes = [d["device_code"] for d in devices if "relay" in (d.get("device_type") or "")
             and str(d.get("status") or "").startswith("out_of_range")]
    if not codes:
        return devices
    last: dict[str, tuple[str, bool]] = {}
    q = ",".join("?" * len(codes))
    for r in await db.fetchall(f"SELECT path, body, at FROM actions WHERE status < 300 AND path IN ({q})",
                               tuple(f"/devices/{c}" for c in codes)):
        try:
            cmd = (_json.loads(r["body"] or "{}") or {}).get("command")
        except ValueError:
            continue
        if cmd in ("activate", "deactivate"):
            code = r["path"].rsplit("/", 1)[-1]
            if r["at"] >= last.get(code, ("", False))[0]:
                last[code] = (r["at"], cmd == "activate")
    for r in await db.fetchall(f"SELECT device_code, received_at FROM events WHERE event = 'relay.activated' "
                               f"AND device_code IN ({q})", tuple(codes)):
        if r["received_at"] >= last.get(r["device_code"], ("", False))[0]:
            last[r["device_code"]] = (r["received_at"], True)
    for code, s in (await db.kv_get("relay_state", {}) or {}).items():   # last seen relaying / inactive (ingest)
        if code in codes and (s.get("at") or "") >= last.get(code, ("", False))[0]:
            last[code] = (s.get("at") or "", bool(s.get("on")))
    return [{**d, "_on": True} if last.get(d.get("device_code"), ("", False))[1] else d for d in devices]


async def relay_overlay(request: Request, st: dict, positions: dict) -> dict:
    """The galaxy map's relay layer: links and nodes per network (active = a replicant inside its coverage: solid;
    inactive: dashed), relays on order (planned) and the suggested bridges."""
    from . import relaynet
    from . import ward_orders as wo
    devs = await relays_on(request.app.state.db, st["devices"])
    rn = relaynet.networks(devs, positions, await _bp_map(request))
    rstars = relaynet.replicant_stars(st["replicants"], st["devices"])
    relaynet.coverage(rn, positions, rstars)
    nets = rn["networks"]
    ends = lambda net_id, star: min(nets[net_id]["stars"], key=lambda x: math.dist(  # noqa: E731
        rn["nodes"][x]["xyz"], relaynet._xyz(positions.get(star)) or (0, 0, 0)))
    from . import relayroll as rr
    long = bool(rr.state(await request.app.state.db.kv_get(rr.KV, {}))["long"])
    suggested = []
    for b in await relaynet.bridges_async(rn, positions, long=long):
        suggested.append({"stars": b["stars"], "from": ends(b["a"], b["stars"][0]), "to": ends(b["b"], b["stars"][-1]),
                          "n": b["n"], "long": b["long"]})
    orders = {**(await request.app.state.db.kv_get(wo.kv_key("station"), {}) or {}),
              **(await request.app.state.db.kv_get(wo.kv_key("relay"), {}) or {})}
    # a hub's links get the hub color; two relays that are both linked to the same hub are connected through it, so
    # their own link isn't drawn
    hub_of: dict[str, set[str]] = defaultdict(set)
    for l in rn["links"]:
        for x, y in ((l["a"], l["b"]), (l["b"], l["a"])):
            if "hub" in rn["nodes"][y]["kinds"]:
                hub_of[x].add(y)
    shown = [l for l in rn["links"] if l.get("hub") or not (hub_of[l["a"]] & hub_of[l["b"]])]
    return {"links": [{"a": l["a"], "b": l["b"], "color": nets[l["net"]]["color"], "active": nets[l["net"]]["active"],
                       "hub": bool(l.get("hub"))} for l in shown],
            "nodes": [{"star": s, "color": nets[i]["color"], "net": i, "active": nets[i]["active"]} for s, i in rn["of"].items()],
            "planned": sorted(orders), "suggested": suggested,
            # relays of yours that are switched off (inactive, or out of range and never switched on): red rings
            "off": sorted({star_of(d.get("location")) for d in devs if "relay" in (d.get("device_type") or "")
                           and d.get("location") and not d.get("stowed_in_device_code") and not d.get("attached_to_device_code")
                           and (str(d.get("status") or "").startswith("inactive")
                                or (str(d.get("status") or "").startswith("out_of_range") and not d.get("_on")))
                           and star_of(d.get("location")) not in rn["of"]})}


def relaynet_max() -> int:
    from .relaynet import MAX_BRIDGE
    return MAX_BRIDGE


_MAP_LAST: dict[str, Any] = {}


def mining_now(devices: list[dict]) -> dict[str, int]:
    """What a system is producing right now: drones mining each resource ("mining (carbon)" → carbon)."""
    out: Counter = Counter()
    for d in devices:
        m = re.match(r"mining \((\w+)\)", str(d.get("status") or ""))
        if m:
            out[m.group(1)] += 1
    return dict(out)


STAR_FIELDS = ("designation", "name", "position", "color", "spectral_type", "region", "estimated_planets", "entry_point",
               "has_hub", "has_ward", "has_life", "explored", "from_census", "from_observatory")


# typical main-sequence radius by spectral class (solar radii): the map's star size until a scan gives the real one
CLASS_RADIUS = {"O": 10.0, "B": 4.0, "A": 1.7, "F": 1.3, "G": 1.0, "K": 0.8, "M": 0.4, "L": 0.12, "T": 0.1, "D": 0.02}


def star_radius(s: dict, known: float | None = None) -> tuple[float | None, bool]:
    """(radius in solar radii, whether it's measured): a scan's radius_solar when there is one, else the typical radius
    of the star's spectral class (giants, class III and brighter, ten times that), else None."""
    for v in (known, s.get("radius_solar")):
        try:
            if v is not None and float(v) > 0:
                return float(v), True
        except (TypeError, ValueError):
            pass
    sp = str(s.get("spectral_type") or "").strip().upper()
    r = CLASS_RADIUS.get(sp[:1])
    if r is None:
        return None, False
    lum = re.search(r"(IV|V|III|II|I)[AB]?$", sp[1:])
    return (r * 10 if lum and lum.group(1) in ("I", "II", "III") else r), False


def scan_tag_list(tags: Any) -> list[str]:
    """A scan's system tags (system_tags, a list or its JSON text)."""
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except ValueError:
            return []
    return [str(t) for t in tags] if isinstance(tags, list) else []


def hazard_tags(tags: Any) -> list[str]:
    """The system tags of a scan that mark the system hazardous."""
    return [t for t in scan_tag_list(tags) if "hazard" in t.lower()]


async def map_hazards(eng) -> dict[str, dict]:
    """{star: {"objects": [...], "hours_left": soonest}}: systems an incoming object (Defence) still threatens, by the
    object's own system and its impact target's. The map hazes them in red."""
    out: dict[str, dict] = {}
    for a in await eng.defence_report():
        if not a.get("threat"):
            continue
        for star in {star_of(a.get("designation")), star_of(a.get("target"))} - {""}:
            h = out.setdefault(star, {"objects": [], "hours_left": None})
            h["objects"].append(a["designation"])
            if a.get("hours_left") is not None and (h["hours_left"] is None or a["hours_left"] < h["hours_left"]):
                h["hours_left"] = a["hours_left"]
    return out


async def map_payload(request: Request, part: str = "all") -> dict:
    """The galaxy map's data (the Galaxy page and the desktop wallpaper), in two parts so the map can draw the stars
    first: part="core" — the stars (only the fields the map uses), where your devices are, replicants; part="overlay"
    — per-star drones and mining, ships in transit, fleets, supply lines; "all" — both. The core comes in two goes when asked
    (`scope`): "mine" — the systems with your devices, replicants, fleet homes, scans or observatory finds, so the map
    can draw them first; "rest" — every other star in the catalog."""
    db = request.app.state.db
    st = await load_state(request)
    if sim_view(request):
        from .sim_world import world
        cat = {"stars": (await world(request.app, sim_view(request)))["stars"]}
    else:
        cat = await db.kv_get("stars", {}) or {}
    out: dict = {}
    if part in ("core", "all"):
        presence = Counter(star_of(d.get("location")) for d in st["devices"])
        infra: dict[str, list[str]] = defaultdict(list)
        from . import relaynet as _rn
        bpm = await _bp_map(request)
        reach: dict[str, dict] = {}   # star -> the working relay / hub reaching furthest there (the map's range sphere)
        for d in st["devices"]:
            t = d.get("device_type") or ""
            if any(k in t for k in ("relay", "hub", "beacon", "ward", "slingshot", "observatory", "parallax")):
                infra[star_of(d.get("location"))].append(t)
            r = _rn.reach(d, bpm)
            s_ = star_of(d.get("location"))
            if r and r > (reach.get(s_) or {}).get("ly", 0):
                reach[s_] = {"ly": r, "hub": "hub" in t}
        radii: dict[str, Any] = {}   # star -> the radius its scan reports (radius_solar)
        hazardous: dict[str, list[str]] = {}   # star -> the scan's system tags that call it hazardous
        tags_of: dict[str, list[str]] = {}     # star -> all its scan's system tags (resource_rich, anomaly …)
        if sim_view(request):
            scanned = {x.get("designation") for x in cat["stars"] if x.get("explored")}
            a = (await request.app.state.worker.automations.sim_state()).get("active") or {}
            if str(a.get("id")) == sim_view(request):
                radii = {k: (v.get("star") or {}).get("radius_solar") for k, v in (a.get("scans") or {}).items()
                         if isinstance(v, dict)}
        else:
            rows = await db.fetchall("SELECT star, CASE WHEN json_valid(data) THEN json_extract(data, '$.star.radius_solar') END AS r, "
                                     "CASE WHEN json_valid(data) THEN json_extract(data, '$.system_tags') END AS tags, "
                                     "updated_at FROM systems")
            from . import season
            reset = await season.reset_at(db)   # a season reset made every system unexplored: older scans don't count
            rows = [r for r in rows if not season.stale(r["updated_at"], reset)]
            scanned = {r["star"] for r in rows}
            radii = {r["star"]: r["r"] for r in rows if r["r"] is not None}
            hazardous = {r["star"]: hz for r in rows if (hz := hazard_tags(r["tags"]))}
            tags_of = {r["star"]: tg for r in rows if (tg := scan_tag_list(r["tags"]))}
        from .census import observatory_stars
        prospected, _ = await observatory_stars(db)   # stars our observatories found: highlighted until scanned
        pfrom = await db.kv_get("prospected_from", {}) or {}   # stars an observatory has prospected from (once each)
        if not pfrom:   # before 1.96 only finds were kept: the stars prospected from, out of the stored prospects
            for r in await db.fetchall("SELECT payload, created_at, device_code FROM events WHERE event='prospect.completed'"):
                o = (json.loads(r["payload"] or "{}") or {}).get("origin")
                if o:
                    pfrom[star_of(o)] = {"at": r["created_at"], "by": r["device_code"], "outcome": "prospected"}
            if pfrom:
                await db.kv_set("prospected_from", pfrom)
        from . import megas
        stations = await megas.stations(db)   # space stations (scans, GET /overview): the map's station wheel
        mega = {k: v for k, v in (await megas.known(db)).items() if megas.other_than_stations(v)}   # the hexagon
        scope = request.query_params.get("scope") or "all"
        mine = set(presence) | set(infra) | scanned | set(prospected) | set(pfrom) \
            | {star_of(r.get("location") or r.get("current_location")) for r in st["replicants"].values()} \
            | {f.get("home") for f in await request.app.state.worker.automations.fleets() if f.get("home")}
        stars = []
        for s in cat.get("stars") or []:
            d = s.get("designation")
            if scope != "all" and (d in mine) != (scope == "mine"):
                continue
            stars.append({**{k: s[k] for k in STAR_FIELDS if k in s}, "devices": presence.get(d, 0), "infra": infra.get(d, []),
                          **({"reach": reach[d]} if d in reach else {}), "scanned": d in scanned,
                          **({"prospected": prospected[d].get("found_at"), "found_by": prospected[d].get("found_by")}
                             if d in prospected else {}),
                          **({"prospected_from": pfrom[d]} if d in pfrom else {}),
                          **({"mega": mega[d]} if d in mega else {}),
                          **({"station": True} if d in stations else {}),
                          **({"hazardous": hazardous[d]} if d in hazardous else {}),
                          **({"tags": tags_of[d]} if d in tags_of else {})})
            rad, measured = star_radius(s, radii.get(d))
            if rad is not None:
                stars[-1].update({"radius": round(rad, 3), "radius_measured": measured})
        # the device each replicant rides in (its vessel or carrier), so the map can follow it in transit
        host = {(d.get("hosting_replicant") or {}).get("replicant_code"): d["device_code"] for d in st["devices"]
                if isinstance(d.get("hosting_replicant"), dict)}
        out.update({"stars": stars, "generated_at": cat.get("generated_at"), "sources": cat.get("sources"),
                    "catalogue_updated": await db.kv_updated("stars"),
                    "replicants": [{"code": c, "name": r.get("name"), "star": star_of(r.get("location") or r.get("current_location")),
                                    "position": r.get("position"), "host": host.get(c)} for c, r in st["replicants"].items()]})
    if part in ("overlay", "all"):
        by_star: dict[str, list[dict]] = defaultdict(list)
        for d in st["devices"]:
            if d.get("location"):
                by_star[star_of(d["location"])].append(d)
        per_star = {}
        for d, ds in by_star.items():
            drones = [{k: g[k] for k in ("kind", "n", "working", "idle", "moving", "state")} for g in drone_summary(ds)]
            from . import wardhub as wh
            per_star[d] = {"drones": drones, "mining": mining_now(ds),
                           # your own working ward here (shown like other players' wards, in your color)
                           "my_ward": any(wh.is_ward(x) and wh.active(x) for x in ds),
                           # being surveyed: a survey drone scanning / searching, or a survey controller launched
                           "surveying": any(str(x.get("status") or "").startswith(("scanning", "searching"))
                                            or ("survey_controller" in (x.get("device_type") or "")
                                                and str(x.get("status") or "").startswith("launched")) for x in ds)}
        from . import transit
        from . import relaynet as _rn
        if sim_view(request):
            positions = {s.get("designation"): s.get("position") for s in cat.get("stars") or [] if s.get("position")}
        else:   # shared and read-only: relaynet.star_grid / bridges memoize on it
            positions = await _rn.star_pos(db)
        items = await request.app.state.worker.automations.fleets()
        fleets = [fl.activity(f, st["devices"], status_class) for f in items]
        # observatories prospecting now: a cone from the system along the direction (reach / width learned from finds)
        from .observatory import is_observatory, live_prospect, reach_estimate
        runs = await db.kv_get("observatory_runs", {}) or {}
        prospects = []
        for d in st["devices"]:
            if not is_observatory(d) or not str(d.get("status") or "").startswith("prospecting"):
                continue
            live = live_prospect(d) or (runs.get(d["device_code"]) or {}).get("live")
            t0, t1 = transit._ts((live or {}).get("started_at")), transit._ts((live or {}).get("completes_at"))
            origin = (live or {}).get("origin") or star_of(d.get("location"))
            if not live or not t0 or not t1 or t1 <= t0 or origin not in positions:
                continue
            prospects.append({"code": d["device_code"], "origin": origin, "direction": live.get("direction"),
                              "t0": t0 * 1000, "t1": t1 * 1000})
        if prospects:
            rows = await db.fetchall("SELECT payload FROM events WHERE event='prospect.completed' ORDER BY seq DESC LIMIT 200")
            reach, half, learned = reach_estimate([json.loads(r["payload"] or "{}") for r in rows], positions)
            for p in prospects:
                p.update({"position": positions[p["origin"]], "reach": reach, "half_angle": half, "learned": learned})
        out["prospects"] = prospects
        # other players: their fixed devices (latest scans), arrivals / departures at our beacons, the followed trail
        from . import others as oth, traffic as tr
        profiles = await db.kv_get("replicant_profiles", {}) or {}
        traffic = await db.kv_get("traffic", {}) or {}
        mine = tr.my_codes(st["account"], st["replicants"], st["devices"])
        out["others"] = oth.map_others(oth.normalize(await db.kv_get(oth.KV, {})), positions,
                                       oth.warded_by_others(cat, st["devices"]), profiles)
        out["traffic"] = oth.map_traffic(traffic.get("entries") or [], mine, positions, profiles)
        out["trail"] = oth.map_trail(await db.kv_get("trail", {}) or {}, positions)
        out["relay_net"] = await relay_overlay(request, st, positions)
        out["hazards"] = {} if sim_view(request) else await map_hazards(request.app.state.worker.automations)
        out.update({"per_star": per_star, "moving": transit.galaxy_movers(transit.trips(st["devices"]), positions),
                    "fleets": [f for f in fleets if f["members"]], "supply": fl.supply_links(items, st["devices"])})
        if part == "all":   # the old shape too: drones / mining on each star
            for s in out.get("stars") or []:
                s.update(per_star.get(s["designation"]) or {"drones": [], "mining": {}})
    return out


# --- blueprints & planning -------------------------------------------------------------------
def printers(state: dict) -> list[dict]:
    """Everything that can print — autofactories first (they queue and wait for materials), then vessels."""
    out = []
    for d in state["devices"]:
        if "print" in (d.get("features") or []) and d.get("device_type") != "heaven_vessel":
            out.append({"kind": "device", "code": d["device_code"], "device": d["device_code"],
                        "name": f"{f_human(d.get('device_type'))} {d['device_code']}", "location": d.get("location"),
                        "autofactory": "autofactory" in (d.get("device_type") or ""),
                        "group": f"{d.get('location')}|{d.get('device_type')}",
                        "vessel": (d.get("device_type") or "").endswith("vessel")})
    out.sort(key=lambda p: (not p["autofactory"], p["name"]))
    for code, r in state["replicants"].items():
        out.append({"kind": "replicant", "code": code, "name": f"{r.get('name') or code} (vessel)",
                    "host": r.get("hosted_device_code"), "device": r.get("hosted_device_code"), "autofactory": False, "vessel": True,
                    "location": r.get("location") or r.get("current_location")})
    return out


def affordable(cost: Any, items: Any) -> int:
    have = as_amounts(items)
    counts = [int(have.get(k, 0.0) // v) for k, v in as_amounts(cost).items() if v > 0]
    return min(counts) if counts else 0


# Blueprint fields the page shows elsewhere (name, texts, features, cost, carry line); every other field is listed as a spec.
_BP_SHOWN = {"device_type", "name", "short_description", "description", "features", "directives", "resources",
             "resources_raw", "print_time", "components", "attach_capacity", "cargo_capacity", "stow_capacity"}
_BP_SPEC = {"relay_range": ("relay range", " ly", "FTL links to relays within this distance"),
            "queue_size": ("print queue", " slots", "prints it can hold queued"),
            "strength": ("strength", "", "diversion or weapon strength"),
            "current_hubs": ("current hubs", "", "system hubs the account has now")}


def blueprint_specs(b: dict) -> list[tuple[str, str, str]]:
    """The blueprint's other fields as (label, value, tooltip), leaving out zero and empty ones: the known ones first,
    then any field the game adds later, as it comes."""
    out = []
    order = list(_BP_SPEC)
    for k, v in sorted(b.items(), key=lambda kv: (order.index(kv[0]) if kv[0] in _BP_SPEC else len(order), kv[0])):
        if k in _BP_SHOWN or k.startswith("_") or v in (None, "", 0, 0.0, False, [], {}):
            continue
        label, unit, tip = _BP_SPEC.get(k, (k.replace("_", " "), "", k))
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        if isinstance(v, dict):
            v = ", ".join(f"{n} {f_human(str(t))}" for t, n in sorted(v.items()))
        elif isinstance(v, list):
            v = ", ".join(map(str, v))
        out.append((label, f"{v}{unit}", tip))
    return out


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
    # spread by default: only ticking "keep it on one autofactory" stops it (an unticked box isn't sent, so a "split" box
    # that hadn't been drawn yet read as off — seen live 2026-10-13: 3 matrices queued on one of two autofactories)
    split_on = form.get("nosplit") != "on"
    parts, split_time = [], None
    if len(peers) > 1 and lines:
        load = {d["device_code"]: printqueue.load_seconds(d, bps) for d in peers}
        start = dict(load)
        for t, n in sorted(lines, key=lambda tn: -float((bps.get(tn[0]) or {}).get("print_time") or 0)):  # longest first
            parts += [(f["device_code"], t, k) for f, k in printqueue.split(peers, t, n, bps, load)]
        split_time = max(load[c] - start[c] for c in load)
    from .modular import modular_type
    large = [t for t, _ in lines if modular_type(t, bps)]
    return {"large": large, "flatpack_on": form.get("flatpack") == "on" and bool(large),
            "rows": rows, "lines": lines, "location": location, "total_time": total_time, "printer": printer,
            "peers": peers, "split_on": split_on, "parts": parts, "split_time": split_time,
            "gather_on": (form.get("gather") == "on") if planned else True,
            "deliver_on": (form.get("deliver") == "on") if planned else True,
            "short": {r["resource"]: r["short"] for r in rows if r["short"] > 0}, "star": star,
            "minings": minings, "transports": transports, "mining": mining, "transport": transport}


async def queue_print(request: Request, user: str, kind: str, code: str, device_type: str, quantity: int,
                      flatpack: bool = False) -> HTMLResponse:
    """Add a print to a printer.

    Autofactories take `enqueue_print` and hold a queue. Heaven vessels have no queue: one print at a
    time through `POST /replicants/{code}/print`, which answers "Printer is busy" while one is running.
    """
    quantity = max(1, quantity)
    if kind != "replicant":
        body = {"command": "enqueue_print", "device_type": device_type, "quantity": quantity}
        if flatpack:
            body["flatpack"] = True   # printed compacted: ready to be carried, no hours of folding up first
        out = await call_action(request, user, "POST", f"/devices/{code}", body,
                                f"enqueue {quantity}× {device_type} on {code}" + (" (compacted)" if flatpack else ""))
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


# --- AMI ---------------------------------------------------------------------------------------
DIRECTIVES = cmdspec.DIRECTIVES
controller_kind = cmdspec.controller_kind


async def device_directives(request: Request, dev: dict) -> list[str]:
    """What the device can be told (cmdspec.directives_for). A type we know nothing of (seen live 2026-10-12: a service
    bot from a trade, no blueprint of ours) is asked about once — the device's detail lists its available_directives —
    and remembered per type in kv "known_directives"."""
    db = request.app.state.db
    bps = normalize_blueprints(await db.kv_get("blueprints", []))
    learned = await db.kv_get("known_directives", {}) or {}
    dtype = dev.get("device_type") or ""
    own = [x for x in (dev.get("available_directives") or []) if isinstance(x, str)]
    if not own and dtype and dtype not in learned and dev.get("device_code"):
        try:
            detail = await request.app.state.api.get(f"/devices/{dev['device_code']}") or {}
            own = [x.get("name") if isinstance(x, dict) else x for x in detail.get("available_directives") or []]
            own = [x for x in own if isinstance(x, str) and x]
        except ApiError:
            own = []
    if own and learned.get(dtype) != own:
        learned[dtype] = own
        await db.kv_set("known_directives", learned)
    return cmdspec.directives_for({**dev, "available_directives": own or dev.get("available_directives")}, bps, learned)


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
            await locread.save(db, code, detail)
            n += 1
        except ApiError:
            pass
    return n, None


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


def _ts_num(v: Any) -> float:
    try:
        return datetime.fromisoformat(str(v)).timestamp()
    except (TypeError, ValueError):
        return 0.0


BOBNET_PAGE = 80   # event-stream BobNet messages per page on the Messages page


async def _bobnet_events(db, my, codes: set, limit: int, before: int | None = None, q: str = "") -> list[dict]:
    """Saved bobnet.new events, newest first (older than seq `before`; with `q`, those whose message, poster, channel
    or system contains it, any case), each flagged when it names one of ours."""
    where, args = "", []
    if before:
        where, args = "AND seq < ?", [before]
    if q := q.strip():
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where += " AND (" + " OR ".join(f"json_extract(payload, '$.{k}') LIKE ? ESCAPE '\\'"
                                       for k in ("message", "replicant_name", "channel", "current_star")) + ")"
        args += [like] * 4
    out = [row_event(r) for r in await db.fetchall(
        f"SELECT * FROM events WHERE event='bobnet.new' {where} ORDER BY seq DESC LIMIT ?", (*args, limit))]
    first = await _bobnet_first_seen(db)
    for e in out:
        e["mention"] = bool(my) and not notify.is_mine(e, my, codes) and notify.mentions((e.get("payload") or {}).get("message"), my)
        k = bobnet_key((e.get("payload") or {}).get("message"))
        e["repeat"] = bool(k) and first.get(k, e["seq"]) < e["seq"]
    return out


_BN_NAME = re.compile(r"\b(?=[A-Z0-9-]*[A-Z])[A-Z0-9][A-Z0-9-]{2,}\b")   # star / location / code: SOL, NOSOXA-OORT, A8F48B26


def bobnet_key(text) -> str:
    """What a BobNet message says with its particulars left out (place names, codes and numbers), so that the same
    line about another star or with another count reads as a repeat (Joe 2026-11-04: novel ones on top, repeats grayed)."""
    t = _BN_NAME.sub("*", str(text or ""))
    t = re.sub(r"\d+(?:[.,]\d+)*", "#", t.lower())
    return " ".join(re.sub(r"[^\w#*]+", " ", t).split())


async def _bobnet_first_seen(db) -> dict:
    """{key: seq of the first saved message saying it}, kept in kv bobnet_first and brought up to date from the
    events saved since the last call (the history is never pruned, so it is read once, then only the new rows)."""
    cur = await db.kv_get("bobnet_first", {}) or {}
    keys, last = cur.get("keys") or {}, int(cur.get("seq") or 0)
    rows = await db.fetchall("SELECT seq, json_extract(payload, '$.message') AS m FROM events "
                             "WHERE event='bobnet.new' AND seq > ? ORDER BY seq", (last,))
    if rows:
        for r in rows:
            k = bobnet_key(r["m"])
            if k and k not in keys:
                keys[k] = r["seq"]
        last = rows[-1]["seq"]
        await db.kv_set("bobnet_first", {"seq": last, "keys": keys})
    return keys


def bobnet_novel_first(rows: list[dict]) -> list[dict]:
    """A feed page with its novel messages first (newest first), then the repeats."""
    return [e for e in rows if not e.get("repeat")] + [e for e in rows if e.get("repeat")]


def bobnet_relay(devices: list[dict]) -> dict | None:
    """The relay to ask about BobNet: a relaying one in comms range first, else any FTL relay."""
    relays = [d for d in devices if "relay" in (d.get("device_type") or "") and d.get("device_code")]
    relays.sort(key=lambda d: (not str(d.get("status") or "").startswith(("relaying", "active")),
                               d.get("in_control_range") is False, d["device_code"]))
    return relays[0] if relays else None


# --- diagnostics: live snapshot + mining diagnosis ----------------------------------------------------
async def _snapshot_run(app, stars: set[str] | None, sections: set[str] | None = None) -> None:
    from . import snapshot as snapmod
    db = app.state.db

    async def progress(done: int, total: int, path: str) -> None:
        await db.kv_set("snapshot_status", {"state": "running", "done": done, "total": total, "path": path, "at": now_iso()})
    try:
        snap = await snapmod.capture(app.state.api, db, app.state.worker.automations, stars, progress=progress,
                                    sections=sections)
        await db.kv_set("snapshot_last", snap)
        await db.kv_set("snapshot_status", {"state": "done", "done": snap["requests"], "total": snap["requests"], "at": now_iso()})
    except Exception as e:  # report, never crash the app
        log.exception("snapshot failed")
        import traceback
        tb = traceback.extract_tb(e.__traceback__)[-1]
        await db.kv_set("snapshot_status", {"state": "failed", "at": now_iso(),
                                            "error": f"{type(e).__name__}: {e} ({tb.filename.rsplit('/', 1)[-1]} line {tb.lineno})"})


async def _snapshot_sections(db) -> set[str]:
    """The Diagnostics checkboxes: what the next snapshot includes (all of it until changed)."""
    saved = await db.kv_get("snapshot_sections", None)
    return set(snapmod.SECTION_KEYS) if saved is None else set(saved) & set(snapmod.SECTION_KEYS)


FEEDBACK_TYPES = ("bug", "idea", "typo")   # POST /v1/feedback {"type", "body"}; the game allows 10 an hour


# (group, method, path template, JSON body, what it does). {replicant} {device} {relay} {controller} {interface} {beacon}
# {locker} {location} {star} {tag} … are filled from pickers on the page (or typed); the path stays editable before it's sent.
CONSOLE_PRESETS = [
    ("Account", "GET", "/accounts/me", "", "Your account: name, settings and event preferences"),
    ("Account", "PATCH", "/accounts/me", '{"events": {"ami_digest_interval": 3}}', "Change account settings, such as the AMI digest interval"),
    ("Account", "GET", "/accounts/achievements", "", "Achievements you have earned"),
    ("Account", "GET", "/accounts/reputation", "", "Your standing with each civilization"),
    ("Account", "GET", "/accounts/events?status=active", "", "Account events still running"),
    ("Account", "GET", "/accounts/simulations", "", "Your simulation runs and results"),
    ("Account", "GET", "/accounts/webhook", "", "The webhook the game calls with your events"),
    ("Account", "POST", "/accounts/webhook", '{"url": "https://example.com/hook"}', "Set the webhook URL the game calls with your events"),
    ("Account", "DELETE", "/accounts/webhook", "", "Remove your webhook"),
    ("Account", "GET", "/messages", "", "Your game messages (salvage, AMI notes, news)"),
    ("Account", "POST", "/messages/read", '{"mark_all": true}', "Mark messages read (all, or a list of ids)"),
    ("Account", "GET", "/tutorials", "", "All tutorials and your progress in them"),
    ("Account", "GET", "/tutorials/{slug}", "", "One tutorial and its steps"),
    ("Account", "GET", "/overview", "", "The game world now: season, storylines, events and opportunities"),
    ("Replicants", "GET", "/replicants?per_page=20", "", "Your replicants, one page at a time"),
    ("Replicants", "GET", "/replicants/{replicant}", "", "One replicant: location, status, profile"),
    ("Replicants", "PATCH", "/replicants/{replicant}", '{"description": "", "plan": ""}', "Edit a replicant profile (name, pronouns, description, plan, project)"),
    ("Replicants", "GET", "/replicants/{replicant}/reputation", "", "The replicant reputation with each species"),
    ("Replicants", "GET", "/replicants/{replicant}/devices", "", "Devices this replicant owns"),
    ("Replicants", "GET", "/replicants/{replicant}/inventory", "", "Materials the replicant can reach where it is"),
    ("Replicants", "GET", "/replicants/{replicant}/stars?per_page=20", "", "Stars this replicant has charted"),
    ("Replicants", "GET", "/replicants/{replicant}/stars/{star}", "", "What the replicant knows of one star system"),
    ("Replicants", "GET", "/replicants/{replicant}/scan/devices", "", "Scan for every device at the replicant's location, yours or not"),
    ("Replicants", "GET", "/replicants/{replicant}/traders", "", "Traders and shops the replicant can deal with"),
    ("Replicants", "GET", "/replicants/{replicant}/events?latest=true&limit=20&exclude_mining_cycles=true", "", "The replicant's latest events, without mining cycles"),
    ("Replicants", "POST", "/replicants/{replicant}/travel", '{"destination": "{location}", "dry_run": true}', "Plan a trip for the replicant (dry run: route and ETA only)"),
    ("Replicants", "DELETE", "/replicants/{replicant}/travel", "", "Cancel the replicant trip"),
    ("Replicants", "POST", "/replicants/{replicant}/scan", "", "Scan the replicant location (bodies, resources, life)"),
    ("Replicants", "POST", "/replicants/{replicant}/mine", '{"resource_type": "{resource}"}', "Start the replicant mining a resource"),
    ("Replicants", "DELETE", "/replicants/{replicant}/mine", "", "Stop the replicant mining"),
    ("Replicants", "POST", "/replicants/{replicant}/print", '{"device_type": "{device_type}"}', "Print a device (on a replicant vessel only when you mean to)"),
    ("Replicants", "POST", "/replicants/{replicant}/transfer", '{"target": "{device}"}', "Move the replicant into another matrix at the same location"),
    ("Replicants", "POST", "/replicants/{replicant}/teleport", '{"target": "{device}"}', "Teleport the replicant into a distant matrix"),
    ("Replicants", "POST", "/replicants/{replicant}/message", '{"channel": "{channel}", "text": ""}', "Post on a relay channel as the replicant"),
    ("Devices", "GET", "/devices?limit=50", "", "Your devices, one page at a time"),
    ("Devices", "GET", "/devices/{device}", "", "One device: status, location, configuration"),
    ("Devices", "GET", "/devices/{device}/logs", "", "A device's recent log lines"),
    ("Devices", "GET", "/devices/{device}/permissions", "", "Who may command or use a device"),
    ("Devices", "POST", "/devices/{device}/permissions", "", "Grant another account a permission on a device (body not documented)"),
    ("Devices", "DELETE", "/devices/{device}/permissions", "", "Revoke a permission on a device (body not documented)"),
    ("Devices", "GET", "/devices/tags/{tag}", "", "Your devices carrying a tag"),
    ("Devices", "POST", "/devices/{device}", '{"command": "travel", "destination": "{location}", "dry_run": true}', "Send a device a command (here a dry-run trip)"),
    ("Devices", "PATCH", "/devices/{device}", '{"configuration": {"add_tags": ["spare"]}}', "Change a device's configuration or tags"),
    ("Devices", "GET", "/devices/{beacon}/audit", "", "A trade beacon's audit log"),
    ("Devices", "GET", "/devices/{relay}/network", "", "A relay's live links to other relays and hubs"),
    ("Devices", "GET", "/devices/{relay}/channels", "", "Message channels on a relay"),
    ("Devices", "GET", "/devices/{relay}/messages", "", "Messages posted on a relay"),
    ("Devices", "GET", "/devices/{controller}/trades", "", "Trades listed by a trade controller (shop)"),
    ("Devices", "POST", "/devices/{controller}/trades", '{"name": "", "stock": 1, "criteria": {"resources": {}, "devices": {}}, "rewards": {"resources": {}, "devices": {}}}', "List a new trade on your controller (rewards are held in escrow)"),
    ("Devices", "POST", "/devices/{controller}/trades/{trade}", "", "Buy one unit of a trade (its price must be on site)"),
    ("Devices", "DELETE", "/devices/{controller}/trades/{trade}", "", "Delete your trade and get the escrow back"),
    ("Devices", "GET", "/devices/{interface}/research", "", "Research offered at a replicant interface"),
    ("Devices", "POST", "/devices/{interface}/research/{topic}", "", "Do one research topic (grants its achievement, unlocks blueprints)"),
    ("Devices", "POST", "/devices/{interface}/upload", '{"device_code": ""}', "Upload a data-feature device deployed at the interface for analysis (consumes it)"),
    ("Lockers", "GET", "/replicants/{replicant}/scan/devices?device_type=equipment_locker", "", "Find equipment lockers at the replicant's location"),
    ("Stations", "GET", "/stations", "", "The colony stations: factions, influence, open jobs"),
    ("Stations", "GET", "/stations/{slug}", "", "A station: your reputation and the contributors"),
    ("Stations", "GET", "/stations/{slug}/jobs", "", "A station's job board"),
    ("Stations", "GET", "/jobs", "", "Your accepted jobs, stats, cooldown and reputation"),
    ("Stations", "POST", "/jobs/{job_id}/accept", "", "Accept a job (one at a time)"),
    ("Stations", "POST", "/jobs/{job_id}/complete", "", "Complete your job (a replicant at the station)"),
    ("Stations", "DELETE", "/jobs/{job_id}", "", "Abandon your job (costs reputation)"),
    ("Lockers", "POST", "/devices/{locker}/retrieve", "", "Open an equipment locker and take its contents (its price must be on site)"),
    ("Places", "GET", "/locations", "", "Locations where you have devices"),
    ("Places", "GET", "/locations/{location}", "", "One location: bodies, resource sites, status"),
    ("Places", "GET", "/locations/{location}/inventory", "", "The stockpile at a location"),
    ("Places", "GET", "/locations/{star}/stars", "", "Stars near a star system"),
    ("Places", "GET", "/locations/{star}/events", "", "Events in a star system (impacts, storms)"),
    ("Places", "POST", "/locations/{location}/events/{designation}", "", "Fulfill a location event (a replicant and the materials there)"),
    ("Places", "POST", "/locations/{location}/contribute", '{"devices": ["{device}"]}', "Give devices to a megastructure"),
    ("Places", "GET", "/inventory", "", "Your stockpiles at every location"),
    ("Places", "GET", "/stars", "", "Known star systems"),
    ("Places", "GET", "/blueprints", "", "Blueprints: what each device costs and needs"),
    ("Places", "GET", "/species", "", "Species and their habitable ranges"),
    ("Events", "GET", "/events?limit=20", "", "Your latest game events"),
    ("Events", "GET", "/events?limit=20&event={event}", "", "Latest events of one type (e.g. travel.completed)"),
    ("Simulations", "GET", "/devices/{interface}/simulate", "", "Scenarios a replicant interface can run"),
    ("Simulations", "GET", "/devices/{interface}/simulate/active", "", "The simulation running at an interface"),
    ("Simulations", "POST", "/devices/{interface}/simulate", '{"replicant_code": "{replicant}", "scenario": "{scenario}"}', "Enter a simulation with a replicant at the interface"),
    ("Simulations", "DELETE", "/devices/{interface}/simulate/{sim_id}", "", "Abandon a running simulation"),
    ("Simulations", "GET", "/leaderboards/simulations", "", "Simulation leaderboards"),
    ("Simulations", "GET", "/leaderboards/simulations/{scenario}", "", "The leaderboard of one scenario"),
    ("Public", "GET", "/achievements", "", "Every achievement in the game"),
    ("Public", "GET", "/achievements/{achievement}", "", "One achievement and how to earn it"),
    ("Public", "GET", "/leaderboards", "", "All leaderboards"),
    ("Public", "GET", "/leaderboards/xp", "", "The XP leaderboard"),
    ("Public", "GET", "/leaderboards/megastructure", "", "Megastructure contributions by player"),
    ("Public", "GET", "/leaderboards/fleet", "", "Top 10 by active devices"),
    ("Public", "GET", "/leaderboards/trades", "", "Top 10 by completed trades"),
    ("Public", "GET", "/leaderboards/distance", "", "Top 10 by light-years traveled"),
    ("Public", "GET", "/leaderboards/reputation", "", "Top 10 by species reputation"),
    ("Public", "GET", "/leaderboards/colony_planet", "", "Top 10 by best planet colony survey"),
    ("Public", "GET", "/leaderboards/colony_moon", "", "Top 10 by best moon colony survey"),
    ("Public", "GET", "/blog", "", "Game news and release notes"),
    ("Public", "GET", "/blog/{post_id}", "", "One blog post"),
    ("Public", "GET", "/health", "", "Whether the game API is up"),
]
CONSOLE_FIELDS = {"replicant": "rep", "device": "dev", "beacon": "dev", "relay": "dev", "controller": "dev",
                  "interface": "dev", "locker": "dev", "location": "loc", "star": "star", "tag": "tag"}


async def add_repeats(request: Request, user: str, specs: list[tuple], every: int, stop_on_find: bool) -> list[dict]:
    """Save one repeat per (name, method, path, body) and send each once now; returns the saved repeats."""
    from . import repeats as rp
    db = request.app.state.db
    seq = (await db.fetchone("SELECT MAX(seq) AS s FROM events") or {}).get("s") or 0
    eng = request.app.state.worker.automations
    base = int(datetime.now(timezone.utc).timestamp() * 1000)
    new = [{"id": f"r{base}{i}", "name": name, "method": method, "path": path, "body": body, "every_minutes": every,
            "stop_on_find": stop_on_find, "enabled": True, "by": user, "created_at": now_iso(), "seen_seq": seq,
            "runs": 0, "finds": 0, "last_run": None, "last_result": None} for i, (name, method, path, body) in enumerate(specs)]
    async with eng.lock:
        items = await db.kv_get(rp.KV, []) or []
        await db.kv_set(rp.KV, items + new)
        for r in new:
            await eng.run_due_repeats(only=r["id"])   # the first one goes now
    for r in new:
        await eng.log("repeats", f"repeat added by {user}: {r['method']} {r['path']} every {every} min")
    return [x for x in await db.kv_get(rp.KV, []) or [] if x["id"] in {r["id"] for r in new}]


# =====================================================================================
# live updates (SSE to the browser)
# =====================================================================================
def _sse(event: str, html: str) -> str:
    data = "\n".join(f"data: {line}" for line in (html.splitlines() or [""]))
    return f"event: {event}\n{data}\n\n"


# =====================================================================================
# automations
# =====================================================================================


# Where each rule's settings live: the page it works on (it loads them from /automations/rules-panel).
RULE_HOME = {
    "census_on_arrival": ("Map › Stars", "/stars"), "scan_on_arrival": ("Map › Systems", "/systems"),
    "devices_on_arrival": ("Map › Systems", "/systems"), "auto_survey": ("Map › Systems", "/systems"),
    "restart_idle_miners": ("Map › Systems", "/systems"), "reopen_sites": ("Map › Systems", "/systems"),
    "salvage_when_depleted": ("Map › Systems", "/systems"), "belt_viability": ("Map › Systems", "/systems"),
    "visitor_alerts": ("Map › Traffic", "/traffic"), "civ_beacons": ("Map › Traffic", "/traffic"),
    "asteroid_defence": ("Map › Defense", "/defence"), "asteroid_mining": ("Map › Defense", "/defence"), "maintenance": ("Map › Upkeep", "/maintenance"),
    "ami_schedules": ("Devices › AMI", "/ami"), "loadouts": ("Fleets", "/fleets"),
    "fleet_fill": ("Fleets", "/fleets"),
    "consolidate": ("Economy › Blueprints", "/blueprints"), "contracts": ("Economy › Contracts", "/game-events"),
}


async def ami_schedules_ctx(request: Request, st: dict) -> dict:
    eng = request.app.state.worker.automations
    controllers = [d for d in st["devices"] if amis.is_controller(d)]
    return {"s": await eng.settings(), "schedules": await eng.schedules(), "controllers": controllers,
            "kinds": sorted({amis.kind_of(d.get("device_type")) for d in controllers}),
            "stars": sorted({star_of(d.get("location")) for d in controllers})}


def _schedule_rep(devices: list[dict], target: str) -> dict | None:
    """A representative controller for a schedule target (for its directive list and system targets)."""
    if target.startswith("kind:"):
        return next((d for d in devices if amis.is_controller(d) and amis.kind_of(d.get("device_type")) == target[5:]), None)
    return next((d for d in devices if d.get("device_code") == target), None)


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
    from . import pagecache
    p = await pagecache.get(request, "loadout_plan", eng.loadout_plan)
    present = Counter(lo.star_of(d.get("location")) for d in st["devices"])
    jobs = [j for j in await eng.jobs() if j["rule"] == "loadouts"]
    tagged = defaultdict(list)
    for d in st["devices"]:
        for t in d.get("tags") or []:
            if t == lo.SPARE or t.startswith("to:"):
                tagged[t].append(d)
    rule = (await eng.settings())["rules"].get("loadouts", {})
    from .ami_schedule import handoffs, managed_by

    async def tags_check() -> list[dict]:   # kept an hour like the plan (Refresh on the page recomputes it)
        cat = await db.kv_get("stars", {}) or {}
        star_cat = {s.get("designation"): s for s in (cat.get("stars") or []) if isinstance(s, dict)}
        hosts = {r.get("hosted_device_code"): code for code, r in st["replicants"].items() if r.get("hosted_device_code")}
        busy = eng.busy_devices(await eng.jobs())
        pending_handoffs = {h["drone"] for h in handoffs(st["devices"], await managed_by(db), busy,
                                                          set(p.get("moves") or {}), set(cfg.get("ignore_tags") or []), limit=200)}
        return lo.audit(cfg, st["devices"], star_cat, p, pending_handoffs, hosts, busy)
    audit = await pagecache.get(request, "audit", tags_check)
    from . import consolidate as co
    consolidation = [co.describe(x) for x in await pagecache.get(request, "consolidate_plan", eng.consolidate_plan)]
    consolidate_on = bool(((await eng.settings())["rules"].get("consolidate") or {}).get("enabled"))
    return {"cfg": cfg, "types": types, "plan": p, "lines": lo.describe(p), "present": present, "audit": audit,
            "audit_age": pagecache.age_minutes(request, "audit"),
            "depot": ", ".join(lo.gather_points(cfg, st["devices"])),
            "consolidation": consolidation, "consolidate_on": consolidate_on,
            "active_jobs": [j for j in jobs if j["status"] in ("running", "waiting")],
            "recent_jobs": [j for j in reversed(jobs) if j["status"] not in ("running", "waiting")][:8],
            "orders": await eng.loadout_orders(), "tagged": dict(tagged), "rule": rule,
            "last": await db.kv_get("loadouts_last", {}) or {},
            "all_tags": sorted({t for d in st["devices"] for t in (d.get("tags") or [])})}


async def save_loadouts(request: Request, cfg: dict) -> None:
    await request.app.state.db.kv_set("loadouts", {k: v for k, v in cfg.items() if k != "fleets"})


# --- in-game events (contracts) --------------------------------------------------------------
from . import gameevents as gev  # noqa: E402


async def game_events_ctx(request: Request) -> dict:
    db = request.app.state.db
    st = await load_state(request)
    inv = {i.get("location"): i.get("items") or {} for i in st["inventory"]}
    evs = await gev.load(db)
    settings = await db.kv_get("event_settings", {}) or {}
    supply = await db.kv_get("contract_supply", {}) or {}
    life = await gev.life_map(db)
    approvals = await gev.approvals_of(db, life)
    at_beacon = gev.beacon_bodies(st["devices"])
    bps = normalize_blueprints(await db.kv_get("blueprints", []))
    open_, closed = [], []
    for e in sorted(evs.values(), key=lambda e: e.get("discovered_at") or "", reverse=True):
        if e["status"] == "open":
            e["prog"] = gev.progress(e, inv, st["devices"], st["replicants"])
            e["plan"] = gev.delivery_plan(e, e["prog"], st["devices"])
            gev.option_costs(e["prog"], st["devices"], bps)
            e["supply"] = supply.get(e["designation"])
            key, label = gev.place_key(e, life)
            e["approval"] = {"key": key, "label": label, "on": key in approvals, "beacon": e["location"] in at_beacon}
            ck = gev.contract_key(e)
            e["approval_one"] = {"key": ck, "on": ck in approvals}
            open_.append(e)
        else:
            closed.append(e)
    # civilizations with no open contract: approve them ahead of their next request
    from .traffic import CIV_STAGES
    asking = {e.get("location") for e in open_}
    quiet: dict[str, dict] = {}
    for body, info in life.items():
        if info.get("life_stage") in CIV_STAGES and body not in asking:
            quiet[body] = {"location": body, "star": info.get("star"), "stage": info.get("life_stage")}
    for e in closed:   # bodies that asked before (their scan may not be stored)
        loc = e.get("location")
        if loc and loc not in asking:
            quiet.setdefault(loc, {"location": loc, "star": e.get("star"), "stage": "asked before"})
    for q in quiet.values():
        key, label = gev.place_key(q, life)
        q["approval"] = {"key": key, "label": label, "on": key in approvals, "beacon": q["location"] in at_beacon}
    quiet_rows = sorted(quiet.values(), key=lambda q: q["location"])   # fixed order: a row never jumps when approved
    # your deployed beacons at each civilization's body, and elsewhere in its system (traffic.coverage)
    from . import traffic as tr
    beacon = {}
    for d in tr.beacons(st["devices"]):
        b = beacon.setdefault(tr.star_of(d["location"]), {"here": {}, "system": []})
        b["here"].setdefault(d["location"], []).append(d["device_code"])
        b["system"].append(d["device_code"])
    from . import shop as sh
    return {"open": open_, "closed": closed[:30], "replicants": st["replicants"], "settings": settings, "quiet": quiet_rows,
            "traders": await sh.known_traders(db), "traders_last": await db.kv_get(sh.LAST_KV),
            "listing_wanted": await db.kv_get(sh.LISTING_KV, {}) or {}, "mine": set(st["replicants"]), "trade_side": sh.describe_side,
            "trade_ok": await db.kv_get("trade_approvals", {}) or {}, "beacon": beacon,
            "beacon_auto": gev.BEACON_KEY in approvals, "beacon_key": gev.BEACON_KEY}


async def _event(request: Request, des: str) -> tuple[dict, dict]:
    ctx = await game_events_ctx(request)
    e = next((x for x in ctx["open"] if x["designation"] == des), None)
    return e, ctx


async def _supply_now(request: Request) -> None:
    eng = request.app.state.worker.automations
    async with eng.lock:
        await eng.contract_supply_pass(force=True)


# --- fleets ----------------------------------------------------------------------------------
from . import fleets as fl  # noqa: E402


# =====================================================================================
# desktop wallpaper (Octos add-on) — see wallpaper.py
# =====================================================================================
from . import wallpaper as wp  # noqa: E402


def _origin(request: Request) -> str:
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    return f"{proto}://{host}"


async def wallpaper_ctx(request: Request) -> dict:
    return {"wp": wp.normalize(await request.app.state.db.kv_get(wp.KV, {})), "wp_slug": wp.slug(),
            "wp_base": f"{_origin(request)}/wallpaper/{wp.slug()}/"}


def _wp_off() -> Response:
    return PlainTextResponse("Not found", status_code=404)


_wp_lock = asyncio.Lock()   # the wallpaper key store: last-used notes vs. revokes


async def _wp_auth(request: Request, slug: str) -> tuple[dict | None, Response | None]:
    """The wallpaper key record for this request, or the response to send instead (404 while off, 401 bad key)."""
    if not wp.slug_ok(slug):
        return None, _wp_off()
    db = request.app.state.db
    st = wp.normalize(await db.kv_get(wp.KV, {}))
    if not st["enabled"]:
        return None, _wp_off()
    rec = wp.check(st, request.headers.get(wp.HEADER))
    if not rec:
        return None, JSONResponse({"error": "wallpaper key missing or revoked"}, status_code=401)
    last = parse_ts(rec.get("last_used"))
    if not last or (datetime.now(timezone.utc) - last).total_seconds() > 600:   # note use, at most every 10 min
        # re-read and write in one go (no await in between): a key revoked meanwhile must not come back
        async with _wp_lock:
            cur = wp.normalize(await db.kv_get(wp.KV, {}))
            r2 = wp.check(cur, request.headers.get(wp.HEADER))
            if not r2:
                return None, JSONResponse({"error": "wallpaper key missing or revoked"}, status_code=401)
            r2["last_used"] = wp._now()
            await db.kv_set(wp.KV, cur)
    return rec, None


WALLPAPER_TTL = 1800   # s; the wallpaper is the lowest priority (owner 2026-10-27): its data may be half an hour old


async def _wp_cached(request: Request, kind: str, make) -> Response:
    """A wallpaper answer, kept WALLPAPER_TTL per URL: each map refresh cost seconds of server time (sampled live
    2026-10-27) that pages and the engine waited for."""
    import time as _t
    store = request.app.state.__dict__.setdefault("wp_cache", {})
    key = f"{kind}?{request.url.query}"
    hit = store.get(key)
    if hit and (_t.time() - hit[0] < WALLPAPER_TTL or request.app.state.worker.automations.focused()):
        return Response(hit[1], media_type="application/json")
    body = json.dumps(await make(), default=str).encode()
    store[key] = (_t.time(), body)
    return Response(body, media_type="application/json")


async def _hud_payload(request: Request) -> dict:
    st = await load_state(request)
    series = await resource_series(request)
    resources = []
    for r in RESOURCES:
        if r not in st["totals"]:
            continue
        v = series.get(r) or []
        resources.append({"name": r, "qty": st["totals"][r], "spark": sparkline(v, 90, 20),
                          "change": round(v[-1] - v[0], 1) if len(v) > 1 else None})
    fs = fleet_summary(st["devices"])
    devices = {"total": fs["total"], "working": fs["by_class"].get("st-work", 0), "moving": fs["by_class"].get("st-move", 0),
               "idle": fs["by_class"].get("st-idle", 0)}
    items = await request.app.state.worker.automations.fleets()
    fleets = [fl.activity(f, st["devices"], status_class) for f in items]
    return {"resources": resources, "devices": devices, "fleets": [f for f in fleets if f["members"]],
            "supply": fl.supply_links(items, st["devices"])}


__all__ = [n for n in dir() if not n.startswith("__")]   # everything, underscore helpers too
