"""Read-only diagnostics over MCP, so a Claude chat or thread can read the app's live state (Account › Claude connector).

Nothing here calls the game or changes anything: every tool reads the app's own store (the cached device list, jobs,
the automation log, notifications, saved game events, fleets, the last logistics pass, timings and the read budget).

claude.ai custom connectors sign in with OAuth, so the app is its own tiny OAuth server for one client:

  /mcp/<slug>                                         the MCP endpoint (Streamable HTTP, JSON answers only)
  /.well-known/oauth-protected-resource/mcp/<slug>    which authorization server guards it (also under /mcp/<slug>/…)
  /.well-known/oauth-authorization-server/mcp/<slug>  that server's endpoints (also under /mcp/<slug>/…)
  /mcp/<slug>/authorize                               hands out a one-time code (PKCE S256, known redirect URIs only)
  /mcp/<slug>/token                                   swaps the code (or a refresh token) for an access token

The client secret is made on the Account page, shown once and stored only as a SHA-256 hash. The authorize step asks
nothing (there's no Google sign-in on these paths): a code is worthless without the client secret and the PKCE verifier.
The secret itself also works as a plain bearer token (Claude Code: `--header "Authorization: Bearer <secret>"`).
Making a new secret or turning the connector off ends every token. `<slug>` is the wallpaper slug (wallpaper.slug()).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode, urlsplit

KV = "diag_mcp"
PREFIX = "rsd_"
CLIENT_ID = "replicant-diagnostics"
ACCESS_TTL = 7 * 86400
REFRESH_TTL = 90 * 86400
CODE_TTL = 300
MAX_OUT = 120_000          # characters of JSON per tool answer; past that the answer is cut and says so
PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER_NAME = "replicant-space-diagnostics"
# callbacks a code may go to: claude.ai / claude.com connectors, and local clients (Claude Code, the MCP inspector)
REDIRECT_HOSTS = {"claude.ai", "claude.com"}
LOCAL_HOSTS = {"localhost", "127.0.0.1"}
# kv keys never readable through kv_get (secrets and key stores)
KV_HIDDEN = re.compile(r"token|secret|password|wallpaper|diag_mcp|api_key|apikey|credential", re.I)

_codes: dict[str, dict] = {}   # code hash -> {exp, challenge, redirect}: in memory, a restart just means signing in again


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# =====================================================================================
# key store
# =====================================================================================
def normalize(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {"enabled": bool(raw.get("enabled")) and bool(raw.get("secret_hash")), "secret_hash": raw.get("secret_hash"),
            "created_at": raw.get("created_at"), "last_used": raw.get("last_used"),
            "tokens": [t for t in raw.get("tokens") or [] if isinstance(t, dict) and t.get("h") and (t.get("exp") or 0) > time.time()]}


def new_secret(st: dict) -> str:
    """A fresh client secret; every earlier token stops working."""
    secret = PREFIX + secrets.token_urlsafe(32)
    st.update(enabled=True, secret_hash=_hash(secret), created_at=_now(), last_used=None, tokens=[])
    return secret


def secret_ok(st: dict, presented: str | None) -> bool:
    return bool(st["enabled"] and presented and st["secret_hash"]
                and hmac.compare_digest(st["secret_hash"], _hash(presented.strip())))


def token_ok(st: dict, presented: str | None, kind: str = "access") -> bool:
    if not st["enabled"] or not presented:
        return False
    if kind == "access" and secret_ok(st, presented):
        return True
    h = _hash(presented.strip())
    return any(t.get("kind") == kind and hmac.compare_digest(t["h"], h) for t in st["tokens"])


def issue(st: dict) -> dict:
    """A new access + refresh token pair, kept as hashes in `st` (the caller saves it)."""
    access, refresh = PREFIX + "at_" + secrets.token_urlsafe(32), PREFIX + "rt_" + secrets.token_urlsafe(32)
    now = time.time()
    st["tokens"] = (st["tokens"] + [{"h": _hash(access), "kind": "access", "exp": now + ACCESS_TTL},
                                    {"h": _hash(refresh), "kind": "refresh", "exp": now + REFRESH_TTL}])[-40:]
    return {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL, "refresh_token": refresh}


def drop_token(st: dict, presented: str) -> None:
    h = _hash(presented.strip())
    st["tokens"] = [t for t in st["tokens"] if t["h"] != h]


# =====================================================================================
# OAuth
# =====================================================================================
def redirect_ok(uri: str | None) -> bool:
    if not uri:
        return False
    try:
        u = urlsplit(uri)
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    if u.scheme == "https" and host in REDIRECT_HOSTS and u.path == "/api/mcp/auth_callback":
        return True
    return u.scheme == "http" and host in LOCAL_HOSTS


def base(origin: str, slug: str) -> str:
    return f"{origin.rstrip('/')}/mcp/{slug}"


def resource_metadata(origin: str, slug: str) -> dict:
    b = base(origin, slug)
    return {"resource": b, "authorization_servers": [b], "bearer_methods_supported": ["header"],
            "resource_name": "Replicant Space diagnostics (read-only)"}


def server_metadata(origin: str, slug: str) -> dict:
    b = base(origin, slug)
    return {"issuer": b, "authorization_endpoint": f"{b}/authorize", "token_endpoint": f"{b}/token",
            "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"]}


def authorize(params: dict) -> tuple[str | None, str | None]:
    """(redirect URL with a code, None) or (None, error text to show: a bad redirect URI is never followed)."""
    redirect = params.get("redirect_uri")
    if not redirect_ok(redirect):
        return None, "Unknown redirect URI."
    if params.get("client_id") != CLIENT_ID:
        return None, f"Unknown client. Use the client ID {CLIENT_ID}."

    def back(**q: str) -> str:
        if params.get("state"):
            q["state"] = params["state"]
        return redirect + ("&" if "?" in redirect else "?") + urlencode(q)

    if params.get("response_type") != "code":
        return back(error="unsupported_response_type"), None
    if not params.get("code_challenge") or (params.get("code_challenge_method") or "plain") != "S256":
        return back(error="invalid_request", error_description="PKCE S256 required"), None
    now = time.time()
    for k in [k for k, v in _codes.items() if v["exp"] < now]:
        _codes.pop(k, None)
    code = secrets.token_urlsafe(32)
    _codes[_hash(code)] = {"exp": now + CODE_TTL, "challenge": params["code_challenge"], "redirect": redirect}
    return back(code=code), None


def take_code(code: str | None, verifier: str | None, redirect: str | None) -> bool:
    rec = _codes.pop(_hash(code or ""), None)
    if not rec or rec["exp"] < time.time() or not verifier:
        return False
    if redirect and redirect != rec["redirect"]:
        return False
    calc = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return hmac.compare_digest(calc, rec["challenge"])


def client_creds(form: dict, auth_header: str | None) -> tuple[str | None, str | None]:
    if auth_header and auth_header.lower().startswith("basic "):
        try:
            cid, _, sec = base64.b64decode(auth_header[6:].strip()).decode().partition(":")
            from urllib.parse import unquote
            return unquote(cid), unquote(sec)
        except Exception:
            return None, None
    return form.get("client_id"), form.get("client_secret")


# =====================================================================================
# tools (read-only)
# =====================================================================================
def _s(v: Any) -> str:
    return str(v or "").lower()


def _lim(args: dict, default: int, top: int) -> int:
    try:
        return max(1, min(top, int(args.get("limit") or default)))
    except (TypeError, ValueError):
        return default


def _has(hay: Any, needle: str | None) -> bool:
    return not needle or needle.lower() in json.dumps(hay, default=str).lower()


def _prop(kind: str, desc: str, **extra: Any) -> dict:
    return {"type": kind, "description": desc, **extra}


LIMIT = _prop("integer", "Most rows to return.")
TOOLS: list[dict] = [
    {"name": "overview", "description": "App version, engine status, dry run, rules on/off, device counts by status, "
     "job counts by status and the game request budget. Start here.", "inputSchema": {"type": "object", "properties": {}}},
    {"name": "devices", "description": "Devices from the app's cached device list (refreshed about every 2 minutes), "
     "filtered. Give a code for one device in full.",
     "inputSchema": {"type": "object", "properties": {
         "code": _prop("string", "Device code (or a prefix)."), "type": _prop("string", "Device type, e.g. mining_drone."),
         "status": _prop("string", "Status contains, e.g. idle, travelling, mining."),
         "star": _prop("string", "Star system, e.g. FALQUORYX."), "location": _prop("string", "Location contains."),
         "tag": _prop("string", "Has this tag (exact), e.g. spare or fleet:<id>."),
         "fleet": _prop("string", "Fleet id or name: its members."), "text": _prop("string", "Anywhere in the record."),
         "fields": _prop("array", "Only these fields (default: a short summary; all fields when code is given).",
                         items={"type": "string"}),
         "limit": LIMIT}}},
    {"name": "jobs", "description": "Automation jobs (newest last), with their steps. Filter to one job, rule, device or fleet.",
     "inputSchema": {"type": "object", "properties": {
         "id": _prop("string", "Job id."), "rule": _prop("string", "Rule, e.g. logistics, fleets, loadouts."),
         "status": _prop("string", "e.g. running, failed, done, waiting."), "device": _prop("string", "Device code."),
         "text": _prop("string", "Anywhere in the job (title, meta, errors)."),
         "active": _prop("boolean", "Only jobs not finished."), "steps": _prop("boolean", "Include steps (default true)."),
         "limit": LIMIT}}},
    {"name": "automation_log", "description": "The automation log (last 300 lines), newest last.",
     "inputSchema": {"type": "object", "properties": {
         "rule": _prop("string", "Rule name."), "level": _prop("string", "info, warning or error."),
         "text": _prop("string", "Text contains."), "limit": LIMIT}}},
    {"name": "notifications", "description": "The app's notifications, newest first.",
     "inputSchema": {"type": "object", "properties": {
         "level": _prop("string", "error, warning, info, done …"), "unread": _prop("boolean", "Only unread."),
         "text": _prop("string", "Title or body contains."), "limit": LIMIT}}},
    {"name": "fleets", "description": "Fleets. Without a fleet: one line each. With a fleet id or name: the full record "
     "(mission, log, settings) and its members.",
     "inputSchema": {"type": "object", "properties": {
         "fleet": _prop("string", "Fleet id or name."), "members": _prop("boolean", "List members (default true with fleet).")}}},
    {"name": "logistics", "description": "The last logistics pass (requests, what it sent, what it couldn't do). Without a "
     "section: the section names and sizes.",
     "inputSchema": {"type": "object", "properties": {
         "section": _prop("string", "Section name from the list."), "text": _prop("string", "Keep list items containing this."),
         "limit": LIMIT}}},
    {"name": "game_events", "description": "Saved game events (the app keeps them beyond the game's window), newest first.",
     "inputSchema": {"type": "object", "properties": {
         "event": _prop("string", "Event name or prefix, e.g. travel. or system.impact_warning."),
         "device": _prop("string", "Device code."), "star": _prop("string", "Star."), "location": _prop("string", "Location."),
         "category": _prop("string", "Category, e.g. ami."), "since": _prop("string", "ISO time."),
         "text": _prop("string", "Payload contains."), "limit": LIMIT}}},
    {"name": "actions", "description": "The last commands sent to the game (by you or the automations) and the answers.",
     "inputSchema": {"type": "object", "properties": {
         "path": _prop("string", "Path contains, e.g. a device code."), "by": _prop("string", "automation or you."),
         "failed": _prop("boolean", "Only answers with status 400 or more."), "limit": LIMIT}}},
    {"name": "performance", "description": "Server timings: slow requests, engine stage timings, lock waits, loop lag, "
     "storage sizes, and the game read budget (requests per minute, held endpoints, reads by path).",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "kv_keys", "description": "The app's stored values (key, size, last update), for kv_get.",
     "inputSchema": {"type": "object", "properties": {"text": _prop("string", "Key contains.")}}},
    {"name": "kv_get", "description": "One stored value, optionally one part of it by a dotted path (list items by "
     "number). Secrets and key stores are never shown.",
     "inputSchema": {"type": "object", "required": ["key"], "properties": {
         "key": _prop("string", "Key, from kv_keys."), "path": _prop("string", "e.g. requests.3 or radii.FALQUORYX.")}}},
]
TOOL_NAMES = {t["name"] for t in TOOLS}
for _t in TOOLS:
    _t["annotations"] = {"readOnlyHint": True, "openWorldHint": False}


DEVICE_SUMMARY = ("device_code", "name", "device_type", "status", "location", "tags", "controller", "in_control_range")


def _dev_view(d: dict, fields: list[str] | None, full: bool) -> dict:
    if full and not fields:
        return d
    keep = fields or DEVICE_SUMMARY
    return {k: d.get(k) for k in keep if k in d}


async def _devices(db) -> list[dict]:
    return [d for d in await db.kv_get("devices", []) or [] if isinstance(d, dict)]


def _find_fleet(items: list[dict], key: str | None) -> dict | None:
    k = _s(key)
    return next((f for f in items if _s(f.get("id")) == k), None) or next((f for f in items if _s(f.get("name")) == k), None) \
        or next((f for f in items if k and k in _s(f.get("name"))), None)


async def call(app, name: str, args: dict) -> Any:
    db = app.state.db
    eng = app.state.worker.automations
    if name == "overview":
        from . import version as ver
        settings = await eng.settings()
        devs = await _devices(db)
        jobs = await eng.jobs()
        unread = await db.fetchone("SELECT COUNT(*) AS n FROM notifications WHERE read=0")
        return {"version": ver.VERSION, "run": ver.RUN_ID, "now": _now(),
                "engine": {**eng.engine_status(), **(await db.kv_get("engine_tick", {}) or {})},
                "dry_run": settings.get("dry_run"),
                "rules": {k: bool((v or {}).get("enabled")) if isinstance(v, dict) else v
                          for k, v in (settings.get("rules") or {}).items()},
                "devices": {"total": len(devs), "by_status": dict(Counter(str(d.get("status")) for d in devs).most_common()),
                            "by_type": dict(Counter(str(d.get("device_type")) for d in devs).most_common(25))},
                "jobs": dict(Counter(str(j.get("status")) for j in jobs)), "unread_notifications": (unread or {}).get("n"),
                "fleets": len(await eng.fleets()), "game_budget": app.state.api.rate_status()}
    if name == "devices":
        devs = await _devices(db)
        if args.get("fleet"):
            from . import fleets as fl
            f = _find_fleet(await eng.fleets(), args["fleet"])
            if not f:
                return {"error": f"no fleet {args['fleet']!r}"}
            devs = fl.members(f, devs)
        code, star = _s(args.get("code")), _s(args.get("star"))
        out = [d for d in devs
               if (not code or _s(d.get("device_code")).startswith(code))
               and (not args.get("type") or _s(d.get("device_type")) == _s(args["type"]))
               and (not args.get("status") or _s(args["status"]) in _s(d.get("status")))
               and (not star or _s(d.get("location")).split("-")[0] == star)
               and (not args.get("location") or _s(args["location"]) in _s(d.get("location")))
               and (not args.get("tag") or args["tag"] in (d.get("tags") or []))
               and _has(d, args.get("text"))]
        lim = _lim(args, 50, 1000)
        return {"count": len(out), "shown": min(lim, len(out)),
                "devices": [_dev_view(d, args.get("fields"), bool(code)) for d in out[:lim]]}
    if name == "jobs":
        jobs = await eng.jobs()
        out = [j for j in jobs
               if (not args.get("id") or str(j.get("id")) == str(args["id"]))
               and (not args.get("rule") or _s(j.get("rule")) == _s(args["rule"]))
               and (not args.get("status") or _s(j.get("status")) == _s(args["status"]))
               and (not args.get("device") or _has([j.get("device"), j.get("meta"), j.get("steps")], args["device"]))
               and (not args.get("active") or not j.get("finished_at"))
               and _has(j, args.get("text"))]
        lim = _lim(args, 20, 200)
        steps = args.get("steps", True)
        return {"count": len(out), "jobs": [j if steps else {k: v for k, v in j.items() if k != "steps"} for j in out[-lim:]]}
    if name == "automation_log":
        log = await db.kv_get("automation_log", []) or []
        out = [e for e in log if (not args.get("rule") or _s(e.get("rule")) == _s(args["rule"]))
               and (not args.get("level") or _s(e.get("level")) == _s(args["level"]))
               and (not args.get("text") or _s(args["text"]) in _s(e.get("text")))]
        return {"count": len(out), "log": out[-_lim(args, 100, 300):]}
    if name == "notifications":
        sql, p = "SELECT id, level, title, body, link, created_at, read FROM notifications WHERE 1=1", []
        if args.get("level"):
            sql += " AND level=?"; p.append(args["level"])
        if args.get("unread"):
            sql += " AND read=0"
        if args.get("text"):
            sql += " AND (title LIKE ? OR body LIKE ?)"; p += [f"%{args['text']}%"] * 2
        sql += " ORDER BY id DESC LIMIT ?"; p.append(_lim(args, 50, 500))
        return {"notifications": await db.fetchall(sql, p)}
    if name == "fleets":
        items = await eng.fleets()
        if not args.get("fleet"):
            return {"fleets": [{"id": f.get("id"), "name": f.get("name"), "role": f.get("role"), "home": f.get("home"),
                                "station": bool(f.get("station")), "mission": (f.get("mission") or {}).get("status"),
                                "phase": (f.get("mission") or {}).get("phase")} for f in items]}
        f = _find_fleet(items, args["fleet"])
        if not f:
            return {"error": f"no fleet {args['fleet']!r}", "fleets": [f.get("name") for f in items]}
        out: dict = {"fleet": f}
        if args.get("members", True):
            from . import fleets as fl
            out["members"] = [_dev_view(d, None, False) for d in fl.members(f, await _devices(db))]
        return out
    if name == "logistics":
        st = {k: v for k, v in (await db.kv_get("logistics_state", {}) or {}).items() if k != "fleet_devices"}
        sec = args.get("section")
        if not sec:
            return {"sections": {k: (len(v) if isinstance(v, (list, dict)) else v) for k, v in st.items()}}
        if sec not in st:
            return {"error": f"no section {sec!r}", "sections": sorted(st)}
        v = st[sec]
        lim = _lim(args, 100, 2000)
        if isinstance(v, list):
            v = [x for x in v if _has(x, args.get("text"))]
            return {"count": len(v), sec: v[:lim]}
        if isinstance(v, dict):
            items = [(k, x) for k, x in v.items() if _has([k, x], args.get("text"))]
            return {"count": len(items), sec: dict(items[:lim])}
        return {sec: v}
    if name == "game_events":
        sql = ("SELECT seq, event, category, device_code, device_type, star, location, payload, created_at "
               "FROM events WHERE 1=1")
        p: list = []
        if args.get("event"):
            sql += " AND event LIKE ?"; p.append(args["event"] + "%")
        for k, col in (("device", "device_code"), ("star", "star"), ("location", "location"), ("category", "category")):
            if args.get(k):
                sql += f" AND {col}=?"; p.append(args[k].upper() if k != "category" else args[k])
        if args.get("since"):
            sql += " AND created_at>=?"; p.append(args["since"])
        if args.get("text"):
            sql += " AND payload LIKE ?"; p.append(f"%{args['text']}%")
        sql += " ORDER BY seq DESC LIMIT ?"; p.append(_lim(args, 50, 500))
        rows = await db.fetchall(sql, p)
        for r in rows:
            try:
                r["payload"] = json.loads(r["payload"]) if r.get("payload") else None
            except ValueError:
                pass
        return {"events": rows}
    if name == "actions":
        sql, p = "SELECT at, user, method, path, body, status, response FROM actions WHERE 1=1", []
        if args.get("path"):
            sql += " AND path LIKE ?"; p.append(f"%{args['path']}%")
        if args.get("by") == "automation":
            sql += " AND user='automation'"
        elif args.get("by") == "you":
            sql += " AND user<>'automation'"
        if args.get("failed"):
            sql += " AND status>=400"
        sql += " ORDER BY id DESC LIMIT ?"; p.append(_lim(args, 40, 300))
        rows = await db.fetchall(sql, p)
        return {"actions": [{**r, "user": "automation" if r["user"] == "automation" else "you",
                             "response": (r.get("response") or "")[:600]} for r in rows]}
    if name == "performance":
        from . import perf
        return {"perf": await perf.report(db), "game_budget": app.state.api.rate_status(), "reads": perf.reads_report()}
    if name == "kv_keys":
        rows = await db.fetchall("SELECT key, LENGTH(value) AS size, updated_at FROM kv ORDER BY key")
        return {"keys": [r for r in rows if not KV_HIDDEN.search(r["key"])
                         and (not args.get("text") or _s(args["text"]) in _s(r["key"]))]}
    if name == "kv_get":
        key = str(args.get("key") or "")
        if not key or KV_HIDDEN.search(key):
            return {"error": "not readable"}
        v = await db.kv_get(key, None)
        for part in [x for x in str(args.get("path") or "").split(".") if x]:
            if isinstance(v, list) and part.lstrip("-").isdigit() and -len(v) <= int(part) < len(v):
                v = v[int(part)]
            elif isinstance(v, dict) and part in v:
                v = v[part]
            else:
                return {"error": f"no {part!r} in path", "keys": sorted(v)[:200] if isinstance(v, dict) else None}
        return {"key": key, "value": v}
    return {"error": f"unknown tool {name}"}


def dump(v: Any) -> str:
    s = json.dumps(v, default=str, separators=(",", ":"))
    if len(s) > MAX_OUT:
        s = s[:MAX_OUT] + f"\n…cut at {MAX_OUT} of {len(s)} characters: narrow the filters or lower limit."
    return s


async def rpc(app, msg: Any) -> dict | None:
    """One JSON-RPC message → its answer (None for a notification)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if "id" not in msg:
        return None

    def ok(result: dict) -> dict:
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "initialize":
        from . import version as ver
        want = params.get("protocolVersion")
        return ok({"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
                   "capabilities": {"tools": {"listChanged": False}},
                   "serverInfo": {"name": SERVER_NAME, "version": ver.VERSION},
                   "instructions": "Read-only view of a Replicant Space web client's live state (no game calls, no "
                                   "commands). Start with overview; filter devices, jobs and fleets rather than "
                                   "listing everything."})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": TOOLS})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in TOOL_NAMES:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"Unknown tool {name}"}}
        try:
            out = await call(app, name, args if isinstance(args, dict) else {})
            return ok({"content": [{"type": "text", "text": dump(out)}], "isError": "error" in out and len(out) <= 3})
        except Exception as e:   # a tool that trips over odd data answers with the error, not a 500
            return ok({"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True})
    if method in ("resources/list", "prompts/list"):
        return ok({method.split("/")[0]: []})
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}}
