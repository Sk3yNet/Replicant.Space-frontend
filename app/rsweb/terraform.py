"""Terraforming (docs: /docs/api/locations/terraforming …): what the monitors report, the devices that change a world, and
the rocks you can drop on it. Pure helpers; the page is web_terraform.py.

A terraform monitor at a body emits `terraform.status` every tick: {body, attributes: {attr: {value, delta}},
projections: {attr: {"+6", "+12", "+24"}}, effects: {cascade, devices, equilibrium}, asteroids, anomaly, warnings}.
Devices take `configuration.settings` {strength 0–1, direction} through PATCH /devices/{code}. A vector charge at a
Kuiper object (found with detect_object) launches it at a planet or moon: {"command": "detonate", target, destination}.
"""
from __future__ import annotations

import json
from typing import Any

ATTRS = ["temperature", "pressure", "oxygen", "toxicity", "hydrosphere", "tectonic", "biosphere"]
UNITS = {"temperature": "K", "pressure": "atm", "oxygen": "%", "hydrosphere": "%"}
DEVICES = ["terraform_monitor", "orbital_mirror", "thermal_lance", "orbital_shade", "cryo_disperser", "atmo_processor",
           "gas_separator", "filtration_array", "aquifer_tap", "bio_seeder", "atmospheric_regulator"]
ROCK_TOOLS = ["vector_charge", "propulsor", "trajectory_deflector", "sensor_array"]
# which settings a device takes (docs: configuration; dropping rocks)
DIRECTIONS = {"atmo_processor": ["increase", "decrease"], "gas_separator": ["increase", "decrease"],
              "aquifer_tap": ["increase", "decrease"], "propulsor": ["diversion", "increase", "decrease"],
              "trajectory_deflector": ["steepen", "shallow"]}
EFFECTS = {   # docs' quick reference (per tick at full strength)
    "terraform_monitor": "tracking only (5 running at most per account)", "orbital_mirror": "temp +0.15",
    "thermal_lance": "temp +0.4; tectonic +0.1, pressure +0.02", "orbital_shade": "temp −0.15",
    "cryo_disperser": "temp −0.4; pressure +0.05", "atmo_processor": "pressure +0.08; temp +0.03 (reversible)",
    "gas_separator": "oxygen +0.5; toxicity −0.1 (reversible)", "filtration_array": "toxicity −0.8; oxygen +0.05, pressure −0.01",
    "aquifer_tap": "hydrosphere +0.6; pressure +0.02 (reversible)", "bio_seeder": "biosphere +0.3; oxygen +0.1",
    "atmospheric_regulator": "stabilization", "vector_charge": "launches a Kuiper object (single use)",
    "propulsor": "diversion / speed (increase, decrease) of a rock", "trajectory_deflector": "approach angle (steepen, shallow)",
    "sensor_array": "detect_object: finds Kuiper objects (24 h)"}
MONITOR_CAP = 5
# habitable ranges (docs' order of work): (low, high), None = open
TARGETS = {"temperature": (200, 350), "pressure": (0.3, 10), "oxygen": (10, 35), "toxicity": (None, 20),
           "hydrosphere": (10, None)}


TICK_S = 30   # a terraform tick (live 2026-10-17: terraform.status every 30 s in the training)


def range_eta(attr: str, value: Any, delta: Any) -> dict | None:
    """Where an attribute is heading at its current delta: {"enter": ticks} until it's habitable, {"leave": ticks}
    until it drifts out, {"ok": True} when it stays in, {"away": True} when it moves away from the range (or stands
    still outside it); None without a range or a value."""
    lo, hi = TARGETS.get(attr, (None, None))
    try:
        v, dv = float(value), float(delta or 0)
    except (TypeError, ValueError):
        return None
    if lo is None and hi is None:
        return None
    if in_range(attr, v):
        if dv > 0 and hi is not None:
            return {"leave": int((hi - v) / dv) + 1}
        if dv < 0 and lo is not None:
            return {"leave": int((v - lo) / -dv) + 1}
        return {"ok": True}
    if lo is not None and v < lo:
        return {"enter": int((lo - v) / dv) + 1} if dv > 0 else {"away": True}
    return {"enter": int((v - hi) / -dv) + 1} if dv < 0 else {"away": True}


def out_of_range(status: dict) -> list[str] | None:
    """The habitable-range attributes a report has outside their range ([] = ready to lock in; None = no report)."""
    attrs = (status or {}).get("attributes") or {}
    if not attrs:
        return None
    return [a for a in TARGETS if in_range(a, (attrs.get(a) or {}).get("value")) is False]


def in_range(attr: str, value: Any) -> bool | None:
    """Whether a value is inside the habitable range (None: no range, or no value)."""
    lo, hi = TARGETS.get(attr, (None, None))
    if (lo is None and hi is None) or value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    return (lo is None or v >= lo) and (hi is None or v <= hi)
# coupling rules (docs: disasters): (name, attribute, ">" or "<", threshold, effect, causal)
COUPLING = [("Greenhouse runaway", "pressure", ">", 10, "temp +1.0", True),
            ("Evaporation", "temperature", ">", 373, "hydro −1.5, pressure +0.05", False),
            ("Freezing", "temperature", "<", 200, "hydro −0.8, pressure −0.03", False),
            ("CO₂ condensation", "temperature", "<", 150, "pressure −0.1", False),
            ("Volcanic outgassing", "tectonic", ">", 60, "toxicity +1.5, temp +0.3", True),
            ("Geological violence", "tectonic", ">", 80, "biosphere −3.0", False),
            ("Toxic suppression", "toxicity", ">", 30, "biosphere −0.8", False),
            ("Hyperoxic fire", "oxygen", ">", 35, "biosphere −1.2", False),
            ("Ocean dampening", "hydrosphere", ">", 90, "pressure +0.03, tectonic −0.5", False),
            ("Biological scrubbing", "biosphere", ">", 50, "oxygen +0.5, toxicity −0.8 (good)", False)]
# bio seeders only work when all hold (docs: strategy, step 5)
BIO_PRE = [("temperature", 200, 350), ("pressure", 0.3, None), ("oxygen", 10, None), ("toxicity", None, 20),
           ("hydrosphere", 10, None)]


def kind(d: dict) -> str | None:
    t = d.get("device_type") or ""
    return t if t in DEVICES or t in ROCK_TOOLS else None


def value(st: dict, attr: str) -> float | None:
    a = (st.get("attributes") or {}).get(attr)
    try:
        return float(a.get("value") if isinstance(a, dict) else a)
    except (TypeError, ValueError):
        return None


def coupling(st: dict, near: float = 0.1) -> list[dict]:
    """Each coupling rule: active (threshold crossed) or near (within `near` of it, heading that way)."""
    out = []
    for name, attr, op, thr, eff, causal in COUPLING:
        v = value(st, attr)
        if v is None:
            continue
        a = (st.get("attributes") or {}).get(attr) or {}
        delta = a.get("delta") if isinstance(a, dict) else None
        on = v > thr if op == ">" else v < thr
        gap = abs(v - thr) / (abs(thr) or 1)
        toward = delta is None or (delta > 0 if op == ">" else delta < 0)
        if on or (gap <= near and toward):
            out.append({"rule": name, "attr": attr, "op": op, "threshold": thr, "effect": eff, "causal": causal,
                        "active": on, "value": v})
    return out


def bio_ready(st: dict) -> list[dict]:
    """Bio seeder preconditions, each met or not."""
    out = []
    for attr, lo, hi in BIO_PRE:
        v = value(st, attr)
        ok = v is not None and (lo is None or v > lo) and (hi is None or v < hi)
        want = f"{lo}–{hi}" if lo is not None and hi is not None else f"> {lo}" if lo is not None else f"< {hi}"
        out.append({"attr": attr, "value": v, "want": want, "ok": ok})
    return out


def latest_by_body(rows: list[dict]) -> dict[str, dict]:
    """{body: latest terraform.status payload (+ at)} from event rows (newest first)."""
    out: dict[str, dict] = {}
    for r in rows:
        try:
            p = json.loads(r["payload"] or "{}") or {}
        except ValueError:
            continue
        body = p.get("body") or r.get("location")
        if body and body not in out:
            out[body] = {**p, "at": r.get("created_at")}
    return out


def settings_of(d: dict) -> dict:
    s = ((d.get("configuration") or {}).get("settings") or {}) if isinstance(d.get("configuration"), dict) else {}
    return {"strength": s.get("strength", 1.0), "direction": s.get("direction")}


def clamp_strength(v: Any) -> float | None:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return None
