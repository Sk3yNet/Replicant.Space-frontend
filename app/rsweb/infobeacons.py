"""Information beacons (game v4.1.0, docs /docs/information-beacons/): a cheap device that broadcasts a description of up
to 2,000 characters to anyone who system-scans its star. Bought from Bill's Skunkworks for an FTL beacon; decommission
one at an autofactory to learn the blueprint.

- Description and comments on/off: PATCH /devices/{code} {"configuration": {"settings": {"description", "comments"}}}.
- Broadcast: POST /devices/{code} {"command": "activate"} once deployed (answer: device_code, activated, description).
- Comments: anyone with a replicant at the beacon's location sends {"command": "comment", "message"} on the beacon's
  code (no need to own it); 20 per beacon, 2,000 characters each. GET /devices/{code} lists them under `comments`
  ({index, replicant_name, comment, created_at}); the owner deletes one with DELETE /devices/{code}/comments/{index}.
  A comment sends the owner `device.commented` (commenter's replicant name, the text and the beacon's location).

The app keeps the last device answer of every beacon it read in kv `info_beacons` ({code: {device, read_at}}): the device
list doesn't carry the settings or the comments, and other players' beacons are readable only from their star.
Unverified live: the scan's field for broadcasts (the docs say "the info section"), and the device.commented payload's
field names; both are read loosely.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

TYPE = "information_beacon"
KV = "info_beacons"
MAX_TEXT = 2000
MAX_COMMENTS = 20
SCAN_KEYS = ("info", "information", "info_beacons", "beacons", "broadcasts")


def is_beacon(dev: dict | None) -> bool:
    return bool(dev) and (str(dev.get("device_type") or "") == TYPE or "comment" in (dev.get("available_commands") or []))


def settings(dev: dict | None) -> dict:
    s = (dev or {}).get("settings")
    return s if isinstance(s, dict) else {}


def description(dev: dict | None) -> str:
    return str(settings(dev).get("description") or (dev or {}).get("description") or "")


def comments_on(dev: dict | None) -> bool:
    """Comments are open unless the owner switched them off (settings.comments false)."""
    return settings(dev).get("comments") is not False


def active(dev: dict | None) -> bool:
    s = settings(dev)
    if "active" in s:
        return bool(s["active"])
    st = str((dev or {}).get("status") or "")
    return st not in ("", "idle", "inactive", "stowed", "compacted", "undeployed", "deployed", "offline")


def comments(dev: dict | None) -> list[dict]:
    out = []
    for i, c in enumerate((dev or {}).get("comments") or []):
        if isinstance(c, dict):
            out.append({"index": c.get("index", i), "name": c.get("replicant_name") or c.get("replicant_code") or "?",
                        "text": str(c.get("comment") or c.get("message") or ""), "at": c.get("created_at")})
        elif isinstance(c, str):
            out.append({"index": i, "name": "?", "text": c, "at": None})
    return out


def settings_body(desc: str | None = None, allow: bool | None = None) -> dict:
    """The PATCH body; raises ValueError when the description is over the game's limit."""
    s: dict[str, Any] = {}
    if desc is not None:
        desc = desc.replace("\r\n", "\n")
        if len(desc) > MAX_TEXT:
            raise ValueError(f"The description is {len(desc)} characters; the game takes at most {MAX_TEXT}.")
        s["description"] = desc
    if allow is not None:
        s["comments"] = bool(allow)
    return {"configuration": {"settings": s}}


def comment_body(message: str) -> dict:
    message = (message or "").replace("\r\n", "\n").strip()
    if not message:
        raise ValueError("Write a comment first.")
    if len(message) > MAX_TEXT:
        raise ValueError(f"The comment is {len(message)} characters; the game takes at most {MAX_TEXT}.")
    return {"command": "comment", "message": message}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def remember(store: dict | None, dev: dict | None) -> dict:
    """Keep a beacon's latest device answer. Returns the store."""
    store = dict(store or {})
    if is_beacon(dev) and dev.get("device_code"):
        store[str(dev["device_code"])] = {"device": dev, "read_at": now()}
    return store


def _entry(x: Any) -> dict | None:
    if isinstance(x, str):
        return {"text": x} if x.strip() else None
    if not isinstance(x, dict):
        return None
    text = x.get("description") or x.get("message") or x.get("text") or x.get("info") or x.get("content")
    if not text:
        return None
    return {"text": str(text), "code": x.get("device_code") or x.get("code"), "location": x.get("location"),
            "type": x.get("device_type") or x.get("type"),
            "owner": x.get("owner_name") or x.get("replicant_name") or x.get("owner_replicant_code") or x.get("owner")}


def scan_info(scan: dict | None) -> list[dict]:
    """Broadcasts in a system scan's info section, whatever shape it takes (a list of texts or of records, or one
    mapping of them). Hub welcome messages may show here too."""
    out: list[dict] = []
    for k in SCAN_KEYS:
        v = (scan or {}).get(k)
        if isinstance(v, dict) and not _entry(v):
            items = []
            for c, i in v.items():   # {code: record}, or {"beacons": [...], "welcome": "..."}
                if isinstance(i, list):
                    items += i
                else:
                    items.append({**i, "device_code": i.get("device_code") or c} if isinstance(i, dict) else i)
        elif isinstance(v, (list, tuple)):
            items = list(v)
        else:
            items = [v] if v else []
        for i in items:
            e = _entry(i)
            if e:
                out.append(e)
    return out


def rows(devices: list[dict], store: dict | None, others: dict | None = None) -> tuple[list[dict], list[dict]]:
    """(yours, others'): your beacons from the device list with the last read details; other players' beacons that a
    device scan saw (others.py `info_beacons`) or that you read."""
    store = store or {}
    mine_codes = set()
    mine = []
    for d in devices or []:
        if str(d.get("device_type") or "") != TYPE:
            continue
        code = str(d.get("device_code"))
        mine_codes.add(code)
        full = {**d, **((store.get(code) or {}).get("device") or {})}
        mine.append({"code": code, "dev": d, "location": d.get("location"), "status": d.get("status"),
                     "description": description(full), "comments": comments(full), "comments_on": comments_on(full),
                     "read_at": (store.get(code) or {}).get("read_at"),
                     "deployed": bool(d.get("location")) and not d.get("stowed_in_device_code")
                     and not d.get("attached_to_device_code") and d.get("status") not in ("stowed", "compacted")})
    theirs: dict[str, dict] = {}
    for code, x in (others or {}).items():
        if code not in mine_codes:
            theirs[code] = {"code": code, "location": x.get("location"), "owner": x.get("owner_name") or x.get("owner"),
                            "seen_at": x.get("seen_at"), "description": "", "comments": [], "read_at": None}
    for code, x in store.items():
        if code in mine_codes:
            continue
        dv = x.get("device") or {}
        r = theirs.setdefault(code, {"code": code, "location": dv.get("location"), "owner": None, "seen_at": None})
        r.update({"description": description(dv), "comments": comments(dv), "read_at": x.get("read_at"),
                  "location": r.get("location") or dv.get("location"), "comments_on": comments_on(dv)})
    mine.sort(key=lambda r: (r["location"] or "", r["code"]))
    return mine, sorted(theirs.values(), key=lambda r: (r.get("location") or "", r["code"]))


def commented_line(p: dict | None) -> str:
    """The notification text for device.commented."""
    p = p or {}
    who = p.get("replicant_name") or p.get("commenter_name") or p.get("commenter") or "Someone"
    text = str(p.get("comment") or p.get("message") or "")
    where = p.get("location") or p.get("beacon_location")
    code = p.get("device_code") or p.get("beacon_code")
    short = text if len(text) <= 120 else text[:117] + "…"
    return f"{who} commented on beacon {code or ''}".replace("  ", " ") + (f" at {where}" if where else "") \
        + (f": “{short}”" if short else "")
