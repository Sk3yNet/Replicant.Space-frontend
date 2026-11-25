"""Local network access (Joe 2026-11-25): at home, the tunnel sends you to the server's LAN address.

nginx serves the LAN entry on port 7443 (multi.conf): private addresses only, no Google sign-in, every request there is
the owner and carries X-Via-Lan: 1. Here, a page opened through the tunnel by the owner (only the owner's server gets
LAN_URL, tenants.py) from the server's own public IP, i.e. from home, is redirected to LAN_URL with the same path. The
server learns its public IPv4 and IPv6 addresses from Cloudflare's trace page every 30 minutes (IPv6: same /64). A cookie `lan=off` on the tunnel address
(set by /lan/off) keeps you on the tunnel; /lan/on clears it.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import time
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from .web import current_user

log = logging.getLogger("rsweb.lan")
router = APIRouter()

COOKIE = "lan"
# Cloudflare answers each with the address it sees; both, since a browser may reach the tunnel over IPv6.
TRACE_URLS = ("https://1.1.1.1/cdn-cgi/trace", "https://[2606:4700:4700::1111]/cdn-cgi/trace")
REFRESH_S = 1800
RETRY_S = 300
# Only full page views move: no live stream, fragments, JSON, static files, wallpaper or connector.
SKIP = ("/static", "/live", "/api", "/wallpaper", "/mcp", "/partials", "/lan/", "/healthz", "/oauth2", "/_tenant",
        "/.well-known")
SKIP_EXT = (".json", ".csv", ".txt", ".png", ".svg", ".zip", ".js", ".css", ".ico")

state: dict = {"ip": "", "ips": [], "at": 0.0, "busy": False, "tunnel": ""}


def lan_url() -> str:
    return os.getenv("LAN_URL", "").strip().rstrip("/")


async def refresh_ip() -> None:
    found: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            for url in TRACE_URLS:
                try:
                    r = await c.get(url)
                except httpx.HTTPError:
                    continue   # no IPv6 route, say
                ip = next((ln[3:].strip() for ln in r.text.splitlines() if ln.startswith("ip=")), "")
                if ip and ip not in found:
                    found.append(ip)
    except Exception as e:   # noqa: BLE001
        log.info("public IP lookup failed: %s", e)
    finally:
        if found:
            state["ips"], state["ip"], state["at"] = found, ", ".join(found), time.time()
        else:   # offline or blocked: try again in 5 minutes
            state["at"] = time.time() - REFRESH_S + RETRY_S
        state["busy"] = False


def public_ips() -> list[str]:
    """The server's public addresses as last looked up; starts a lookup in the background when they're over 30 min old."""
    if not state["busy"] and time.time() - state["at"] > REFRESH_S:
        state["busy"] = True
        try:
            asyncio.get_running_loop().create_task(refresh_ip())
        except RuntimeError:
            state["busy"] = False
    return state["ips"]


def at_home(me: str, homes: list[str]) -> bool:
    """Same IPv4 address as the server, or the same IPv6 /64 network."""
    try:
        a = ipaddress.ip_address(me)
    except ValueError:
        return False
    for h in homes:
        try:
            b = ipaddress.ip_address(h)
        except ValueError:
            continue
        if a.version == b.version == 4 and a == b:
            return True
        if a.version == b.version == 6 and ipaddress.ip_network(f"{b}/64", strict=False) == ipaddress.ip_network(f"{a}/64", strict=False):
            return True
    return False


def _cookie(raw: str, name: str) -> str:
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return ""


def redirect_for(method: str, path: str, query: str, headers: dict[str, str]) -> str | None:
    """Where to send this request, or None to serve it here. Remembers the tunnel's address for the Account card."""
    base = lan_url()
    if not base or headers.get("x-via-lan") == "1":
        return None
    host = headers.get("x-forwarded-host") or headers.get("host") or ""
    if host:
        state["tunnel"] = f"https://{host}"
    if method != "GET" or path.startswith(SKIP) or path.lower().endswith(SKIP_EXT):
        return None
    if headers.get("hx-request") or headers.get("x-window") or headers.get("x-live"):
        return None
    if "text/html" not in headers.get("accept", ""):
        return None
    if _cookie(headers.get("cookie", ""), COOKIE) == "off":
        return None
    if not at_home((headers.get("x-real-ip") or "").strip(), public_ips()):
        return None
    return base + path + (f"?{query}" if query else "")


class Middleware:
    """Pure ASGI, so streamed answers pass straight through."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and lan_url():
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}
            to = redirect_for(scope.get("method", ""), scope.get("path", ""),
                              (scope.get("query_string") or b"").decode("latin-1"), headers)
            if to:
                return await RedirectResponse(to, status_code=302)(scope, receive, send)
        await self.app(scope, receive, send)


def _safe(rd: str) -> str:
    u = urlsplit(rd or "/")
    return rd if rd.startswith("/") and not rd.startswith("//") and not u.netloc else "/"


@router.get("/lan/off")
async def lan_off(request: Request, rd: str = "/", user: str = Depends(current_user)):
    """Stay on the tunnel in this browser, even at home."""
    r = RedirectResponse(_safe(rd), status_code=303)
    r.set_cookie(COOKIE, "off", max_age=365 * 86400, path="/", samesite="lax", secure=True, httponly=True)
    return r


@router.get("/lan/on")
async def lan_on(request: Request, user: str = Depends(current_user)):
    """Go back to being sent to the local address at home."""
    r = RedirectResponse("/account#lan", status_code=303)
    r.delete_cookie(COOKIE, path="/")
    return r


def lan_ctx(request: Request) -> dict:
    h = request.headers
    return {"lan": {"url": lan_url(), "here": h.get("x-via-lan") == "1", "me": h.get("x-real-ip") or "",
                    "home_ip": state["ip"] if lan_url() else "", "off": request.cookies.get(COOKIE) == "off",
                    "tunnel": state["tunnel"]}}
