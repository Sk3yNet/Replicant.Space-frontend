"""Multi-user mode: one app server per Google account, started and supervised by this manager.

The app itself is single-user: one game token, one SQLite file, one event-stream connection and one rate limiter, all
in-process. Rather than thread a user through all of that, multi-user mode runs a separate copy of the app per user,
each a `uvicorn rsweb.main:app` subprocess on its own port with its own token file, database and allow-list (just that
user's email). This manager:

  * keeps the registry (`<DATA_DIR>/tenants.json`) and each user's token (`<DATA_DIR>/tenants/<slug>/token`, mode 600);
  * starts every registered user's server at boot, restarts it if it dies (with backoff) and stops them all on exit;
  * answers nginx's routing sub-request (`/_tenant/route`): which port serves the signed-in user, or 403 when they
    have no server yet, which nginx turns into a redirect to the sign-up walkthrough;
  * serves the walkthrough (`/_tenant/`): register a game account, find the API key in the verification email,
    paste it here. The key is checked with `GET /accounts/me` before anything is stored.

The owner (OWNER_EMAIL, else the first ALLOWED_EMAIL) keeps the stack's RS_API_TOKEN and the original database at
DB_PATH, so switching an existing single-user stack to multi-user mode loses nothing.

Who may sign up: the owner, the addresses in ALLOWED_EMAILS, anyone at ALLOWED_DOMAINS, or every Google account when
OPEN_SIGNUP=1. Nobody else gets past the walkthrough's first page.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import signal
import sys
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

HERE = Path(__file__).parent
log = logging.getLogger("rsweb.tenants")

TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-.~+/=]{16,256}$")
# Settings that belong to the manager or the owner and must not leak into another user's server.
_PRIVATE_ENV = ("RS_API_TOKEN", "RS_API_TOKEN_FILE", "ALLOWED_EMAILS", "ALLOWED_EMAIL", "ALLOWED_DOMAINS", "DEV_USER",
                "DB_PATH", "OWNER_EMAIL", "ADMIN_EMAILS", "OPEN_SIGNUP", "DATA_DIR", "TENANT_BASE_PORT", "MAX_TENANTS")


def _csv(name: str) -> list[str]:
    return [x.strip().lower() for x in os.getenv(name, "").split(",") if x.strip()]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class TenantSettings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR") or Path(os.getenv("DB_PATH", "./rsweb.sqlite")).parent))
    owner_db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "./rsweb.sqlite"))
    api_base: str = field(default_factory=lambda: os.getenv("RS_API_BASE", "https://api.replicant.space/v1").rstrip("/"))
    owner_token: str = field(default_factory=lambda: os.getenv("RS_API_TOKEN", "").strip())
    owner_email: str = field(default_factory=lambda: (os.getenv("OWNER_EMAIL", "").strip().lower()
                                                      or (_csv("ALLOWED_EMAILS") or _csv("ALLOWED_EMAIL") or [""])[0]))
    allowed_emails: list[str] = field(default_factory=lambda: _csv("ALLOWED_EMAILS") or _csv("ALLOWED_EMAIL"))
    allowed_domains: list[str] = field(default_factory=lambda: [d.lstrip("@") for d in _csv("ALLOWED_DOMAINS")])
    open_signup: bool = field(default_factory=lambda: os.getenv("OPEN_SIGNUP", "") == "1")
    admin_emails: list[str] = field(default_factory=lambda: _csv("ADMIN_EMAILS"))
    base_port: int = field(default_factory=lambda: int(os.getenv("TENANT_BASE_PORT", "8100")))
    max_tenants: int = field(default_factory=lambda: int(os.getenv("MAX_TENANTS", "10")))
    default_tz: str = field(default_factory=lambda: os.getenv("TZ") or "America/New_York")
    auth_header: str = "x-auth-request-email"
    dev_user: str = field(default_factory=lambda: os.getenv("DEV_USER", "").strip().lower())

    def allowed(self, email: str) -> bool:
        if not email:
            return False
        if email == self.owner_email or self.open_signup or email in self.allowed_emails:
            return True
        return email.rsplit("@", 1)[-1] in self.allowed_domains

    def is_admin(self, email: str) -> bool:
        return bool(email) and (email in self.admin_emails or email == self.owner_email)


def slug_for(email: str) -> str:
    local = re.sub(r"[^a-z0-9]+", "-", email.split("@")[0].lower()).strip("-")[:24] or "user"
    return f"{local}-{hashlib.sha1(email.encode()).hexdigest()[:6]}"


# =====================================================================================
# registry
# =====================================================================================
class Registry:
    """tenants.json: {email: {slug, port, created_at, account_name, account_email, replicant, tz, key_set_at}}."""

    def __init__(self, ts: TenantSettings) -> None:
        self.ts = ts
        self.path = ts.data_dir / "tenants.json"
        self.tenants: dict[str, dict] = {}
        if self.path.exists():
            try:
                self.tenants = json.loads(self.path.read_text())
            except Exception:
                log.exception("tenants.json is unreadable; starting with no users (the file is left as it is)")
                self.path = ts.data_dir / f"tenants.{int(time.time())}.json"
        if ts.owner_email and ts.owner_token:
            self.tenants.setdefault(ts.owner_email, {"created_at": _now()})
        for email, t in self.tenants.items():
            t.setdefault("slug", slug_for(email))
        self._assign_ports()
        self.save()

    def _assign_ports(self) -> None:
        used = {t["port"] for t in self.tenants.values() if t.get("port")}
        for t in self.tenants.values():
            if not t.get("port"):
                t["port"] = self.free_port(used)
                used.add(t["port"])

    def free_port(self, used: set[int] | None = None) -> int:
        used = used if used is not None else {t["port"] for t in self.tenants.values() if t.get("port")}
        p = self.ts.base_port
        while p in used:
            p += 1
        return p

    def save(self) -> None:
        self.ts.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.tenants, indent=2, sort_keys=True))
        tmp.replace(self.path)

    def dir(self, email: str) -> Path:
        return self.ts.data_dir / "tenants" / self.tenants[email]["slug"]

    def token_path(self, email: str) -> Path:
        return self.dir(email) / "token"

    def stack_key(self, email: str) -> bool:
        """The owner's key can come from the stack (RS_API_TOKEN) instead of the walkthrough."""
        return email == self.ts.owner_email and bool(self.ts.owner_token)

    def db_path(self, email: str) -> str:
        # The owner keeps the single-user database, so switching modes loses nothing.
        if email == self.ts.owner_email:
            return self.ts.owner_db_path
        return str(self.dir(email) / "rsweb.sqlite")

    def has_token(self, email: str) -> bool:
        if email not in self.tenants:
            return False
        return self.stack_key(email) or self.token_path(email).exists()

    def active(self) -> list[str]:
        return [e for e in self.tenants if self.has_token(e)]

    def put(self, email: str, token: str, account: dict) -> dict:
        t = self.tenants.setdefault(email, {"slug": slug_for(email), "created_at": _now()})
        if not t.get("port"):
            t["port"] = self.free_port()
        reps = account.get("replicants") or []
        t.update(account_name=account.get("name"), account_email=(account.get("email") or "").lower(),
                 replicant=(reps[0].get("name") if reps else None), tz=account.get("timezone"), key_set_at=_now())
        d = self.dir(email)
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o700)
        p = self.token_path(email)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(token)
        self.save()
        return t

    def disconnect(self, email: str) -> None:
        """Forget the key; keep the database, so pasting a key again picks up where the user left off."""
        if email in self.tenants and not self.stack_key(email):
            self.token_path(email).unlink(missing_ok=True)
            self.tenants[email].pop("key_set_at", None)
            self.save()

    def owner_of_account(self, account_email: str) -> str | None:
        for email, t in self.tenants.items():
            if account_email and t.get("account_email") == account_email and self.has_token(email):
                return email
        return None


# =====================================================================================
# supervisor
# =====================================================================================
@dataclass
class Proc:
    email: str
    port: int
    task: asyncio.Task | None = None
    process: asyncio.subprocess.Process | None = None
    ready: bool = False
    stopping: bool = False
    started_at: float = 0.0
    restarts: int = 0
    last_exit: str | None = None
    log: deque = field(default_factory=lambda: deque(maxlen=200))

    @property
    def state(self) -> str:
        if self.stopping:
            return "stopping"
        if self.ready:
            return "running"
        if self.process and self.process.returncode is None:
            return "starting"
        return "crashed" if self.last_exit else "starting"


class Supervisor:
    """Runs one uvicorn subprocess per user and keeps it running."""

    def __init__(self, ts: TenantSettings, registry: Registry) -> None:
        self.ts, self.reg = ts, registry
        self.procs: dict[str, Proc] = {}

    def env_for(self, email: str) -> dict[str, str]:
        t = self.reg.tenants[email]
        env = {k: v for k, v in os.environ.items() if k not in _PRIVATE_ENV}
        env.update(DB_PATH=self.reg.db_path(email), ALLOWED_EMAILS=email, TENANT_MODE="1",
                   TZ=t.get("tz") or self.ts.default_tz, RS_API_BASE=self.ts.api_base,
                   WALLPAPER_SLUG=t["slug"])   # its desktop-wallpaper address: /wallpaper/<slug>/ (wallpaper.py)
        if email == self.ts.owner_email:
            env["TZ"] = self.ts.default_tz
        if self.reg.stack_key(email):
            env["RS_API_TOKEN"] = self.ts.owner_token
        else:
            env["RS_API_TOKEN_FILE"] = str(self.reg.token_path(email))
        return env

    def command(self, port: int) -> list[str]:
        return [sys.executable, "-m", "uvicorn", "rsweb.main:app", "--host", "0.0.0.0", "--port", str(port),
                "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-graceful-shutdown", "4"]

    def start(self, email: str) -> Proc:
        old = self.procs.get(email)
        if old and old.task and not old.task.done():
            return old
        p = Proc(email=email, port=self.reg.tenants[email]["port"])
        self.procs[email] = p
        p.task = asyncio.create_task(self._run(p), name=f"tenant:{self.reg.tenants[email]['slug']}")
        return p

    async def _run(self, p: Proc) -> None:
        slug = self.reg.tenants[p.email]["slug"]
        backoff = 5.0
        while not p.stopping:
            p.ready, p.started_at = False, time.monotonic()
            Path(self.reg.db_path(p.email)).parent.mkdir(parents=True, exist_ok=True)
            try:
                p.process = await asyncio.create_subprocess_exec(
                    *self.command(p.port), env=self.env_for(p.email), cwd=str(HERE.parent),
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            except Exception as e:
                p.last_exit = f"could not start: {e}"
                log.error("[%s] %s", slug, p.last_exit)
            else:
                ready = asyncio.create_task(self._wait_ready(p))
                assert p.process.stdout is not None
                async for raw in p.process.stdout:
                    line = raw.decode(errors="replace").rstrip()
                    p.log.append(line)
                    print(f"[{slug}] {line}", flush=True)
                code = await p.process.wait()
                ready.cancel()
                p.ready = False
                if p.stopping:
                    break
                p.last_exit = f"exited with code {code} at {_now()}"
                log.warning("[%s] %s", slug, p.last_exit)
            if time.monotonic() - p.started_at > 600:
                backoff = 5.0
            p.restarts += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300.0)

    async def _wait_ready(self, p: Proc) -> None:
        async with httpx.AsyncClient(timeout=2) as c:
            for _ in range(240):
                try:
                    if (await c.get(f"http://127.0.0.1:{p.port}/healthz")).status_code == 200:
                        p.ready = True
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.5)

    async def stop(self, email: str) -> None:
        p = self.procs.pop(email, None)
        if not p:
            return
        p.stopping, p.ready = True, False
        if p.process and p.process.returncode is None:
            p.process.send_signal(signal.SIGTERM)
            try:
                await asyncio.wait_for(p.process.wait(), 10)
            except asyncio.TimeoutError:
                p.process.kill()
        if p.task:
            p.task.cancel()
            try:
                await p.task
            except (asyncio.CancelledError, Exception):
                pass

    async def restart(self, email: str) -> Proc:
        await self.stop(email)
        return self.start(email)

    async def stop_all(self) -> None:
        await asyncio.gather(*(self.stop(e) for e in list(self.procs)), return_exceptions=True)

    def status(self, email: str) -> dict:
        p = self.procs.get(email)
        if not p:
            return {"state": "stopped", "port": None, "restarts": 0, "last_exit": None, "log": []}
        return {"state": p.state, "port": p.port, "restarts": p.restarts, "last_exit": p.last_exit,
                "uptime": int(time.monotonic() - p.started_at) if p.ready else 0, "log": list(p.log)[-40:]}


# =====================================================================================
# game API calls made on the user's behalf (registration, recovery, key check)
# =====================================================================================
async def _game(ts: TenantSettings, method: str, path: str, transport: httpx.AsyncBaseTransport | None = None,
                **kw) -> tuple[int, Any]:
    async with httpx.AsyncClient(base_url=ts.api_base, timeout=20, transport=transport) as c:
        try:
            r = await c.request(method, path, **kw)
        except httpx.HTTPError as e:
            return 0, {"error": f"Could not reach the game API ({type(e).__name__})."}
    try:
        body = r.json()
    except ValueError:
        body = {"error": r.text[:300]}
    return r.status_code, body


def _msg(body: Any) -> str:
    if isinstance(body, dict):
        for k in ("message", "error", "detail"):
            v = body.get(k)
            if isinstance(v, str) and v:
                return v
            if isinstance(v, dict) and v.get("message"):
                return str(v["message"])
    return str(body)[:300]


# =====================================================================================
# web
# =====================================================================================
def create_manager(ts: TenantSettings | None = None, supervisor_factory: Callable[..., Supervisor] = Supervisor,
                   transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    ts = ts or TenantSettings()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    templates = Jinja2Templates(directory=HERE / "templates")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        reg = Registry(ts)
        sup = supervisor_factory(ts, reg)
        app.state.reg, app.state.sup = reg, sup
        if ts.owner_token and not ts.owner_email:
            log.warning("RS_API_TOKEN is set but there is no OWNER_EMAIL/ALLOWED_EMAIL, so no one owns it")
        for email in reg.active():
            sup.start(email)
        log.info("multi-user manager: %d server(s) starting", len(reg.active()))
        try:
            yield
        finally:
            await sup.stop_all()

    app = FastAPI(title="Replicant Space servers", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount("/_tenant/static", StaticFiles(directory=HERE / "static"), name="static")

    def user(request: Request) -> str:
        return (request.headers.get(ts.auth_header) or "").strip().lower() or ts.dev_user

    def same_origin(request: Request) -> bool:
        if request.headers.get("sec-fetch-site") in ("cross-site", "same-site"):
            return False
        origin = request.headers.get("origin")
        if origin and origin != "null":
            host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
            # nginx's $host has no port, so compare host names only
            return (urlparse(origin).hostname or "") == host.rsplit(":", 1)[0].lower()
        return True

    def page(request: Request, email: str, **kw) -> HTMLResponse:
        reg: Registry = request.app.state.reg
        sup: Supervisor = request.app.state.sup
        t = reg.tenants.get(email) or {}
        ctx = dict(user=email, allowed=ts.allowed(email), tenant=t, has_key=reg.has_token(email),
                   status=sup.status(email), is_admin=ts.is_admin(email), stack_key=reg.stack_key(email),
                   api_base=ts.api_base, full=len(reg.active()) >= ts.max_tenants and not reg.has_token(email),
                   default_tz=ts.default_tz, notice=None, error=None, step=None, form={})
        ctx.update(kw)
        return templates.TemplateResponse(request, "tenant.html", ctx)

    @app.get("/healthz")
    async def healthz():
        return PlainTextResponse("ok")

    @app.get("/_tenant/route")
    async def route(request: Request):
        """nginx auth_request: 200 + X-Tenant-Port for a running server, else 403 (→ walkthrough)."""
        email = user(request)
        if not email:
            return Response(status_code=401)
        reg: Registry = request.app.state.reg
        if not ts.allowed(email) or not reg.has_token(email):
            return Response(status_code=403, headers={"X-Tenant-State": "none"})
        st = request.app.state.sup.status(email)
        if st["state"] != "running":
            return Response(status_code=403, headers={"X-Tenant-State": st["state"]})
        return Response(status_code=200, headers={"X-Tenant-Port": str(st["port"])})

    @app.get("/_tenant/wallpaper-route")
    async def wallpaper_route(request: Request):
        """nginx auth_request for /wallpaper/<slug>/: the port of that user's running server, else 403 (→ 404). No
        sign-in here: the user's own server checks the wallpaper key (wallpaper.py)."""
        slug = (request.headers.get("x-wallpaper-slug") or "").strip().lower()
        reg: Registry = request.app.state.reg
        email = next((e for e, t in reg.tenants.items() if t.get("slug") == slug), None) if slug else None
        if not email or not reg.has_token(email):
            return Response(status_code=403)
        st = request.app.state.sup.status(email)
        if st["state"] != "running":
            return Response(status_code=403)
        return Response(status_code=200, headers={"X-Tenant-Port": str(st["port"])})

    @app.get("/_tenant/", response_class=HTMLResponse)
    async def home(request: Request, step: str | None = None):
        email = user(request)
        if not email:
            return PlainTextResponse("Not signed in", status_code=401)
        return page(request, email, step=step)

    @app.get("/_tenant/status")
    async def status(request: Request):
        email = user(request)
        st = request.app.state.sup.status(email)
        return JSONResponse({"state": st["state"], "has_key": request.app.state.reg.has_token(email)})

    async def guarded(request: Request) -> str | Response:
        email = user(request)
        if not email:
            return PlainTextResponse("Not signed in", status_code=401)
        if not same_origin(request):
            return PlainTextResponse("Cross-site request refused", status_code=403)
        if not ts.allowed(email):
            return page(request, email)
        return email

    @app.post("/_tenant/register", response_class=HTMLResponse)
    async def register(request: Request, email: str = Form(""), name: str = Form(""), timezone_: str = Form("", alias="timezone")):
        me = await guarded(request)
        if isinstance(me, Response):
            return me
        form = {"email": email.strip(), "name": name.strip(), "timezone": timezone_.strip() or ts.default_tz}
        if not form["email"] or not form["name"]:
            return page(request, me, step="register", form=form, error="Fill in both the email and the name.")
        code, body = await _game(ts, "POST", "/accounts", transport, json=form)
        if code in (200, 201, 202):
            return page(request, me, step="verify", form=form,
                        notice=f"Done. The game says: “{_msg(body)}” Check {form['email']} for its email (and the spam folder).")
        hint = " That email may already have an account: use “I already have an account” below." if code in (400, 409, 422) else ""
        return page(request, me, step="register", form=form, error=f"The game refused the registration ({code or 'no answer'}): {_msg(body)}.{hint}")

    @app.post("/_tenant/recover", response_class=HTMLResponse)
    async def recover(request: Request, email: str = Form("")):
        me = await guarded(request)
        if isinstance(me, Response):
            return me
        if not email.strip():
            return page(request, me, step="recover", error="Enter the email of your game account.")
        code, body = await _game(ts, "POST", "/accounts/recover", transport, json={"email": email.strip()})
        if code in (200, 201, 202):
            return page(request, me, step="verify", form={"email": email.strip()},
                        notice=f"The game says: “{_msg(body)}” Opening that link gives you a new key; the old one stops working.")
        return page(request, me, step="recover", error=f"The game refused ({code or 'no answer'}): {_msg(body)}")

    @app.post("/_tenant/key", response_class=HTMLResponse)
    async def set_key(request: Request, token: str = Form("")):
        me = await guarded(request)
        if isinstance(me, Response):
            return me
        reg: Registry = request.app.state.reg
        sup: Supervisor = request.app.state.sup
        if reg.stack_key(me):
            return page(request, me, error="Your key is the stack's RS_API_TOKEN: change it in Portainer and update the stack.")
        token = token.strip().strip('"').strip()
        if token.lower().startswith("bearer "):
            token = token[7:].strip()
        if not TOKEN_RE.match(token):
            return page(request, me, step="key", error="That doesn't look like an API key. Paste only the text between the "
                                                       "quotes after \"api_token\", with no spaces.")
        if len(reg.active()) >= ts.max_tenants and not reg.has_token(me):
            return page(request, me, step="key", error=f"This server is full ({ts.max_tenants} users). Ask its owner to raise MAX_TENANTS.")
        code, body = await _game(ts, "GET", "/accounts/me", transport, headers={"Authorization": f"Bearer {token}"})
        if code == 401:
            return page(request, me, step="key", error="The game didn't accept that key. If you used account recovery since, "
                                                       "only the newest key works.")
        if code != 200 or not isinstance(body, dict):
            return page(request, me, step="key", error=f"Couldn't check the key ({code or 'no answer'}): {_msg(body)}")
        taken = reg.owner_of_account((body.get("email") or "").lower())
        if taken and taken != me:
            return page(request, me, step="key", error="That game account is already connected to another user here. "
                                                       "Two servers on one key would share (and halve) its rate limit.")
        reg.put(me, token, body)
        await sup.restart(me)
        log.info("key set for %s (game account %s)", me, body.get("name"))
        return RedirectResponse("/_tenant/?step=starting", status_code=303)

    @app.post("/_tenant/restart")
    async def restart(request: Request):
        me = await guarded(request)
        if isinstance(me, Response):
            return me
        if request.app.state.reg.has_token(me):
            await request.app.state.sup.restart(me)
        return RedirectResponse("/_tenant/?step=starting", status_code=303)

    @app.post("/_tenant/disconnect")
    async def disconnect(request: Request):
        me = await guarded(request)
        if isinstance(me, Response):
            return me
        reg: Registry = request.app.state.reg
        if reg.stack_key(me):
            return page(request, me, error="The owner's server runs on the stack's RS_API_TOKEN and can't be disconnected here.")
        await request.app.state.sup.stop(me)
        reg.disconnect(me)
        return RedirectResponse("/_tenant/", status_code=303)

    @app.get("/_tenant/admin", response_class=HTMLResponse)
    async def admin(request: Request):
        email = user(request)
        if not ts.is_admin(email):
            return PlainTextResponse("Only the owner (or ADMIN_EMAILS) can see this page.", status_code=403)
        reg: Registry = request.app.state.reg
        sup: Supervisor = request.app.state.sup
        rows = [dict(t, email=e, has_key=reg.has_token(e), status=sup.status(e), owner=e == ts.owner_email,
                     stack_key=reg.stack_key(e)) for e, t in sorted(reg.tenants.items())]
        return templates.TemplateResponse(request, "tenant_admin.html", dict(user=email, rows=rows, ts=ts,
                                                                              active=len(reg.active())))

    @app.post("/_tenant/admin/{action}")
    async def admin_action(request: Request, action: str, email: str = Form("")):
        me = user(request)
        if not ts.is_admin(me) or not same_origin(request):
            return PlainTextResponse("Refused", status_code=403)
        reg: Registry = request.app.state.reg
        sup: Supervisor = request.app.state.sup
        email = email.strip().lower()
        if email not in reg.tenants:
            return PlainTextResponse("No such user", status_code=404)
        if action == "restart" and reg.has_token(email):
            await sup.restart(email)
        elif action == "stop":
            await sup.stop(email)
        elif action == "disconnect" and not reg.stack_key(email):
            await sup.stop(email)
            reg.disconnect(email)
        return RedirectResponse("/_tenant/admin", status_code=303)

    return app


def main() -> None:
    import uvicorn
    port = int(os.getenv("MANAGER_PORT", "8000"))
    uvicorn.run(create_manager(), host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*",
                timeout_graceful_shutdown=15)


if __name__ == "__main__":
    main()
