"""Autofactory print queues: what's printing now and what's waiting.

The game's `print_queue` on GET /devices/{code} lists only the *waiting* items, e.g.
    [{"device_type": "ftl_relay", "notify": {"device": null}}, ...]
The item being printed is not in it, so "now printing" comes from the device status
(`printing (<type>)` / `waiting_for_resources`) plus the latest `print.started` event, which
carries `completes_at`. Heaven vessels have no queue (one print at a time).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def has_queue(dev: dict) -> bool:
    cmds = dev.get("available_commands") or []
    return "print_queue" in dev or "enqueue_print" in cmds or "autofactory" in (dev.get("device_type") or "")


def items(dev: dict) -> list[dict]:
    """Normalise the waiting items. `index` is the 1-based position shown to the player; `api_index` is the 0-based
    position `dequeue_print` expects (confirmed live: sending 1 removed the second item)."""
    raw = dev.get("print_queue")
    if raw is None:
        raw = dev.get("queue")
    out = []
    for i, it in enumerate(raw or [], 1):
        if isinstance(it, str):
            it = {"device_type": it}
        if not isinstance(it, dict):
            continue
        notify = it.get("notify") or {}
        out.append({"index": i, "api_index": i - 1, "device_type": it.get("device_type") or "?",
                    "quantity": it.get("quantity"),
                    "notify": notify.get("device") if isinstance(notify, dict) else notify,
                    "tags": it.get("tags") or [], "oncomplete": it.get("oncomplete"),
                    "extra": {k: v for k, v in it.items() if k not in ("device_type", "notify", "quantity", "tags", "oncomplete")}})
    return out


def _ts(v: Any) -> datetime | None:
    if not v:
        return None
    try:
        t = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


async def current(db, dev: dict) -> dict | None:
    """What the printer is doing now, or None when it's idle."""
    status = str(dev.get("status") or "")
    pr = dev.get("printing")
    if isinstance(pr, dict) and pr.get("device_type"):
        state = "waiting" if status.startswith("waiting_for_resources") else "printing"
        return {"state": state, "device_type": pr.get("device_type"), "started_at": pr.get("started_at"),
                "completes_at": pr.get("completes_at"), "tags": pr.get("tags") or [], "progress": pr.get("progress_percent")}
    row = await db.fetchone("SELECT event, payload, created_at FROM events WHERE device_code=? "
                            "AND event IN ('print.started', 'print.completed', 'print.cancelled') ORDER BY seq DESC LIMIT 1",
                            (dev.get("device_code"),))
    started = None
    if row and row["event"] == "print.started":
        p = json.loads(row["payload"] or "{}")
        started = {"device_type": p.get("device_type"), "started_at": row["created_at"],
                   "completes_at": p.get("completes_at"), "print_mode": p.get("print_mode"), "tags": p.get("tags") or []}
    if status.startswith("printing"):
        dtype = status[status.find("(") + 1:status.rfind(")")] if "(" in status else None
        cur = started if started and (not dtype or started["device_type"] == dtype) else {"device_type": dtype}
        end = _ts(cur.get("completes_at"))
        if end and end < datetime.now(timezone.utc):
            cur = {**cur, "overdue": True}
        return {"state": "printing", **cur}
    if status.startswith("waiting_for_resources"):
        return {"state": "waiting", **(started or {})}
    if started and not status:  # no live status (fetch failed) — trust the event
        end = _ts(started.get("completes_at"))
        if end and end > datetime.now(timezone.utc):
            return {"state": "printing", **started}
    return None


def remaining_seconds(cur: dict | None, queue: list[dict], bps: dict[str, dict]) -> tuple[float | None, bool]:
    """(seconds until the whole queue is done, exact?) — print times from the blueprint list."""
    total, exact = 0.0, True
    if cur:
        end = _ts(cur.get("completes_at"))
        if end:
            total += max(0.0, (end - datetime.now(timezone.utc)).total_seconds())
        else:
            exact = False
            total += float((bps.get(cur.get("device_type") or "") or {}).get("print_time") or 0)
    for it in queue:
        bp = bps.get(it["device_type"])
        if not bp or not bp.get("print_time"):
            exact = False
            continue
        total += float(bp["print_time"]) * int(it.get("quantity") or 1)
    return (total if (cur or queue) else None), exact


def summary(queue: list[dict]) -> str:
    counts: dict[str, int] = {}
    for it in queue:
        counts[it["device_type"]] = counts.get(it["device_type"], 0) + int(it.get("quantity") or 1)
    return ", ".join(f"{n}× {t.replace('_', ' ')}" for t, n in counts.items())


# --- spreading prints over several autofactories --------------------------------------------------------------
def load_seconds(dev: dict, bps: dict[str, dict]) -> float:
    """Rough seconds of printing already on an autofactory: the current print plus its waiting queue, from the
    device list alone (no events), so planners can call it for every factory."""
    pr = dev.get("printing")
    status = str(dev.get("status") or "")
    cur = None
    if isinstance(pr, dict) and pr.get("device_type"):
        cur = pr
    elif status.startswith("printing"):
        cur = {"device_type": status[status.find("(") + 1:status.rfind(")")] if "(" in status else None}
    secs, _ = remaining_seconds(cur, items(dev), bps)
    return secs or 0.0


def least_loaded(factories: list[dict], bps: dict[str, dict]) -> dict | None:
    """The autofactory that would start a new print soonest."""
    return min(factories, key=lambda f: (load_seconds(f, bps), len(items(f)), f.get("device_code") or ""), default=None)


def split(factories: list[dict], device_type: str, n: int, bps: dict[str, dict], load: dict[str, float],
          room: dict[str, int] | None = None) -> list[tuple[dict, int]]:
    """Spread `n` prints of one type over `factories` evenly by print time: each one goes to the factory that would
    finish it first. `load` (code -> seconds already queued) is updated, so successive calls balance across types;
    `room` (code -> free queue slots), if given, caps each factory and is updated too."""
    t = float((bps.get(device_type) or {}).get("print_time") or 0) or 1.0
    got: dict[str, int] = {}
    for _ in range(n):
        open_f = [f for f in factories if room is None or room.get(f["device_code"], 1) > 0]
        if not open_f:
            break
        f = min(open_f, key=lambda f: (load.get(f["device_code"], 0.0) + t, f["device_code"]))
        code = f["device_code"]
        load[code] = load.get(code, 0.0) + t
        got[code] = got.get(code, 0) + 1
        if room is not None:
            room[code] = room.get(code, 1) - 1
    return [(f, got[f["device_code"]]) for f in factories if got.get(f["device_code"])]
