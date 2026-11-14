"""Terraformer fleets (Joe 2026-11-14): a fleet assigned one planet or moon, far from home, that stays until the world
is habitable for a species. Pure rules; the engine side is tf_engine.py (stage terraformer_pass), the card
partials/terraformer_card.html.

The fleet gathers its whole loadout and an active replicant aboard (it works outside relay coverage, so the replicant
carries the commands; the loadout option *Needs an active replicant aboard* is ticked for it), flies to the planet with
everyone, deploys its terraform devices there and runs them the way the terraforming training was played on the console
(terraform.py):
  • the monitor stays on and its report (terraform.status, every 30 s tick) drives everything
  • each attribute outside its band (the species' range, `margin` inside each edge) gets the devices whose main effect
    moves it the right way, at a strength that grows with the distance to the middle; a device stops once the value,
    projected 6 ticks ahead at its current delta, reaches the middle (no overshoot, no flapping)
  • strong temperature devices (thermal lance, cryo disperser) only for big gaps, and never when their side effect
    pushes an attribute that is already out of range further out; bio seeders only once their preconditions hold
  • nothing pushes an attribute further into an active disaster (the docs' coupling rules)
  • once temperature, pressure and hydrosphere are all in range the atmospheric regulator locks them (their devices
    switch off) while oxygen, toxicity and biosphere keep being steered; it lets go if one of them leaves its range
  • the fleet stays and reports terraforming for as long as it is assigned (Joe 2026-11-14): there is no "complete";
    in range, the regulator and the steering keep the world there until the fleet gets another planet or is recalled
Alerts: a disaster (coupling rule) becoming active and an anomaly in the report, as warnings (the fleet
counters disasters by itself); errors only when Joe must act (a disaster while you steer by hand, no replicant to go).
"""
from __future__ import annotations

import re
from typing import Any

from . import terraform as tf

ROLE = "terraformer"
DEFAULTS: dict[str, Any] = {"planet": "", "species": "humans", "margin_pct": 15.0, "steer": False}   # steer: off by default (Joe: you play it)
# Humans from GET /species (live 2026-10-17), used when the species list hasn't been read
HUMANS = {"temperature": (270, 300), "pressure": (0.8, 1.2), "oxygen": (18, 25), "toxicity": (0, 5),
          "hydrosphere": (40, 70), "biosphere": (30, 70), "tectonic": (5, 30)}
# a starting kit (the loadout of a new terraformer fleet): a cargo vessel carries it all (hold 50) and hosts the replicant
KIT = {"cargo_vessel": 1, "terraform_monitor": 1, "atmospheric_regulator": 1, "orbital_mirror": 2, "orbital_shade": 2,
       "thermal_lance": 1, "cryo_disperser": 1, "atmo_processor": 2, "gas_separator": 2, "filtration_array": 2,
       "aquifer_tap": 2, "bio_seeder": 2}
# the distance from the middle of the band that runs a device at full strength
SCALE = {"temperature": 30.0, "pressure": 0.5, "oxygen": 5.0, "toxicity": 10.0, "hydrosphere": 15.0, "biosphere": 15.0,
         "tectonic": 10.0}
LOOKAHEAD = 6          # ticks: a push ends once the projected value reaches the middle
STRONG_GAP = {"temperature": 25.0}   # thermal lance / cryo disperser only when the value is this far from the middle
HELD = ("temperature", "pressure", "hydrosphere")   # what the atmospheric regulator holds (live 2026-10-17)
STALE_MIN = 10         # a report older than this doesn't steer
BODY_RE = re.compile(r"^[A-Z0-9]+(-[0-9]+){1,2}$")   # a planet (STAR-3) or a moon (STAR-3-1)


def settings(f: dict) -> dict:
    out = {**DEFAULTS, **(f.get("terraform") or {})}
    out["planet"] = str(out.get("planet") or "").strip().upper()
    out["species"] = str(out.get("species") or "humans").strip().lower() or "humans"
    try:
        out["margin_pct"] = max(0.0, float(out["margin_pct"]))
    except (TypeError, ValueError):
        out["margin_pct"] = DEFAULTS["margin_pct"]
    out["margin_pct"] = min(out["margin_pct"], 40.0)
    out["steer"] = bool(out.get("steer"))
    return out


def valid_body(code: str) -> bool:
    return bool(BODY_RE.match(code or ""))


def targets_for(species_key: str, species: list[dict] | None) -> dict[str, tuple]:
    sp = next((s for s in species or [] if str(s.get("species_key") or "").lower() == species_key), None)
    got = tf.species_targets(sp)
    return got or (HUMANS if species_key == "humans" else dict(tf.TARGETS))


def band(attr: str, targets: dict, margin_pct: float) -> tuple[float | None, float | None, float | None]:
    """(low edge, high edge, middle) of the band the fleet aims for: the range with `margin_pct` taken off each bounded
    side; an open side aims a margin past the bound."""
    lo, hi = targets.get(attr, (None, None))
    if lo is None and hi is None:
        return None, None, None
    if lo is not None and hi is not None:
        m = (hi - lo) * margin_pct / 100
        return lo + m, hi - m, (lo + hi) / 2
    pad = SCALE.get(attr, 1.0) * margin_pct / 100
    if lo is not None:
        return lo + pad, None, lo + 2 * pad
    return None, hi - pad, hi - 2 * pad


def _delta(st: dict, attr: str) -> float:
    a = (st.get("attributes") or {}).get(attr)
    try:
        return float(a.get("delta") or 0) if isinstance(a, dict) else 0.0
    except (TypeError, ValueError):
        return 0.0


def wants(st: dict, targets: dict, margin_pct: float, prev: dict | None = None) -> dict[str, dict]:
    """Which way each attribute should be pushed now: {attr: {"dir": +1 | -1, "gap": distance to the middle, "out": the
    value is outside its range}}. An attribute inside its band gets nothing, unless a push already under way (`prev`) has
    not yet brought its projection to the middle."""
    out = {}
    for a in tf.ATTRS:
        lo_b, hi_b, mid = band(a, targets, margin_pct)
        v = tf.value(st, a)
        if mid is None or v is None:
            continue
        ahead = v + _delta(st, a) * LOOKAHEAD
        d = 0
        if lo_b is not None and v < lo_b:
            d = 1
        elif hi_b is not None and v > hi_b:
            d = -1
        p = (prev or {}).get(a)
        if not d and p in (1, -1):
            d = p
        if d == 1 and ahead >= mid or d == -1 and ahead <= mid:
            d = 0   # far enough: the middle is reached within the look-ahead
        if d:
            out[a] = {"dir": d, "gap": abs(mid - v), "out": tf.in_range(a, v, targets) is False}
    return out


def strength(attr: str, gap: float) -> float:
    s = gap / SCALE.get(attr, 1.0)
    return max(0.25, min(1.0, round(s * 4) / 4))


def disasters(st: dict) -> list[dict]:
    """Coupling rules active now that hurt (the docs' disasters; biological scrubbing is a good one)."""
    return [c for c in tf.coupling(st) if c["active"] and "(good)" not in c["effect"]]


def anomaly_text(st: dict) -> str:
    a = (st or {}).get("anomaly")
    if not a:
        return ""
    if isinstance(a, str):
        return a
    if isinstance(a, dict):
        for k in ("description", "message", "name", "type", "title"):
            if a.get(k):
                return str(a[k])
        return ", ".join(f"{k}: {v}" for k, v in a.items())[:200]
    if isinstance(a, list):
        return "; ".join(anomaly_text({"anomaly": x}) for x in a if x)[:300]
    return str(a)


def plan(st: dict, targets: dict, devices: list[dict], cfg: dict, prev: dict | None = None) -> dict:
    """What every terraform device at the planet should do now.
    Returns {"set": {code: {"on": bool, "strength": float | None, "direction": str | None}}, "push": {attr: dir},
    "regulate": bool, "why": {device type: reason}}."""
    w = wants(st, targets, cfg["margin_pct"], prev)
    out_attrs = set(tf.out_of_range(st, targets) or [])
    held_ok = all(a not in out_attrs for a in HELD if a in targets)
    regulate = held_ok and any(d.get("device_type") == "atmospheric_regulator" for d in devices)
    if regulate:
        w = {a: x for a, x in w.items() if a not in HELD}   # locked: their devices rest
    bad = disasters(st)
    worse = {(c["attr"], 1 if c["op"] == ">" else -1) for c in bad}   # pushing these further deepens a disaster
    bio_ok = all(x["ok"] for x in tf.bio_ready(st))
    by_type: dict[str, list[dict]] = {}
    for d in devices:
        by_type.setdefault(d.get("device_type") or "", []).append(d)
    sets: dict[str, dict] = {}
    why: dict[str, str] = {}

    def hurts(t: str) -> str | None:
        for a, s in tf.PUSHES.get(t, [])[1:]:
            if s and a in w and w[a]["dir"] == -s and w[a]["out"]:
                return f"its {a} side effect would make {a} worse"
            if s and (a, s) in worse:
                return f"its {a} side effect feeds a disaster"
        return None

    for t, ds in by_type.items():
        if t == "terraform_monitor":
            for d in ds:
                sets[d["device_code"]] = {"on": True, "strength": None, "direction": None}
            continue
        if t == "atmospheric_regulator":
            for d in ds:
                sets[d["device_code"]] = {"on": regulate, "strength": None, "direction": None}
            why[t] = "holds temperature, pressure and hydrosphere" if regulate else "waits for temperature, pressure and hydrosphere in range"
            continue
        if t not in tf.PUSHES:
            continue
        a, sign = tf.PUSHES[t][0]
        x = w.get(a)
        on, direction, reason = False, None, f"{a} needs nothing"
        if x and (sign == 0 or sign == x["dir"]):
            on, reason = True, f"{a} {'up' if x['dir'] > 0 else 'down'}"
            if sign == 0:
                direction = "increase" if x["dir"] > 0 else "decrease"
            if (a, x["dir"]) in worse:
                on, reason = False, f"{a} is in a disaster that way"
            elif t in ("thermal_lance", "cryo_disperser") and x["gap"] < STRONG_GAP.get(a, 0):
                on, reason = False, f"{a} is close: the gentle devices do it"
            elif t in ("thermal_lance", "cryo_disperser") and hurts(t):
                on, reason = False, hurts(t)
            elif t == "bio_seeder" and not bio_ok:
                on, reason = False, "bio seeder preconditions not met yet"
        elif x and sign != x["dir"]:
            reason = f"{a} has to go the other way"
        why[t] = reason
        s = strength(a, x["gap"]) if on and x else None
        for d in ds:
            sets[d["device_code"]] = {"on": on, "strength": s, "direction": direction}
    return {"set": sets, "push": {a: x["dir"] for a, x in w.items()}, "regulate": regulate, "why": why}


def commands(sets: dict[str, dict], devices: list[dict], recent: dict | None = None) -> list[tuple[str, str, dict | None]]:
    """The orders that bring each device to its planned state: (code, "settings" | "activate" | "deactivate", body).
    Settings go before a switch-on, so a device starts the right way. `recent`: {code: state key} sent lately, not repeated."""
    by = {d["device_code"]: d for d in devices}
    out = []
    for code, want in sorted(sets.items()):
        d = by.get(code)
        if not d:
            continue
        if (recent or {}).get(code) == key(want):
            continue
        on = tf.is_on(d)
        cur = tf.settings_of(d)
        if want["on"]:
            s: dict = {}
            if want.get("strength") is not None:
                try:
                    have = float(cur.get("strength")) if cur.get("strength") is not None else None
                except (TypeError, ValueError):
                    have = None
                if have is None or abs(have - want["strength"]) >= 0.2:
                    s["strength"] = want["strength"]
            if want.get("direction") and cur.get("direction") != want["direction"]:
                s["direction"] = want["direction"]
            if s:
                out.append((code, "settings", {"configuration": {"settings": s}}))
            if not on:
                out.append((code, "activate", {"command": "activate"}))
        elif on:
            out.append((code, "deactivate", {"command": "deactivate"}))
    return out


def key(want: dict) -> str:
    return f"{int(bool(want.get('on')))}|{want.get('strength')}|{want.get('direction')}"


def norm(text: str) -> str:
    """An alert's identity: numbers don't make a new one."""
    return re.sub(r"[-+]?\d+(\.\d+)?", "#", str(text or "").lower()).strip()[:160]
