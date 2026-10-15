"""Attempt-engine use cases: the session lifecycle and the grading call.

The flow is start → answer (n times) → complete, with resume and abandon as the two ways a
sitting ends without a score. Every rule lives here rather than in the route, so "may this
question be answered", "is it too late" and "what does it score" can be exercised without
an HTTP request.

Two invariants are worth naming:

* **Grading is server-side.** The service loads the question with its answer key, compares
  the submission in ``domain/policy.py`` and returns the verdict. The key is never part of
  a session payload, so a client cannot learn an answer before committing to one.
* **A session's question set is frozen.** ``question_ids`` is captured at start and never
  re-read from the lesson, so editing the lesson mid-session cannot change what is scored.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from app.core.config import Settings
from app.core.errors import ConflictError, ErrorCode, NotFoundError, ValidationFailed
from app.core.events import EventBus, LessonCompleted
from app.core.time import ensure_aware, utc_now
from app.core.types import ContentId, UserId, new_id
from app.core.uow import UnitOfWorkProtocol
from app.modules.content.domain.entities import Question
from app.modules.quiz.application.dto import (
    CompletedSessionView,
    GradedAnswerView,
    QuestionView,
    SessionDetailView,
    SessionView,
)
from app.modules.quiz.domain.entities import (
    LessonProgress,
    ProgressState,
    QuizAnswer,
    QuizSession,
    SessionMode,
    SessionStatus,
)
from app.modules.quiz.domain.policy import (
    FIRST_ATTEMPT,
    GradeableQuestion,
    SubmittedAnswer,
    grade_answer,
)
from app.modules.quiz.domain.presentation import (
    choice_order,
    order_choices,
    order_correct_index,
    question_seed,
)
from app.modules.quiz.domain.review import new_card

__all__ = ["QuizService"]


class QuizService:
    def __init__(
        self,
        uow: UnitOfWorkProtocol,
        settings: Settings,
        *,
        bus: EventBus | None = None,
    ) -> None:
        self._uow = uow
        self._settings = settings
        #: Optional on purpose: the event bus is a *side channel*, not a dependency of the
        #: engine. A completion is a complete unit of work with or without a subscriber, so a
        #: test (or a future worker) can construct the service with no bus and the lesson
        #: still scores. When a bus is present, ``complete_session`` publishes one
        #: ``LessonCompleted`` and the gamification subscriber folds it into the projection.
        self._bus = bus

    # ================================================================ start
    async def start_session(
        self,
        user_id: UserId,
        *,
        mode: SessionMode,
        lesson_id: uuid.UUID | None,
    ) -> SessionDetailView:
        """Open a sitting over a published lesson's questions.

        The lesson must be published and carry at least one question. An unpublishable
        attempt (a lesson with no questions) is refused here rather than handed to a client
        that would show an empty quiz.
        """
        if mode is not SessionMode.LESSON:
            raise ValidationFailed(
                "Only lesson sessions can be started today.",
                code=ErrorCode.VALIDATION_ERROR,
                details={"field": "mode"},
            )
        if lesson_id is None:
            raise ValidationFailed(
                "A lesson session needs a lessonId.",
                code=ErrorCode.VALIDATION_ERROR,
                details={"field": "lessonId"},
            )

        lesson = await self._uow.lessons.get_published(ContentId(lesson_id))
        if lesson is None:
            raise NotFoundError(
                "There is no published lesson with that id.",
                code=ErrorCode.LESSON_NOT_FOUND,
            )

        questions = await self._uow.questions.list_for_lessons([lesson.id])
        if not questions:
            raise NotFoundError(
                "That lesson has no questions to attempt yet.",
                code=ErrorCode.LESSON_NOT_AVAILABLE,
            )

        now = utc_now()
        session = QuizSession(
            id=new_id(),
            user_id=user_id,
            mode=mode,
            lesson_id=lesson.id,
            question_ids=tuple(question.id for question in questions),
            started_at=now,
            expires_at=now + timedelta(seconds=self._settings.quiz_session_ttl_seconds),
        )
        await self._uow.quiz_sessions.add(session)
        await self._start_progress(user_id, lesson.id)
        await self._uow.commit()

        return SessionDetailView(
            session=_session_view(session, answered_question_ids=()),
            questions=tuple(
                _question_view(question, order=_choice_order(session.id, question, FIRST_ATTEMPT))
                for question in questions
            ),
        )

    # =============================================================== resume
    async def resume_session(self, user_id: UserId, session_id: uuid.UUID) -> SessionDetailView:
        """Re-open a sitting after the app was killed, or to show a finished one.

        A completed or abandoned session is returned as it stands so the client can render
        its result; only an expired lease is refused, because there is nothing left to
        resume.

        The choices are re-served in the order the learner is on: the first attempt's order
        for an untouched question, and the *next* attempt's order for one that was missed —
        so a client that reloads after a wrong answer sees a fresh layout, and a client that
        only reloads sees the one it already had.
        """
        session = await self._require_owned(user_id, session_id)
        await self._expire_if_due(session)

        questions = await self._load_session_questions(session)
        answers = await self._uow.quiz_answers.list_for_session(session.id)
        answered = tuple(answer.question_id for answer in answers)
        by_question = {answer.question_id: answer for answer in answers}

        return SessionDetailView(
            session=_session_view(session, answered_question_ids=answered),
            questions=tuple(
                _question_view(
                    question,
                    order=_choice_order(
                        session.id, question, _display_attempt(by_question.get(question.id))
                    ),
                )
                for question in questions
            ),
        )

    # =============================================================== answer
    async def submit_answer(
        self,
        user_id: UserId,
        session_id: uuid.UUID,
        *,
        question_id: uuid.UUID,
        answer: SubmittedAnswer,
    ) -> GradedAnswerView:
        """Grade one answer and return the verdict.

        ``correctAnswer`` and ``explanation`` are produced only here, after the learner has
        committed — that ordering is the contract, not a nicety.

        A question that was answered **wrongly** may be answered again, which is how the
        halved-points rule is reached: the retry is graded by ``domain.policy`` as a second
        attempt, so a correct retry is worth half. A question already answered *correctly*
        is closed, so a learner cannot re-open one for another go.
        """
        session = await self._require_owned(user_id, session_id)
        await self._expire_if_due(session)
        _require_answerable(session)

        if not session.contains(question_id):
            raise NotFoundError(
                "That question is not part of this session.",
                code=ErrorCode.QUESTION_NOT_IN_SESSION,
            )
        prior = await self._uow.quiz_answers.get(session.id, question_id)
        if prior is not None and prior.is_correct:
            raise ConflictError(
                "That question has already been answered correctly.",
                code=ErrorCode.QUESTION_ALREADY_ANSWERED,
            )

        question = await self._uow.questions.get_published(ContentId(question_id))
        if question is None:
            raise NotFoundError(
                "There is no published question with that id.",
                code=ErrorCode.QUESTION_NOT_FOUND,
            )

        # A first answer is attempt one; every re-answer of a missed question is one more.
        # The attempt number decides two things: a correct retry is worth half (see
        # ``domain.policy.points_awarded``), and the choices are served in that attempt's
        # order, so grading is done against exactly the layout the learner is looking at.
        attempt = _next_attempt(prior)
        order = _choice_order(session.id, question, attempt)
        graded = grade_answer(_gradeable(question, order=order), answer, attempt=attempt)

        resolved = QuizAnswer(
            id=prior.id if prior is not None else new_id(),
            session_id=session.id,
            question_id=question.id,
            position=question.position,
            given_answer=_given_answer(answer),
            is_correct=graded.is_correct,
            points_awarded=graded.points_awarded,
            explanation=graded.explanation,
            attempt=attempt,
        )
        if prior is None:
            await self._uow.quiz_answers.add(resolved)
        else:
            # Overwritten, not appended: the unique constraint keeps one row per question,
            # so a retry cannot inflate the answered count the completion check relies on.
            await self._uow.quiz_answers.save(resolved)
        if graded.is_correct and (prior is None or not prior.is_correct):
            session.register_correct()
            await self._ensure_review_card(user_id, question_id)
        await self._uow.quiz_sessions.save(session)
        await self._uow.commit()

        answered_count = await self._uow.quiz_answers.count_for_session(session.id)
        return GradedAnswerView(
            is_correct=graded.is_correct,
            correct_answer=graded.correct_answer,
            explanation=graded.explanation,
            points_awarded=graded.points_awarded,
            answered_count=answered_count,
            total_count=session.total,
            correct_count=session.correct_count,
            is_complete=answered_count >= session.total,
        )

    # ============================================================= complete
    async def complete_session(
        self, user_id: UserId, session_id: uuid.UUID
    ) -> CompletedSessionView:
        """Finish a fully answered sitting, score it and fold it into progress.

        Refuses a partial session so a score always describes a whole lesson; the client
        finishes the remaining questions first.
        """
        session = await self._require_owned(user_id, session_id)
        await self._expire_if_due(session)
        _require_answerable(session)

        answers = await self._uow.quiz_answers.list_for_session(session.id)
        if len(answers) < session.total:
            raise ConflictError(
                "Answer every question before completing the session.",
                code=ErrorCode.QUIZ_NOT_COMPLETED,
            )

        correct_count = sum(1 for answer in answers if answer.is_correct)
        xp_awarded = sum(answer.points_awarded for answer in answers)
        score_percent = _percentage(correct_count, session.total)

        session.complete(xp_awarded=xp_awarded)
        await self._uow.quiz_sessions.save(session)
        is_retry = await self._record_progress(
            user_id, session.lesson_id, score_percent=score_percent, xp=xp_awarded
        )
        await self._publish_completion(
            user_id,
            session,
            score_percent=score_percent,
            correct_count=correct_count,
            xp_awarded=xp_awarded,
            is_retry=is_retry,
        )
        await self._uow.commit()

        return CompletedSessionView(
            session=_session_view(
                session,
                answered_question_ids=tuple(answer.question_id for answer in answers),
            ),
            score_percent=score_percent,
        )

    # ============================================================== abandon
    async def abandon_session(self, user_id: UserId, session_id: uuid.UUID) -> SessionView:
        """End a sitting without a score. Idempotent once abandoned."""
        session = await self._require_owned(user_id, session_id)
        if session.status is SessionStatus.COMPLETED:
            raise ConflictError(
                "This session is already complete.", code=ErrorCode.QUIZ_ALREADY_COMPLETED
            )
        if session.status is not SessionStatus.ABANDONED:
            session.abandon()
            await self._uow.quiz_sessions.save(session)
            await self._uow.commit()

        answers = await self._uow.quiz_answers.list_for_session(session.id)
        return _session_view(
            session, answered_question_ids=tuple(answer.question_id for answer in answers)
        )

    # ============================================================= internals
    async def _require_owned(self, user_id: UserId, session_id: uuid.UUID) -> QuizSession:
        session = await self._uow.quiz_sessions.get_for_user(session_id, user_id)
        if session is None:
            raise NotFoundError(
                "There is no such quiz session.",
                code=ErrorCode.QUIZ_SESSION_NOT_FOUND,
            )
        return session

    async def _expire_if_due(self, session: QuizSession) -> None:
        """Turn a stale lease into a terminal state and refuse the operation.

        Persisted before raising so the expiry is durable: the next read sees ``expired``
        instead of re-deriving it, and the client is told once rather than on every retry.
        """
        if session.status is SessionStatus.ACTIVE and session.is_expired():
            session.mark_expired()
            await self._uow.quiz_sessions.save(session)
            await self._uow.commit()
            raise ConflictError(
                "This quiz session has expired. Start the lesson again.",
                code=ErrorCode.QUIZ_SESSION_EXPIRED,
            )

    async def _load_session_questions(self, session: QuizSession) -> list[Question]:
        """The session's questions, in the order frozen at start.

        Re-read from the lesson but re-ordered by ``question_ids``, and filtered to ids
        that are still present, so a question retired mid-session is skipped rather than
        resurrecting a stale row.
        """
        if session.lesson_id is None:
            return []
        questions = await self._uow.questions.list_for_lessons([ContentId(session.lesson_id)])
        by_id = {question.id: question for question in questions}
        wanted = (ContentId(question_id) for question_id in session.question_ids)
        return [by_id[question_id] for question_id in wanted if question_id in by_id]

    async def _start_progress(self, user_id: UserId, lesson_id: uuid.UUID) -> None:
        progress = await self._uow.lesson_progress.get(user_id, lesson_id)
        if progress is None:
            progress = LessonProgress(id=new_id(), user_id=user_id, lesson_id=lesson_id)
        elif progress.state is ProgressState.COMPLETED:
            return
        progress.start()
        await self._uow.lesson_progress.save(progress)

    async def _record_progress(
        self,
        user_id: UserId,
        lesson_id: uuid.UUID | None,
        *,
        score_percent: int,
        xp: int,
    ) -> bool:
        """Fold the completion into the learner's progress row, returning ``is_retry``.

        ``is_retry`` is true when the lesson was already complete before this sitting — the
        learner replaying a lesson they have finished. It only matters for the event, but the
        fact is known here (where the prior state is read) and nowhere else, so it is
        returned rather than re-derived by the caller.
        """
        if lesson_id is None:  # pragma: no cover - lesson mode always sets it
            return False
        progress = await self._uow.lesson_progress.get(user_id, lesson_id)
        if progress is None:
            progress = LessonProgress(id=new_id(), user_id=user_id, lesson_id=lesson_id)
        is_retry = progress.state is ProgressState.COMPLETED
        progress.record_completion(score_percent=score_percent, xp=xp)
        await self._uow.lesson_progress.save(progress)
        return is_retry

    async def _publish_completion(
        self,
        user_id: UserId,
        session: QuizSession,
        *,
        score_percent: int,
        correct_count: int,
        xp_awarded: int,
        is_retry: bool,
    ) -> None:
        """Announce a completed lesson to subscribers — before the commit, on the same session.

        Two details are load-bearing:

        * **Before the commit.** The gamification subscriber writes through this same unit of
          work; publishing first means the completion and the XP/streak/badges it earned land
          in one transaction, so a subscriber failure rolls the whole thing back rather than
          leaving a scored lesson whose rewards vanished.
        * **After an explicit flush.** The session is configured with ``autoflush=False``, so
          the progress row and the session's ``completed_at`` are still pending in the
          session. The subscriber reads both to project stats and completion times; without
          the flush it would not see the very completion it is recording.
        """
        if self._bus is None or session.lesson_id is None:
            return
        await self._uow.flush()
        await self._bus.publish(
            LessonCompleted(
                user_id=user_id,
                lesson_id=session.lesson_id,
                score_percent=score_percent,
                correct_count=correct_count,
                total_count=session.total,
                duration_seconds=_duration_seconds(session),
                xp_awarded=xp_awarded,
                is_retry=is_retry,
            )
        )

    async def _ensure_review_card(self, user_id: UserId, question_id: uuid.UUID) -> None:
        """Seed a spaced-repetition card the first time a question is answered correctly.

        Idempotent: the lookup by ``(user, question)`` is checked before inserting, so
        answering the same question correctly in a later lesson does not open a second card
        — and the unique constraint backs that up at the database if two requests race. The
        card is left ``NEW`` with a due date one day out; the SM-2 schedule only starts when
        the learner reviews it.
        """
        existing = await self._uow.review_cards.get_for_question(user_id, question_id)
        if existing is not None:
            return
        await self._uow.review_cards.add(new_card(user_id, question_id))


# ------------------------------------------------------------------ helpers
def _require_answerable(session: QuizSession) -> None:
    """Refuse a sitting that can no longer be answered.

    ``EXPIRED`` is checked even though ``_expire_if_due`` normally catches a stale lease
    first, because a session expired by an *earlier* request is read back with that status
    already set and ``_expire_if_due`` only acts on a still-``ACTIVE`` one. Without this
    branch the durable expiry the engine goes out of its way to persist would be
    unenforceable: the next request would happily accept an answer or complete the sitting.

    Both terminal non-completed states report ``QUIZ_SESSION_EXPIRED``, because from a
    client's side "you may not answer this" is one condition; only the message says why.
    """
    if session.status is SessionStatus.COMPLETED:
        raise ConflictError(
            "This session is already complete.", code=ErrorCode.QUIZ_ALREADY_COMPLETED
        )
    if session.status is SessionStatus.ABANDONED:
        raise ConflictError("This session was abandoned.", code=ErrorCode.QUIZ_SESSION_EXPIRED)
    if session.status is SessionStatus.EXPIRED:
        raise ConflictError(
            "This quiz session has expired. Start the lesson again.",
            code=ErrorCode.QUIZ_SESSION_EXPIRED,
        )


def _percentage(correct: int, total: int) -> int:
    if total <= 0:  # pragma: no cover - a session always has at least one question
        return 0
    return round(correct / total * 100)


def _duration_seconds(session: QuizSession) -> int:
    """Wall-clock length of a finished sitting, floored at zero.

    Both ends are normalised: a session read back on this request has a naive ``started_at``
    (SQLite drops tzinfo on the way out) while ``completed_at``, set in memory by
    ``session.complete()``, is timezone-aware. Subtracting the two raw would raise.
    """
    started = ensure_aware(session.started_at)
    completed = ensure_aware(session.completed_at) if session.completed_at else utc_now()
    return max(0, int((completed - started).total_seconds()))


def _given_answer(answer: SubmittedAnswer) -> dict[str, str]:
    if answer.choice is not None:
        return {"choice": answer.choice}
    return {"text": answer.text or ""}


def _next_attempt(prior: QuizAnswer | None) -> int:
    """Which try the learner is about to make: the first, or one past what they have done."""
    return FIRST_ATTEMPT if prior is None else prior.attempt + 1


def _display_attempt(prior: QuizAnswer | None) -> int:
    """The attempt whose choice order to serve for a question in a session view.

    An untouched question is shown for the first attempt. A question that was missed is
    shown for the retry that follows, so reloading after a wrong answer hands back the fresh
    layout rather than the one already seen. A question already answered correctly is closed
    and keeps the order it was closed on, so a review screen matches what the learner saw.
    """
    if prior is None:
        return FIRST_ATTEMPT
    if prior.is_correct:
        return prior.attempt
    return prior.attempt + 1


def _choice_order(session_id: uuid.UUID, question: Question, attempt: int) -> tuple[int, ...]:
    """The display order for a question's choices on a given attempt.

    Free-text questions carry no choices, so there is no order to compute; the empty tuple
    flows through :func:`order_choices` unchanged.
    """
    if not question.choices:
        return ()
    seed = question_seed(session_id=session_id, question_id=question.id, attempt=attempt)
    return choice_order(len(question.choices), seed=seed)


def _gradeable(question: Question, *, order: tuple[int, ...]) -> GradeableQuestion:
    """Project a question onto grading, in the order the learner was shown.

    Choice questions are translated into display space — the choices permuted and the
    correct index remapped — so a letter or index the client derived from what it saw
    resolves correctly. Free-text items have no order and pass through untouched.
    """
    if order and question.choices:
        choices = order_choices(question.choices, order)
        correct_index = order_correct_index(order, question.correct_index)
    else:
        choices = question.choices
        correct_index = question.correct_index
    return GradeableQuestion(
        kind=question.kind,
        choices=choices,
        correct_index=correct_index,
        accepted_answers=question.accepted_answers,
        points=question.points,
        explanation=question.explanation,
    )


def _question_view(question: Question, *, order: tuple[int, ...]) -> QuestionView:
    """Project a question onto what a learner may see *before* answering.

    Built by omission: no ``correct_index``, no ``accepted_answers``, no ``explanation``.
    The media aids stay, because they teach the item rather than reveal the answer. Choices
    are served in ``order``, a per-attempt permutation, so the correct option's position is
    not the stable thing a learner could carry from one try to the next.
    """
    return QuestionView(
        id=question.id,
        position=question.position,
        kind=question.kind,
        skill=question.skill,
        prompt=question.prompt,
        choices=order_choices(question.choices, order),
        points=question.points,
        image_url=question.image_url,
        audio_url=question.audio_url,
    )


def _session_view(
    session: QuizSession, *, answered_question_ids: tuple[uuid.UUID, ...]
) -> SessionView:
    return SessionView(
        id=session.id,
        mode=session.mode.value,
        lesson_id=session.lesson_id,
        status=session.status.value,
        started_at=session.started_at,
        expires_at=session.expires_at,
        completed_at=session.completed_at,
        total_count=session.total,
        answered_count=len(answered_question_ids),
        correct_count=session.correct_count,
        xp_awarded=session.xp_awarded,
        answered_question_ids=answered_question_ids,
    )
