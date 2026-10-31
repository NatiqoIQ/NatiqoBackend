"""The AI tutor against a real schema and the real thread repository.

What only the database can prove lives here: that a conversation and its turns are persisted
in one transaction, that a thread id belonging to another account reads as absent, and that
the context the tutor is given is the learner's own level and explanation language. The
grading rule itself is pinned in ``tests/unit/test_quiz_policy.py``; this file is about the
wiring around it.

Two platform stand-ins split the theme in half. A real :class:`AIService` over
:class:`DisabledProvider` exercises the deterministic fallbacks the whole design rests on —
the suite runs with AI off, and the tutor must degrade to the authored rationale rather than
fail. A recording fake pins the other half: that the service forwards the model's text and
its ``source`` verbatim, instead of hard-coding ``authored`` or inventing an explanation.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import create_all, dispose_engine, get_session_factory
from app.core.errors import ErrorCode, NotFoundError, ValidationFailed
from app.core.registry import import_all_models
from app.core.types import CEFRLevel, ContentLanguage, ContentState, UserId
from app.core.uow_sqlalchemy import SqlAlchemyUnitOfWork
from app.modules.ai.application.tutor_service import AiTutorService
from app.modules.ai.domain.budget import BudgetPort, BudgetStatus, UnmeteredBudget
from app.modules.ai.domain.conversation import MessageRole
from app.modules.content.infrastructure.models import (
    LessonModel,
    LevelModel,
    QuestionModel,
    UnitModel,
)
from app.modules.quiz.domain.policy import SubmittedAnswer
from app.modules.users.infrastructure.models import (
    UserModel,
    UserProfileModel,
    UserSettingsModel,
)
from app.platform.ai.port import AIFeature, TutorContext
from app.platform.ai.providers import DisabledProvider
from app.platform.ai.service import AIService

pytestmark = pytest.mark.integration

_PUBLISHED = ContentState.PUBLISHED.value
_DRAFT = ContentState.DRAFT.value
_EXPLANATION = "Hello is the greeting."


# ------------------------------------------------------------------------ fixtures
@pytest_asyncio.fixture
async def uow() -> AsyncIterator[SqlAlchemyUnitOfWork]:
    import_all_models()
    await create_all()
    async with SqlAlchemyUnitOfWork(get_session_factory()) as open_uow:
        yield open_uow
    await dispose_engine()


# ------------------------------------------------------------------- stand-ins
class RecordingAIService:
    """A platform-service stand-in that records the context and returns a chosen result.

    Implements the whole port so the tutor service cannot tell it from the real one, and
    echoes a chosen ``source`` — which is what lets a test prove the service reports
    provenance rather than assuming it.
    """

    def __init__(
        self,
        *,
        text: str = "AI explanation",
        reply: str = "AI reply",
        source: str = "ai",
        enabled: bool = True,
    ) -> None:
        self._text = text
        self._reply = reply
        self._source = source
        self._enabled = enabled
        self.contexts: list[TutorContext] = []
        self.messages: list[str] = []

    @property
    def enabled(self) -> bool:
        return self._enabled

    async def explain_answer(
        self,
        *,
        context: TutorContext,
        question_text: str,
        learner_answer: str,
        correct_answer: str,
        is_correct: bool,
        explanation_en: str | None = None,
        explanation_iq: str | None = None,
    ) -> dict[str, Any]:
        self.contexts.append(context)
        return {"source": self._source, "text": self._text}

    async def correct_writing(
        self, *, context: TutorContext, text: str, task: str | None = None
    ) -> dict[str, Any]:
        return {"source": "unavailable", "original": text, "corrected": text, "errors": []}

    async def tutor_reply(
        self, *, context: TutorContext, history: Sequence[dict[str, str]], message: str
    ) -> dict[str, Any]:
        self.contexts.append(context)
        self.messages.append(message)
        return {"source": self._source, "reply": self._reply}


class FixedBudget:
    """A budget that answers with one prepared status, to pin the quota mapping."""

    def __init__(self, status: BudgetStatus) -> None:
        self._status = status

    async def status(self, user_id: UserId, *, feature: AIFeature) -> BudgetStatus:
        return self._status


def _ai_off() -> AIService:
    """The real platform service with a disabled provider — the suite's AI configuration."""
    return AIService(DisabledProvider())


def _service(
    uow: SqlAlchemyUnitOfWork,
    *,
    ai: Any = None,
    budget: BudgetPort | None = None,
) -> AiTutorService:
    return AiTutorService(uow, ai or _ai_off(), budget or UnmeteredBudget())


# --------------------------------------------------------------------- builders
def _user(session: AsyncSession) -> UserModel:
    row = UserModel(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@example.com",
        status="active",
    )
    session.add(row)
    return row


def _profile(session: AsyncSession, user_id: uuid.UUID, *, level: CEFRLevel = CEFRLevel.A1) -> None:
    session.add(
        UserProfileModel(user_id=user_id, display_name="Learner", current_level=level.value)
    )


def _settings(
    session: AsyncSession, user_id: uuid.UUID, *, language: ContentLanguage = ContentLanguage.EN
) -> None:
    session.add(UserSettingsModel(user_id=user_id, explanation_language=language.value))


def _learner(
    session: AsyncSession,
    *,
    level: CEFRLevel = CEFRLevel.A1,
    language: ContentLanguage = ContentLanguage.EN,
) -> UserModel:
    user = _user(session)
    _profile(session, user.id, level=level)
    _settings(session, user.id, language=language)
    return user


def _lesson(session: AsyncSession, *, state: str = _PUBLISHED) -> tuple[LessonModel, QuestionModel]:
    """One published level → unit → lesson, with one answerable multiple-choice question."""
    level = LevelModel(id=uuid.uuid4(), code="A1", title="A1", position=1, state=_PUBLISHED)
    session.add(level)
    unit = UnitModel(
        id=uuid.uuid4(),
        level_id=level.id,
        title="Introductions",
        position=1,
        state=_PUBLISHED,
    )
    session.add(unit)
    lesson = LessonModel(
        id=uuid.uuid4(),
        unit_id=unit.id,
        title="Greetings",
        summary="A short summary.",
        position=1,
        estimated_minutes=5,
        state=state,
    )
    session.add(lesson)
    question = QuestionModel(
        id=uuid.uuid4(),
        lesson_id=lesson.id,
        position=1,
        kind="multiple_choice",
        skill="vocabulary",
        prompt="Which one means 'hello'?",
        choices=["Hello", "Goodbye", "Thank you"],
        accepted_answers=[],
        correct_index=0,
        explanation=_EXPLANATION,
        points=1,
    )
    session.add(question)
    return lesson, question


# ================================================================ explain
@pytest.mark.asyncio
async def test_explaining_falls_back_to_the_authored_rationale(
    uow: SqlAlchemyUnitOfWork,
) -> None:
    """With AI off the rationale is the CMS text and ``source`` says so — never a fake "ai"."""
    session = uow.session
    user = _learner(session)
    _, question = _lesson(session)
    await session.commit()

    view = await _service(uow).explain(
        UserId(user.id), question_id=question.id, answer=SubmittedAnswer(choice="Hello")
    )

    assert view.is_correct is True
    assert view.correct_answer == {"choice": "Hello"}
    assert view.explanation == _EXPLANATION
    assert view.source == "authored"


@pytest.mark.asyncio
async def test_explaining_a_wrong_answer_reports_the_verdict(
    uow: SqlAlchemyUnitOfWork,
) -> None:
    session = uow.session
    user = _learner(session)
    _, question = _lesson(session)
    await session.commit()

    view = await _service(uow).explain(
        UserId(user.id), question_id=question.id, answer=SubmittedAnswer(choice="Goodbye")
    )

    assert view.is_correct is False
    assert view.correct_answer == {"choice": "Hello"}


@pytest.mark.asyncio
async def test_explaining_an_unpublished_question_is_refused(
    uow: SqlAlchemyUnitOfWork,
) -> None:
    """A question under a draft lesson has nothing to explain, exactly as in ``/review``."""
    session = uow.session
    user = _learner(session)
    _, question = _lesson(session, state=_DRAFT)
    await session.commit()

    with pytest.raises(NotFoundError) as raised:
        await _service(uow).explain(
            UserId(user.id), question_id=question.id, answer=SubmittedAnswer(choice="Hello")
        )

    assert raised.value.code is ErrorCode.QUESTION_NOT_FOUND


@pytest.mark.asyncio
async def test_the_models_text_and_provenance_are_forwarded(
    uow: SqlAlchemyUnitOfWork,
) -> None:
    """When a model answers, its text and ``source`` reach the caller unchanged."""
    session = uow.session
    user = _learner(session)
    _, question = _lesson(session)
    await session.commit()
    recorder = RecordingAIService(text="Because 'hello' is a greeting.", source="ai")

    view = await _service(uow, ai=recorder).explain(
        UserId(user.id), question_id=question.id, answer=SubmittedAnswer(choice="Hello")
    )

    assert view.explanation == "Because 'hello' is a greeting."
    assert view.source == "ai"


@pytest.mark.asyncio
async def test_the_context_carries_the_learners_level_and_language(
    uow: SqlAlchemyUnitOfWork,
) -> None:
    """The tutor is told the learner's CEFR band and chosen explanation language."""
    session = uow.session
    user = _learner(session, level=CEFRLevel.B1, language=ContentLanguage.IQ)
    _, question = _lesson(session)
    await session.commit()
    recorder = RecordingAIService()

    await _service(uow, ai=recorder).explain(
        UserId(user.id), question_id=question.id, answer=SubmittedAnswer(choice="Hello")
    )

    (context,) = recorder.contexts
    assert context.user_id == UserId(user.id)
    assert context.level is CEFRLevel.B1
    assert context.explanation_language is ContentLanguage.IQ


@pytest.mark.asyncio
async def test_a_missing_profile_falls_back_to_the_safest_defaults(
    uow: SqlAlchemyUnitOfWork,
) -> None:
    """A learner without a profile row is still tutored, at A1 in English, not failed."""
    session = uow.session
    user = _user(session)
    _, question = _lesson(session)
    await session.commit()
    recorder = RecordingAIService()

    await _service(uow, ai=recorder).explain(
        UserId(user.id), question_id=question.id, answer=SubmittedAnswer(choice="Hello")
    )

    (context,) = recorder.contexts
    assert context.level is CEFRLevel.A1
    assert context.explanation_language is ContentLanguage.EN


# =================================================================== chat
@pytest.mark.asyncio
async def test_a_thread_opens_persists_both_turns_and_continues(
    uow: SqlAlchemyUnitOfWork,
) -> None:
    """The first message opens a thread; a second continues it and the transcript keeps both.

    Both turns are persisted in the same transaction, so a stored question can never be
    left without its answer.
    """
    session = uow.session
    user = _learner(session)
    await session.commit()
    service = _service(uow)

    first = await service.ask(UserId(user.id), message="How do I say hello?")

    assert first.source == "authored"
    assert first.reply.strip() != ""
    turns = await uow.ai_conversations.list_messages(first.conversation_id)
    assert [turn.role for turn in turns] == [MessageRole.USER.value, MessageRole.ASSISTANT.value]
    assert turns[0].content == "How do I say hello?"

    second = await service.ask(
        UserId(user.id), message="And goodbye?", conversation_id=first.conversation_id
    )

    assert second.conversation_id == first.conversation_id
    grown = await uow.ai_conversations.list_messages(first.conversation_id)
    assert [turn.role for turn in grown] == [
        MessageRole.USER.value,
        MessageRole.ASSISTANT.value,
        MessageRole.USER.value,
        MessageRole.ASSISTANT.value,
    ]


@pytest.mark.asyncio
async def test_a_thread_records_the_lesson_it_was_anchored_to(
    uow: SqlAlchemyUnitOfWork,
) -> None:
    session = uow.session
    user = _learner(session)
    lesson, _ = _lesson(session)
    await session.commit()
    service = _service(uow)

    opened = await service.ask(UserId(user.id), message="Help", lesson_id=lesson.id)
    view = await service.history(UserId(user.id), opened.conversation_id)

    assert view.lesson_id == lesson.id


@pytest.mark.asyncio
async def test_a_blank_message_is_rejected(uow: SqlAlchemyUnitOfWork) -> None:
    session = uow.session
    user = _learner(session)
    await session.commit()

    with pytest.raises(ValidationFailed):
        await _service(uow).ask(UserId(user.id), message="   ")


@pytest.mark.asyncio
async def test_a_thread_is_scoped_to_its_owner(uow: SqlAlchemyUnitOfWork) -> None:
    """One learner's thread must not be readable or continuable by another."""
    session = uow.session
    owner = _learner(session)
    stranger = _learner(session)
    await session.commit()
    service = _service(uow)

    opened = await service.ask(UserId(owner.id), message="Private question.")

    with pytest.raises(NotFoundError) as read_raised:
        await service.history(UserId(stranger.id), opened.conversation_id)
    assert read_raised.value.code is ErrorCode.CONVERSATION_NOT_FOUND

    with pytest.raises(NotFoundError) as write_raised:
        await service.ask(
            UserId(stranger.id), message="Still here?", conversation_id=opened.conversation_id
        )
    assert write_raised.value.code is ErrorCode.CONVERSATION_NOT_FOUND

    # The owner can still read it; the refusals above changed nothing.
    mine = await service.history(UserId(owner.id), opened.conversation_id)
    assert mine.id == opened.conversation_id


# ================================================================== quota
@pytest.mark.asyncio
async def test_the_quota_reports_the_budget_it_read(uow: SqlAlchemyUnitOfWork) -> None:
    """The quota is the budget's own figures restated, not a second estimate."""
    session = uow.session
    user = _learner(session)
    await session.commit()
    budget = FixedBudget(
        BudgetStatus(
            allowed=False,
            spent_usd=Decimal("0.50"),
            limit_usd=Decimal("0.50"),
            reason="Daily AI budget reached (tutor_chat). It resets tomorrow.",
        )
    )

    view = await _service(uow, budget=budget).quota(UserId(user.id))

    assert view.enabled is False
    assert view.allowed is False
    assert view.limit_usd == 0.50
    assert view.spent_usd == 0.50
    assert view.remaining_usd == 0.0
    assert view.reason == "Daily AI budget reached (tutor_chat). It resets tomorrow."


# ======================================================== connection discipline
class _CommitProbeAI(RecordingAIService):
    """Records whether the UoW had already committed when the provider was called."""

    def __init__(self, uow: SqlAlchemyUnitOfWork) -> None:
        super().__init__()
        self._uow = uow
        self.saw_commit = False

    async def explain_answer(self, **kwargs: Any) -> dict[str, Any]:
        self.saw_commit = self._uow.is_committed
        return await super().explain_answer(**kwargs)

    async def tutor_reply(self, **kwargs: Any) -> dict[str, Any]:
        self.saw_commit = self._uow.is_committed
        return await super().tutor_reply(**kwargs)


@pytest.mark.asyncio
async def test_the_model_is_called_after_the_database_transaction_ends(
    uow: SqlAlchemyUnitOfWork,
) -> None:
    """A ten-second provider call must not pin a pooled connection for ten seconds.

    The transaction is ended before the upstream call: the data the provider needs is
    copied out first, and the connection goes back to the pool while the request waits.
    """
    session = uow.session
    user = _learner(session)
    _, question = _lesson(session)
    await session.commit()
    probe = _CommitProbeAI(uow)

    await _service(uow, ai=probe).explain(
        UserId(user.id), question_id=question.id, answer=SubmittedAnswer(choice="Hello")
    )

    assert probe.saw_commit is True


@pytest.mark.asyncio
async def test_a_chat_turn_is_persisted_before_the_model_is_called(
    uow: SqlAlchemyUnitOfWork,
) -> None:
    """The learner's message survives a provider failure, and the connection is free."""
    session = uow.session
    user = _learner(session)
    await session.commit()
    probe = _CommitProbeAI(uow)

    await _service(uow, ai=probe).ask(UserId(user.id), message="How do I say hello?")

    assert probe.saw_commit is True
