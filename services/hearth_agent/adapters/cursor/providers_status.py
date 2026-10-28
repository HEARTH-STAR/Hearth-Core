"""
Cursor providers-status adapter.

A Cursor login and a ``CURSOR_API_KEY`` are mutually exclusive ways the CLI is authenticated. Like
claude_code, we run ``cursor-agent status`` **once**, with the env turns use, and light one tile:

- logged in while ``CURSOR_API_KEY`` is set → the Cursor **API key** tile;
- logged in otherwise → the Cursor **login** (OAuth) tile.

Presence, not validity: a signed-in but expired account still reads connected until a turn fails.
"""

from __future__ import annotations

import os
from typing import Any

from services.hearth_agent.adapters.cursor.models_status import CursorStatusError, fetch_status
from services.hearth_agent.providers_status_lib import build_providers_status


async def get_providers_status() -> dict[str, Any]:
    try:
        status = await fetch_status()
    except CursorStatusError:
        status = {}
    logged_in = bool(status.get("logged_in"))
    via_key = logged_in and bool((os.environ.get("CURSOR_API_KEY") or "").strip())
    return await build_providers_status(
        "cursor",
        oauth_present={"cursor": lambda: logged_in and not via_key},
        api_keys_present={"cursor": lambda: via_key},
    )


__all__ = ["get_providers_status"]
