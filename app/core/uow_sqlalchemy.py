"""Concrete SQLAlchemy unit of work.

Kept apart from :mod:`app.core.uow` so the port stays driver-free. The runtime
imports of the repository classes are deferred to the property bodies: this
module may import SQLAlchemy, but a module that only wants
:class:`~app.core.uow.UnitOfWorkProtocol` must not have to.
"""

from __future__ import annotations

from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:  # pragma: no cover
    from app.core.uow import UnitOfWorkProtocol
    from app.modules.admin.domain.ports import (
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


class SqlAlchemyUnitOfWork:
    """Concrete UoW satisfying :class:`~app.core.uow.UnitOfWorkProtocol`.

    Usage::

        uow = make_uow()
        async with uow:
            user = await uow.users.get(user_id)
            ...
            await uow.commit()

    Leaving the block without ``commit()`` rolls back, so a partially-applied
    use case can never persist half its effects.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None
        self._committed = False
        self._repositories: dict[str, Any] = {}

    # ------------------------------------------------------------ lifecycle
    def __enter__(self) -> Self:
        if self._session is None:
            self._session = self._session_factory()
            self._committed = False
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Not supported: the underlying session is async.

        ``AsyncSession.rollback`` and ``.close`` return coroutines, so a synchronous
        ``with`` block would silently leak an open connection and an uncommitted
        transaction. Failing loudly beats leaking.
        """
        self._detach()
        raise RuntimeError(
            "SqlAlchemyUnitOfWork is asynchronous. Use `async with uow:` instead of `with uow:`."
        )

    async def __aenter__(self) -> Self:
        return self.__enter__()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Capture the committed flag *before* detaching: `_detach` resets it, and
        # reading it afterwards would roll back a transaction that already landed.
        committed = self._committed
        session = self._detach()
        if session is None:
            return
        try:
            if exc_type is not None or not committed:
                await session.rollback()
        finally:
            await session.close()

    def _detach(self) -> AsyncSession | None:
        session = self._session
        self._session = None
        self._committed = False
        self._repositories.clear()
        return session

    # -------------------------------------------------------------- session
    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise RuntimeError("UnitOfWork is not open. Use `async with uow:`.")
        return self._session

    @property
    def is_committed(self) -> bool:
        return self._committed

    # -------------------------------------------------------------- control
    async def commit(self) -> None:
        await self.session.commit()
        self._committed = True

    async def rollback(self) -> None:
        await self.session.rollback()
        self._committed = False

    async def flush(self) -> None:
        await self.session.flush()

    async def refresh(self, instance: Any) -> None:
        await self.session.refresh(instance)

    # ---------------------------------------------------------- repositories
    @property
    def users(self) -> UserRepositoryPort:
        from app.modules.users.infrastructure.repositories import SqlAlchemyUserRepository

        return self._repo("users", SqlAlchemyUserRepository)

    @property
    def refresh_tokens(self) -> RefreshTokenRepositoryPort:
        from app.modules.users.infrastructure.repositories import (
            SqlAlchemyRefreshTokenRepository,
        )

        return self._repo("refresh_tokens", SqlAlchemyRefreshTokenRepository)

    @property
    def user_devices(self) -> UserDeviceRepositoryPort:
        from app.modules.users.infrastructure.repositories import (
            SqlAlchemyUserDeviceRepository,
        )

        return self._repo("user_devices", SqlAlchemyUserDeviceRepository)

    @property
    def identities(self) -> ExternalIdentityRepositoryPort:
        from app.modules.users.infrastructure.repositories import (
            SqlAlchemyExternalIdentityRepository,
        )

        return self._repo("identities", SqlAlchemyExternalIdentityRepository)

    @property
    def ai_usage(self) -> AiUsageRepositoryPort:
        from app.modules.ai.infrastructure.repositories import SqlAlchemyAiUsageRepository

        return self._repo("ai_usage", SqlAlchemyAiUsageRepository)

    @property
    def ai_conversations(self) -> ConversationRepositoryPort:
        from app.modules.ai.infrastructure.conversation_repositories import (
            SqlAlchemyConversationRepository,
        )

        return self._repo("ai_conversations", SqlAlchemyConversationRepository)

    @property
    def levels(self) -> LevelRepositoryPort:
        from app.modules.content.infrastructure.repositories import (
            SqlAlchemyLevelRepository,
        )

        return self._repo("levels", SqlAlchemyLevelRepository)

    @property
    def units(self) -> UnitRepositoryPort:
        from app.modules.content.infrastructure.repositories import SqlAlchemyUnitRepository

        return self._repo("units", SqlAlchemyUnitRepository)

    @property
    def lessons(self) -> LessonRepositoryPort:
        from app.modules.content.infrastructure.repositories import (
            SqlAlchemyLessonRepository,
        )

        return self._repo("lessons", SqlAlchemyLessonRepository)

    @property
    def questions(self) -> QuestionRepositoryPort:
        from app.modules.content.infrastructure.repositories import (
            SqlAlchemyQuestionRepository,
        )

        return self._repo("questions", SqlAlchemyQuestionRepository)

    @property
    def quiz_sessions(self) -> QuizSessionRepositoryPort:
        from app.modules.quiz.infrastructure.repositories import (
            SqlAlchemyQuizSessionRepository,
        )

        return self._repo("quiz_sessions", SqlAlchemyQuizSessionRepository)

    @property
    def quiz_answers(self) -> QuizAnswerRepositoryPort:
        from app.modules.quiz.infrastructure.repositories import (
            SqlAlchemyQuizAnswerRepository,
        )

        return self._repo("quiz_answers", SqlAlchemyQuizAnswerRepository)

    @property
    def lesson_progress(self) -> LessonProgressRepositoryPort:
        from app.modules.quiz.infrastructure.repositories import (
            SqlAlchemyLessonProgressRepository,
        )

        return self._repo("lesson_progress", SqlAlchemyLessonProgressRepository)

    @property
    def review_cards(self) -> ReviewCardRepositoryPort:
        from app.modules.quiz.infrastructure.review_repositories import (
            SqlAlchemyReviewCardRepository,
        )

        return self._repo("review_cards", SqlAlchemyReviewCardRepository)

    @property
    def review_logs(self) -> ReviewLogRepositoryPort:
        from app.modules.quiz.infrastructure.review_repositories import (
            SqlAlchemyReviewLogRepository,
        )

        return self._repo("review_logs", SqlAlchemyReviewLogRepository)

    @property
    def user_stats(self) -> UserStatsRepositoryPort:
        from app.modules.gamification.infrastructure.repositories import (
            SqlAlchemyUserStatsRepository,
        )

        return self._repo("user_stats", SqlAlchemyUserStatsRepository)

    @property
    def achievements(self) -> AchievementRepositoryPort:
        from app.modules.gamification.infrastructure.repositories import (
            SqlAlchemyAchievementRepository,
        )

        return self._repo("achievements", SqlAlchemyAchievementRepository)

    @property
    def user_achievements(self) -> UserAchievementRepositoryPort:
        from app.modules.gamification.infrastructure.repositories import (
            SqlAlchemyUserAchievementRepository,
        )

        return self._repo("user_achievements", SqlAlchemyUserAchievementRepository)

    @property
    def media(self) -> MediaRepositoryPort:
        from app.modules.media.infrastructure.repositories import SqlAlchemyMediaRepository

        return self._repo("media", SqlAlchemyMediaRepository)

    @property
    def editorial(self) -> EditorialRepositoryPort:
        from app.modules.admin.infrastructure.repositories import SqlAlchemyEditorialRepository

        return self._repo("editorial", SqlAlchemyEditorialRepository)

    @property
    def audit_logs(self) -> AuditLogRepositoryPort:
        from app.modules.admin.infrastructure.repositories import SqlAlchemyAuditLogRepository

        return self._repo("audit_logs", SqlAlchemyAuditLogRepository)

    def _repo(self, key: str, factory: Any) -> Any:
        if key not in self._repositories:
            self._repositories[key] = factory(self.session)
        return self._repositories[key]

    def register(self, key: str, repository: Any) -> None:
        """Allow a module to contribute its own repository to this UoW.

        Modules call this from their own unit-of-work extension so that
        :class:`~app.core.uow.UnitOfWorkProtocol` stays the single access point.
        """
        self._repositories[key] = repository


#: The concrete class structurally satisfies the port; asserted at import time so a
#: signature drift on either side fails loudly instead of at the first use case.
_: type[UnitOfWorkProtocol] = SqlAlchemyUnitOfWork


__all__ = ["SqlAlchemyUnitOfWork"]
