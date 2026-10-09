"""Normalize loosely-documented API shapes.

The docs show resource amounts as objects ({"structural": 120}), but the live API
returns lists in some places. Everything is converted to {name: float} before use.
"""
from __future__ import annotations

from typing import Any

NAME_KEYS = ("resource", "resource_type", "name", "type", "item", "item_type", "key", "device_type")
QTY_KEYS = ("quantity", "qty", "amount", "count", "value", "total", "units")


def _num(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def as_amounts(x: Any) -> dict[str, float]:
    """{"a": 1} | [{"resource": "a", "quantity": 1}] | [["a", 1]] | [{"a": 1}] -> {"a": 1.0}."""
    out: dict[str, float] = {}
    if isinstance(x, dict):
        # A single entry shaped like {"resource": "a", "quantity": 1}
        name = next((x[k] for k in NAME_KEYS if isinstance(x.get(k), str)), None)
        qty = next((_num(x[k]) for k in QTY_KEYS if _num(x.get(k)) is not None), None)
        if name is not None and qty is not None:
            return {name: qty}
        for k, v in x.items():
            n = _num(v)
            if n is not None:
                out[str(k)] = out.get(str(k), 0.0) + n
        return out
    if isinstance(x, (list, tuple)):
        for item in x:
            if isinstance(item, (list, tuple)) and len(item) == 2 and _num(item[1]) is not None:
                out[str(item[0])] = out.get(str(item[0]), 0.0) + float(item[1])
            elif isinstance(item, dict):
                for k, v in as_amounts(item).items():
                    out[k] = out.get(k, 0.0) + v
    return out


def normalize_inventory(locs: Any) -> list[dict]:
    """Inventory list with `items` always a {resource: qty} dict (original kept in `items_raw`)."""
    out = []
    for loc in locs or []:
        if not isinstance(loc, dict):
            continue
        raw = loc.get("items")
        out.append({**loc, "items": as_amounts(raw), "items_raw": raw})
    return out


def normalize_blueprints(bps: Any) -> list[dict]:
    out = []
    for b in bps or []:
        if not isinstance(b, dict):
            continue
        out.append({**b, "resources": as_amounts(b.get("resources")), "resources_raw": b.get("resources"),
                    "device_type": str(b.get("device_type") or b.get("name") or "?"),
                    "print_time": _num(b.get("print_time")) or 0})
    return out
