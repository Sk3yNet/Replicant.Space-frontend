"""Autobuy (Joe 2026-11-17): device types on the Missing blueprints page marked to buy when Riker's shop lists them.

kv "autobuy" {types: {device_type: {at, by, key?, approved_at?, bought_at?}}, read_at, read_error}. The engine pass
(Engine.autobuy_pass) reads Riker's listing at most hourly while something is marked (his listings change daily), and
approves one unit of a listing whose rewards hold a marked type, the same approval as the Shop page: logistics pays and
fetches it. A type is skipped while you own one (decommission it to unlock the blueprint) or once one was bought; the mark
drops off by itself when the blueprint unlocks. Nothing is bought from any other shop."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

KV = "autobuy"
SHOP_OWNER = "riker"
READ_EVERY_MIN = 60


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize(s: Any) -> dict:
    s = dict(s) if isinstance(s, dict) else {}
    s["types"] = {t: dict(v) for t, v in (s.get("types") or {}).items() if isinstance(v, dict)}
    return s


def is_rikers(t: dict) -> bool:
    return str(t.get("owner_name") or "").strip().lower() == SHOP_OWNER


def rikers_shops(traders: dict[str, dict]) -> dict[str, dict]:
    return {c: t for c, t in (traders or {}).items() if isinstance(t, dict) and is_rikers(t) and not c.startswith("post:")}


def listed(shops: dict[str, dict]) -> dict[str, list[tuple[str, dict]]]:
    """Device type -> [(controller:trade_code, trade)] for every listing with stock left."""
    out: dict[str, list[tuple[str, dict]]] = {}
    for code, t in shops.items():
        for tr in t.get("trades") or []:
            if not isinstance(tr, dict) or not tr.get("trade_code"):
                continue
            stock = tr.get("current_stock")
            if stock is not None and int(stock or 0) <= 0:
                continue
            for dt in ((tr.get("rewards") or {}).get("devices") or {}):
                out.setdefault(dt, []).append((f"{code}:{tr['trade_code']}", tr))
    return out


def plan(s: dict, shops: dict[str, dict], approvals: dict, owned: set[str], unlocked: set[str]) -> dict:
    """What to do now: {"drop": [types unlocked], "bought": [(type, key)], "approve": [(type, key, trade)]}.
    Changes nothing."""
    trades, done = approvals.get("trades") or {}, approvals.get("done") or {}
    offers = listed(shops)
    live = {k for v in offers.values() for k, _ in v}
    out: dict[str, list] = {"drop": [], "bought": [], "approve": []}
    for t, m in sorted(s["types"].items()):
        if t in unlocked:
            out["drop"].append(t)
            continue
        if m.get("bought_at"):
            continue
        if m.get("key") and m["key"] in done:
            out["bought"].append((t, m["key"]))
            continue
        if t in owned:
            continue   # decommission the one you have
        if m.get("key") and m["key"] in trades and m["key"] in live:
            continue   # approved and still listed: logistics is on it
        for key, tr in offers.get(t) or []:
            if key in done:
                continue
            out["approve"].append((t, key, tr))
            break
    return out


def status(m: dict, t: str, owned: set[str], offers: dict, approvals: dict) -> str:
    """One line for the Missing blueprints page."""
    trades = approvals.get("trades") or {}
    if m.get("bought_at"):
        return "bought: decommission it to unlock the blueprint"
    if t in owned:
        return "you own one: decommission it"
    if m.get("key") and m["key"] in trades and any(k == m["key"] for k, _ in offers.get(t) or []):
        return "approved: logistics is fetching it"
    if offers.get(t):
        return "listed now: approving next pass"
    return "watching Riker's shop"
