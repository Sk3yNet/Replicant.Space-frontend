"""Background workers: live event ingestion and periodic state polling."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any

from .api import ApiError, RSClient
from .config import Settings
from .db import DB, now_iso
from .hub import Hub
from . import notify
from .shapes import normalize_blueprints, normalize_inventory

log = logging.getLogger("rsweb.ingest")
DEVICE_CODE_RE = re.compile(r"[0-9A-F]{8}")


def _parse_ts(v: Any) -> datetime | None:
    if not v or not isinstance(v, str):
        return None
    try:
        dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# An event older than this when it reaches us is a replay (the stream catching up after downtime or a stall):
# it goes in the feed, but raises no notification and triggers no reactive rules.
LATE_EVENT = timedelta(minutes=30)


def is_late(ev: dict, now: datetime | None = None) -> bool:
    created = _parse_ts(ev.get("created_at"))
    return bool(created) and (now or datetime.now(timezone.utc)) - created > LATE_EVENT


BLUEPRINT_HINT_CATEGORIES ={"experience", "progression", "achievement", "story", "event", "trade", "simulation", "message"}


def may_unlock_blueprint(ev: dict) -> bool:
    """Events after which the blueprint list may have grown. Cheap check; a false positive costs one GET."""
    p = ev.get("payload") or {}
    if p.get("blueprint_discovered") or p.get("blueprint_unlocked") or p.get("blueprint"):
        return True
    name = ev.get("event") or ""
    if "blueprint" in name or "unlock" in name or "achievement" in name:
        return True
    if ev.get("category") in BLUEPRINT_HINT_CATEGORIES and name != "experience.gained":
        return "blueprint" in json.dumps(p).lower() or name in ("event.completed", "trade.completed",
                                                                  "story.awakened", "simulation.completed")
    return "blueprint" in json.dumps(p).lower()


def _target(label: str) -> str:
    return label.split("→", 1)[1].strip().upper() if "→" in (label or "") else ""


def duplicate_timers(rows: list[dict], window: float = 20.0) -> tuple[list[dict], list[str]]:
    """Collapse timers that describe the same thing (returns kept rows, keys of the duplicates).

    The game can report one activity several ways: the command's own response (keyed by the
    replicant), an event for the host vessel, a replicant-level event with no device code …
    Two timers are the same activity when they have the same kind, finish within `window`
    seconds of each other, and either share a device, or one of them has no device / came from
    a command response, or they head for the same target. Separate devices doing the same thing
    at the same time (e.g. an AMI launching five drones) are kept apart.
    Event-sourced timers win over command-response ones.
    """
    def ends(r):
        dt = _parse_ts(r.get("ends_at"))
        return dt.timestamp() if dt else 0.0

    ordered = sorted(rows, key=lambda r: (r.get("source") == "action", ends(r)))
    kept: list[dict] = []
    dups: list[str] = []
    for r in ordered:
        match = None
        for k in kept:
            if k.get("kind") != r.get("kind") or abs(ends(k) - ends(r)) > window:
                continue
            same_dev = r.get("device_code") and r.get("device_code") == k.get("device_code")
            loose = (not r.get("device_code") or not k.get("device_code")
                     or r.get("source") == "action" or k.get("source") == "action")
            same_target = _target(r.get("label", "")) and _target(r.get("label", "")) == _target(k.get("label", ""))
            if same_dev or (loose and (same_target or not _target(r.get("label", "")) or r.get("label") == k.get("label"))):
                match = k
                break
        if match:
            dups.append(r["key"])
        else:
            kept.append(r)
    kept.sort(key=ends)
    return kept, dups


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


# docs catalogue: device.lost (v3.5.0, flung off by a diversion); device.changed_owner {direction: sent | received} —
# only "sent" means it left us (device.transferred / device.owner_changed kept from before the catalogue was read)
SALVAGE_MSG = re.compile(r"salvage discovered:|salvage[^.]{0,60}(discover|found|detect)|(discover|found|detect)[^.]{0,60}salvage",
                         re.I)   # live (owner, 2026-10-15): "Salvage discovered: …"


def salvage_message(m: dict) -> bool:
    """A game message announcing salvage: any of its text fields says so (the field names vary: title / body /
    message / text / subject …)."""
    return bool(SALVAGE_MSG.search(" ".join(str(v) for v in m.values() if isinstance(v, str))))


def message_id(m: dict):
    return m.get("id") if m.get("id") is not None else m.get("message_id")


def message_unread(m: dict) -> bool:
    return not (m.get("read") or m.get("is_read") or m.get("read_at"))
GONE_EVENTS = ("device.decommissioned", "device.destroyed", "device.lost", "device.transferred", "device.owner_changed",
               "hub.destroyed")
GONE_SQL = (f"(event IN ({','.join('?' * len(GONE_EVENTS))}) OR "
            "(event='device.changed_owner' AND payload NOT LIKE '%\"received\"%'))")
UNLISTED_KEEP_HOURS = 12


def foreign(d: dict, mine) -> bool:
    """Another player's device. The game's device list names no owner; GET /devices/{code} answers for anyone's device
    and names its owner (live 2026-10-27: Bill's replicant interface F4791EDF, re-fetched after a simulation order,
    landed in the list and then lingered 12 h as unlisted)."""
    o = d.get("owner")
    code = o.get("replicant_code") if isinstance(o, dict) else None
    return bool(code) and code not in mine


def _uploaded_look(d: dict, datacenter: str) -> bool:
    """A data-feature device deployed at the datacenter: when it leaves the list, the interface consumed it."""
    return ("data" in (d.get("features") or []) and d.get("location") == datacenter
            and not d.get("stowed_in_device_code") and not d.get("attached_to_device_code"))


def merge_device_snapshot(prev: list[dict], new: list[dict], partial: bool = False, gone: set[str] | None = None,
                          now: str | None = None, accept_short: bool = False) -> list[dict] | None:
    """Guard the device list against snapshots that leave devices out. None = ignore this snapshot entirely.

    • an empty or far-shorter list than last time (< half) is ignored
    • partial snapshot (empty first page with a cursor, while the replicant travels): what it left out is kept, stale
    • complete snapshot: a device that was there last time but isn't now is kept for up to 12 h, flagged
      `unlisted` + `location_stale`, unless an event says it's gone (decommissioned, destroyed, given away).
      Seen live (2026-10-02): cargo freighters that are *surging* between systems are missing from GET /devices
      until they arrive — without this they vanished from fleets and loadouts.
    • a device that comes back with location null but isn't stowed/attached keeps its last known
      location, marked `location_stale: true`, so loadouts don't count it as gone
    """
    gone = gone or set()
    now = now or _iso(datetime.now(timezone.utc))
    if prev and not new:
        return None
    if partial and prev:
        # keep what this snapshot left out, flagged stale; take everything it did return
        seen = {d.get("device_code") for d in new}
        new = new + [{**d, "location_stale": True} for d in prev if d.get("device_code") not in seen]
    elif prev and not accept_short and len(new) < len([d for d in prev if d.get("device_code") not in gone]) / 2:
        return None   # devices an event says are gone don't count; a short list that keeps coming back is accepted
    else:
        seen = {d.get("device_code") for d in new}
        keep = []
        for d in prev:
            code = d.get("device_code")
            if code in seen or code in gone:
                continue
            since = d.get("unlisted_since") or now
            try:
                age_h = (datetime.fromisoformat(now) - datetime.fromisoformat(since)).total_seconds() / 3600
            except ValueError:
                age_h = 0
            if age_h > UNLISTED_KEEP_HOURS:
                continue
            st = str(d.get("status") or "")
            keep.append({**d, "unlisted": True, "unlisted_since": since, "location_stale": True,
                         "status": st if st.startswith(("surg", "travel", "cruis")) else (st or "unlisted")})
        new = new + keep
    last = {d.get("device_code"): d.get("location") for d in prev if d.get("location")}
    out = []
    for d in new:
        if not d.get("unlisted"):
            d = {k: v for k, v in d.items() if k not in ("unlisted", "unlisted_since")}
        if not d.get("location") and not d.get("stowed_in_device_code") and not d.get("attached_to_device_code") \
                and last.get(d.get("device_code")):
            d = {**d, "location": last[d["device_code"]], "location_stale": True}
        out.append(d)
    return out


SWOOP_FOCUS_S = 180   # a swoop run: 20 s warmup + 5 rounds of up to 20 s, with room to spare

class Worker:
    def __init__(self, settings: Settings, db: DB, api: RSClient, hub: Hub):
        self.s, self.db, self.api, self.hub = settings, db, api, hub
        self.tasks: list[asyncio.Task] = []
        self.stream_state = "stopped"
        self.stream_since: str | None = None
        self._bp_refresh: asyncio.Task | None = None
        self.late = {"n": 0, "oldest": None}   # late events since the last live one, for the catch-up note
        self._watch: dict[str, datetime] = {}   # device code → until when its events refresh it (after_order)
        self._bg: set[asyncio.Task] = set()
        self._auto_q: deque = deque()   # events waiting for the engine lock, in order (see _automate)
        self._auto_task: asyncio.Task | None = None
        self.devices_lock = asyncio.Lock()   # quick read-modify-writes of the cached device list (refreshes, bulk tags)
        self._arrivals: list[datetime] = []   # arrival refreshes lately (watched_event)
        self._rep_dirty: set[str] = set()     # replicants whose details the next account sync re-reads (sync_account)
        self._hosted: dict[str, str] = {}     # hosted device code → replicant code
        self._event_read: dict[str, float] = {}   # device code → when an event last had it re-read (after_order)
        from .automations import AutomationEngine
        self.automations = AutomationEngine(db, api, hub, self)

    # --- lifecycle --------------------------------------------------------------
    def start(self) -> None:
        if not self.api.configured:
            log.warning("No API token configured; background workers not started")
            self.stream_state = "no token"
            return
        self.automations.start()
        self.tasks = [
            asyncio.create_task(self._stream_loop(), name="stream"),
            asyncio.create_task(self._poll_loop("account", self.s.poll_account, self.sync_account), name="p-account"),
            asyncio.create_task(self._poll_loop("devices", self.s.poll_devices, self.sync_devices), name="p-devices"),
            asyncio.create_task(self._poll_loop("inventory", self.s.poll_inventory, self.sync_inventory), name="p-inv"),
            asyncio.create_task(self._poll_loop("messages", self.s.poll_messages, self.sync_messages), name="p-msg"),
            asyncio.create_task(self._poll_loop("blueprints", self.s.poll_blueprints, self.sync_blueprints), name="p-bp"),
            asyncio.create_task(self._poll_loop("catalogue", self.s.poll_catalogue, self.sync_catalogue), name="p-cat"),
            asyncio.create_task(self._poll_loop("traffic", self.traffic_interval, self.automations.sync_traffic), name="p-traffic"),
            asyncio.create_task(self._poll_loop("objects", lambda: self.automations.objects_interval(self.s.poll_objects),
                                                   self.automations.poll_objects), name="p-objects"),
        ]

    async def stop(self) -> None:
        await self.automations.stop()
        for t in self.tasks + ([self._auto_task] if self._auto_task else []):
            t.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    # --- event stream -------------------------------------------------------------
    async def _stream_loop(self) -> None:
        backoff = 2.0
        while True:
            try:   # the stream keeps ~10,000 events; /events keeps months: fill any gap first
                if await self.db.kv_get("event_cursor"):
                    n = await self.backfill()
                    if n:
                        log.info("backfilled %d events before (re)connecting the stream", n)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("backfill before connecting failed")
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
        if not isinstance(ev.get("payload"), dict):
            ev["payload"] = {}
        if await self._virtual(ev):
            # inside a simulation: stored apart (category simulation) and only wakes the run's own jobs — no alerts,
            # timers, rules, maps or stock from the virtual galaxy
            ev["category"], ev["virtual"] = "simulation", True
            if await self.db.insert_event(ev):
                try:
                    await self.automations.on_event(ev, late=True)
                except Exception:
                    log.exception("simulation event %s failed", ev.get("event"))
            await self.db.kv_set("event_cursor", str(ev["id"]))
            return
        is_new = await self.db.insert_event(ev)
        if is_new:
            await self.process_event(ev)
        await self.db.kv_set("event_cursor", str(ev["id"]))

    async def _virtual(self, ev: dict) -> bool:
        from .sim import virtual_event
        if virtual_event(ev):
            return True
        code = ev.get("device_code")
        return bool(code) and any(d.get("device_code") == code for d in await self.db.kv_get("sim:devices", []) or [])

    async def process_event(self, ev: dict, notify_live: bool = True) -> None:
        """Everything a new event sets off. Each part is guarded: the event is stored already, so a part that fails
        on odd data must not stop the rest (or drop the stream connection)."""
        if self._hosted:   # an event about a replicant's vessel: its details are re-read at the next account sync
            p = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            for c in {ev.get("device_code")} | {v for v in p.values() if isinstance(v, str)}:
                if c in self._hosted:
                    self._rep_dirty.add(self._hosted[c])
        for part in (self.apply_timers, self.on_prospect if ev["event"] == "prospect.completed" else None):
            if part:
                try:
                    await part(ev)
                except Exception:
                    log.exception("%s failed on %s", part.__name__, ev.get("event"))
        try:
            await self._announce(ev, notify_live)
        except Exception:
            log.exception("notifying %s failed", ev.get("event"))

        if ev["event"].startswith("ami.") and ev["event"].endswith(".digest"):
            try:
                await self.expand_digest(ev)
            except Exception:
                log.exception("expanding %s failed", ev.get("event"))

    async def expand_digest(self, ev: dict) -> None:
        """Devices adopted by an AMI controller stop sending their own events: the controller's digest carries them
        (docs: AMI digests). A survey digest's report.scans[] become stored scan.completed events (the map and the
        system pages read those), and each device's last_event wakes jobs waiting on that device. Both go to the
        engine as replays (late): they update what we know and wake jobs, without firing reactive rules."""
        p = ev.get("payload") or {}
        for i, sc in enumerate((p.get("report") or {}).get("scans") or []):
            if not isinstance(sc, dict) or not sc.get("device_code"):
                continue
            target = sc.get("scan_target")
            syn = {"id": f"{ev['id']}#scan{i}", "event": "scan.completed", "category": "scan",
                   "device_code": sc["device_code"], "location": target, "star": (target or "").split("-")[0] or None,
                   "created_at": ev.get("created_at"), "payload": {**sc, "from_digest": ev["id"]}}
            if await self.db.insert_event(syn):
                await self.automations.on_event(syn, late=True)
        for d in p.get("devices") or []:
            if isinstance(d, dict) and d.get("device_code") and d.get("last_event") and d.get("events"):
                await self.automations.on_event({"id": f"{ev['id']}#{d['device_code']}", "event": d["last_event"],
                                                 "device_code": d["device_code"], "created_at": ev.get("created_at"),
                                                 "payload": {"from_digest": ev["id"], "status": d.get("status")}},
                                                late=True)

    async def _announce(self, ev: dict, notify_live: bool) -> None:
        late = is_late(ev) or not notify_live
        if late:
            self.late["n"] += 1
            self.late["oldest"] = self.late["oldest"] or ev.get("created_at")
            n = None
        else:
            await self.note_caught_up()
            n = await notify.add_notification(self.db, ev) or await notify.add_mention(self.db, ev)
        self.hub.publish("event", ev)
        if n:
            self.hub.publish("notify", n)
        if ev["event"] in notify.DONE_EVENTS or ev["event"].startswith(("device.", "travel.", "mining.")):
            self.hub.publish("state", ev["event"])
        if not late:
            self.watched_event(ev)
        if may_unlock_blueprint(ev):
            bp = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
            if ev["event"] == "blueprint.unlocked" and bp.get("device_type"):
                # v3.6.1: how it was unlocked (xp, achievement, decommission), for the announcement
                src = await self.db.kv_get("blueprint_sources", {}) or {}
                src[bp["device_type"]] = bp.get("source")
                await self.db.kv_set("blueprint_sources", dict(list(src.items())[-200:]))
            self.request_blueprint_refresh()
        if ev.get("category") == "swoop" and not late:   # a live run: keep the loop free until it's over
            if ev["event"] == "swoop.started":
                self.automations.focus(SWOOP_FOCUS_S)
            elif ev["event"] == "swoop.complete":
                self.automations.focus(0)
        await self._automate(ev, late)

    async def _automate(self, ev: dict, late: bool) -> None:
        """Hand the event to the automations. They take the engine lock, which a tick can hold for a minute (80 s
        live 2026-10-29 after a restart): waiting here held up the event stream, so a swoop run's rounds reached the
        cockpit only after it ended. While the lock is busy, events queue in order for a worker instead."""
        lock = self.automations.lock
        if not self._auto_q and not (self._auto_task and not self._auto_task.done()) and (
                not lock.locked() or lock._owner is asyncio.current_task()):
            try:
                await self.automations.on_event(ev, late=late)
            except Exception:
                log.exception("automations failed on %s", ev.get("event"))
            return
        self._auto_q.append((ev, late))
        if not self._auto_task or self._auto_task.done():
            self._auto_task = asyncio.create_task(self._drain_automations())

    async def _drain_automations(self) -> None:
        while self._auto_q:
            ev, late = self._auto_q.popleft()
            try:
                await self.automations.on_event(ev, late=late)
            except Exception:
                log.exception("automations failed on %s", ev.get("event"))

    async def note_caught_up(self) -> None:
        """First live event after a run of late ones: one note instead of a notification per replayed event."""
        n, oldest = self.late["n"], self.late["oldest"]
        if not n:
            return
        self.late = {"n": 0, "oldest": None}
        since = _parse_ts(oldest)
        span = f" from the last {(datetime.now(timezone.utc) - since).total_seconds() / 3600:.0f} h" if since else ""
        await self.automations.log("engine", f"caught up {n} late event(s){span}: they're in the Events feed, "
                                             "without notifications, and arrival / salvage rules skipped them", notify=True)

    # --- blueprints ------------------------------------------------------------------
    def request_blueprint_refresh(self, delay: float = 5.0) -> None:
        """Re-read /blueprints shortly after something that may have unlocked one (coalesced)."""
        if self._bp_refresh and not self._bp_refresh.done():
            return

        async def later():
            await asyncio.sleep(delay)
            try:
                await self.sync_blueprints()
            except Exception as e:
                log.info("blueprint refresh failed: %s", e)

        self._bp_refresh = asyncio.create_task(later())

    async def sync_blueprints(self) -> list[str]:
        """Refresh known blueprints; returns (and announces) any newly unlocked device types."""
        body = await self.api.get("/blueprints", background=True)
        new_bps = normalize_blueprints((body or {}).get("blueprints"))
        old = await self.db.kv_get("blueprints", None)
        await self.db.kv_set("blueprints", new_bps)
        from .fleets import learn_stowable
        learn_stowable(new_bps)
        await self.db.kv_set("sync:blueprints", {"ok": True, "at": now_iso()})
        if old is None:  # first sync: everything is "known", nothing is "new"
            return []
        before = {b.get("device_type") for b in normalize_blueprints(old)}
        added = [b["device_type"] for b in new_bps if b["device_type"] not in before]
        if added:
            unlocks = await self.db.kv_get("blueprint_unlocks", []) or []
            srcs = await self.db.kv_get("blueprint_sources", {}) or {}
            for t in added:
                unlocks.append({"device_type": t, "at": now_iso(), "source": srcs.get(t)})
                how = {"xp": "experience", "decommission": "decommissioning"}.get(srcs.get(t), srcs.get(t))
                title = f"New blueprint unlocked: {t.replace('_', ' ')}" + (f" (by {how})" if how else "")
                cur = await self.db.execute(
                    "INSERT INTO notifications(event_id, level, title, body, link, created_at) VALUES(?,?,?,?,?,?)",
                    (None, "done", title, None, f"/blueprints?q={t}", now_iso()))
                self.hub.publish("notify", {"id": cur.lastrowid, "level": "done", "title": title, "link": f"/blueprints?q={t}"})
            await self.db.kv_set("blueprint_unlocks", unlocks[-200:])
            self.hub.publish("state", "blueprints")
        return added

    async def backfill(self) -> int:
        """Pull anything missed from /events: before the stream (re)connects, and on demand from the Events page."""
        cursor = await self.db.kv_get("event_cursor")
        added = 0
        for _ in range(20):
            body = await self.api.get("/events", background=True, limit=100, cursor=cursor, filtered="true")
            items = (body or {}).get("events") or []
            for ev in items:
                if not isinstance(ev.get("payload"), dict):
                    ev["payload"] = {}
                if ev.get("id") and ev.get("event") and await self.db.insert_event(ev):
                    added += 1
                    await self.process_event(ev)   # timers, notifications and the automations, as from the stream
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
        rows = await self.db.fetchall("SELECT * FROM timers WHERE kind=?", (kind,))
        _, dups = duplicate_timers(rows)
        for k in dups:
            await self.clear_timer(k)

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
        elif e == "print.cancelled":   # v3.5.2
            await self.clear_timer(f"print:{dc}")
        elif e == "print.completed":
            await self.clear_timer(f"print:{dc}")
            if p.get("compacted") or p.get("print_mode") == "flatpack":   # printed folded up
                from .modular import remember
                await remember(self.db, p.get("new_device_code"), True, _iso(created))
        elif e in ("scan.started", "search.started"):
            eta = p.get("eta_seconds")
            if eta is not None:
                target = p.get("scan_target") or p.get("search_target") or ev.get("location")
                await self.set_timer(f"scan:{dc}", "scan", f"{who} {e.split('.')[0]} {target}",
                                     created + timedelta(seconds=float(eta)), **kw)
        elif e in ("scan.completed", "search.completed"):
            await self.clear_timer(f"scan:{dc}")
        elif e in ("device.compacting", "device.unfurling", "triangulation.started"):
            if e == "device.unfurling":
                from .modular import remember
                await remember(self.db, ev.get("device_code"), False, _iso(created))
            end = _parse_ts(p.get("completes_at"))
            if end:
                await self.set_timer(f"{e}:{dc}", e.split(".")[1], f"{who} {e.split('.')[1]}", end, **kw)
        elif e in ("device.compacted", "device.unfurled"):
            await self.clear_timer(f"device.{'compacting' if e.endswith('compacted') else 'unfurling'}:{dc}")
            from .modular import remember
            await remember(self.db, ev.get("device_code"), e == "device.compacted", _iso(created))
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
        if kind == "travel" and resp.get("departed_at"):
            await self.note_trip(resp.get("device_code") or code, resp)

    # --- devices touched by your own orders ---------------------------------------------------------
    WATCH_MINUTES = 10

    @staticmethod
    def order_codes(path: str, body: Any) -> set[str]:
        """The devices an order touches: the one in the path (/devices/CODE…) and any device code in the body (stow
        target / cargo, attach device / carrier …)."""
        parts = [x for x in str(path).strip("/").split("/") if x]
        codes = {parts[1]} if len(parts) > 1 and parts[0] == "devices" else set()
        if isinstance(body, dict):
            codes |= {v for v in body.values() if isinstance(v, str) and DEVICE_CODE_RE.fullmatch(v)}
        return codes

    def after_order(self, path: str, body: Any) -> None:
        """An order you gave (by hand, or a chain you started) went through: ask the game for those devices now and
        again shortly, and for the next WATCH_MINUTES on any event about them, so pages show the result within
        seconds instead of at the next device sync (up to a minute, or much longer while the engine runs)."""
        parts = [x for x in str(path).strip("/").split("/") if x]
        if len(parts) > 1 and parts[0] == "replicants":
            self._rep_dirty.add(parts[1])
        codes = self.order_codes(path, body)
        if not codes:
            return
        until = datetime.now(timezone.utc) + timedelta(minutes=self.WATCH_MINUTES)
        for c in codes:
            self._watch[c] = until

        async def go() -> None:
            t0 = time.time()
            await self.refresh_devices_now(codes)
            await asyncio.sleep(20)   # stowing / attaching / launching can take a moment in the game
            # once more, unless an event about the device already had it re-read meanwhile (1.293.0)
            later = {c for c in codes if self._event_read.get(c, 0) < t0}
            if later:
                await self.refresh_devices_now(later)
        self._spawn(go())

    def _spawn(self, coro) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    ARRIVAL_REFRESHES = 30   # per 10 minutes: a fleet arriving at once doesn't eat the request budget

    def watched_event(self, ev: dict) -> None:
        """An event about a device a recent order touched, or any arrival: refresh it (and any device the event names),
        so the device page shows the new location when its countdown ends instead of at the next device sync."""
        watch = self._watch
        now = datetime.now(timezone.utc)
        if ev.get("event") == "travel.arrived" and ev.get("device_code"):
            self._arrivals = [t for t in self._arrivals if now - t < timedelta(minutes=10)]
            if len(self._arrivals) < self.ARRIVAL_REFRESHES:
                self._arrivals.append(now)
                watch.setdefault(ev["device_code"], now + timedelta(seconds=1))
        if not watch:
            return
        for c in [c for c, t in watch.items() if t < now]:
            watch.pop(c)
        p = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
        named = {ev.get("device_code")} | {v for v in p.values() if isinstance(v, str) and DEVICE_CODE_RE.fullmatch(v)}
        if any(c in watch for c in named if c):
            t = time.time()
            for c in named:
                if c:
                    self._event_read[c] = t
            self._event_read = {c: v for c, v in self._event_read.items() if v > t - 120}
            self._spawn(self.refresh_devices_now({c for c in named if c}))

    async def refresh_devices_now(self, codes: set[str]) -> None:
        """GET each device (at most 8) and put the answers on the cached device list."""
        fresh = {}
        for c in sorted(codes)[:8]:
            try:
                d = await self.api.get(f"/devices/{c}")
            except ApiError:
                continue
            except Exception:
                log.exception("refreshing device %s failed", c)
                continue
            if isinstance(d, dict) and d.get("device_code"):
                fresh[d["device_code"]] = d
        if not fresh:
            return
        async with self.devices_lock:
            mine = set(await self.db.kv_get("replicants", {}) or {})
            fresh = {c: d for c, d in fresh.items() if not foreign(d, mine)}
            devices = [d for d in await self.db.kv_get("devices", []) or [] if not foreign(d, mine)]
            for d in devices:
                f = fresh.pop(d.get("device_code"), None)
                if f is not None:
                    d.update(f)
            devices.extend(fresh.values())   # new to the list (printed, adopted)
            await self.db.kv_set("devices", devices)
        self.hub.publish("state", "devices")

    async def note_trip(self, code: str, resp: dict) -> None:
        """A travel command's answer is the trip itself (origin, route, departed_at, arrives_at …): put it on the cached
        device — and on the devices riding along — straight away, so the maps show it now rather than after the next
        device sync (up to a minute later)."""
        riders = {x if isinstance(x, str) else (x or {}).get("device_code") for x in resp.get("attached_devices") or []}
        codes = {code} | {c for c in riders if c}
        trip = {k: v for k, v in resp.items() if k not in ("attached_devices", "device_code")}
        devices = await self.db.kv_get("devices", []) or []
        hit = False
        for d in devices:
            if d.get("device_code") in codes:
                d["travel"] = trip
                if not str(d.get("status") or "").startswith(("travel", "cruis", "surg", "stowed", "attached")):
                    d["status"] = "travelling"
                hit = True
        if hit:
            await self.db.kv_set("devices", devices)
            self.hub.publish("state", "travel.started")

    async def prune_timers(self) -> None:
        cutoff = _iso(datetime.now(timezone.utc) - timedelta(minutes=10))
        await self.db.execute("DELETE FROM timers WHERE ends_at < ?", (cutoff,))
        # the action log and the bell would otherwise grow for ever: keep 30 days of actions, and read
        # notifications for 30 days (unread ones stay until read)
        month = _iso(datetime.now(timezone.utc) - timedelta(days=30))
        await self.db.execute("DELETE FROM actions WHERE at < ?", (month,))
        await self.db.execute("DELETE FROM notifications WHERE read = 1 AND created_at < ?", (month,))

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
            await asyncio.sleep(await interval() if callable(interval) else interval)

    async def _note_relay_states(self, devices: list[dict]) -> None:
        """Remember whether each relay was last seen on (relaying) or off (inactive): out of everyone's reach the game
        lists it out_of_range either way, so the last clear reading — e.g. while a replicant was there — stands
        (web.relays_on). Relays rarely switch off once on. kv "relay_state" {code: {"on", "at"}}."""
        state = await self.db.kv_get("relay_state", {}) or {}
        changed = False
        for d in devices:
            if "relay" not in (d.get("device_type") or ""):
                continue
            st = str(d.get("status") or "")
            on = True if st.startswith("relaying") else False if st.startswith("inactive") else None
            code = d.get("device_code")
            if on is not None and code and (state.get(code) or {}).get("on") is not on:
                state[code] = {"on": on, "at": now_iso()}
                changed = True
        if changed:
            await self.db.kv_set("relay_state", state)

    async def traffic_interval(self) -> int:
        """Beacon audit polling: Map › Traffic's setting (minutes), else POLL_TRAFFIC."""
        m = ((await self.db.kv_get("traffic_settings", {})) or {}).get("poll_minutes")
        try:
            return max(60, int(float(m) * 60)) if m not in (None, "") else self.s.poll_traffic
        except (TypeError, ValueError):
            return self.s.poll_traffic

    async def sync_account(self) -> None:
        me = await self.api.get("/accounts/me", background=True)
        await self.db.kv_set("account", me)
        from .shapes import ACCOUNT_TZ
        ACCOUNT_TZ[0] = (me or {}).get("timezone") or ACCOUNT_TZ[0]
        # Replicant details (position, stowed devices, status): one GET per replicant, so only when it moved (the
        # account's summary has its location each minute), an event named its vessel, an order named it, or every
        # POLL_REPLICANT seconds (1.293.0: re-reading all 22 each minute was a fifth of the read budget)
        prev = await self.db.kv_get("replicants", {}) or {}
        now = time.time()
        reps = {}
        for r in (me or {}).get("replicants") or []:
            code = r.get("replicant_code")
            if not code:
                continue
            old = prev.get(code) or {}
            moved = any(old.get(k) != r.get(k) for k in ("current_location", "current_star", "hosted_device_code"))
            due = now - float(old.get("_read_at") or 0) >= self.s.poll_replicant
            if old and not old.get("_error") and not moved and not due and code not in self._rep_dirty:
                reps[code] = {**old, **r}
                continue
            self._rep_dirty.discard(code)
            try:
                reps[code] = {**r, **(await self.api.get(f"/replicants/{code}", background=True) or {}), "_read_at": now}
            except ApiError as e:
                reps[code] = {**r, "_error": e.message}
        self._hosted = {r["hosted_device_code"]: c for c, r in reps.items() if r.get("hosted_device_code")}
        await self.db.kv_set("replicants", reps)
        await self.prune_timers()
        self.hub.publish("state", "account")

    async def sync_devices(self) -> None:
        # page by hand: while the replicant travels the game returns an EMPTY first page that still has a
        # next_cursor — a partial snapshot, so anything missing from it is kept from the last good one
        devices, cursor, partial = [], None, False
        for _ in range(40):
            body = await self.api.get("/devices", background=True, limit=50, cursor=cursor) or {}
            page = body.get("devices") or []
            cursor = body.get("next_cursor")
            if not page and cursor:
                partial = True
            devices.extend(page)
            if not cursor:
                break
        mine = set(await self.db.kv_get("replicants", {}) or {})   # someone else's device: not ours to keep or miss
        devices = [d for d in devices if not foreign(d, mine)]
        prev = [d for d in await self.db.kv_get("devices", []) or [] if not foreign(d, mine)]
        listed = {d.get("device_code") for d in devices}
        missing = [d.get("device_code") for d in prev if d.get("device_code") not in listed]
        gone: set[str] = set()
        if missing and not partial:
            rows = await self.db.fetchall(
                f"SELECT DISTINCT device_code FROM events WHERE {GONE_SQL} AND device_code IN ({','.join('?' * len(missing))})",
                (*GONE_EVENTS, *missing))
            gone = {r["device_code"] for r in rows}
            # paid away in a trade or contract: gone at once, not kept as an unlisted (lost) device for 12 h
            from .lost import given_away
            by_prev = {d.get("device_code"): d for d in prev}
            gone |= await given_away(self.db, {c: ((by_prev.get(c) or {}).get("location"), (by_prev.get(c) or {}).get("device_type"))
                                               for c in missing if c not in gone})
            # broken down by the data interface (Simulations › Data uploads): gone at once as well
            # (or a data device deployed at the datacenter that left the list: only an upload takes it from there)
            from .sim import DATACENTER
            done = await self.db.kv_get("sim:uploaded", {}) or {}
            uploaded = {c for c in missing if c in done or _uploaded_look(by_prev.get(c) or {}, DATACENTER)} - gone
            if uploaded - set(done):
                await self.db.kv_set("sim:uploaded", {**done, **{c: {"type": by_prev[c].get("device_type"), "at": now_iso()}
                                                              for c in uploaded - set(done)}})
            gone |= uploaded
            if gone:   # explained: on the Lost devices page with the reason, not silently dropped
                from . import lost as lost_mod
                why = {**await lost_mod.reasons(self.db, sorted(gone)), **{c: "uploaded at the data interface" for c in uploaded}}
                ls = lost_mod.normalize(await self.db.kv_get(lost_mod.KV, {}))
                lost_mod.record_gone(ls, [{"code": c, "type": (by_prev.get(c) or {}).get("device_type"),
                                           "last_location": (by_prev.get(c) or {}).get("location"),
                                           "reason": why.get(c) or "paid in a deal"} for c in sorted(gone)])
                await self.db.kv_set(lost_mod.KV, ls)
        # the same short list three syncs running is the truth (devices really lost), not a glitch
        short = None if partial else len(devices)
        streak = getattr(self, "_short_streak", (None, 0))
        merged = merge_device_snapshot(prev, devices, partial, gone, accept_short=streak[0] == short and streak[1] >= 2)
        if merged is None:
            self._short_streak = (short, streak[1] + 1 if streak[0] == short else 1)
            log.warning("device sync returned an incomplete list (%s → %s); keeping the previous one", len(prev), len(devices))
            return
        self._short_streak = (None, 0)
        devices = merged
        # a renamed fleet: devices the game still lists under the old fleet: tag show the new one until retagged;
        # a rename nothing carries any more is done
        from . import fleets as fl
        renames = await self.db.kv_get(fl.RENAMES_KV, {}) or {}
        if renames:
            fl.apply_renames(devices, renames)
            still = {d["_retag"] for d in devices if d.get("_retag")}
            left = {o: n for o, n in renames.items() if fl.fleet_tag(o) in still}
            if left != renames:
                await self.db.kv_set(fl.RENAMES_KV, left)
        from .modular import FOLDED_KV
        marks = await self.db.kv_get(FOLDED_KV, {}) or {}
        for d in devices:   # large devices the game told us are folded (modular.folded)
            st = str(d.get("status") or "")
            if st.startswith("unfurling") and d.get("device_code") in marks:
                marks.pop(d["device_code"])
                await self.db.kv_set(FOLDED_KV, marks)
            if d.get("device_code") in marks:
                d["folded"] = True
            else:
                d.pop("folded", None)
        await self._note_relay_states(devices)
        await self.db.kv_set("devices", devices)
        self.hub.publish("state", "devices")
        try:   # lost equipment: out of sight (surging, unlisted, out of range) until seen again
            from .lost import track
            await track(self.db, devices)
        except Exception as e:  # never let this break the device sync
            log.info("lost-equipment tracking failed: %s", e)
        try:
            await self.sync_stowed(devices)
        except Exception as e:  # never let this break the device sync
            log.info("stowed map refresh failed: %s", e)

    async def sync_stowed(self, devices: list[dict], max_carriers: int = 25) -> None:
        """Which carrier holds which device: read each carrier's detail (the device list doesn't say)."""
        from .carrier import is_carrier
        if any("stowed_in_device_code" in d for d in devices):  # the device list says it directly: no extra requests
            direct: dict[str, list[str]] = {}
            for d in devices:
                if d.get("stowed_in_device_code"):
                    direct.setdefault(d["stowed_in_device_code"], []).append(d["device_code"])
            await self.db.kv_set("stowed_map", direct)
            return
        if not any(str(d.get("status", "")).startswith("stowed") for d in devices):
            await self.db.kv_set("stowed_map", {})
            return
        bps = normalize_blueprints(await self.db.kv_get("blueprints", []))
        stowed_map: dict[str, list[str]] = {}
        carriers = [d for d in devices if is_carrier(d, bps)][:max_carriers]
        for c in carriers:
            items = c.get("stowed_devices")
            if items is None:
                try:
                    items = (await self.api.get(f"/devices/{c['device_code']}", background=True) or {}).get("stowed_devices")
                except ApiError:
                    items = None
            codes = [i.get("device_code") for i in items or [] if isinstance(i, dict) and i.get("device_code")]
            if codes:
                stowed_map[c["device_code"]] = codes
        await self.db.kv_set("stowed_map", stowed_map)

    async def sync_inventory(self) -> None:
        locs = normalize_inventory(await self.api.paged("/inventory", "locations"))
        before = await self.db.kv_get("inventory", []) or []
        await self.db.kv_set("inventory", locs)
        try:   # Dashboard › Trends: mining output per hour (metrics.mining_gain)
            from . import metrics
            mining = await self.db.kv_derived("devices", "mining_locs", lambda ds: {
                d.get("location") for d in ds or [] if isinstance(d, dict) and str(d.get("status") or "").startswith("mining")}, set())
            await metrics.mining_gain(self.db, {i.get("location"): i.get("items") or {} for i in before},
                                      {i.get("location"): i.get("items") or {} for i in locs}, mining)
        except Exception as e:   # a chart never stops the inventory sync
            log.warning("mining gain failed: %s", e)
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
        msgs = (body or {}).get("messages") or []
        # owner, 2026-10-15: salvage discoveries mustn't raise the Messages badge — they're marked read (the Events
        # feed and the system pages still have them)
        quiet = [message_id(m) for m in msgs if isinstance(m, dict) and message_unread(m) and message_id(m) is not None
                 and salvage_message(m)]
        if quiet:
            try:
                await self.api.request("POST", "/messages/read", json_body={"ids": quiet}, background=True)
                for m in msgs:
                    if message_id(m) in quiet:
                        m["read"] = m["is_read"] = True
            except ApiError as e:
                log.warning("marking salvage messages read failed: %s", e.message)
        await self.db.kv_set("messages", msgs)

    async def on_prospect(self, ev: dict) -> None:
        """An observatory's prospect finished: put the stars it found on the map (merged like census stars) and re-read
        the catalog, which the game regenerates with them."""
        # its stars go on the map now, merged into the catalog we have (no request) …
        try:
            from .census import full_catalogue
            cur = await self.db.kv_get("stars", {}) or {}
            cat = await full_catalogue(self.db, cur)
            for k in ("catalogue", "catalogue_total", "pages_read"):
                if k in (cur.get("sources") or {}):
                    cat["sources"][k] = cur["sources"][k]
            await self.db.kv_set("stars", cat)
        except Exception:
            log.exception("merging prospect stars failed")

        # … and the catalog is re-read once the game has regenerated it (docs: every 5 minutes; reading now would
        # miss them and spend the 1-a-minute catalog allowance)
        async def later() -> None:
            await asyncio.sleep(330)
            try:   # the event is stored already: full_catalogue picks its stars up from the events table
                await self.sync_catalogue()
            except Exception:   # never let this break anything
                log.exception("merging prospect stars failed")
        asyncio.create_task(later(), name="prospect-catalogue")

    async def sync_catalogue(self) -> None:
        try:  # 1/min limit on the catalog; we only ask every 30 minutes.
            from .census import fetch_catalogue, full_catalogue   # + the stars our censuses / observatories found
            await self.db.kv_set("stars", await full_catalogue(self.db, await fetch_catalogue(self.api)))
        except ApiError as e:
            log.info("star catalog failed: %s", e)
        try:
            ach = await self.api.get("/accounts/achievements", background=True)
            await self.db.kv_set("achievements", ach or {})
        except ApiError:
            pass
        await self.sync_overview()

    async def sync_overview(self) -> None:
        """The game's story overview (GET /overview, no auth; docs 2026-10-19): season, storylines, time-limited events,
        opportunities, NPCs. Shown on the Dashboard's Story card."""
        try:
            ov = await self.api.get("/overview", background=True)
            if isinstance(ov, dict):
                await self.db.kv_set("overview", ov)
                from . import megas
                await megas.remember_stations(self.db, ov)
                await self.season_check(ov.get("season"))
        except ApiError as e:
            log.info("overview failed: %s", e)

    async def season_check(self, now: dict | None) -> None:
        """A new season number: hold the engine until you resume (season.py)."""
        from . import season
        if not isinstance(now, dict) or now.get("number") is None:
            return
        prev = await self.db.kv_get(season.SEASON_KV, None)
        if season.changed(prev, now) and not await self.db.kv_get(season.HOLD_KV, None):
            await self.db.kv_set(season.HOLD_KV, {"since": now_iso(), "from": prev, "to": now, "by": "season change"})
            await self.automations.log("season", f"New season: {season.label(prev)} → {season.label(now)}. Automations "
                                                 "are paused until you resume them (Dashboard › Story); the copy from "
                                                 "before the change is kept.", "alert", notify=True)
        if prev != now:
            await self.db.kv_set(season.SEASON_KV, {k: now.get(k) for k in ("number", "title", "in_game_year")})

    async def refresh_system(self, star: str) -> dict:
        data = await self.api.get(f"/locations/{star}")
        await self.db.execute(
            "INSERT OR REPLACE INTO systems(star, data, updated_at) VALUES(?,?,?)",
            (star, json.dumps(data), now_iso()),
        )
        return data
