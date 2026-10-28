"""
Antigravity (agy) login detection — file-based.

agy has no ``auth status`` subcommand, so login state is read from its consumer OAuth token file::

    {"token": {"access_token": "…", "refresh_token": "…", "token_type": "Bearer",
               "expiry": "2026-07-15T18:43:37.5…Z"}, "auth_method": "consumer"}

A login is usable when a ``refresh_token`` is present (agy refreshes the access token itself) or the
``access_token`` has not expired. Nothing here returns or logs the token.

Shape mirrors xo-space ``adapters/antigravity/auth.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

from services.hearth_agent.adapters.antigravity.paths import token_path


def login_state() -> str:
    """``"ok"`` | ``"expired"`` | ``"logged_out"`` | ``"invalid"``. Never raises."""
    path = token_path()
    if not path.is_file():
        return "logged_out"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "invalid"
    tok = data.get("token") if isinstance(data, dict) else None
    if not isinstance(tok, dict):
        return "invalid"
    if tok.get("refresh_token"):
        return "ok"
    access, expiry = tok.get("access_token"), tok.get("expiry")
    if not access:
        return "invalid"
    if isinstance(expiry, str):
        try:
            exp = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        except ValueError:
            return "ok"
        return "ok" if exp > datetime.now(UTC) else "expired"
    return "ok"


def has_usable_login() -> bool:
    return login_state() == "ok"


def auth_method() -> str | None:
    try:
        data = json.loads(token_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    method = data.get("auth_method") if isinstance(data, dict) else None
    return method if isinstance(method, str) else None


LOGIN_REQUIRED_MESSAGE = (
    "Antigravity is not signed in. Use Connect Antigravity in the web app (or run `agy` once in a terminal) "
    "to complete the Google sign-in, then retry."
)

__all__ = ["LOGIN_REQUIRED_MESSAGE", "auth_method", "has_usable_login", "login_state"]
