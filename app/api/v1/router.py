"""v1 router.

Modules register their sub-routers here. Order matters only for documentation: the
health routes stay first so they are never shadowed by an authenticated prefix.

The users module owns both its auth and profile routes, so its router is mounted once
at the top level and declares the ``/auth`` and ``/me`` paths itself.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.modules.content.presentation.routes import router as content_router
from app.modules.quiz.presentation.progress_routes import router as progress_router
from app.modules.quiz.presentation.routes import router as quiz_router
from app.modules.users.presentation.routes import (
    dev_router as dev_login_router,
    router as users_router,
)

api_router = APIRouter()

#: Authentication and the caller's own profile. Phase 3.
api_router.include_router(users_router)

#: The public curriculum catalogue. No prefix of its own and no auth dependency: see
#: ``modules/content/presentation/routes.py`` for why that is safe.
api_router.include_router(content_router, prefix="/content", tags=["content"])

#: The attempt engine. Authenticated, unlike the catalogue: a session is account data and
#: every route is scoped to the caller's own sessions.
api_router.include_router(quiz_router, prefix="/quiz", tags=["quiz"])

#: The progress read surface. Authenticated and scoped to the caller's own record: a
#: summary, one lesson's progress, the curriculum-ordered unlock map, and the XP history.
api_router.include_router(progress_router, prefix="/progress", tags=["progress"])

#: The development sign-in deliberately stays *off* ``api_router``: ``create_app``
#: mounts it only when ``DEV_LOGIN_ENABLED`` is set, so a production app object never
#: contains the route — it is absent from the router and from the OpenAPI document,
#: not merely refused at request time.

# Later modules follow the same shape:
#   api_router.include_router(lessons.router, prefix="/lessons", tags=["lessons"])

__all__ = ["api_router", "dev_login_router"]
