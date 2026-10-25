"""v1 router.

Modules register their sub-routers here. Order matters only for documentation: the
health routes stay first so they are never shadowed by an authenticated prefix.

The users module owns both its auth and profile routes, so its router is mounted once
at the top level and declares the ``/auth`` and ``/me`` paths itself.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.modules.admin.presentation.routes import router as admin_router
from app.modules.ai.presentation.routes import router as ai_router
from app.modules.content.presentation.routes import router as content_router
from app.modules.gamification.presentation.routes import router as gamification_router
from app.modules.media.presentation.routes import router as media_router
from app.modules.quiz.presentation.progress_routes import router as progress_router
from app.modules.quiz.presentation.review_routes import router as review_router
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

#: The progress read surface. Authenticated and scoped to the caller's own record: an
#: overview (summary plus the XP feed) and the curriculum-ordered unlock map.
api_router.include_router(progress_router, prefix="/progress", tags=["progress"])

#: The spaced-repetition surface. Authenticated and scoped to the caller's own cards: the
#: due queue, the stats, a due-count badge, and the answer command that advances a card's
#: SM-2 schedule.
api_router.include_router(review_router, prefix="/review", tags=["review"])

#: The AI tutor surface. Authenticated and scoped to the caller: explain an answer, chat with
#: the tutor, read a thread back, and read the daily AI quota. Every response says whether it
#: came from a model or the deterministic fallback, because AI is an enhancement, never a
#: dependency the learning loop can be broken by.
api_router.include_router(ai_router, prefix="/ai", tags=["ai"])

#: The gamification surface. Authenticated and scoped to the caller: the hub summary, the
#: achievement catalogue with claim, and the streak with its premium-gated freeze. It is
#: mounted without a prefix because its paths are not one segment — the summary lives under
#: ``/gamification`` while achievements and streaks are top-level nouns; the router declares
#: its own full paths, the same way the users router does.
api_router.include_router(gamification_router, tags=["gamification"])

#: The admin surface. Gated by the single ``isAdmin`` flag, and mounted under its own
#: ``/admin`` prefix so the whole staff surface is one namespace. It declares its own
#: ``/content/...`` and ``/users`` paths beneath that prefix, and writes an audit row for
#: every mutation.
api_router.include_router(admin_router, prefix="/admin", tags=["admin"])

#: The media surface. Authenticated and mounted under its own ``/media`` prefix: presign
#: mints a write grant, confirm verifies the upload landed and records its metadata, and the
#: read re-signs a short-lived URL. The bytes never pass through the API — a client PUTs
#: straight to storage — so the request-body ceiling is untouched by how large an upload may
#: be.
api_router.include_router(media_router, prefix="/media", tags=["media"])

#: The development sign-in deliberately stays *off* ``api_router``: ``create_app``
#: mounts it only when ``DEV_LOGIN_ENABLED`` is set (off unless a local ``.env`` turns
#: it on, and refused outright in production), so the default deployment keeps the route
#: out of the router and out of the OpenAPI document entirely — absent, not merely
#: refused at request time.

# Later modules follow the same shape:
#   api_router.include_router(lessons.router, prefix="/lessons", tags=["lessons"])

__all__ = ["api_router", "dev_login_router"]
