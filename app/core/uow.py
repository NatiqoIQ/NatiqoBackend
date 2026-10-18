"""Unit of work: the transactional boundary of the application.

This module is a **port** and nothing else. It deliberately imports no database
driver, so that ``application`` and ``domain`` can depend on
:class:`UnitOfWorkProtocol` without pulling SQLAlchemy into their import graph.

The concrete implementation is :class:`app.core.uow_sqlalchemy.SqlAlchemyUnitOfWork`,
which is only ever constructed by :mod:`app.core.database` and by tests.

Note what the port does *not* expose: the underlying session. Application code
reaches persistence exclusively through repositories, so there is no seam by
which a use case can issue ad-hoc queries or bypass the transaction.
"""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Protocol, Self, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover
    from app.modules.admin.domain.ports import (
        AdminStatsRepositoryPort,
        AuditLogRepositoryPort,
        EditorialRepositoryPort,
    )
    from app.modules.ai.domain.conversation import ConversationRepositoryPort
    from app.modules.ai.domain.usage import AiUsageRepositoryPort
    from app.modules.content.domain.ports import (
        LessonRepositoryPort,
        LevelRepositoryPort,
        QuestionRepositoryPort,
        UnitRepositoryPort,
    )
    from app.modules.gamification.domain.ports import (
        AchievementRepositoryPort,
        UserAchievementRepositoryPort,
        UserStatsRepositoryPort,
    )
    from app.modules.media.domain.ports import MediaRepositoryPort
    from app.modules.quiz.domain.ports import (
        LessonProgressRepositoryPort,
        QuizAnswerRepositoryPort,
        QuizSessionRepositoryPort,
    )
    from app.modules.quiz.domain.review_ports import (
        ReviewCardRepositoryPort,
        ReviewLogRepositoryPort,
    )
    from app.modules.users.domain.ports import (
        ExternalIdentityRepositoryPort,
        RefreshTokenRepositoryPort,
        UserDeviceRepositoryPort,
        UserRepositoryPort,
    )


@runtime_checkable
class UnitOfWorkProtocol(Protocol):
    """What application code is allowed to assume about persistence.

    Repositories are attached lazily by the concrete implementation. Application
    services receive the UoW and ask it for the ports they need.

    Leaving the ``async with`` block without ``commit()`` rolls back, so a
    partially-applied use case can never persist half its effects.
    """

    @property
    def users(self) -> UserRepositoryPort: ...

    @property
    def refresh_tokens(self) -> RefreshTokenRepositoryPort: ...

    @property
    def user_devices(self) -> UserDeviceRepositoryPort: ...

    @property
    def identities(self) -> ExternalIdentityRepositoryPort: ...

    @property
    def ai_usage(self) -> AiUsageRepositoryPort: ...

    @property
    def ai_conversations(self) -> ConversationRepositoryPort: ...

    @property
    def levels(self) -> LevelRepositoryPort: ...

    @property
    def units(self) -> UnitRepositoryPort: ...

    @property
    def lessons(self) -> LessonRepositoryPort: ...

    @property
    def questions(self) -> QuestionRepositoryPort: ...

    @property
    def quiz_sessions(self) -> QuizSessionRepositoryPort: ...

    @property
    def quiz_answers(self) -> QuizAnswerRepositoryPort: ...

    @property
    def lesson_progress(self) -> LessonProgressRepositoryPort: ...

    @property
    def review_cards(self) -> ReviewCardRepositoryPort: ...

    @property
    def review_logs(self) -> ReviewLogRepositoryPort: ...

    @property
    def user_stats(self) -> UserStatsRepositoryPort: ...

    @property
    def achievements(self) -> AchievementRepositoryPort: ...

    @property
    def user_achievements(self) -> UserAchievementRepositoryPort: ...

    @property
    def media(self) -> MediaRepositoryPort: ...

    @property
    def editorial(self) -> EditorialRepositoryPort: ...

    @property
    def audit_logs(self) -> AuditLogRepositoryPort: ...

    @property
    def stats(self) -> AdminStatsRepositoryPort: ...

    async def commit(self) -> None: ...

    async def rollback(self) -> None: ...

    async def flush(self) -> None: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...


__all__ = ["UnitOfWorkProtocol"]
