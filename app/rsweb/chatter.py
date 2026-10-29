"""NPC chatter (Activity › Chatter): your NPC replicants post canned BobNet lines when game events happen, answer
each other in short conversations, and answer what other NPCs (Bill, Riker …) say.

The library (kv "chatter") holds approved messages, imported in bulk with a per-message approval (web_chatter.py):
- event lines: {id, by, on, channel, text}; `on` is a trigger (TRIGGERS), `by` a replicant code or "any" (whoever's
  device the event is about);
- replies: {id, by, after | to (+ match), channel, text}; `after` names the line or reply it answers (a conversation:
  posting that line queues one of its replies), `to` a poster's name on BobNet ("*" anyone not yours) and `match` an
  optional regex on their message. A reply's `by` may be "any": one of your NPCs other than the one it answers.
`channel` is "general" or "bots" (the two channels in the settings), "same" (a reply: where the post it answers was),
or a literal "#channel".

Only replicants marked NPC on their profile post (the game's is_npc flag: readers can hide NPC chatter). Posting is off
until switched on. Each channel has one limiter, `limits` (hours, for all NPCs together; blank = never; default 24 for
the general and bots channels): how often one of the NPCs may start a post there, an event line or an answer to
another NPC (a conversation's replies don't count). Each trigger fires with a chance. A refused reply that any NPC may give is tried
by another NPC (up to MAX_TRIES); an event line stays with the NPC that took part.
A daily cap bounds it all.
Replies wait a random delay in a queue (kv "chatter:state") that the engine stage chatter_pass sends."""
from __future__ import annotations

import hashlib
import json
import logging
import random
import re
from datetime import datetime, timezone
from .db import now_iso

log = logging.getLogger("rsweb.chatter")
KV = "chatter"
STATE_KV = "chatter:state"
TEXT_MAX = 500
TRIGGERS = {
    "survey_done": "a survey finished (AMI survey of a system, or a belt or system scan)",
    "belt_mined": "the last open site of a belt was mined out",
    "site_depleted": "a mining site was mined out (the belt has more)",
    "arrived": "the replicant's own vessel arrived",
    "salvage_found": "salvage was discovered",
    "contract_done": "a contract or civilization event was completed",
}
DEFAULTS = {"enabled": False, "general": "#general", "bots": "#bots", "chance": 0.5, "reply_chance": 0.85,
            "npc_reply_chance": 0.5, "daily_cap": 20, "delay_min": 1, "delay_max": 6,
            "max_depth": 4}
UNSAFE = re.compile(r"[\"'&<>]")
# placeholders a message may use: filled from the post it answers or the event it is about; a message whose
# placeholders can't all be filled isn't picked
SLOTS = {"poster": "who posted (answers)", "from": "the system the post names, such as the one Bill left",
         "star": "the system the poster or the event is in",
         "arrival": "who a post says just arrived (Riker: Jim-1 just arrived back in SOL)",
         "pilot": "who a post is about: its first word, or the name after picking up / start from (Patch: creme-1707 hits full reverse)",
         "score": "the number after Score: in a post", "region": "the catalog region of {from}, else of {star}"}
SLOT_RE = re.compile(r"\{(\w+)\}")
ARRIVAL_RE = re.compile(r"([\w-]+) (?:just |has )?(?:arrived|returned)\b", re.I)
PILOT_RE = re.compile(r"(?:picking up|start from|go for|over to) ([\w-]+)", re.I)
NOT_NAMES = {"i", "im", "quiet", "the", "a", "an", "heads", "and", "it", "this", "that", "we", "they", "he", "she"}
SCORE_RE = re.compile(r"\bscore:?\s*(\d+)", re.I)


def pilot_of(text: str) -> str | None:
    """Who an announcer's post is about (Patch at the exploding moon: "creme-1707 hits full reverse", "Quiet start
    from creme-1707", "I am picking up creme-1707 on sensors")."""
    m = PILOT_RE.search(text or "")
    if m:
        return m.group(1)
    first = re.match(r"\s*([\w-]+)", text or "")
    w = first.group(1) if first else ""
    return w if w and re.sub(r"\W", "", w).lower() not in NOT_NAMES else None
POSTED_KEEP = 200
MAX_TRIES = 3   # NPCs that try a refused reply before it's dropped


def normalize(c: dict | None) -> dict:
    c = dict(c or {})
    if not isinstance(c.get("limits"), dict):   # before 1.301.2: a timer per NPC (every_hours, general_hours, bots_hours)
        every = c.get("every_hours", 24)
        c["limits"] = {k: c.get(f"{k}_hours") if c.get(f"{k}_hours") not in (None, "") else every
                       for k in ("general", "bots")}
    for k in ("every_hours", "general_hours", "bots_hours"):
        c.pop(k, None)
    for k, v in DEFAULTS.items():
        c.setdefault(k, v)
    c["lines"] = [x for x in c.get("lines") or [] if isinstance(x, dict)]
    c["replies"] = [x for x in c.get("replies") or [] if isinstance(x, dict)]
    return c


def norm_state(s: dict | None) -> dict:
    s = dict(s or {})
    s["queue"] = list(s.get("queue") or [])
    s["posted"] = list(s.get("posted") or [])
    s["last"] = dict(s.get("last") or {})       # replicant code -> when it last posted
    s["answered"] = dict(s.get("answered") or {})
    s["no_relay"] = dict(s.get("no_relay") or {})   # system -> when the game said it has no active relay
    s["chan"] = dict(s.get("chan") or {})           # channel -> when an NPC last started a post there
    return s


def msg_id(m: dict) -> str:
    key = json.dumps([m.get("by"), m.get("on"), m.get("after"), m.get("to"), m.get("text")], sort_keys=True)
    return hashlib.sha1(key.encode()).hexdigest()[:8]


def kind(m: dict) -> str:
    return "reply" if (m.get("after") or m.get("to")) else "line"


def channel_of(m: dict, cfg: dict, same: str | None = None) -> str:
    ch = str(m.get("channel") or ("same" if kind(m) == "reply" else "bots")).strip()
    if ch == "same":
        return same or cfg["bots"]
    if ch in ("general", "bots"):
        return cfg[ch]
    return ch if ch.startswith("#") else "#" + ch


def _resolve(who: str, reps: dict[str, dict]) -> str | None:
    if who == "any":
        return "any"
    if who in reps:
        return who
    for c, r in reps.items():
        if str(r.get("name") or "").lower() == who.lower():
            return c
    return None


def parse_import(text: str, reps: dict[str, dict], cfg: dict) -> tuple[list[dict], list[str]]:
    """Bulk import: {"lines": [...], "replies": [...]} or {"messages": [...]} or a list. Each row has the cleaned
    message, its errors (it can't be imported) and warnings (it can, but may not post as hoped)."""
    try:
        data = json.loads(text or "")
    except ValueError as e:
        return [], [f"not valid JSON: {e}"]
    items: list = []
    if isinstance(data, dict):
        for k in ("lines", "replies", "messages"):
            items += data.get(k) or []
    elif isinstance(data, list):
        items = data
    items = [x for x in items if isinstance(x, dict)]
    if not items:
        return [], ['expected {"lines": [...], "replies": [...]} with at least one message']
    have = {m["id"] for m in cfg["lines"] + cfg["replies"]}
    known = set(have) | {str(x.get("id")) for x in items if x.get("id")}
    dup = {(m.get("by"), m.get("on"), m.get("after"), m.get("to"), m.get("text")) for m in cfg["lines"] + cfg["replies"]}
    rows = []
    for x in items:
        m = {k: (str(v).strip() if not isinstance(v, (int, float)) else v) for k, v in x.items() if v not in (None, "")}
        row = {"msg": m, "errors": [], "warnings": [], "kind": kind(m), "who": ""}
        by = _resolve(str(m.get("by") or ""), reps)
        if not m.get("by"):
            row["errors"].append("no by (replicant code, name or any)")
        elif not by:
            row["errors"].append(f"{m['by']} is not one of your replicants")
        else:
            m["by"] = by
            r = reps.get(by) or {}
            row["who"] = "any NPC" if by == "any" else (r.get("name") or by)
            if by != "any" and not r.get("is_npc"):
                row["warnings"].append("not marked NPC: it will not post until it is")
        if row["kind"] == "line":
            if m.get("on") not in TRIGGERS:
                row["errors"].append(f"on must be one of {', '.join(TRIGGERS)}" + (f" (not {m['on']})" if m.get("on") else ""))
        else:
            if m.get("on"):
                row["errors"].append("a reply has after or to, not on")
            if m.get("after") and str(m["after"]) not in known:
                row["errors"].append(f"after {m['after']}: no message has that id")
            if m.get("match"):
                try:
                    re.compile(str(m["match"]))
                except re.error as e:
                    row["errors"].append(f"match is not a valid pattern: {e}")
        t = str(m.get("text") or "")
        if not t:
            row["errors"].append("no text")
        elif len(t) > TEXT_MAX:
            row["errors"].append(f"text is {len(t)} characters (at most {TEXT_MAX})")
        elif UNSAFE.search(t):
            row["warnings"].append("has quotes, ampersands or angle brackets, which BobNet may show oddly")
        odd = slots(t) - set(SLOTS)
        if odd:
            row["errors"].append(f"unknown placeholder {', '.join('{' + x + '}' for x in sorted(odd))} (use {', '.join('{' + x + '}' for x in SLOTS)})")
        if "poster" in slots(t) and row["kind"] == "line":
            row["warnings"].append("{poster} is only filled in replies")
        if (m.get("by"), m.get("on"), m.get("after"), m.get("to"), m.get("text")) in dup:
            row["errors"].append("already in the library")
        m.setdefault("id", msg_id(m))
        if m["id"] in have:
            row["errors"].append(f"id {m['id']} is already in the library")
        try:
            channel_of(m, cfg)
        except Exception:
            row["errors"].append("bad channel")
        rows.append(row)
    return rows, []


def add_rows(cfg: dict, rows: list[dict], approved: set[int], replace: bool = False) -> int:
    """Put the approved rows without errors in the library; returns how many."""
    if replace:
        cfg["lines"], cfg["replies"] = [], []
    n = 0
    for i, row in enumerate(rows):
        if i not in approved or row["errors"]:
            continue
        cfg["lines" if row["kind"] == "line" else "replies"].append(row["msg"])
        n += 1
    return n


def _ts(v: str | None) -> float:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _now() -> float:
    return datetime.now(timezone.utc).timestamp()


def posted_today(state: dict, now: float | None = None) -> int:
    now = now or _now()
    return sum(1 for p in state["posted"] if p.get("ok") and now - _ts(p.get("at")) < 86400)


def npcs(reps: dict[str, dict]) -> list[str]:
    return [c for c, r in reps.items() if r.get("is_npc")]


def trigger_of(ev: dict, devices: list[dict], reps: dict[str, dict], exhausted: set[str] | None = None) -> tuple[str, str] | None:
    """(trigger, speaker code) for a game event, or None. The speaker is the replicant that took part: the one riding
    the vessel the event is about, else the owner of the device."""
    from .sites import belt_of
    name = ev.get("event") or ""
    p = ev.get("payload") or {}
    code = ev.get("device_code")
    dev = next((d for d in devices if d.get("device_code") == code), None) if code else None
    hosted = {r.get("hosted_device_code"): c for c, r in reps.items() if r.get("hosted_device_code")}
    who = hosted.get(code) or (dev or {}).get("replicant_code") or ev.get("replicant_code")
    trig = None
    if name == "directive.completed" and "survey" in (ev.get("device_type") or "") \
            and (p.get("directive") or "survey_system") == "survey_system":
        trig = "survey_done"
    elif name == "scan.completed":
        target = str(p.get("scan_target") or ev.get("location") or "")
        if str(p.get("scan_type") or "") in ("system", "belt") or "-BELT-" in target:
            trig = "survey_done"
    elif name == "site.depleted":
        belt = belt_of(p.get("site") or ev.get("location"))
        trig = "belt_mined" if belt and belt in (exhausted or set()) else "site_depleted"
    elif name == "travel.arrived":
        if code in hosted:
            trig = "arrived"
    elif name == "salvage.discovered":
        trig = "salvage_found"
    elif name == "event.completed":
        trig = "contract_done"
    if not trig or not who or who not in reps:
        return None
    return trig, who


def slots(text: str) -> set[str]:
    return set(SLOT_RE.findall(text or ""))


def fill(text: str, values: dict) -> str:
    return SLOT_RE.sub(lambda m: str(values.get(m.group(1)) or m.group(0)), text or "")


def fillable(options: list[dict], values: dict | None) -> list[dict]:
    values = values or {}
    return [o for o in options if all(values.get(k) for k in slots(o.get("text")))]


def hours_for(cfg: dict, channel: str) -> float | None:
    """How often one of the NPCs may start a post on a channel (Joe 2026-10-29: one limiter per channel, for all NPCs
    together), in hours; None (blank) means never. limits is keyed "general" and "bots" for the two named channels,
    else by the channel itself."""
    key = "general" if channel == cfg.get("general") else "bots" if channel == cfg.get("bots") else channel
    v = (cfg.get("limits") or {}).get(key)
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def ready(state: dict, channel: str, cfg: dict, now: float | None = None) -> bool:
    """An NPC may start a post on `channel` now."""
    h = hours_for(cfg, channel)
    return h is not None and (now or _now()) - _ts(state["chan"].get(channel)) >= h * 3600


def post_values(payload: dict, stars: dict[str, dict]) -> dict:
    """Placeholder values from a BobNet post: the poster, the system they're in, and the first other known system the
    post names (Bill: "NOSOXA is in the rear-view mirror")."""
    star = str(payload.get("current_star") or "")
    text = str(payload.get("message") or "")
    named = [w for w in re.findall(r"[A-Z][A-Z0-9]{2,}", text) if w in stars and w != star]
    frm = named[0] if named else None
    region = (stars.get(frm or star) or {}).get("region")
    arr = ARRIVAL_RE.search(text)
    return {"poster": payload.get("replicant_name"), "star": star or None, "from": frm, "region": region,
            "arrival": arr.group(1) if arr else None, "pilot": pilot_of(text),
            "score": (SCORE_RE.search(text) or [None, None])[1]}


def pick(options: list[dict], state: dict, rnd: random.Random | None = None) -> dict | None:
    """A random option among those never posted, else among the least recently posted third: a poster answered
    every day cycles through the whole set before anything repeats."""
    rnd = rnd or random
    if not options:
        return None
    last = {}
    for i, p in enumerate(state["posted"]):
        last[p.get("id")] = i
    fresh = [o for o in options if o["id"] not in last]
    if fresh:
        return rnd.choice(fresh)
    old = sorted(options, key=lambda o: last[o["id"]])
    return rnd.choice(old[:max(1, len(old) // 3)])


def replies_to(post_id: str, cfg: dict) -> list[dict]:
    return [r for r in cfg["replies"] if str(r.get("after") or "") == post_id]


def npc_replies(name: str, text: str, cfg: dict) -> list[dict]:
    """Replies that answer a post by someone not yours: `to` the poster's name (or "*"), `match` on the text."""
    out = []
    for r in cfg["replies"]:
        to = str(r.get("to") or "")
        if not to or r.get("after"):
            continue
        if to != "*" and to.lower() != (name or "").lower():
            continue
        if r.get("match"):
            try:
                if not re.search(str(r["match"]), text or "", re.I):
                    continue
            except re.error:
                continue
        out.append(r)
    # a reply that matches the text beats a catch-all for the poster
    specific = [r for r in out if r.get("match")]
    return specific or out


RELAY_TYPES = ("ftl_relay", "system_hub")
NO_RELAY_HOURS = 6   # a system the game said has no active relay is skipped this long (unless we have one there)


def rep_star(r: dict) -> str:
    return str(r.get("current_star") or str(r.get("current_location") or r.get("location") or "").split("-")[0])


def relay_stars(devices: list[dict]) -> set[str]:
    """Systems with an active relay or hub of ours: a replicant posts on BobNet only from a system with an active
    relay ("No active relay in this system", live 2026-10-29)."""
    from .wardhub import active
    return {str(d.get("location") or "").split("-")[0] for d in devices
            if d.get("device_type") in RELAY_TYPES and active(d)}


def poster_check(reps: dict[str, dict], relays: set[str], state: dict, now: float | None = None):
    """can(code) -> (may post, has our relay). Unknown systems (someone else's relay may be there) may post until the
    game refuses one; then that system waits NO_RELAY_HOURS."""
    now = now or _now()
    def can(code: str) -> tuple[bool, bool]:
        star = rep_star(reps.get(code) or {})
        if not star:
            return False, False
        if star in relays:
            return True, True
        return now - _ts(state["no_relay"].get(star)) >= NO_RELAY_HOURS * 3600, False
    return can


def speaker_for(m: dict, other_than: str | None, reps: dict[str, dict], rnd: random.Random | None = None,
                can=None) -> str | None:
    """Who says `m`: its own NPC, or for "any" an NPC that can post, preferring one with our relay in its system."""
    rnd = rnd or random
    can = can or (lambda c: (True, True))
    if m.get("by") != "any":
        by = m.get("by")
        return by if by in reps and reps[by].get("is_npc") and by != other_than and can(by)[0] else None
    pool = [c for c in npcs(reps) if c != other_than and can(c)[0]]
    best = [c for c in pool if can(c)[1]]
    return rnd.choice(best or pool) if pool else None


def queue_item(m: dict, by: str, channel: str, cfg: dict, depth: int, why: str, rnd: random.Random | None = None,
               now: float | None = None, values: dict | None = None) -> dict:
    rnd = rnd or random
    lo, hi = float(cfg["delay_min"]), max(float(cfg["delay_max"]), float(cfg["delay_min"]))
    due = (now or _now()) + rnd.uniform(lo, hi) * 60
    return {"due": datetime.fromtimestamp(due, timezone.utc).isoformat(timespec="seconds"), "id": m["id"], "by": by,
            "channel": channel, "text": fill(m["text"], values or {}), "depth": depth, "why": why,
            "values": {k: v for k, v in (values or {}).items() if v}}


class ChatterMixin:
    async def chatter_cfg(self) -> dict:
        return normalize(await self.db.kv_get(KV, {}))

    async def chatter_can(self, reps: dict, state: dict):
        return poster_check(reps, relay_stars(await self.devices()), state)

    async def chatter_on_event(self, ev: dict) -> None:
        """A game event or BobNet post: queue a line or a reply when the library has one and the dice say so."""
        cfg = await self.chatter_cfg()
        if not cfg["enabled"] or ev.get("virtual"):
            return
        reps = await self.db.kv_get("replicants", {}) or {}
        state = norm_state(await self.db.kv_get(STATE_KV, {}))
        name = ev.get("event") or ""
        p = ev.get("payload") or {}
        item = None
        if name == "bobnet.new":
            poster = p.get("replicant_code")
            if poster in reps:   # our own posts: conversations are chained when a line is sent, not from the feed
                return
            pname = str(p.get("replicant_name") or "")
            cat = await self.db.kv_get("stars", {}) or {}
            stars = {x.get("designation"): x for x in cat.get("stars") or [] if isinstance(x, dict)}
            values = post_values(p, stars)
            opts = [m for m in fillable(npc_replies(pname, str(p.get("message") or ""), cfg), values)
                    if ready(state, channel_of(m, cfg, p.get("channel")), cfg)]
            if not opts or random.random() >= float(cfg["npc_reply_chance"]):
                return
            m = pick(opts, state)
            by = speaker_for(m, None, reps, can=await self.chatter_can(reps, state))
            if not by:
                return
            chan = channel_of(m, cfg, p.get("channel"))
            state["chan"][chan] = now_iso()
            item = queue_item(m, by, chan, cfg, 1, f"answers {pname}", values=values)
            item["alt"] = m.get("by") == "any"   # refused: another NPC may say it instead
        else:
            from . import sites
            exhausted = None
            if name == "site.depleted":
                exhausted = set(sites.exhausted_belts(await self.devices(), await self.exhausted_places()))
            hit = trigger_of(ev, await self.devices(), reps, exhausted)
            if not hit:
                return
            trig, who = hit
            if not reps[who].get("is_npc") or not (await self.chatter_can(reps, state))(who)[0]:
                return   # not an NPC, or no relay where it is: the line would be refused
            star = str(ev.get("star") or ev.get("location") or "").split("-")[0] or None
            cat = await self.db.kv_get("stars", {}) or {}
            region = next((x.get("region") for x in cat.get("stars") or [] if isinstance(x, dict) and x.get("designation") == star), None)
            values = {"star": star, "region": region}
            opts = [m for m in fillable([m for m in cfg["lines"] if m.get("on") == trig and m.get("by") in (who, "any")], values)
                    if ready(state, channel_of(m, cfg), cfg)]
            if not opts or random.random() >= float(cfg["chance"]):
                return
            m = pick(opts, state)
            item = queue_item(m, who, channel_of(m, cfg), cfg, 0, trig, values=values)
            item["due"] = now_iso()   # an event line goes out on the next tick; only replies wait
            state["chan"][item["channel"]] = now_iso()
        state["queue"].append(item)
        await self.db.kv_set(STATE_KV, state)

    async def chatter_send(self, item: dict, cfg: dict, state: dict) -> dict:
        """Post one queued message; on success queue one of the replies that answer it."""
        rec = {"at": now_iso(), "id": item["id"], "by": item["by"], "channel": item["channel"], "text": item["text"],
               "why": item.get("why")}
        try:
            await self.api.post(f"/replicants/{item['by']}/message", {"channel": item["channel"], "text": item["text"]},
                                background=True)
            rec["ok"] = True
        except Exception as e:
            rec["ok"], rec["error"] = False, str(getattr(e, "message", None) or e)[:200]
            await self.log("chatter", f"{item['by']} could not post on {item['channel']}: {rec['error']}", "warning")
            # a reply anyone may give: another NPC tries on the next tick (an event line stays with the one who
            # took part)
            reps = await self.db.kv_get("replicants", {}) or {}
            if "relay" in rec["error"].lower():
                star = rep_star(reps.get(item["by"]) or {})
                if star:
                    state["no_relay"][star] = now_iso()
            tried = [*(item.get("tried") or []), item["by"]]
            if item.get("alt") and len(tried) < MAX_TRIES:
                can = await self.chatter_can(reps, state)
                pool = [c for c in npcs(reps) if c not in tried and c != item.get("other_than") and can(c)[0]]
                pool = [c for c in pool if can(c)[1]] or pool
                if pool:
                    nxt = random.choice(pool)
                    state["queue"].append({**item, "by": nxt, "tried": tried, "due": now_iso()})
                    rec["retry"] = nxt
        state["posted"] = (state["posted"] + [rec])[-POSTED_KEEP:]
        if rec["ok"] and item.get("depth", 0) < int(cfg["max_depth"]):
            reps = await self.db.kv_get("replicants", {}) or {}
            values = {**(item.get("values") or {}), "poster": (reps.get(item["by"]) or {}).get("name") or item["by"]}
            opts = fillable(replies_to(item["id"], cfg), values)
            if opts and random.random() < float(cfg["reply_chance"]):
                m = pick(opts, state)
                by = speaker_for(m, item["by"], reps, can=await self.chatter_can(reps, state))
                if by:
                    nxt = queue_item(m, by, channel_of(m, cfg, item["channel"]), cfg, item.get("depth", 0) + 1,
                                     f"answers {item['by']}", values=values)
                    nxt.update(alt=m.get("by") == "any", other_than=item["by"])
                    state["queue"].append(nxt)
        return rec

    async def chatter_pass(self) -> list[str]:
        """Send the queued messages that are due, within the daily cap (one per replicant per tick)."""
        cfg = await self.chatter_cfg()
        state = norm_state(await self.db.kv_get(STATE_KV, {}))
        if not state["queue"]:
            return []
        if not cfg["enabled"]:
            return []   # kept until it's switched on again, or cleared on the Chatter page
        reps = await self.db.kv_get("replicants", {}) or {}
        now, out, busy, keep = _now(), [], set(), []
        items, state["queue"] = state["queue"], []   # answers queued while sending wait for a later tick
        for item in items:
            if not (reps.get(item["by"]) or {}).get("is_npc"):
                continue   # only NPCs post (the flag may have been taken off since it was queued)
            if _ts(item.get("due")) > now or item["by"] in busy:
                keep.append(item)
                continue
            if now - _ts(item.get("due")) > 6 * 3600:
                continue   # stale: the moment has passed
            if posted_today(state, now) >= int(cfg["daily_cap"]):
                continue   # over the daily cap: dropped, not saved for later
            busy.add(item["by"])
            rec = await self.chatter_send(item, cfg, state)
            if rec.get("ok"):
                out.append(f"{item['by']} on {item['channel']}: {item['text'][:60]}")
        state["queue"] = keep + state["queue"]
        await self.db.kv_set(STATE_KV, state)
        return out


def post_now(m: dict, by: str, cfg: dict) -> dict:
    """A queue item due now (the Chatter page's Post now)."""
    item = queue_item(m, by, channel_of(m, cfg), cfg, 0, "by hand")
    item.update(due=now_iso(), alt=m.get("by") == "any")
    return item


def describe(m: dict, reps: dict[str, dict]) -> str:
    if m.get("on"):
        return TRIGGERS.get(m["on"], m["on"])
    if m.get("after"):
        return f"answers {m['after']}"
    to = "anyone else" if m.get("to") == "*" else m.get("to")
    return f"answers {to}" + (f" when they say /{m['match']}/" if m.get("match") else "")

