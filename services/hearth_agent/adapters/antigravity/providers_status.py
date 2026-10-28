"""
Antigravity providers-status adapter.

agy's only credential is the Google account it is signed in with (a consumer OAuth token file), so the
envelope has one tile, ``oauth.antigravity``, connected when that login is usable. No API keys.

Shape mirrors xo-space ``adapters/antigravity/providers_status.py``.
"""

from __future__ import annotations

from typing import Any

from services.hearth_agent.adapters.antigravity.auth import has_usable_login
from services.hearth_agent.providers_status_lib import build_providers_status


async def get_providers_status() -> dict[str, Any]:
    return await build_providers_status("antigravity", oauth_present={"antigravity": has_usable_login})


__all__ = ["get_providers_status"]
