"""The attempt engine against a real schema with the real repositories.

What only the database can prove is here: the unique constraints that stop a double answer
and a doubled progress row, the foreign keys that tie a session to its owner, and the
transaction the service commits so answers, session counters and lesson progress land
together or not at all.

The other theme is the answer key. Every read that returns questions is checked to be free
of ``correct_index``, ``accepted_answers`` and ``explanation``; the verdict appears only in
the response to an answer.

Retrying a missed question is the third theme: it is graded as a second attempt (half
marks), and it must *replace* the stored answer rather than add a row, so a question still
counts once towards its session.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import create_all, dispose_engine, get_session_factory
from app.core.errors import ConflictError, ErrorCode, NotFoundError, ValidationFailed
from app.core.registry import import_all_models
from app.core.types import ContentState, UserId
from app.core.uow_sqlalchemy import SqlAlchemyUnitOfWork
from app.modules.content.infrastructure.models import (
    LessonModel,
    LevelModel,
    QuestionModel,
    UnitModel,
)
from app.modules.quiz.application.quiz_service import QuizService
from app.modules.quiz.domain.entities import ProgressState, SessionMode, SessionStatus
from app.modules.quiz.domain.policy import FIRST_ATTEMPT, SubmittedAnswer
from app.modules.quiz.domain.presentation import (
    choice_order,
    order_choices,
    question_seed,
)
from app.modules.users.infrastructure.models import UserModel

pytestmark = pytest.mark.integration


# ------------------------------------------------------------------------ fixtures
@pytest_asyncio.fixture
async def uow() -> AsyncIterator[SqlAlchemyUnitOfWork]:
    import_all_models()
    await create_all()
    async with SqlAlchemyUnitOfWork(get_session_factory()) as open_uow:
        yield open_uow
    await dispose_engine()


@pytest.fixture
def service(uow: SqlAlchemyUnitOfWork) -> QuizService:
    return QuizService(uow, get_settings())


@pytest.fixture
def session(uow: SqlAlchemyUnitOfWork) -> AsyncSession:
    return uow.session


# -------------------------------------------------------------------- row builders
def _user_row(session: AsyncSession) -> UserModel:
    row = UserModel(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@example.com",
        status="active",
    )
    session.add(row)
    return row


def _question_rows(lesson_id: uuid.UUID) -> list[QuestionModel]:
    """Three questions covering all three gradeable kinds.

    Points differ per question on purpose, so a scored total cannot be right by accident.
    """
    return [
        QuestionModel(
            id=uuid.uuid4(),
            lesson_id=lesson_id,
            position=1,
            kind="multiple_choice",
            skill="vocabulary",
            prompt="Which one means 'hello'?",
            choices=["Hello", "Goodbye", "Thank you"],
            accepted_answers=[],
            correct_index=0,
            explanation="Hello is the greeting.",
            points=2,
        ),
        QuestionModel(
            id=uuid.uuid4(),
            lesson_id=lesson_id,
            position=2,
            kind="fill_blank",
            skill="grammar",
            prompt="She ___ to school every day.",
            choices=[],
            accepted_answers=["goes"],
            correct_index=None,
            explanation="Third person singular takes -s.",
            points=3,
        ),
        QuestionModel(
            id=uuid.uuid4(),
            lesson_id=lesson_id,
            position=3,
            kind="true_false",
            skill="grammar",
            prompt="'He go to school' is correct.",
            choices=["True", "False"],
            accepted_answers=[],
            correct_index=1,
            explanation="It should be 'He goes'.",
            points=1,
        ),
    ]


def _lesson(
    session: AsyncSession,
    *,
    code: str = "A1",
    position: int = 1,
    state: str = ContentState.PUBLISHED.value,
    with_questions: bool = True,
) -> tuple[LessonModel, list[QuestionModel]]:
    """One level → unit → lesson, plus its questions.

    ``code`` is unique per level, so a test that needs a second lesson must pass a
    different CEFR code rather than colliding on the unique index. That second level also
    needs a higher ``position``: the curriculum is ordered by level position, and with both
    at the same one which unit came first — and so which unit is unlocked — would be a coin
    toss.
    """
    level = LevelModel(
        id=uuid.uuid4(),
        code=code,
        title=code,
        position=position,
        state=ContentState.PUBLISHED.value,
    )
    session.add(level)
    unit = UnitModel(
        id=uuid.uuid4(),
        level_id=level.id,
        title="Introductions",
        position=1,
        state=ContentState.PUBLISHED.value,
    )
    session.add(unit)
    lesson = LessonModel(
        id=uuid.uuid4(),
        unit_id=unit.id,
        title="Greetings",
        summary="Say hello.",
        position=1,
        estimated_minutes=8,
        state=state,
    )
    session.add(lesson)
    questions = _question_rows(lesson.id) if with_questions else []
    session.add_all(questions)
    return lesson, questions


async def _seed(session: AsyncSession) -> tuple[UserId, uuid.UUID, list[QuestionModel]]:
    user = _user_row(session)
    lesson, questions = _lesson(session)
    await session.flush()
    return UserId(user.id), lesson.id, questions


async def _start(service: QuizService, user_id: UserId, lesson_id: uuid.UUID) -> uuid.UUID:
    detail = await service.start_session(user_id, mode=SessionMode.LESSON, lesson_id=lesson_id)
    return detail.session.id


async def _answer_all(
    service: QuizService,
    user_id: UserId,
    session_id: uuid.UUID,
    questions: list[QuestionModel],
) -> None:
    """Answer every seeded question correctly, in order."""
    await service.submit_answer(
        user_id, session_id, question_id=questions[0].id, answer=SubmittedAnswer(choice="Hello")
    )
    await service.submit_answer(
        user_id, session_id, question_id=questions[1].id, answer=SubmittedAnswer(text="goes")
    )
    await service.submit_answer(
        user_id, session_id, question_id=questions[2].id, answer=SubmittedAnswer(choice="False")
    )


def _expected_choices(
    session_id: uuid.UUID, question: QuestionModel, attempt: int
) -> tuple[str, ...]:
    """The choices the service should serve for a question on a given attempt.

    Recomputed from the same seed the service uses, so a test asserts the *wiring* — which
    attempt feeds the order — rather than a hard-coded permutation that would only prove the
    hash function is the hash function.
    """
    order = choice_order(
        len(question.choices),
        seed=question_seed(session_id=session_id, question_id=question.id, attempt=attempt),
    )
    return order_choices(tuple(question.choices), order)


# ==================================================================== start session
class TestStartSession:
    async def test_it_freezes_the_lesson_questions_in_order(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, _ = await _seed(session)

        detail = await service.start_session(user_id, mode=SessionMode.LESSON, lesson_id=lesson_id)

        assert detail.session.status == SessionStatus.ACTIVE.value
        assert detail.session.total_count == 3
        assert detail.session.answered_count == 0
        assert [question.position for question in detail.questions] == [1, 2, 3]

    async def test_the_questions_carry_no_answer_key(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        """The reason this slice exists: an uncommitted client cannot learn the answer."""
        user_id, lesson_id, _ = await _seed(session)

        detail = await service.start_session(user_id, mode=SessionMode.LESSON, lesson_id=lesson_id)

        for question in detail.questions:
            for leaked in ("correct_index", "accepted_answers", "explanation"):
                assert not hasattr(question, leaked), f"{leaked} must not reach a session"

    async def test_the_client_receives_points_and_every_choice(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        """The options arrive shuffled per attempt, so presence — not order — is the contract."""
        user_id, lesson_id, _ = await _seed(session)

        detail = await service.start_session(user_id, mode=SessionMode.LESSON, lesson_id=lesson_id)

        served = detail.questions[0].choices
        assert detail.questions[0].points == 2
        assert len(served) == 3
        assert set(served) == {"Hello", "Goodbye", "Thank you"}

    async def test_a_draft_lesson_reads_as_not_found(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user = _user_row(session)
        lesson, _ = _lesson(session, state=ContentState.DRAFT.value)
        await session.flush()

        with pytest.raises(NotFoundError) as raised:
            await service.start_session(
                UserId(user.id), mode=SessionMode.LESSON, lesson_id=lesson.id
            )

        assert raised.value.code is ErrorCode.LESSON_NOT_FOUND

    async def test_a_lesson_with_no_questions_is_not_available(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user = _user_row(session)
        lesson, _ = _lesson(session, with_questions=False)
        await session.flush()

        with pytest.raises(NotFoundError) as raised:
            await service.start_session(
                UserId(user.id), mode=SessionMode.LESSON, lesson_id=lesson.id
            )

        assert raised.value.code is ErrorCode.LESSON_NOT_AVAILABLE

    async def test_an_unknown_lesson_reads_as_not_found(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user = _user_row(session)
        await session.flush()

        with pytest.raises(NotFoundError) as raised:
            await service.start_session(
                UserId(user.id), mode=SessionMode.LESSON, lesson_id=uuid.uuid4()
            )

        assert raised.value.code is ErrorCode.LESSON_NOT_FOUND

    async def test_a_mode_that_does_not_exist_yet_is_refused(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, _ = await _seed(session)

        with pytest.raises(ValidationFailed):
            await service.start_session(user_id, mode=SessionMode.REVIEW, lesson_id=lesson_id)

    async def test_a_lesson_session_needs_a_lesson(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user = _user_row(session)
        await session.flush()

        with pytest.raises(ValidationFailed):
            await service.start_session(UserId(user.id), mode=SessionMode.LESSON, lesson_id=None)

    async def test_starting_marks_the_lesson_in_progress(
        self, service: QuizService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        user_id, lesson_id, _ = await _seed(session)

        await service.start_session(user_id, mode=SessionMode.LESSON, lesson_id=lesson_id)

        progress = await uow.lesson_progress.get(user_id, lesson_id)
        assert progress is not None
        assert progress.state is ProgressState.IN_PROGRESS


# =================================================================== grade an answer
class TestSubmitAnswer:
    async def test_a_correct_choice_scores_full_points(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, questions = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)

        graded = await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice="Hello"),
        )

        assert graded.is_correct is True
        assert graded.points_awarded == 2
        assert graded.correct_answer == {"choice": "Hello"}
        assert graded.explanation == "Hello is the greeting."
        assert graded.answered_count == 1
        assert graded.is_complete is False

    async def test_a_wrong_choice_scores_nothing_but_still_explains(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, questions = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)

        graded = await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice="Goodbye"),
        )

        assert graded.is_correct is False
        assert graded.points_awarded == 0
        assert graded.correct_answer == {"choice": "Hello"}
        assert graded.explanation == "Hello is the greeting."

    async def test_a_free_text_answer_is_graded_case_insensitively(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, questions = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)

        graded = await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[1].id,
            answer=SubmittedAnswer(text="  Goes "),
        )

        assert graded.is_correct is True
        assert graded.points_awarded == 3
        assert graded.correct_answer == {"text": "goes"}

    async def test_the_letter_form_resolves_against_the_served_choices(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        """The letter indexes the order the learner was shown, not the stored order."""
        user_id, lesson_id, questions = await _seed(session)
        detail = await service.start_session(user_id, mode=SessionMode.LESSON, lesson_id=lesson_id)
        served = next(q for q in detail.questions if q.id == questions[2].id)

        letter = "abcdefghijklmnopqrstuvwxyz"[served.choices.index("False")]

        graded = await service.submit_answer(
            user_id,
            detail.session.id,
            question_id=questions[2].id,
            answer=SubmittedAnswer(choice=letter),
        )

        assert graded.is_correct is True  # the letter points at "False" as served
        assert graded.correct_answer == {"choice": "False"}

    async def test_answering_every_question_reports_completion(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, questions = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)

        last = None
        for question, answer in (
            (questions[0], SubmittedAnswer(choice="Hello")),
            (questions[1], SubmittedAnswer(text="goes")),
            (questions[2], SubmittedAnswer(choice="False")),
        ):
            last = await service.submit_answer(
                user_id, session_id, question_id=question.id, answer=answer
            )

        assert last is not None
        assert last.is_complete is True
        assert last.answered_count == 3
        assert last.correct_count == 3

    async def test_a_question_answered_correctly_is_closed(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        """A right answer ends the question. Retrying for another go is what half marks stops."""
        user_id, lesson_id, questions = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)
        await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice="Hello"),
        )

        with pytest.raises(ConflictError) as raised:
            await service.submit_answer(
                user_id,
                session_id,
                question_id=questions[0].id,
                answer=SubmittedAnswer(choice="Hello"),
            )

        assert raised.value.code is ErrorCode.QUESTION_ALREADY_ANSWERED

    async def test_a_question_from_another_lesson_is_not_in_the_session(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, _ = await _seed(session)
        # A question from a *later* unit, which the sequential-unlock rule leaves shut —
        # which is fine here: this test never starts a sitting on it. All it needs is a
        # question that exists and does not belong to the lesson the session was opened on.
        _, other_questions = _lesson(session, code="A2", position=2)
        await session.flush()
        session_id = await _start(service, user_id, lesson_id)

        with pytest.raises(NotFoundError) as raised:
            await service.submit_answer(
                user_id,
                session_id,
                question_id=other_questions[0].id,
                answer=SubmittedAnswer(choice="Hello"),
            )

        assert raised.value.code is ErrorCode.QUESTION_NOT_IN_SESSION

    async def test_a_submission_of_the_wrong_shape_is_rejected(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, questions = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)

        with pytest.raises(ValidationFailed):
            await service.submit_answer(
                user_id,
                session_id,
                question_id=questions[1].id,
                answer=SubmittedAnswer(choice="goes"),
            )

    async def test_a_session_belonging_to_another_learner_is_not_found(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        owner_id, lesson_id, questions = await _seed(session)
        other = _user_row(session)
        await session.flush()
        session_id = await _start(service, owner_id, lesson_id)

        with pytest.raises(NotFoundError) as raised:
            await service.submit_answer(
                UserId(other.id),
                session_id,
                question_id=questions[0].id,
                answer=SubmittedAnswer(choice="Hello"),
            )

        assert raised.value.code is ErrorCode.QUIZ_SESSION_NOT_FOUND


# ============================================================= retry a wrong answer
class TestRetryingAWrongAnswer:
    """A missed question may be answered again, and a correct retry earns half.

    What only the database can confirm is that the retry *replaces* the stored answer rather
    than appending a second one: the row count stays equal to the questions the learner has
    committed to, so completion keeps meaning "answered the lesson" instead of "answered a
    question enough times".
    """

    async def test_a_correct_retry_earns_half_points(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, questions = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)
        first = await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice="Goodbye"),
        )

        retry = await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice="Hello"),
        )

        assert first.is_correct is False
        assert first.points_awarded == 0
        assert retry.is_correct is True
        assert retry.points_awarded == 1  # half of the question's 2

    async def test_a_wrong_retry_stays_wrong(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, questions = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)
        await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice="Goodbye"),
        )

        retry = await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice="Thank you"),
        )

        assert retry.is_correct is False
        assert retry.points_awarded == 0
        assert retry.correct_count == 0

    async def test_a_free_text_retry_is_halved(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, questions = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)
        await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[1].id,
            answer=SubmittedAnswer(text="go"),
        )

        retry = await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[1].id,
            answer=SubmittedAnswer(text="GOES"),
        )

        assert retry.is_correct is True
        assert retry.points_awarded == 1  # 3 // 2

    async def test_the_retry_replaces_the_stored_answer(
        self, service: QuizService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        """One row per question: the unique constraint has to keep holding."""
        user_id, lesson_id, questions = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)
        await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice="Goodbye"),
        )
        before = await uow.quiz_answers.get(session_id, questions[0].id)
        assert before is not None

        retry = await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice="Hello"),
        )

        stored = await uow.quiz_answers.list_for_session(session_id)
        assert len(stored) == 1
        assert stored[0].id == before.id  # re-graded in place, not a second row
        assert stored[0].given_answer == {"choice": "Hello"}
        assert stored[0].is_correct is True
        assert stored[0].points_awarded == 1
        assert retry.answered_count == 1

    async def test_the_session_counts_a_retried_question_once(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        """A retry cannot inflate the answered count the completion check relies on."""
        user_id, lesson_id, questions = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)
        await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice="Goodbye"),
        )
        await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice="Hello"),
        )

        resumed = await service.resume_session(user_id, session_id)

        assert resumed.session.answered_count == 1
        assert resumed.session.answered_question_ids == (questions[0].id,)
        assert resumed.session.total_count == 3

    async def test_a_retry_after_the_lease_runs_out_is_refused(
        self, service: QuizService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        """The retry window is the session's lease, not an extension of it.

        The lease is ended directly here rather than by shortening the TTL at start, so the
        *first* answer is a real attempt and only the retry runs into an expired session —
        which is the case a client that sat on a wrong answer would actually hit.
        """
        user_id, lesson_id, questions = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)
        await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice="Goodbye"),
        )
        stored = await uow.quiz_sessions.get(session_id)
        assert stored is not None
        stored.mark_expired()
        await uow.quiz_sessions.save(stored)
        await uow.commit()

        with pytest.raises(ConflictError) as raised:
            await service.submit_answer(
                user_id,
                session_id,
                question_id=questions[0].id,
                answer=SubmittedAnswer(choice="Hello"),
            )

        assert raised.value.code is ErrorCode.QUIZ_SESSION_EXPIRED

    async def test_a_retry_keeps_the_session_ordered_by_position(
        self, service: QuizService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        """``position`` is what orders a session's answers, and a retry must not disturb it."""
        user_id, lesson_id, questions = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)
        await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[1].id,
            answer=SubmittedAnswer(text="go"),
        )
        await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice="Hello"),
        )
        await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[1].id,
            answer=SubmittedAnswer(text="goes"),
        )

        stored = await uow.quiz_answers.list_for_session(session_id)

        assert [answer.position for answer in stored] == [1, 2]
        assert [answer.question_id for answer in stored] == [questions[0].id, questions[1].id]

    async def test_completing_after_a_retry_scores_the_halved_total(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        """The score the learner sees reflects the retry, not the full value of the question."""
        user_id, lesson_id, questions = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)
        await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice="Goodbye"),  # wrong, then corrected below: 1 of 2
        )
        await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice="Hello"),
        )
        await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[1].id,
            answer=SubmittedAnswer(text="goes"),  # correct first try: 3
        )
        await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[2].id,
            answer=SubmittedAnswer(choice="False"),  # correct first try: 1
        )

        completed = await service.complete_session(user_id, session_id)

        assert completed.score_percent == 100  # every question ended up right
        assert completed.session.xp_awarded == 5  # 1 + 3 + 1, the retry halved
        assert completed.session.correct_count == 3


# ================================================================ multi-select
def _multi_select_row(lesson_id: uuid.UUID, position: int = 4) -> QuestionModel:
    return QuestionModel(
        id=uuid.uuid4(),
        lesson_id=lesson_id,
        position=position,
        kind="multi_select",
        skill="vocabulary",
        prompt="Which of these can greet someone?",
        choices=["Hello", "Good morning", "Goodbye", "Thank you"],
        accepted_answers=["Hello", "Good morning"],
        correct_index=None,
        explanation="'Hello' and 'good morning' are greetings.",
        points=2,
    )


class TestMultiSelectAnswer:
    """A multi-select is graded as a set, and the key never reaches the client."""

    async def test_the_exact_set_scores_full_points(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, _ = await _seed(session)
        question = _multi_select_row(lesson_id)
        session.add(question)
        await session.flush()
        session_id = await _start(service, user_id, lesson_id)

        graded = await service.submit_answer(
            user_id,
            session_id,
            question_id=question.id,
            answer=SubmittedAnswer(choices=("Hello", "Good morning")),
        )

        assert graded.is_correct is True
        assert graded.points_awarded == 2
        # Reported in the order the options were served, so the string matches what the
        # learner was looking at rather than the stored order behind the reshuffle.
        reported = graded.correct_answer["choices"]
        assert set(reported.split(", ")) == {"Hello", "Good morning"}

    async def test_the_selection_order_does_not_matter(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, _ = await _seed(session)
        question = _multi_select_row(lesson_id)
        session.add(question)
        await session.flush()
        session_id = await _start(service, user_id, lesson_id)

        graded = await service.submit_answer(
            user_id,
            session_id,
            question_id=question.id,
            answer=SubmittedAnswer(choices=("Good morning", "Hello")),
        )

        assert graded.is_correct is True

    async def test_a_subset_is_wrong_and_the_retry_is_worth_half(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, _ = await _seed(session)
        question = _multi_select_row(lesson_id)
        session.add(question)
        await session.flush()
        session_id = await _start(service, user_id, lesson_id)

        wrong = await service.submit_answer(
            user_id,
            session_id,
            question_id=question.id,
            answer=SubmittedAnswer(choices=("Hello",)),
        )
        right = await service.submit_answer(
            user_id,
            session_id,
            question_id=question.id,
            answer=SubmittedAnswer(choices=("Hello", "Good morning")),
        )

        assert wrong.is_correct is False
        assert wrong.points_awarded == 0
        assert right.is_correct is True
        assert right.points_awarded == 1  # half of two points on the second attempt

    async def test_the_pre_attempt_payload_carries_no_key(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, _ = await _seed(session)
        question = _multi_select_row(lesson_id)
        session.add(question)
        await session.flush()

        detail = await service.start_session(user_id, mode=SessionMode.LESSON, lesson_id=lesson_id)
        served = next(q for q in detail.questions if q.id == question.id)

        assert served.kind == "multi_select"
        assert set(served.choices) == {"Hello", "Good morning", "Goodbye", "Thank you"}
        for leaked in ("correct_index", "accepted_answers", "explanation"):
            assert not hasattr(served, leaked)


def _word_bank_row(lesson_id: uuid.UUID, position: int = 5) -> QuestionModel:
    return QuestionModel(
        id=uuid.uuid4(),
        lesson_id=lesson_id,
        position=position,
        kind="word_bank",
        skill="grammar",
        prompt="Complete the sentence: 'Good ___, how are you?'",
        choices=["morning", "night", "afternoon", "teacher"],
        accepted_answers=["Morning"],
        correct_index=None,
        explanation="'Good morning' is the greeting that fits 'how are you?'.",
        points=2,
    )


class TestWordBankAnswer:
    """A word-bank item is free text with a tile bank, and the bank is never the key."""

    async def test_the_assembled_word_scores_full_points(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, _ = await _seed(session)
        question = _word_bank_row(lesson_id)
        session.add(question)
        await session.flush()
        session_id = await _start(service, user_id, lesson_id)

        graded = await service.submit_answer(
            user_id,
            session_id,
            question_id=question.id,
            answer=SubmittedAnswer(text="morning"),
        )

        assert graded.is_correct is True
        assert graded.points_awarded == 2
        # The reported key is the stored (folded) accepted answer, like fill_blank.
        assert graded.correct_answer == {"text": "morning"}

    async def test_a_distractor_tile_is_wrong_and_the_retry_is_worth_half(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, _ = await _seed(session)
        question = _word_bank_row(lesson_id)
        session.add(question)
        await session.flush()
        session_id = await _start(service, user_id, lesson_id)

        wrong = await service.submit_answer(
            user_id,
            session_id,
            question_id=question.id,
            answer=SubmittedAnswer(text="night"),
        )
        right = await service.submit_answer(
            user_id,
            session_id,
            question_id=question.id,
            answer=SubmittedAnswer(text="Morning"),
        )

        assert wrong.is_correct is False
        assert wrong.points_awarded == 0
        assert right.is_correct is True
        assert right.points_awarded == 1  # half of two points on the second attempt

    async def test_the_pre_attempt_payload_serves_shuffled_tiles_and_no_key(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        """The bank is offered to assemble from; the key stays on the server until answered."""
        user_id, lesson_id, _ = await _seed(session)
        question = _word_bank_row(lesson_id)
        session.add(question)
        await session.flush()

        detail = await service.start_session(user_id, mode=SessionMode.LESSON, lesson_id=lesson_id)
        served = next(q for q in detail.questions if q.id == question.id)

        assert served.kind == "word_bank"
        assert set(served.choices) == {"morning", "night", "afternoon", "teacher"}
        for leaked in ("correct_index", "accepted_answers", "explanation"):
            assert not hasattr(served, leaked)

    async def test_the_bank_is_reoffered_in_the_same_order_on_resume(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        """A learner who reloads sees the tiles in the layout they were already working with."""
        user_id, lesson_id, _ = await _seed(session)
        question = _word_bank_row(lesson_id)
        session.add(question)
        await session.flush()
        session_id = await _start(service, user_id, lesson_id)

        first = await service.resume_session(user_id, session_id)
        again = await service.resume_session(user_id, session_id)
        served_first = next(q for q in first.questions if q.id == question.id)
        served_again = next(q for q in again.questions if q.id == question.id)

        assert served_first.choices == served_again.choices


def _ordering_row(lesson_id: uuid.UUID, position: int = 6) -> QuestionModel:
    return QuestionModel(
        id=uuid.uuid4(),
        lesson_id=lesson_id,
        position=position,
        kind="ordering",
        skill="reading",
        prompt="Put the lines of the greeting in order.",
        choices=["Hello!", "How are you?", "I am fine.", "Goodbye!"],
        accepted_answers=[],
        correct_index=None,
        explanation="A greeting opens, asks, answers, then closes.",
        points=2,
    )


class TestOrderingAnswer:
    """An ordering answer is graded against the authored sequence, all or nothing."""

    async def test_the_authored_order_scores_full_points(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, _ = await _seed(session)
        question = _ordering_row(lesson_id)
        session.add(question)
        await session.flush()
        session_id = await _start(service, user_id, lesson_id)

        graded = await service.submit_answer(
            user_id,
            session_id,
            question_id=question.id,
            answer=SubmittedAnswer(order=("Hello!", "How are you?", "I am fine.", "Goodbye!")),
        )

        assert graded.is_correct is True
        assert graded.points_awarded == 2
        assert graded.correct_answer == {"order": "Hello! → How are you? → I am fine. → Goodbye!"}

    async def test_a_wrong_order_is_zero_and_the_retry_is_worth_half(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, _ = await _seed(session)
        question = _ordering_row(lesson_id)
        session.add(question)
        await session.flush()
        session_id = await _start(service, user_id, lesson_id)

        wrong = await service.submit_answer(
            user_id,
            session_id,
            question_id=question.id,
            answer=SubmittedAnswer(order=("Hello!", "I am fine.", "How are you?", "Goodbye!")),
        )
        right = await service.submit_answer(
            user_id,
            session_id,
            question_id=question.id,
            answer=SubmittedAnswer(order=("Hello!", "How are you?", "I am fine.", "Goodbye!")),
        )

        assert wrong.is_correct is False
        assert wrong.points_awarded == 0
        assert right.is_correct is True
        assert right.points_awarded == 1  # half of two points on the second attempt

    async def test_the_served_order_never_reveals_the_authored_order(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        """The identity permutation is the answer, so the served order must never be it."""
        user_id, lesson_id, _ = await _seed(session)
        question = _ordering_row(lesson_id)
        session.add(question)
        await session.flush()

        detail = await service.start_session(user_id, mode=SessionMode.LESSON, lesson_id=lesson_id)
        served = next(q for q in detail.questions if q.id == question.id)

        assert served.kind == "ordering"
        assert sorted(served.choices) == sorted(question.choices)
        assert tuple(served.choices) != tuple(question.choices)
        for leaked in ("correct_index", "accepted_answers", "explanation"):
            assert not hasattr(served, leaked)

    async def test_an_unknown_item_is_rejected(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, _ = await _seed(session)
        question = _ordering_row(lesson_id)
        session.add(question)
        await session.flush()
        session_id = await _start(service, user_id, lesson_id)

        with pytest.raises(ValidationFailed):
            await service.submit_answer(
                user_id,
                session_id,
                question_id=question.id,
                answer=SubmittedAnswer(order=("Hello!", "How are you?", "I am fine.", "Nonsense!")),
            )

    async def test_a_length_mismatch_is_rejected(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, _ = await _seed(session)
        question = _ordering_row(lesson_id)
        session.add(question)
        await session.flush()
        session_id = await _start(service, user_id, lesson_id)

        with pytest.raises(ValidationFailed):
            await service.submit_answer(
                user_id,
                session_id,
                question_id=question.id,
                answer=SubmittedAnswer(order=("Hello!", "How are you?")),
            )


# =================================================================== complete
class TestCompleteSession:
    async def test_completing_scores_the_session_and_records_progress(
        self, service: QuizService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        user_id, lesson_id, questions = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)
        await _answer_all(service, user_id, session_id, questions)

        completed = await service.complete_session(user_id, session_id)

        assert completed.score_percent == 100
        assert completed.session.status == SessionStatus.COMPLETED.value
        assert completed.session.xp_awarded == 6  # 2 + 3 + 1

        progress = await uow.lesson_progress.get(user_id, lesson_id)
        assert progress is not None
        assert progress.state is ProgressState.COMPLETED
        assert progress.best_score == 100
        assert progress.attempts == 1
        assert progress.xp_earned == 6

    async def test_a_partial_session_cannot_be_completed(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, questions = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)
        await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice="Hello"),
        )

        with pytest.raises(ConflictError) as raised:
            await service.complete_session(user_id, session_id)

        assert raised.value.code is ErrorCode.QUIZ_NOT_COMPLETED

    async def test_a_mixed_result_scores_the_expected_percentage(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, questions = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)
        await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice="Goodbye"),  # wrong: 0 of 2
        )
        await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[1].id,
            answer=SubmittedAnswer(text="goes"),  # correct: 3
        )
        await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[2].id,
            answer=SubmittedAnswer(choice="False"),  # correct: 1
        )

        completed = await service.complete_session(user_id, session_id)

        assert completed.score_percent == 67  # 2 of 3
        assert completed.session.xp_awarded == 4

    async def test_completing_twice_is_refused(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, questions = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)
        await _answer_all(service, user_id, session_id, questions)
        await service.complete_session(user_id, session_id)

        with pytest.raises(ConflictError) as raised:
            await service.complete_session(user_id, session_id)

        assert raised.value.code is ErrorCode.QUIZ_ALREADY_COMPLETED


# =================================================================== resume
class TestResumeSession:
    async def test_resume_lists_the_answered_questions(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, questions = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)
        await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice="Hello"),
        )

        resumed = await service.resume_session(user_id, session_id)

        assert resumed.session.answered_question_ids == (questions[0].id,)
        assert resumed.session.answered_count == 1
        assert len(resumed.questions) == 3

    async def test_resume_still_hides_the_answer_key(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, _ = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)

        resumed = await service.resume_session(user_id, session_id)

        for question in resumed.questions:
            for leaked in ("correct_index", "accepted_answers", "explanation"):
                assert not hasattr(question, leaked)

    async def test_resume_after_a_full_round_of_answers_stays_active(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, questions = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)
        await _answer_all(service, user_id, session_id, questions)

        resumed = await service.resume_session(user_id, session_id)

        assert resumed.session.status == SessionStatus.ACTIVE.value
        assert resumed.session.answered_count == 3


# ================================================================ choice reshuffle
class TestChoiceReshuffle:
    """Choices are served in a per-attempt order, so a retry is not a memory test.

    The order is a pure function of (session, question, attempt), which is what lets grading
    and presentation agree without the server storing a permutation: resume recomputes what
    it showed, and submit grades against the same recomputation.
    """

    async def test_an_untouched_question_is_shown_in_its_first_attempt_order(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, questions = await _seed(session)

        detail = await service.start_session(user_id, mode=SessionMode.LESSON, lesson_id=lesson_id)

        served = next(q for q in detail.questions if q.id == questions[0].id)
        assert served.choices == _expected_choices(detail.session.id, questions[0], FIRST_ATTEMPT)
        assert set(served.choices) == set(questions[0].choices)

    async def test_resume_repeats_the_order_without_a_new_answer(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        """A reload must not shuffle under the learner: the same attempt is the same layout."""
        user_id, lesson_id, _ = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)

        first = await service.resume_session(user_id, session_id)
        second = await service.resume_session(user_id, session_id)

        assert [q.choices for q in first.questions] == [q.choices for q in second.questions]

    async def test_a_wrong_answer_advances_the_question_to_the_next_attempt_order(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, questions = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)
        await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice="Goodbye"),  # wrong: the question stays open
        )

        resumed = await service.resume_session(user_id, session_id)

        served = next(q for q in resumed.questions if q.id == questions[0].id)
        assert served.choices == _expected_choices(session_id, questions[0], FIRST_ATTEMPT + 1)

    async def test_a_correct_answer_keeps_the_order_it_was_closed_on(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        """A closed question is only reviewed, so it holds the layout the learner answered."""
        user_id, lesson_id, questions = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)
        await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice="Hello"),  # correct: the question is closed
        )

        resumed = await service.resume_session(user_id, session_id)

        served = next(q for q in resumed.questions if q.id == questions[0].id)
        assert served.choices == _expected_choices(session_id, questions[0], FIRST_ATTEMPT)

    async def test_grading_resolves_an_index_against_the_served_order(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        """The client's index means what the client saw, not the stored order."""
        user_id, lesson_id, questions = await _seed(session)
        detail = await service.start_session(user_id, mode=SessionMode.LESSON, lesson_id=lesson_id)
        served = next(q for q in detail.questions if q.id == questions[0].id)

        index = served.choices.index("Hello")
        graded = await service.submit_answer(
            user_id,
            detail.session.id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice=str(index)),
        )

        assert graded.is_correct is True
        assert graded.correct_answer == {"choice": "Hello"}

    async def test_a_retry_is_graded_against_the_newly_served_order_and_halved(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, questions = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)
        await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice="Goodbye"),  # wrong
        )

        resumed = await service.resume_session(user_id, session_id)
        served = next(q for q in resumed.questions if q.id == questions[0].id)
        letter = "abcdefghijklmnopqrstuvwxyz"[served.choices.index("Hello")]

        graded = await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice=letter),
        )

        assert graded.is_correct is True
        assert graded.points_awarded == 1  # 2 points, halved on the retry
        assert graded.correct_answer == {"choice": "Hello"}

    async def test_each_retry_records_the_next_attempt_number(
        self, service: QuizService, session: AsyncSession, uow: SqlAlchemyUnitOfWork
    ) -> None:
        """The attempt counter is the one piece of state the reshuffle is rebuilt from."""
        user_id, lesson_id, questions = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)
        await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice="Goodbye"),
        )
        await service.submit_answer(
            user_id,
            session_id,
            question_id=questions[0].id,
            answer=SubmittedAnswer(choice="Goodbye"),
        )
        await service.submit_answer(
            user_id, session_id, question_id=questions[0].id, answer=SubmittedAnswer(choice="Hello")
        )

        stored = await uow.quiz_answers.list_for_session(session_id)
        answer = next(a for a in stored if a.question_id == questions[0].id)

        assert len(stored) == 1  # replaced in place, not appended
        assert answer.attempt == 3
        assert answer.is_correct is True
        assert answer.points_awarded == 1


# =================================================================== expiry
class TestExpiry:
    @staticmethod
    def _expiring(uow: SqlAlchemyUnitOfWork) -> QuizService:
        return QuizService(uow, get_settings().model_copy(update={"quiz_session_ttl_seconds": -1}))

    async def test_an_expired_session_refuses_an_answer(
        self, uow: SqlAlchemyUnitOfWork, session: AsyncSession
    ) -> None:
        expiring = self._expiring(uow)
        user_id, lesson_id, questions = await _seed(session)
        detail = await expiring.start_session(user_id, mode=SessionMode.LESSON, lesson_id=lesson_id)

        with pytest.raises(ConflictError) as raised:
            await expiring.submit_answer(
                user_id,
                detail.session.id,
                question_id=questions[0].id,
                answer=SubmittedAnswer(choice="Hello"),
            )

        assert raised.value.code is ErrorCode.QUIZ_SESSION_EXPIRED

    async def test_expiry_is_persisted_not_re_derived(
        self, uow: SqlAlchemyUnitOfWork, session: AsyncSession
    ) -> None:
        expiring = self._expiring(uow)
        user_id, lesson_id, _ = await _seed(session)
        detail = await expiring.start_session(user_id, mode=SessionMode.LESSON, lesson_id=lesson_id)

        with pytest.raises(ConflictError):
            await expiring.resume_session(user_id, detail.session.id)

        stored = await uow.quiz_sessions.get(detail.session.id)
        assert stored is not None
        assert stored.status is SessionStatus.EXPIRED

    async def test_an_expired_session_cannot_be_resumed(
        self, uow: SqlAlchemyUnitOfWork, session: AsyncSession
    ) -> None:
        expiring = self._expiring(uow)
        user_id, lesson_id, _ = await _seed(session)
        detail = await expiring.start_session(user_id, mode=SessionMode.LESSON, lesson_id=lesson_id)

        with pytest.raises(ConflictError) as raised:
            await expiring.resume_session(user_id, detail.session.id)

        assert raised.value.code is ErrorCode.QUIZ_SESSION_EXPIRED


# =================================================================== abandon
class TestAbandonSession:
    async def test_abandoning_ends_the_session(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, _ = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)

        view = await service.abandon_session(user_id, session_id)

        assert view.status == SessionStatus.ABANDONED.value

    async def test_an_abandoned_session_refuses_a_late_answer(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, questions = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)
        await service.abandon_session(user_id, session_id)

        with pytest.raises(ConflictError) as raised:
            await service.submit_answer(
                user_id,
                session_id,
                question_id=questions[0].id,
                answer=SubmittedAnswer(choice="Hello"),
            )

        assert raised.value.code is ErrorCode.QUIZ_SESSION_EXPIRED

    async def test_abandoning_twice_is_a_no_op(
        self, service: QuizService, session: AsyncSession
    ) -> None:
        user_id, lesson_id, _ = await _seed(session)
        session_id = await _start(service, user_id, lesson_id)

        first = await service.abandon_session(user_id, session_id)
        second = await service.abandon_session(user_id, session_id)

        assert first.status == second.status == SessionStatus.ABANDONED.value


# ------------------------------------------------------- concurrent submissions
@pytest.mark.asyncio
async def test_losing_the_insert_race_returns_the_winners_verdict(
    service: QuizService,
    session: AsyncSession,
    uow: SqlAlchemyUnitOfWork,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two taps on one question must not become a 500 or a second award.

    The read-then-insert path is what both requests take. Here the second request's
    *read* is forced to miss the row the first already committed, so its INSERT hits
    ``uq_quiz_answers_session_question``; the service must return the stored verdict
    rather than propagate an IntegrityError.
    """
    user_id, lesson_id, questions = await _seed(session)
    session_id = await _start(service, user_id, lesson_id)
    first = await service.submit_answer(
        user_id,
        session_id,
        question_id=questions[0].id,
        answer=SubmittedAnswer(choice="Goodbye"),
    )
    assert first.is_correct is False
    assert first.correct_count == 0

    repository = uow.quiz_answers
    real_get = repository.get
    calls = {"count": 0}

    async def _miss_once(session_arg: uuid.UUID, question_arg: uuid.UUID) -> object:
        calls["count"] += 1
        if calls["count"] == 1:
            return None
        return await real_get(session_arg, question_arg)

    monkeypatch.setattr(repository, "get", _miss_once)

    second = await service.submit_answer(
        user_id,
        session_id,
        question_id=questions[0].id,
        answer=SubmittedAnswer(choice="Hello"),
    )

    assert second.is_correct is False, "the first recorded answer wins"
    assert second.points_awarded == 0
    assert second.correct_count == 0, "the loser must not award a second correct answer"
    assert second.answered_count == 1
