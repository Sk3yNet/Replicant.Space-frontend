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
import json
import re
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


# --- automatic reads and deploy locations (1.559.0, Joe 2026-11-25) ---------------------------------------------------
AUTO_READ_MAX = 8      # other players' beacons read per device scan (each is one GET)
REREAD_HOURS = 24      # a beacon read this long ago is read again by the next scan of its system
TARGETS_KV = "info_beacon_targets"   # {code: {dest, activate, set_at, sent: {step: iso}, tries: {step: n}, stuck}}
RESEND_MIN = 10        # a step sent this long ago without the device list showing it done is sent again
MAX_TRIES = 3


def _age_h(iso: str | None, now: datetime | None = None) -> float | None:
    try:
        t = datetime.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - t).total_seconds() / 3600


def due_reads(seen: dict | None, store: dict | None, star: str, now: datetime | None = None,
              hours: float = REREAD_HOURS, limit: int = AUTO_READ_MAX) -> list[str]:
    """Other players' beacons in `star` (others.py `info_beacons`) never read, or read over `hours` ago: a replicant
    of yours is in the system right after a device scan, so they can be read (only works from their star)."""
    out = []
    for code, x in sorted((seen or {}).items()):
        if str((x or {}).get("location") or "").split("-")[0] != star:
            continue
        age = _age_h(((store or {}).get(code) or {}).get("read_at"), now)
        if age is None or age >= hours:
            out.append(code)
    return out[:limit]


def at_dest(d: dict, dest: str) -> bool:
    """On the ground at `dest` (a spot, or anywhere in a star's system) and not waiting on a carrier."""
    loc = str(d.get("location") or "").upper()
    dest = (dest or "").upper()
    if not loc or d.get("stowed_in_device_code") or d.get("attached_to_device_code"):
        return False
    if any(str(t).startswith(("to:", "at:")) or t == "lift" for t in d.get("tags") or []):
        return False   # logistics still has it to move (the tags go on arrival)
    return loc == dest if "-" in dest else loc.split("-")[0] == dest


def target_step(d: dict | None, t: dict) -> str:
    """What a beacon with a deploy location needs next: gone, wait, deploy, describe, activate or done."""
    if not d:
        return "gone"
    if not at_dest(d, t.get("dest") or ""):
        return "wait"
    cmds = d.get("available_commands") or []
    if "deploy" in cmds:
        return "deploy"
    if t.get("text") and not t.get("described") and description(d) != t["text"]:
        return "describe"   # the bulk import's text goes on before the broadcast starts
    if t.get("activate") and "activate" in cmds:
        return "activate"
    return "done"


# --- bulk import (Joe 2026-11-26: place a whole set of texts at once, e.g. a 36-log story) -----------------------------
BULK_MAX = 200


def parse_bulk(raw: str) -> tuple[list[dict], bool]:
    """The import's JSON: a list of {dest, text[, title, code, activate]}, or {"activate": bool, "beacons": [...]}.
    `dest` may be spelled destination/location, `text` description, `code` device_code. Returns (entries, activate
    default); raises ValueError with a readable reason."""
    try:
        data = json.loads(raw or "")
    except ValueError as e:
        raise ValueError(f"That isn't valid JSON ({e}).") from None
    act = True
    if isinstance(data, dict):
        act = data.get("activate") is not False
        data = data.get("beacons") or data.get("entries") or data.get("logs")
    if not isinstance(data, list) or not data:
        raise ValueError('Expected a list of {"dest": "STAR-OORT", "text": "..."} entries (or {"beacons": [...]}).')
    if len(data) > BULK_MAX:
        raise ValueError(f"{len(data)} entries; at most {BULK_MAX} at once.")
    out, errs = [], []
    for i, e in enumerate(data, 1):
        if not isinstance(e, dict):
            errs.append(f"entry {i} isn't an object")
            continue
        dest = str(e.get("dest") or e.get("destination") or e.get("location") or "").strip().upper()
        text = str(e.get("text") if e.get("text") is not None else e.get("description") or "").replace("\r\n", "\n").strip()
        title = str(e.get("title") or (text.split("\n", 1)[0] if text else "") or f"entry {i}")[:80]
        if not dest or not re.fullmatch(r"[A-Z0-9][A-Z0-9\-_.]*", dest):
            errs.append(f"entry {i} ({title}): no star or location code")
        if not text:
            errs.append(f"entry {i} ({title}): no text")
        elif len(text) > MAX_TEXT:
            errs.append(f"entry {i} ({title}): text is {len(text)} characters, the game takes {MAX_TEXT}")
        code = str(e.get("code") or e.get("device_code") or "").strip().upper() or None
        out.append({"n": i, "dest": dest, "text": text, "title": title, "code": code,
                    "activate": act if e.get("activate") is None else bool(e.get("activate"))})
    if errs:
        raise ValueError("; ".join(errs[:10]) + (f" (and {len(errs) - 10} more)" if len(errs) > 10 else "") + ".")
    return out, act


def free_beacon(d: dict, store: dict | None, targets: dict | None) -> bool:
    """Yours to hand a new text: no deploy location set, not broadcasting, no description yet."""
    code = d.get("device_code")
    if d.get("device_type") != TYPE or code in (targets or {}):
        return False
    known = ((store or {}).get(code) or {}).get("device") or {}
    if description(d) or description(known):
        return False
    cmds = d.get("available_commands") or []
    return "deactivate" not in cmds and not (settings(d).get("active") or settings(known).get("active"))


def assign(entries: list[dict], devices: list[dict], store: dict | None, targets: dict | None,
           dist=None, far=None, prints: dict | None = None) -> list[dict]:
    """Pick a beacon for each entry: the one it names, else the nearest free one (`dist(star_a, star_b)` in ly; the
    lowest code when distances are unknown). An entry whose destination and text already have a beacon on the way, or a
    print ordered (`prints`), is kept as it is, so importing the same file again only fills what's missing. A free beacon
    `far(star_a, star_b)` from the destination (beyond the sector range: logistics never carries it there) doesn't
    count; an entry no free beacon covers gets status "print" (Joe 2026-11-28: printed near its destination)."""
    targets = targets or {}
    prints = prints or {}
    mine = {d.get("device_code"): d for d in devices if d.get("device_type") == TYPE}
    free = sorted(c for c, d in mine.items() if free_beacon(d, store, targets))
    taken: set = set()
    rows = []
    for e in entries:
        row = {**e, "status": "new", "why": ""}
        have = next((c for c, t in targets.items() if t.get("dest") == e["dest"] and t.get("text") == e["text"]), None)
        printing = next((p for p in prints.values() if p.get("dest") == e["dest"] and p.get("text") == e["text"]), None)
        if have:
            row.update(code=have, status="kept", why="already set")
        elif printing:
            row.update(status="kept", why=print_line(printing))
        elif e.get("code"):
            if e["code"] not in mine:
                row.update(status="none", why=f"{e['code']} isn't one of your information beacons")
            elif e["code"] in taken:
                row.update(status="none", why=f"{e['code']} is named twice")
            else:
                row["why"] = "named in the file" + (" (replaces its deploy location)" if e["code"] in targets else "")
        else:
            star = e["dest"].split("-")[0]
            pool = [c for c in free if c not in taken]
            near = [c for c in pool if not (far and far(str(mine[c].get("location") or "").split("-")[0], star))]
            if not near:
                row.update(status="print", why="no free information beacon" + (" within the sector range" if pool else "")
                           + f": one is printed near {star}")
            else:
                pool = near

                def key(c):
                    here = str(mine[c].get("location") or "").split("-")[0]
                    try:
                        ly = float(dist(here, star)) if dist and here else float("inf")
                    except (TypeError, ValueError):
                        ly = float("inf")
                    return (ly, c)
                row["code"] = min(pool, key=key)
                ly = key(row["code"])[0]
                at = mine[row["code"]].get("location") or "?"
                row["why"] = f"from {at}" + (f", {ly:,.0f} ly" if ly != float("inf") else "")
        if row.get("code") and row["status"] not in ("none", "print"):
            taken.add(row["code"])
        rows.append(row)
    return rows


# --- prints for the bulk import (Joe 2026-11-28: entries no spare covers are printed near their destination) -----------
PRINTS_KV = "info_beacon_prints"   # {id: {dest, text, title, activate, at, factory?, sourced?, tries?, why?, stuck?}}
PRINT_TAG = "ibp:"                 # on the print, so the printed beacon is known by it (then it gets the entry's text)
PRINT_TRIES = 3
PRINT_LOST_H = 12                  # a sourced print neither queued nor printed this long is ordered again


def new_print(e: dict, prints: dict) -> str:
    """Record a print order for bulk entry `e`; returns its id."""
    import secrets
    pid = secrets.token_hex(4)
    while pid in prints:
        pid = secrets.token_hex(4)
    prints[pid] = {"dest": e["dest"], "text": e["text"], "title": e.get("title") or "", "activate": bool(e.get("activate")),
                   "at": now()}
    return pid


def print_tags(pid: str, dest: str) -> list[str]:
    """The print's tags: logistics carries the new beacon to `dest` (to:/at:, as a hand delivery), and the id finds it."""
    from .loadouts import at_tag, to_tag
    star = dest.split("-")[0]
    return [to_tag(star)] + ([at_tag(dest)] if "-" in dest else []) + [PRINT_TAG + pid]


def printed(devices: list[dict], pid: str) -> dict | None:
    """The beacon printed for order `pid`, once it's in the device list."""
    tag = PRINT_TAG + pid
    return next((d for d in devices if d.get("device_type") == TYPE and tag in (d.get("tags") or [])), None)


def queued(devices: list[dict], vq_orders: list[dict] | None, pid: str) -> bool:
    """The print for `pid` waits in a factory's queue, is printing, or waits in the app's virtual queue."""
    tag = PRINT_TAG + pid
    if any(tag in ((o.get("body") or {}).get("tags") or []) for o in vq_orders or []):
        return True
    return any(tag in json.dumps([d.get("print_queue"), d.get("printing"), d.get("current_print")], default=str)
               for d in devices if d.get("print_queue") or d.get("printing") or d.get("current_print"))


def print_line(p: dict) -> str:
    """Where a print order stands, in words."""
    if p.get("stuck"):
        return f"print stuck: {p['stuck']}"
    if p.get("factory"):
        return f"printing on {p['factory']} for {p.get('dest')}"
    return f"print for {p.get('dest')} waits: {p['why']}" if p.get("why") else f"print for {p.get('dest')} ordered"
