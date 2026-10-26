"""Repeating console commands: send one request every N minutes until stopped (e.g. a belt scan for debris or devices).

Each repeat watches for finds in its system (device.discovered, salvage.discovered): it counts them, and stops at the
first one only when asked to. The finds raise their own notifications either way (notify.NOTIFY_DONE)."""
from datetime import datetime, timedelta, timezone

KV = "console_repeats"
FOUND_EVENTS = ("device.discovered", "salvage.discovered")
MAX_ERRORS = 3          # this many failed sends in a row stop the repeat (the device moved, was lost, ...)
DEFAULT_MINUTES = 10


def _ts(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def due(r: dict, now: datetime | None = None) -> bool:
    if not r.get("enabled", True):
        return False
    last = _ts(r.get("last_run"))
    now = now or datetime.now(timezone.utc)
    return last is None or now - last >= timedelta(minutes=max(1, int(r.get("every_minutes") or DEFAULT_MINUTES)))


def subject(path: str) -> tuple[str, str] | None:
    """('replicant' | 'device', code) the request is about, from its path."""
    parts = [p for p in path.split("?")[0].strip("/").split("/") if p]
    if len(parts) >= 2 and parts[0] in ("replicants", "devices"):
        return parts[0][:-1], parts[1].upper()
    return None


def star_for(path: str, devices: list[dict], replicants: dict) -> str:
    """The system the request works in now (where its replicant or device is), '' if unknown."""
    sub = subject(path)
    if not sub:
        return ""
    kind, code = sub
    if kind == "replicant":
        loc = (replicants.get(code) or {}).get("location")
    else:
        loc = next((d.get("location") for d in devices if d.get("device_code") == code), None)
    return str(loc or "").split("-")[0]


def find_text(ev: dict) -> str:
    p = ev.get("payload") or {}
    if ev.get("event") == "device.discovered":
        t = str(p.get("device_type") or "device").replace("_", " ")
        return f"{t} {p.get('device_code') or ''}".strip()
    return f"salvage {p.get('name') or p.get('designation') or ''}".strip()
