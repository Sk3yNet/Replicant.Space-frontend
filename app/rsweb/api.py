"""Rate-limited client for the Replicant Space API.

All game traffic goes through here so one budget is shared by the poller, the
event ingester and every browser tab.
"""
from __future__ import annotations

import asyncio
import json
import logging
import posixpath
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

import httpx

from .config import Settings

log = logging.getLogger("rsweb.api")

READ_METHODS = {"GET", "HEAD"}

# Paths the UI refuses to call: they would wipe the account or invalidate our token.
BLOCKED = [
    ("DELETE", "/accounts/me"),
    ("POST", "/accounts/recover"),
    ("POST", "/accounts"),
]


class ApiError(Exception):
    def __init__(self, status: int, message: str, body: Any = None):
        super().__init__(f"{status}: {message}")
        self.status = status
        self.message = message
        self.body = body


@dataclass
class Bucket:
    """Token bucket refilled continuously at `per_min` tokens per minute."""

    per_min: int
    tokens: float = 0.0
    updated: float = field(default_factory=time.monotonic)
    blocked_until: float = 0.0
    recent: deque = field(default_factory=lambda: deque(maxlen=500))

    def __post_init__(self) -> None:
        self.tokens = float(self.per_min)

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.per_min, self.tokens + (now - self.updated) * self.per_min / 60.0)
        self.updated = now

    def wait_time(self, reserve: int = 0) -> float:
        self._refill()
        now = time.monotonic()
        if now < self.blocked_until:
            return self.blocked_until - now
        need = 1 + reserve - self.tokens
        return 0.0 if need <= 0 else need * 60.0 / self.per_min

    def take(self) -> None:
        self.tokens -= 1
        self.recent.append(time.time())

    def used_last_minute(self) -> int:
        cutoff = time.time() - 60
        return sum(1 for t in self.recent if t >= cutoff)


class RSClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.s = settings
        self.http = httpx.AsyncClient(
            base_url=settings.api_base,
            headers={"Authorization": f"Bearer {settings.api_token}", "Accept": "application/json",
                     "User-Agent": "rsweb/1.0 (self-hosted client)"},
            timeout=httpx.Timeout(20.0, read=30.0),
            transport=transport,
        )
        self.get_bucket = Bucket(settings.get_per_min)
        self.act_bucket = Bucket(settings.act_per_min)
        self._lock = asyncio.Lock()
        # Last values reported by the server.
        self.server_remaining: dict[str, str] = {}
        self.unread_count: int | None = None
        self.last_error: str | None = None
        self.last_ok: float | None = None

    async def close(self) -> None:
        await self.http.aclose()

    @property
    def configured(self) -> bool:
        return bool(self.s.api_token)

    # --- budget ------------------------------------------------------------
    async def _acquire(self, method: str, background: bool) -> None:
        bucket = self.get_bucket if method in READ_METHODS else self.act_bucket
        reserve = 0
        if background:
            reserve = self.s.get_reserve if bucket is self.get_bucket else self.s.act_reserve
        while True:
            async with self._lock:
                wait = bucket.wait_time(reserve)
                if wait <= 0:
                    bucket.take()
                    return
            await asyncio.sleep(min(wait, 5.0))

    def rate_status(self) -> dict:
        return {
            "get_used": self.get_bucket.used_last_minute(),
            "get_limit": 120,
            "act_used": self.act_bucket.used_last_minute(),
            "act_limit": 60,
            "server_remaining": self.server_remaining.get("remaining"),
            "blocked": max(0.0, max(self.get_bucket.blocked_until, self.act_bucket.blocked_until) - time.monotonic()),
            "unread": self.unread_count,
            "last_error": self.last_error,
        }

    # --- requests ------------------------------------------------------------
    @staticmethod
    def check_allowed(method: str, path: str) -> None:
        raw = urllib.parse.unquote(path.split("?")[0].split("#")[0])
        if any(seg in (".", "..") for seg in raw.split("/")):   # httpx resolves dot segments after this check
            raise ApiError(400, "paths with . or .. segments aren't sent")
        p = "/" + posixpath.normpath("/" + raw.lstrip("/")).lstrip("/").lower()
        if p.startswith("/v1/"):
            p = p[3:]
        for m, blocked in BLOCKED:
            if method.upper() == m and p.rstrip("/") == blocked.lower():
                raise ApiError(403, f"{m} {blocked} is blocked by this client for safety")

    async def request(self, method: str, path: str, *, params: dict | None = None, json_body: Any = None,
                      background: bool = False, retries: int = 2) -> Any:
        method = method.upper()
        self.check_allowed(method, path)
        if not self.configured:
            raise ApiError(0, "No API token configured (set RS_API_TOKEN)")
        path = "/" + path.lstrip("/")
        if path.startswith("/v1/"):
            path = path[3:]
        attempt = 0
        while True:
            await self._acquire(method, background)
            try:
                resp = await self.http.request(method, path, params=params, json=json_body)
            except httpx.HTTPError as e:
                self.last_error = f"network: {e}"
                # a command that timed out may have been carried out: only resend reads, or writes that never left
                sent = not isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout))
                if attempt >= retries or (method not in READ_METHODS and sent):
                    raise ApiError(0, f"network error: {e}") from e
                attempt += 1
                await asyncio.sleep(2 ** attempt)
                continue
            self._note_headers(resp)
            if resp.status_code == 429:
                retry = retry_after(resp.headers.get("Retry-After"), 10.0)
                bucket = self.get_bucket if method in READ_METHODS else self.act_bucket
                bucket.blocked_until = time.monotonic() + retry
                self.last_error = f"rate limited for {retry:.0f}s"
                log.warning("429 on %s %s, backing off %ss", method, path, retry)
                if attempt >= retries:
                    raise ApiError(429, "rate limited", _safe_json(resp))
                attempt += 1
                continue
            body = _safe_json(resp)
            if resp.status_code >= 400:
                msg = body.get("error") if isinstance(body, dict) else None
                self.last_error = f"{resp.status_code} {msg or resp.reason_phrase}"
                raise ApiError(resp.status_code, msg or resp.reason_phrase, body)
            self.last_ok = time.time()
            self.last_error = None
            return body

    def _note_headers(self, resp: httpx.Response) -> None:
        h = resp.headers
        if "X-RateLimit-Remaining" in h:
            self.server_remaining = {"remaining": h.get("X-RateLimit-Remaining"), "limit": h.get("X-RateLimit-Limit")}
        if "X-Replicant-Space-Unread-Count" in h:
            try:
                self.unread_count = int(h["X-Replicant-Space-Unread-Count"])
            except ValueError:
                pass

    async def get(self, path: str, background: bool = False, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        return await self.request("GET", path, params=params or None, background=background)

    async def post(self, path: str, body: Any = None, background: bool = False) -> Any:
        return await self.request("POST", path, json_body=body if body is not None else {}, background=background)

    async def paged(self, path: str, key: str, background: bool = True, limit: int = 50,
                    max_pages: int = 40, **params: Any) -> list:
        """Follow `next_cursor` until exhausted."""
        items: list = []
        cursor = None
        for _ in range(max_pages):
            body = await self.get(path, background=background, limit=limit, cursor=cursor, **params)
            items.extend((body or {}).get(key) or [])
            cursor = (body or {}).get("next_cursor")
            if not cursor:
                break
        return items

    # --- event stream -----------------------------------------------------------
    async def stream_events(self, cursor: str | None) -> AsyncIterator[dict]:
        """Yield parsed SSE events from /events/stream. Raises on disconnect."""
        headers = {"Accept": "text/event-stream"}
        if cursor:
            headers["Last-Event-ID"] = cursor
        params = {"cursor": cursor} if cursor else None
        await self._acquire("GET", background=True)
        timeout = httpx.Timeout(20.0, read=120.0)  # server sends keepalives
        async with self.http.stream("GET", "/events/stream", headers=headers, params=params,
                                    timeout=timeout) as resp:
            self._note_headers(resp)
            if resp.status_code == 429:
                retry = retry_after(resp.headers.get("Retry-After"), 30.0)
                raise ApiError(429, f"stream rate limited, retry in {retry}s", {"retry": retry})
            if resp.status_code >= 400:
                await resp.aread()
                raise ApiError(resp.status_code, f"stream refused: {resp.text[:200]}")
            async for ev in parse_sse(resp.aiter_lines()):
                yield ev


def retry_after(v: str | None, default: float) -> float:
    """Retry-After as seconds: a number, or an HTTP date (RFC 9110)."""
    if not v:
        return default
    try:
        return max(0.0, float(v))
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        from datetime import datetime, timezone
        return max(0.0, (parsedate_to_datetime(v) - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return default


async def parse_sse(lines: AsyncIterator[str]) -> AsyncIterator[dict]:
    """Minimal SSE parser -> {'id','event','data'} with data JSON-decoded and merged."""
    ev_id, ev_name, data_lines = None, None, []
    async for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if data_lines:
                data = "\n".join(data_lines)
                try:
                    obj = json.loads(data)
                except ValueError:
                    obj = {"raw": data}
                if not isinstance(obj, dict):
                    obj = {"raw": obj}
                if ev_id and not obj.get("id"):
                    obj["id"] = ev_id
                if ev_name and not obj.get("event"):
                    obj["event"] = ev_name
                yield obj
            ev_id, ev_name, data_lines = None, None, []
            continue
        if line.startswith(":"):
            continue  # keepalive comment
        field_, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field_ == "id":
            ev_id = value
        elif field_ == "event":
            ev_name = value
        elif field_ == "data":
            data_lines.append(value)


def _safe_json(resp: httpx.Response) -> Any:
    if resp.status_code == 204 or not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text[:2000]}
