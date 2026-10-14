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
from app.core.time import utc_now
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

__all__ = ["QuizService"]


class QuizService:
    def __init__(self, uow: UnitOfWorkProtocol, settings: Settings) -> None:
        self._uow = uow
        self._settings = settings

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
            questions=tuple(_question_view(question) for question in questions),
        )

    # =============================================================== resume
    async def resume_session(self, user_id: UserId, session_id: uuid.UUID) -> SessionDetailView:
        """Re-open a sitting after the app was killed, or to show a finished one.

        A completed or abandoned session is returned as it stands so the client can render
        its result; only an expired lease is refused, because there is nothing left to
        resume.
        """
        session = await self._require_owned(user_id, session_id)
        await self._expire_if_due(session)

        questions = await self._load_session_questions(session)
        answers = await self._uow.quiz_answers.list_for_session(session.id)
        answered = tuple(answer.question_id for answer in answers)

        return SessionDetailView(
            session=_session_view(session, answered_question_ids=answered),
            questions=tuple(_question_view(question) for question in questions),
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

        # A first answer is attempt one; re-answering a question that was missed is a retry,
        # and a correct retry is worth half (see ``domain.policy.points_awarded``).
        attempt = FIRST_ATTEMPT if prior is None else FIRST_ATTEMPT + 1
        graded = grade_answer(_gradeable(question), answer, attempt=attempt)

        resolved = QuizAnswer(
            id=prior.id if prior is not None else new_id(),
            session_id=session.id,
            question_id=question.id,
            position=question.position,
            given_answer=_given_answer(answer),
            is_correct=graded.is_correct,
            points_awarded=graded.points_awarded,
            explanation=graded.explanation,
        )
        if prior is None:
            await self._uow.quiz_answers.add(resolved)
        else:
            # Overwritten, not appended: the unique constraint keeps one row per question,
            # so a retry cannot inflate the answered count the completion check relies on.
            await self._uow.quiz_answers.save(resolved)
        if graded.is_correct and (prior is None or not prior.is_correct):
            session.register_correct()
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
        await self._record_progress(
            user_id, session.lesson_id, score_percent=score_percent, xp=xp_awarded
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
    ) -> None:
        if lesson_id is None:  # pragma: no cover - lesson mode always sets it
            return
        progress = await self._uow.lesson_progress.get(user_id, lesson_id)
        if progress is None:
            progress = LessonProgress(id=new_id(), user_id=user_id, lesson_id=lesson_id)
        progress.record_completion(score_percent=score_percent, xp=xp)
        await self._uow.lesson_progress.save(progress)


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


def _given_answer(answer: SubmittedAnswer) -> dict[str, str]:
    if answer.choice is not None:
        return {"choice": answer.choice}
    return {"text": answer.text or ""}


def _gradeable(question: Question) -> GradeableQuestion:
    return GradeableQuestion(
        kind=question.kind,
        choices=question.choices,
        correct_index=question.correct_index,
        accepted_answers=question.accepted_answers,
        points=question.points,
        explanation=question.explanation,
    )


def _question_view(question: Question) -> QuestionView:
    """Project a question onto what a learner may see *before* answering.

    Built by omission: no ``correct_index``, no ``accepted_answers``, no ``explanation``.
    The media aids stay, because they teach the item rather than reveal the answer.
    """
    return QuestionView(
        id=question.id,
        position=question.position,
        kind=question.kind,
        skill=question.skill,
        prompt=question.prompt,
        choices=question.choices,
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
