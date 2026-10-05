"""v1 router.

Modules register their sub-routers here. Order matters only for documentation: the
health routes stay first so they are never shadowed by an authenticated prefix.

The users module owns both its auth and profile routes, so its router is mounted once
at the top level and declares the ``/auth`` and ``/me`` paths itself.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.modules.content.presentation.routes import router as content_router
from app.modules.users.presentation.routes import router as users_router

api_router = APIRouter()

#: Authentication and the caller's own profile. Phase 3.
api_router.include_router(users_router)

#: The public curriculum catalogue. No prefix of its own and no auth dependency: see
#: ``modules/content/presentation/routes.py`` for why that is safe.
api_router.include_router(content_router, prefix="/content", tags=["content"])

# Later modules follow the same shape:
#   api_router.include_router(lessons.router, prefix="/lessons", tags=["lessons"])

__all__ = ["api_router"]
