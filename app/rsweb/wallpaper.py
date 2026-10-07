"""Desktop wallpaper: the Galaxy and System maps, read-only, for the Octos live-wallpaper add-on
(github.com/sk3ynet/replicant-space-octos).

A wallpaper can't sign in with Google, so it uses a wallpaper key instead: made under Account › Desktop wallpaper, shown
once, stored only as a SHA-256 hash, revocable. The key opens nothing but the map data below — no commands, no settings.

  /wallpaper/<slug>/                     the page (no data in it; off unless the wallpaper is enabled)
  /wallpaper/<slug>/static/<file>        the few scripts and styles it needs (the rest of /static stays behind sign-in)
  /wallpaper/<slug>/api/map.json         galaxy data         ┐
  /wallpaper/<slug>/api/systems.json     systems to cycle    │
  /wallpaper/<slug>/api/hud.json         dashboard panel     ├ header X-Wallpaper-Key: <key>
  /wallpaper/<slug>/api/system/<STAR>    a system's SVG map  ┘

The key travels in the link's #fragment (`…/wallpaper/<slug>/#key=rsw_…`), which browsers never send to a server, so it
stays out of access logs; the page's script sends it as a header. `<slug>` names the user's server in multi-user mode
(nginx asks the manager which port serves it); a single-user stack accepts any slug.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from datetime import datetime, timezone
from typing import Any

KV = "wallpaper"
HEADER = "X-Wallpaper-Key"
PREFIX = "rsw_"
# files the wallpaper page may load without signing in
STATIC = {"map.js", "movers.js", "wallpaper.js", "app.css", "vendor/three.module.min.js", "vendor/OrbitControls.js"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def slug() -> str:
    """This server's wallpaper slug: the tenant slug in multi-user mode (set by the manager), else "me"."""
    return os.getenv("WALLPAPER_SLUG") or "me"


def slug_ok(s: str) -> bool:
    return not os.getenv("WALLPAPER_SLUG") or s == os.getenv("WALLPAPER_SLUG")


def normalize(raw: Any) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {"enabled": bool(raw.get("enabled")), "keys": [k for k in raw.get("keys") or [] if isinstance(k, dict) and k.get("hash")]}


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def new_key(label: str) -> tuple[str, dict]:
    key = PREFIX + secrets.token_urlsafe(24)
    rec = {"id": secrets.token_hex(4), "hash": _hash(key), "label": (label or "").strip()[:40] or "wallpaper",
           "created_at": _now(), "last_used": None}
    return key, rec


def check(state: dict, presented: str | None) -> dict | None:
    """The key record the presented key matches (constant-time), or None."""
    if not presented or not presented.startswith(PREFIX):
        return None
    h = _hash(presented.strip())
    hit = None
    for rec in state["keys"]:
        if hmac.compare_digest(rec["hash"], h):
            hit = rec
    return hit


def link(origin: str, key: str) -> str:
    return f"{origin.rstrip('/')}/wallpaper/{slug()}/#key={key}"
