"""Attempt-engine use cases: the session lifecycle and the grading call.

The flow is start → answer (n times) → complete, with resume and abandon as the two ways a
sitting ends without a score. Every rule lives here rather than in the route, so "may this
question be answered", "is it too late" and "what does it score" can be exercised without
an HTTP request.

Two session shapes share that lifecycle: a **lesson** session over one published lesson's
questions, and a **checkpoint** — a weighted sample across a unit's lessons that a learner
sits once every lesson in the unit is done, and which unlocks the next unit at 80%. The
checkpoint adds gates (is the unit open, are its lessons finished) and a scorer's side
effect (the unit-progress row), but the attempt engine below answer/complete is mode-blind:
a frozen question list is a frozen question list.

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
from app.core.errors import (
    ConflictError,
    ErrorCode,
    ForbiddenError,
    NotFoundError,
    ValidationFailed,
)
from app.core.events import EventBus, LessonCompleted
from app.core.time import ensure_aware, utc_now
from app.core.types import ContentId, QuestionKind, UserId, new_id
from app.core.uow import UnitOfWorkProtocol
from app.modules.content.domain.entities import Question
from app.modules.content.domain.policy import (
    matching_answer_positions,
    pre_attempt_prompts,
)
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
    UnitProgress,
)
from app.modules.quiz.domain.policy import (
    FIRST_ATTEMPT,
    GradeableQuestion,
    SubmittedAnswer,
    grade_answer,
)
from app.modules.quiz.domain.presentation import (
    choice_order,
    deranged_order,
    order_choices,
    order_correct_index,
    ordering_order,
    question_seed,
)
from app.modules.quiz.domain.review import new_card
from app.modules.quiz.domain.selection import (
    QuestionStat,
    card_is_easy,
    select_checkpoint_questions,
)
from app.modules.quiz.domain.unlock import (
    CHECKPOINT_PASSING_SCORE,
    next_unit_id,
    unit_locked,
)

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
        unit_id: uuid.UUID | None = None,
    ) -> SessionDetailView:
        """Open a sitting: one published lesson's questions, or one unit's checkpoint.

        A lesson session needs a published ``lessonId`` carrying at least one question.
        A checkpoint session needs a ``unitId`` instead and passes three gates — the unit
        must be published, open under the sequential-unlock rule, and finished (every
        published lesson complete) — before its weighted sample is drawn. An unpublishable
        attempt is refused here rather than handed to a client that would show an empty
        quiz.

        The field each mode does *not* use is ignored rather than rejected: ``lessonId``
        on a checkpoint and ``unitId`` on a lesson describe the other mode, and the
        session records only what its own mode says, so the stray field cannot smuggle a
        second scope into a sitting.
        """
        if mode is SessionMode.CHECKPOINT:
            if unit_id is None:
                raise ValidationFailed(
                    "A checkpoint session needs a unitId.",
                    code=ErrorCode.VALIDATION_ERROR,
                    details={"field": "unitId"},
                )
            return await self._start_checkpoint(user_id, unit_id)
        if mode is not SessionMode.LESSON:
            raise ValidationFailed(
                "Only lesson and checkpoint sessions can be started today.",
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
        await self._assert_unit_open(user_id, lesson.unit_id)

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

        Refuses a partial session so a score always describes a whole sitting; the client
        finishes the remaining questions first. A lesson's completion lands on its
        progress row; a checkpoint's lands on the unit's row instead, where the 80% bar
        decides whether ``passed_at`` latches and the next unit opens. Both verdicts come
        back on the response so one client branch renders either result.
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

        # Practice XP is a lesson ledger — ``lesson_progress.xp_earned`` backs the
        # learner's total and the gamification projection, while completed sessions back
        # admin stats — so a checkpoint sitting completes with no XP of its own rather
        # than earning a number only one of those roll-ups would see. A checkpoint is a
        # gate: its reward is the unlock, and (with the gamification phase) an explicit
        # passing bonus announced as its own event.
        is_checkpoint = session.mode is SessionMode.CHECKPOINT
        session.complete(xp_awarded=0 if is_checkpoint else xp_awarded)
        await self._uow.quiz_sessions.save(session)

        passed: bool | None = None
        unlocked_unit_id: uuid.UUID | None = None
        is_retry = False
        if is_checkpoint and session.unit_id is not None:
            passed, unlocked_unit_id = await self._record_unit_progress(
                user_id, session.unit_id, score_percent=score_percent
            )
        else:
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
            passed=passed,
            unlocked_unit_id=unlocked_unit_id,
            correct_question_ids=tuple(
                answer.question_id for answer in answers if answer.is_correct
            ),
            missed_question_ids=tuple(
                answer.question_id for answer in answers if not answer.is_correct
            ),
            retry_available=any(not answer.is_correct for answer in answers),
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
    async def _assert_unit_open(self, user_id: UserId, unit_id: uuid.UUID) -> None:
        """Refuse a sitting in a unit the sequential-unlock rule has not reached.

        The hard half of the lock: the read-only views grey a shut unit out, but a client
        that calls the API directly is stopped here, so "locked" is a rule and not a hint.
        Both facts the rule needs — the published order and what the learner has passed —
        are read here rather than passed in, so every caller agrees on the chain.
        """
        ordered = await self._uow.units.list_published_ordered()
        progress_rows = await self._uow.unit_progress.list_for_user(user_id)
        passed_units = {row.unit_id for row in progress_rows if row.has_passed}
        if unit_locked(unit_id, [item.unit.id for item in ordered], passed_units):
            raise ForbiddenError(
                "Pass the previous unit's checkpoint to unlock this unit.",
                code=ErrorCode.LESSON_LOCKED,
            )

    async def _start_checkpoint(self, user_id: UserId, unit_id: uuid.UUID) -> SessionDetailView:
        """Open a sitting over a weighted sample of one unit's questions.

        Three gates, each its own error so a client can say the right thing:

        1. **Existence** (404) is answered *from the published order* — the same read
           that settles "which unit", "is it shut" and "what would it unlock" at once, so
           the three answers can never disagree. A unit under a draft level is absent
           from that order and reads as not found, inheriting visibility like everything
           else in the catalogue.
        2. **The sequential-unlock rule** (403): the predecessor must be passed.
        3. **Readiness** (409): every published lesson of the unit complete, so the
           sample's coverage promise — one question per lesson — describes lessons the
           learner has actually met.

        After the gates the facts are gathered (answer history, review-card state, the
        unit's questions) and handed to the pure selector in ``domain/selection.py``.
        Its output is frozen into the session exactly as a lesson's question list is.
        """
        ordered = await self._uow.units.list_published_ordered()
        placement = next((item for item in ordered if item.unit.id == unit_id), None)
        if placement is None:
            raise NotFoundError(
                "There is no published unit with that id.",
                code=ErrorCode.UNIT_NOT_FOUND,
            )

        # The same rule ``_assert_unit_open`` applies, judged against the order already read
        # for the existence check above rather than fetched a second time.
        progress_rows = await self._uow.unit_progress.list_for_user(user_id)
        passed_units = {row.unit_id for row in progress_rows if row.has_passed}
        if unit_locked(unit_id, [item.unit.id for item in ordered], passed_units):
            raise ForbiddenError(
                "Pass the previous unit's checkpoint to unlock this unit.",
                code=ErrorCode.LESSON_LOCKED,
            )

        lesson_counts = await self._uow.lessons.count_published_by_unit()
        lessons_total = lesson_counts.get(ContentId(unit_id), 0)
        if lessons_total == 0:
            raise ConflictError(
                "This unit has no published lessons to be examined on yet.",
                code=ErrorCode.CHECKPOINT_NOT_READY,
            )
        lessons = await self._uow.lessons.list_published_for_unit(
            ContentId(unit_id), limit=lessons_total, after=None
        )

        lesson_progress = await self._uow.lesson_progress.list_for_user(user_id)
        completed = {
            row.lesson_id for row in lesson_progress if row.state is ProgressState.COMPLETED
        }
        lessons_remaining = sum(1 for lesson in lessons if lesson.id not in completed)
        if lessons_remaining:
            raise ConflictError(
                "Finish every lesson in this unit before sitting its checkpoint.",
                code=ErrorCode.CHECKPOINT_NOT_READY,
                details={"lessons_remaining": lessons_remaining},
            )

        questions = await self._uow.questions.list_for_lessons(
            [ContentId(lesson.id) for lesson in lessons]
        )
        if not questions:
            raise NotFoundError(
                "This unit has no questions to attempt yet.",
                code=ErrorCode.LESSON_NOT_AVAILABLE,
            )

        selected_ids = select_checkpoint_questions(
            await self._checkpoint_stats(user_id, questions)
        )
        by_id = {question.id: question for question in questions}
        # The coverage pass guarantees a question exists for every lesson *that has one*;
        # a published lesson with no questions cannot be represented and the sample still
        # stands. Filtering is the same freeze-safety resume applies: an id that vanished
        # between selection and insert is skipped, never resurrected.
        ordered_questions = [
            by_id[ContentId(question_id)]
            for question_id in selected_ids
            if ContentId(question_id) in by_id
        ]

        now = utc_now()
        session = QuizSession(
            id=new_id(),
            user_id=user_id,
            mode=SessionMode.CHECKPOINT,
            lesson_id=None,
            question_ids=tuple(question.id for question in ordered_questions),
            started_at=now,
            expires_at=now + timedelta(seconds=self._settings.quiz_session_ttl_seconds),
            unit_id=unit_id,
        )
        await self._uow.quiz_sessions.add(session)
        await self._uow.commit()

        return SessionDetailView(
            session=_session_view(session, answered_question_ids=()),
            questions=tuple(
                _question_view(question, order=_choice_order(session.id, question, FIRST_ATTEMPT))
                for question in ordered_questions
            ),
        )

    async def _checkpoint_stats(
        self, user_id: UserId, questions: list[Question]
    ) -> list[QuestionStat]:
        """Reduce the learner's history and review schedule to the facts selection weighs.

        Three batched reads for the whole unit — answers, cards, and the question list
        already in hand — because a per-question read would make starting a checkpoint
        dozens of round trips. Absence is data: a question with no answer row is
        ``answered=False``, one with no card is not due and not easy.
        """
        question_ids = [question.id for question in questions]
        history = await self._uow.quiz_answers.history_for_user(user_id, question_ids)
        cards = await self._uow.review_cards.list_for_questions(user_id, question_ids)
        card_by_question = {card.question_id: card for card in cards}
        now = utc_now()

        stats: list[QuestionStat] = []
        for question in questions:
            facts = history.get(question.id)
            card = card_by_question.get(question.id)
            stats.append(
                QuestionStat(
                    question_id=question.id,
                    lesson_id=question.lesson_id,
                    position=question.position,
                    answered=facts.answered if facts is not None else False,
                    was_wrong=facts.ever_wrong if facts is not None else False,
                    card_lapses=card.lapses if card is not None else 0,
                    card_due=card is not None and ensure_aware(card.due_at) <= now,
                    card_easy=card is not None
                    and card_is_easy(card.ease_factor, card.lapses),
                )
            )
        return stats

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

        Re-read from the catalogue but re-ordered by ``question_ids``, and filtered to ids
        that are still present, so a question retired mid-session is skipped rather than
        resurrecting a stale row. A lesson session re-reads through its lesson; a
        checkpoint spans the unit's lessons, so the frozen list is fetched by id directly
        — it is already the scope, and the published-lesson predicate it comes with is the
        same visibility check the lesson path gets from its caller having checked.
        """
        if session.lesson_id is None:
            questions = await self._uow.questions.list_published_by_ids(
                [ContentId(question_id) for question_id in session.question_ids]
            )
        else:
            questions = await self._uow.questions.list_for_lessons(
                [ContentId(session.lesson_id)]
            )
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
        if lesson_id is None:  # pragma: no cover - checkpoint completions take the other branch
            return False
        progress = await self._uow.lesson_progress.get(user_id, lesson_id)
        if progress is None:
            progress = LessonProgress(id=new_id(), user_id=user_id, lesson_id=lesson_id)
        is_retry = progress.state is ProgressState.COMPLETED
        progress.record_completion(score_percent=score_percent, xp=xp)
        await self._uow.lesson_progress.save(progress)
        return is_retry

    async def _record_unit_progress(
        self,
        user_id: UserId,
        unit_id: uuid.UUID,
        *,
        score_percent: int,
    ) -> tuple[bool, uuid.UUID | None]:
        """Fold a finished checkpoint into the unit's row, returning the verdict.

        ``passed`` is the 80% bar applied to this sitting's score — an immediate,
        lock-free retake can flip it, and the record keeps ``best_score`` and the first
        ``passed_at`` regardless, so a later failure never takes back what an earlier pass
        opened. ``unlocked_unit_id`` is the navigation target handed back only to a
        passing score: the next unit in curriculum order, or ``None`` at the end of the
        course. The unlock inputs are re-read here rather than cached from start because
        a completion may itself be the pass that opens the door, and the answer has to
        reflect the row this very call just wrote.
        """
        passed = score_percent >= CHECKPOINT_PASSING_SCORE
        progress = await self._uow.unit_progress.get(user_id, unit_id)
        if progress is None:
            progress = UnitProgress(id=new_id(), user_id=user_id, unit_id=unit_id)
        progress.record_attempt(score_percent=score_percent, passed=passed)
        await self._uow.unit_progress.save(progress)

        unlocked_unit_id = None
        if passed:
            ordered = await self._uow.units.list_published_ordered()
            unlocked_unit_id = next_unit_id(unit_id, [item.unit.id for item in ordered])
        return passed, unlocked_unit_id

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
    """The learner's own submission, stored for the audit trail.

    A multi-select, ordering or matching answer is joined with a separator — the column is a
    single string and this value is never re-parsed to grade, so a lossless-enough record is
    the right trade. Ordering keeps its arrow so a reader can see the sequence the learner
    chose, and matching keeps the pairs verbatim, under the same ``pairs`` key the verdict
    uses, so a stored answer and the key it was marked against read the same way.
    """
    if answer.matching:
        return {"pairs": "; ".join(answer.matching)}
    if answer.order:
        return {"order": " → ".join(answer.order)}
    if answer.choices:
        return {"choices": ", ".join(answer.choices)}
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


#: The kinds whose served order changes how an answer resolves, because a client may send
#: back the *position* of what it saw. Written as the positive list on purpose: a kind added
#: later is unshuffled here until someone decides, rather than silently permuted.
_ORDERED_KINDS: tuple[QuestionKind, ...] = (
    QuestionKind.MULTIPLE_CHOICE,
    QuestionKind.TRUE_FALSE,
    QuestionKind.MULTI_SELECT,
)


def _choice_order(session_id: uuid.UUID, question: Question, attempt: int) -> tuple[int, ...]:
    """The display order for a question's choices on a given attempt.

    Free-text questions carry no choices, so there is no order to compute; the empty tuple
    flows through :func:`order_choices` unchanged. Two kinds cannot be served by a plain
    shuffle:

    * an **ordering** question, because its authored order *is* the answer key and a shuffle
      that came out as the identity would show the learner the answer;
    * a **matching** question, because its prompts and the bank they are paired against are
      two columns read side by side, so the one arrangement to avoid is the alignment where
      each option faces the prompt it answers — the authored order, which a shuffle is
      perfectly free to reproduce.
    """
    if not question.choices:
        return ()
    seed = question_seed(session_id=session_id, question_id=question.id, attempt=attempt)
    if question.kind is QuestionKind.ORDERING:
        return ordering_order(len(question.choices), seed=seed)
    if question.kind is QuestionKind.MATCHING:
        return deranged_order(
            len(question.choices),
            forbidden=matching_answer_positions(
                question.kind, question.choices, question.accepted_answers
            ),
            seed=seed,
        )
    return choice_order(len(question.choices), seed=seed)


def _gradeable(question: Question, *, order: tuple[int, ...]) -> GradeableQuestion:
    """Project a question onto grading, in the order the learner was shown.

    Choice questions are translated into display space — the choices permuted and the
    correct index remapped — so a letter or index the client derived from what it saw
    resolves correctly. Free-text items have no order and pass through untouched. An
    ordering question also passes through untouched, and deliberately so: its ``choices``
    *are* the answer key, and grading compares the learner's text sequence against them, so
    permuting them here would compare against the shuffle instead of the answer. A matching
    question is left alone for the same reason: its pairs are compared as a set, so the orders
    mean nothing to grading, and its ``choices`` is the bank the answers are checked against —
    permuting that would only reorder its stored key.
    """
    if order and question.choices and question.kind in _ORDERED_KINDS:
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
    not the stable thing a learner could carry from one try to the next. A matching question
    additionally carries its ``prompts``, which are read straight from the key's left-hand
    halves by the domain rule that owns that shape — public, because a prompt on its own says
    what is being asked and not what the answer is.
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
        video_url=question.video_url,
        prompts=pre_attempt_prompts(question.kind, question.accepted_answers),
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
        unit_id=session.unit_id,
        answered_question_ids=answered_question_ids,
    )
