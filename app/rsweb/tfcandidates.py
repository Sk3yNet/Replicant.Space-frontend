"""Map › Terraforming candidates: surveyed planets and moons rated as terraforming targets for a species.

A body's details come from GET /locations/<body> (kept in kv loc:<BODY>): surface_temp_k, atmo_pressure_atm,
atmo_o2_pct, atmo_toxicity, hydrosphere_pct, biosphere_index, tectonic_index, surface_gravity, life_stage. A missing
value counts as 0 (no atmosphere: no pressure, oxygen or toxicity), except temperature and gravity, which aren't judged. Each out-of-range attribute is closed by a kit of
`kit` devices of each kind it takes, at full strength, stacking (1 + ln n) (docs); side effects and couplings are left
out, so the time is an estimate. Attributes are worked at once; bio seeders start once BIO_PRE holds.
Dropped: gravity outside the range (no device changes it), gas giants (no surface), intelligent / spacefaring natives."""
from __future__ import annotations

import math
from typing import Any

from . import terraform as tf

FIELDS = {"temperature": "surface_temp_k", "pressure": "atmo_pressure_atm", "oxygen": "atmo_o2_pct",
          "toxicity": "atmo_toxicity", "hydrosphere": "hydrosphere_pct", "biosphere": "biosphere_index",
          "tectonic": "tectonic_index", "gravity": "surface_gravity"}
ATMO = ("pressure", "oxygen", "toxicity")
# main effect per tick at full strength (docs' quick reference, terraform.EFFECTS): {(attr, +1/-1): {device: rate}}
RATES = {("temperature", 1): {"orbital_mirror": 0.15, "thermal_lance": 0.4},
         ("temperature", -1): {"orbital_shade": 0.15, "cryo_disperser": 0.4},
         ("pressure", 1): {"atmo_processor": 0.08}, ("pressure", -1): {"atmo_processor": 0.08},
         ("oxygen", 1): {"gas_separator": 0.5}, ("oxygen", -1): {"gas_separator": 0.5},
         ("toxicity", -1): {"filtration_array": 0.8},
         ("hydrosphere", 1): {"aquifer_tap": 0.6}, ("hydrosphere", -1): {"aquifer_tap": 0.6},
         ("biosphere", 1): {"bio_seeder": 0.3}, ("tectonic", 1): {"thermal_lance": 0.1}}
NO_SURFACE = ("gas", "giant")
CIV_STAGES = ("intelligent", "spacefaring")
DEFAULT_KIT = 3
READS_KV = "tf_body_reads"   # {body: iso}: when the candidates page last read a body's details
READ_EVERY_S = 7 * 86400
READ_CAP = 25   # bodies read per click


def body_of(detail: dict | None) -> dict:
    d = detail or {}
    b = d.get("planet") or d.get("moon")
    return b if isinstance(b, dict) else {}


def _f(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def values(body: dict) -> dict[str, float]:
    """{attr: value} for a body, missing values as 0 (temperature from °C when there's no K)."""
    out = {}
    for a, k in FIELDS.items():
        v = _f(body.get(k))
        if v is None and a == "temperature":
            c = _f(body.get("surface_temp_c"))
            v = c + 273.15 if c is not None else None
        if v is None and a in ("temperature", "gravity"):
            continue   # unknown temperature or gravity: not judged (shown as —)
        out[a] = 0.0 if v is None else v
    return out


def gap(attr: str, v: float, rng: tuple) -> float:
    """Signed distance to the range: + below it, − above it, 0 inside."""
    lo, hi = rng
    if lo is not None and v < lo:
        return lo - v
    if hi is not None and v > hi:
        return hi - v
    return 0.0


def rate(attr: str, sign: int, kit: int) -> tuple[float, list[str]]:
    """(per-tick rate, device kinds) for closing `attr` in direction `sign` with `kit` devices of each kind."""
    kinds = RATES.get((attr, sign)) or {}
    return sum(kinds.values()) * (1 + math.log(max(kit, 1))), list(kinds)


def ticks(attr: str, g: float, kit: int) -> int | None:
    """Ticks to close a gap (0 inside the range, None when no device moves it that way)."""
    if not g:
        return 0
    r, _ = rate(attr, 1 if g > 0 else -1, kit)
    return math.ceil(abs(g) / r) if r else None


def rate_body(body: dict, targets: dict, kit: int = DEFAULT_KIT) -> dict:
    """{values, attrs: {attr: {value, gap, ticks, devices}}, devices, ticks, seconds, blocked, dropped}.
    blocked: attributes no device can bring into range; dropped: why the body isn't a candidate at all."""
    vals = values(body)
    kind = f"{body.get('type') or ''} {body.get('category') or ''}".lower()
    stage = str(body.get("life_stage") or "").lower()
    dropped = None
    g_rng = targets.get("gravity")
    if any(w in kind for w in NO_SURFACE):
        dropped = "no surface (gas giant)"
    elif g_rng and "gravity" in vals and gap("gravity", vals["gravity"], g_rng):
        dropped = f"gravity {vals['gravity']:g} g (needs {g_rng[0]}–{g_rng[1]}: no device changes it)"
    elif stage in CIV_STAGES:
        dropped = f"{stage} natives"
    attrs, blocked, devices = {}, [], []
    for a in tf.ATTRS:
        if a not in targets or a not in vals:
            continue
        g = gap(a, vals[a], targets[a])
        t = ticks(a, g, kit)
        kinds = rate(a, 1 if g > 0 else -1, kit)[1] if g else []
        attrs[a] = {"value": vals[a], "gap": g, "ticks": t, "devices": kinds}
        if t is None:
            blocked.append(a)
        devices += [k for k in kinds if k not in devices]
    # bio seeders only work once the preconditions hold: biosphere starts after those are met
    pre = 0
    for a, lo, hi in tf.BIO_PRE:
        if a in vals:
            t = ticks(a, gap(a, vals[a], (lo, hi)), kit)
            pre = max(pre, t or 0)
    total = 0
    for a, x in attrs.items():
        if x["ticks"] is None:
            continue
        total = max(total, x["ticks"] + (pre if a == "biosphere" and x["ticks"] else 0))
    return {"values": vals, "attrs": attrs, "devices": devices, "ticks": total, "seconds": total * tf.TICK_S,
            "blocked": blocked, "dropped": dropped, "life_stage": stage if stage not in ("", "none") else None}


def bodies_to_read(scans: dict[str, dict], surveyed: dict, ami_surveyed: dict) -> list[str]:
    """Planets and moons worth reading: every body of a system an AMI survey finished, else the bodies marked
    surveyed (and their moons when the planet is marked). Moons are STAR-n-m, 1..moon_count."""
    out: list[str] = []
    for star, scan in (scans or {}).items():
        full = star in (ami_surveyed or {})
        for p in (scan or {}).get("planets") or []:
            des = (p or {}).get("designation")
            if not des:
                continue
            moons = [m.get("designation") for m in p.get("moons") or [] if isinstance(m, dict) and m.get("designation")]
            if not moons:
                try:
                    moons = [f"{des}-{i}" for i in range(1, int(p.get("moon_count") or 0) + 1)]
                except (TypeError, ValueError):
                    moons = []
            if full or des in surveyed:
                out.append(des)
                out += [m for m in moons if full or m in surveyed or des in surveyed]
            else:
                out += [m for m in moons if m in surveyed]
    return list(dict.fromkeys(out))


def ly(a: Any, b: Any) -> float | None:
    from .pathing import xyz
    p, q = xyz(a), xyz(b)
    return math.dist(p, q) if p and q else None
