"""Saved galaxy map markers (Joe 2026-11-20): a shape and a color on a system, kept on the server until removed.

The map resolves the pasted names against its star catalog and sends designations; this module only checks and stores
them. One marker per system: adding one to a marked system replaces it. Stored in kv `map_markers` as
{"markers": [{star, shape, color, note, at}]}."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

KV = "map_markers"
SHAPES = ("circle", "ring", "square", "diamond", "triangle", "star", "cross", "flag", "pin")
DEFAULT_COLOR = "#ffeb3b"
MAX_MARKERS = 2000
STAR_RE = re.compile(r"^[A-Z0-9][A-Z0-9 ._'-]{0,47}$")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def normalize(st: Any) -> list[dict]:
    rows = (st or {}).get("markers") if isinstance(st, dict) else None
    return [m for m in (rows or []) if isinstance(m, dict) and m.get("star")]


def clean(m: Any) -> dict | None:
    """One marker to add, checked: None when its star isn't a designation."""
    if not isinstance(m, dict):
        return None
    star = str(m.get("star") or "").strip().upper()
    if not STAR_RE.match(star):
        return None
    shape = m.get("shape") if m.get("shape") in SHAPES else SHAPES[0]
    color = m.get("color") if isinstance(m.get("color"), str) and COLOR_RE.match(m["color"]) else DEFAULT_COLOR
    return {"star": star, "shape": shape, "color": color.lower(), "note": str(m.get("note") or "").strip()[:120],
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def apply(markers: list[dict], body: dict) -> tuple[list[dict], str]:
    """body: {"add": [marker]}, {"remove": [star]} or {"clear": true}. Answers the new list and what changed."""
    if body.get("clear"):
        return [], f"removed all {len(markers)} markers"
    gone = {str(s).strip().upper() for s in body.get("remove") or []}
    if gone:
        left = [m for m in markers if m["star"] not in gone]
        return left, f"removed {len(markers) - len(left)} marker(s)"
    add = [c for c in (clean(m) for m in body.get("add") or []) if c]
    if not add:
        return markers, "nothing to add"
    new = {m["star"]: m for m in add}
    replaced = sum(1 for m in markers if m["star"] in new)
    out = ([m for m in markers if m["star"] not in new] + list(new.values()))[-MAX_MARKERS:]
    return out, f"added {len(new) - replaced}, changed {replaced}" if replaced else f"added {len(new)} marker(s)"
