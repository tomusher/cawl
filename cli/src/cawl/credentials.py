"""Persistent CLI credentials — written by `cawl login`, read as a fallback
when CAWL_API_URL / CAWL_TOKEN aren't in the environment.

Stored at ~/.config/cawl/credentials.json (override dir with CAWL_CONFIG_DIR),
mode 0600.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def path() -> Path:
    base = os.environ.get("CAWL_CONFIG_DIR") or os.path.join(
        os.path.expanduser("~"), ".config", "cawl")
    return Path(base) / "credentials.json"


def save(api_url: str, token: str) -> None:
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"api_url": api_url, "token": token}))
    p.chmod(0o600)


def load() -> tuple[str | None, str | None]:
    p = path()
    if not p.exists():
        return None, None
    try:
        d = json.loads(p.read_text())
    except (ValueError, OSError):
        return None, None
    return d.get("api_url"), d.get("token")


def clear() -> bool:
    p = path()
    if p.exists():
        p.unlink()
        return True
    return False
