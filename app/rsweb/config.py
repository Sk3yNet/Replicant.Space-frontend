"""Runtime settings, all from environment variables."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _read_token() -> str:
    path = os.getenv("RS_API_TOKEN_FILE")
    if path and Path(path).exists():
        return Path(path).read_text().strip()
    return os.getenv("RS_API_TOKEN", "").strip()


def _csv(name: str) -> list[str]:
    return [x.strip().lower() for x in os.getenv(name, "").split(",") if x.strip()]


@dataclass
class Settings:
    api_base: str = field(default_factory=lambda: os.getenv("RS_API_BASE", "https://api.replicant.space/v1").rstrip("/"))
    api_token: str = field(default_factory=_read_token)
    db_path: str = field(default_factory=lambda: os.getenv("DB_PATH", "./rsweb.sqlite"))

    # Auth: nginx/oauth2-proxy put the signed-in Google email in this header.
    auth_header: str = "x-auth-request-email"
    allowed_emails: list[str] = field(default_factory=lambda: _csv("ALLOWED_EMAILS"))
    # For running the app directly on a laptop without the proxy in front.
    dev_user: str = field(default_factory=lambda: os.getenv("DEV_USER", ""))

    # Rate limits (game allows 120 GET + 60 actions per minute; keep headroom).
    get_per_min: int = int(os.getenv("RS_GET_PER_MIN", "110"))
    act_per_min: int = int(os.getenv("RS_ACT_PER_MIN", "55"))
    # Background polling may not dip below these reserves, so the UI always has budget.
    get_reserve: int = int(os.getenv("RS_GET_RESERVE", "30"))
    act_reserve: int = int(os.getenv("RS_ACT_RESERVE", "20"))

    # Poll intervals (seconds).
    poll_account: int = int(os.getenv("POLL_ACCOUNT", "60"))
    poll_devices: int = int(os.getenv("POLL_DEVICES", "60"))
    poll_inventory: int = int(os.getenv("POLL_INVENTORY", "120"))
    poll_messages: int = int(os.getenv("POLL_MESSAGES", "300"))
    poll_catalogue: int = int(os.getenv("POLL_CATALOGUE", "1800"))

    # A gap this long between page loads starts a new "visit" (drives the since-last-login digest).
    visit_gap_minutes: int = int(os.getenv("VISIT_GAP_MINUTES", "30"))

    disable_background: bool = os.getenv("DISABLE_BACKGROUND", "") == "1"


settings = Settings()
