"""Live status routes resolved through the active agent's capabilities:
``/models/status``, ``/providers/status``, ``/channels/status``.

Shape mirrors xo-space ``routers/status/``.
"""

from .channels import router as channels_router
from .models import router as models_router
from .providers import router as providers_router
from .update import router as update_router

all_routers = [models_router, providers_router, channels_router, update_router]
