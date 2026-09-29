"""Background workers: live event ingestion and periodic state polling."""
from __future__ import annotations

import asyncio
import json
import logging
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from .api import ApiError, RSClient
from .config import Settings
from .db import DB, now_iso
from .hub import Hub
from . import notify
from .shapes import normalize_blueprints, normalize_inventory

log = logging.getLogger("rsweb.ingest")


def _parse_ts(v: Any) -> datetime | None:
    if not v or not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


class Worker:
    def __init__(self, settings: Settings, db: DB, api: RSClient, hub: Hub):
        self.s, self.db, self.api, self.hub = settings, db, api, hub
        self.tasks: list[asyncio.Task] = []
        self.stream_state = "stopped"
        self.stream_since: str | None = None

    # --- lifecycle --------------------------------------------------------------
    def start(self) -> None:
        if not self.api.configured:
            log.warning("No API token configured; background workers not started")
            self.stream_state = "no token"
            return
        self.tasks = [
            asyncio.create_task(self._stream_loop(), name="stream"),
            asyncio.create_task(self._poll_loop("account", self.s.poll_account, self.sync_account), name="p-account"),
            asyncio.create_task(self._poll_loop("devices", self.s.poll_devices, self.sync_devices), name="p-devices"),
            asyncio.create_task(self._poll_loop("inventory", self.s.poll_inventory, self.sync_inventory), name="p-inv"),
            asyncio.create_task(self._poll_loop("messages", self.s.poll_messages, self.sync_messages), name="p-msg"),
            asyncio.create_task(self._poll_loop("catalogue", self.s.poll_catalogue, self.sync_catalogue), name="p-cat"),
        ]

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    # --- event stream -------------------------------------------------------------
    async def _stream_loop(self) -> None:
        backoff = 2.0
        while True:
            cursor = await self.db.kv_get("event_cursor")
            try:
                self.stream_state = "connecting"
                async for ev in self.api.stream_events(cursor):
                    self.stream_state = "live"
                    self.stream_since = self.stream_since or now_iso()
                    backoff = 2.0
                    await self.handle_event(ev)
            except asyncio.CancelledError:
                raise
            except ApiError as e:
                self.stream_state = f"error: {e.message}"
                if e.status == 429 and isinstance(e.body, dict):
                    backoff = max(backoff, float(e.body.get("retry", 30)))
                log.warning("event stream error: %s", e)
            except Exception as e:  # network drops, timeouts
                self.stream_state = "reconnecting"
                log.info("event stream dropped: %r", e)
            self.stream_since = None
            await asyncio.sleep(backoff + random.random())
            backoff = min(backoff * 2, 120)

    async def handle_event(self, ev: dict) -> None:
        if not ev.get("id") or not ev.get("event"):
            return
        is_new = await self.db.insert_event(ev)
        await self.db.kv_set("event_cursor", str(ev["id"]))
        if not is_new:
            return
        await self.apply_timers(ev)
        n = await notify.add_notification(self.db, ev)
        self.hub.publish("event", ev)
        if n:
            self.hub.publish("notify", n)
        if ev["event"] in notify.DONE_EVENTS or ev["event"].startswith(("device.", "travel.", "mining.")):
            self.hub.publish("state", ev["event"])

    async def backfill(self) -> int:
        """Pull anything missed from /events (used on demand from the Events page)."""
        cursor = await self.db.kv_get("event_cursor")
        added = 0
        for _ in range(20):
            body = await self.api.get("/events", background=True, limit=100, cursor=cursor)
            items = (body or {}).get("events") or []
            for ev in items:
                if await self.db.insert_event(ev):
                    added += 1
                    await self.apply_timers(ev)
                    await notify.add_notification(self.db, ev)
                cursor = str(ev.get("id"))
            nxt = (body or {}).get("next_cursor")
            if cursor:
                await self.db.kv_set("event_cursor", cursor)
            if not nxt or not items:
                break
            cursor = nxt
        return added

    # --- timers ------------------------------------------------------------------------
    async def set_timer(self, key: str, kind: str, label: str, ends_at: datetime, *, device_code=None,
                        replicant_code=None, location=None, source=None, started_at: datetime | None = None) -> None:
        await self.db.execute(
            "INSERT INTO timers(key, kind, label, device_code, replicant_code, location, started_at, ends_at, source) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET kind=excluded.kind, label=excluded.label, "
            "ends_at=excluded.ends_at, started_at=excluded.started_at, source=excluded.source, location=excluded.location",
            (key, kind, label, device_code, replicant_code, location,
             _iso(started_at or datetime.now(timezone.utc)), _iso(ends_at), source),
        )

    async def clear_timer(self, key: str) -> None:
        await self.db.execute("DELETE FROM timers WHERE key=?", (key,))

    async def apply_timers(self, ev: dict) -> None:
        e = ev.get("event", "")
        p = ev.get("payload") or {}
        dc = ev.get("device_code") or ev.get("replicant_code") or "?"
        who = f"{(ev.get('device_type') or 'device').replace('_', ' ')} {dc}"
        created = _parse_ts(ev.get("created_at")) or datetime.now(timezone.utc)
        kw = dict(device_code=ev.get("device_code"), replicant_code=ev.get("replicant_code"),
                  location=ev.get("location"), source=e, started_at=created)
        if e == "travel.departed":
            end = _parse_ts(p.get("arrives_at"))
            if not end and p.get("travel_time_seconds") is not None:
                end = created + timedelta(seconds=float(p["travel_time_seconds"]))
            if end:
                await self.set_timer(f"travel:{dc}", "travel", f"{who} → {p.get('destination', '?')}", end, **kw)
                # Drop the provisional countdown created from the action response for the same trip.
                await self.db.execute(
                    "DELETE FROM timers WHERE source='action' AND kind='travel' AND key != ? "
                    "AND abs(strftime('%s', ends_at) - strftime('%s', ?)) < 15", (f"travel:{dc}", _iso(end)))
        elif e in ("travel.arrived", "travel.cancelled"):
            await self.clear_timer(f"travel:{dc}")
        elif e == "print.started":
            end = _parse_ts(p.get("completes_at"))
            if end:
                await self.set_timer(f"print:{dc}", "print", f"print {p.get('device_type', '?')} @ {ev.get('location') or dc}", end, **kw)
        elif e == "print.completed":
            await self.clear_timer(f"print:{dc}")
        elif e in ("scan.started", "search.started"):
            eta = p.get("eta_seconds")
            if eta is not None:
                target = p.get("scan_target") or p.get("search_target") or ev.get("location")
                await self.set_timer(f"scan:{dc}", "scan", f"{who} {e.split('.')[0]} {target}",
                                     created + timedelta(seconds=float(eta)), **kw)
        elif e in ("scan.completed", "search.completed"):
            await self.clear_timer(f"scan:{dc}")
        elif e in ("device.compacting", "device.unfurling", "triangulation.started"):
            end = _parse_ts(p.get("completes_at"))
            if end:
                await self.set_timer(f"{e}:{dc}", e.split(".")[1], f"{who} {e.split('.')[1]}", end, **kw)
        elif e in ("device.compacted", "device.unfurled"):
            await self.clear_timer(f"device.{'compacting' if e.endswith('compacted') else 'unfurling'}:{dc}")
        elif e.startswith("triangulation.") and e != "triangulation.started":
            await self.clear_timer(f"triangulation.started:{dc}")
        elif e.startswith("teleport.") and e != "teleport.started":
            await self.clear_timer(f"teleport:{ev.get('replicant_code') or dc}")

    async def timers_from_response(self, path: str, resp: Any, label: str) -> None:
        """Action responses often carry arrives_at / completes_at — show a countdown straight away."""
        if not isinstance(resp, dict):
            return
        end = _parse_ts(resp.get("arrives_at")) or _parse_ts(resp.get("completes_at"))
        if not end or resp.get("status") == "preview":
            return
        parts = [x for x in path.strip("/").split("/") if x]
        code = parts[1] if len(parts) > 1 else "?"
        if "arrives_at" in resp:
            kind, key = "travel", f"travel:{code}"
        elif resp.get("status") == "teleporting":
            kind, key = "teleport", f"teleport:{code}"
        else:
            kind, key = resp.get("status") or "action", f"{resp.get('status') or 'action'}:{code}"
        await self.set_timer(key, kind, label, end, device_code=code, source="action")

    async def prune_timers(self) -> None:
        cutoff = _iso(datetime.now(timezone.utc) - timedelta(minutes=10))
        await self.db.execute("DELETE FROM timers WHERE ends_at < ?", (cutoff,))

    # --- polling -----------------------------------------------------------------------
    async def _poll_loop(self, name: str, interval: int, fn) -> None:
        await asyncio.sleep(random.random() * 3)
        while True:
            try:
                await fn()
                await self.db.kv_set(f"sync:{name}", {"ok": True, "at": now_iso()})
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("sync %s failed: %s", name, e)
                await self.db.kv_set(f"sync:{name}", {"ok": False, "at": now_iso(), "error": str(e)})
            await asyncio.sleep(interval)

    async def sync_account(self) -> None:
        me = await self.api.get("/accounts/me", background=True)
        await self.db.kv_set("account", me)
        # Replicant details (position, stowed devices, status).
        reps = {}
        for r in (me or {}).get("replicants") or []:
            code = r.get("replicant_code")
            if not code:
                continue
            try:
                reps[code] = {**r, **(await self.api.get(f"/replicants/{code}", background=True) or {})}
            except ApiError as e:
                reps[code] = {**r, "_error": e.message}
        await self.db.kv_set("replicants", reps)
        await self.prune_timers()
        self.hub.publish("state", "account")

    async def sync_devices(self) -> None:
        devices = await self.api.paged("/devices", "devices")
        await self.db.kv_set("devices", devices)
        self.hub.publish("state", "devices")

    async def sync_inventory(self) -> None:
        locs = normalize_inventory(await self.api.paged("/inventory", "locations"))
        await self.db.kv_set("inventory", locs)
        totals: dict[str, float] = defaultdict(float)
        for loc in locs:
            for k, v in loc["items"].items():
                totals[k] += v
        prev = await self.db.kv_get("inventory_totals")
        if prev != totals or not prev:
            ts = now_iso()
            for k, v in totals.items():
                await self.db.execute("INSERT OR REPLACE INTO inventory_history(ts, resource, qty) VALUES(?,?,?)", (ts, k, v))
            await self.db.kv_set("inventory_totals", totals)
        try:
            await self.db.kv_set("locations", (await self.api.get("/locations", background=True) or {}).get("locations") or {})
        except ApiError as e:
            log.info("locations overview failed: %s", e)

    async def sync_messages(self) -> None:
        body = await self.api.get("/messages", background=True, limit=50, latest="true")
        await self.db.kv_set("messages", (body or {}).get("messages") or [])

    async def sync_catalogue(self) -> None:
        body = await self.api.get("/blueprints", background=True)
        await self.db.kv_set("blueprints", normalize_blueprints((body or {}).get("blueprints")))
        try:  # 1/min limit on the catalogue; we only ask every 30 minutes.
            stars = await self.api.get("/stars", background=True)
            await self.db.kv_set("stars", stars or {})
        except ApiError as e:
            log.info("star catalogue failed: %s", e)
        try:
            ach = await self.api.get("/accounts/achievements", background=True)
            await self.db.kv_set("achievements", ach or {})
        except ApiError:
            pass

    async def refresh_system(self, star: str) -> dict:
        data = await self.api.get(f"/locations/{star}")
        await self.db.execute(
            "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
            (star, json.dumps(data), now_iso()),
        )
        return data
