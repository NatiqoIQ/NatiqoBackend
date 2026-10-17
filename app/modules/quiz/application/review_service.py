"""Spaced-repetition use cases: the review queue, answering a card, and the stats.

The queue is a projection of cards onto the same answer-free :class:`QuestionView` a lesson
uses, so a review screen renders like a lesson screen and the key still only appears after
an answer. Answering grades with the identical policy the attempt engine uses — a review is
a first attempt, never a half-point retry — and then advances the card's SM-2 schedule.

Two reads carry a subtlety worth naming:

* A card can outlive its question. If the question is gone, or its lesson has slipped back
  to draft, the card is skipped rather than surfaced with nothing to show. That filtering
  lives in the content repository (``list_published_by_ids``), which is why the queue asks
  for questions by id rather than joining in SQL.
* Scheduling is not idempotent by nature, but a card is created at most once per question by
  the unique constraint; the creation path lives in :class:`QuizService`, triggered by the
  first correct answer.
"""

from __future__ import annotations

import uuid

from app.core.errors import ErrorCode, NotFoundError
from app.core.time import ensure_aware, utc_now
from app.core.types import ContentId, QuestionKind, UserId
from app.core.uow import UnitOfWorkProtocol
from app.modules.content.domain.entities import Question
from app.modules.content.domain.policy import (
    matching_answer_positions,
    pre_attempt_prompts,
)
from app.modules.quiz.application.dto import QuestionView
from app.modules.quiz.application.review_dto import (
    DueCardsView,
    DueCardView,
    ReviewedCardView,
    ReviewPreviewView,
    ReviewStatsView,
)
from app.modules.quiz.domain.policy import (
    FIRST_ATTEMPT,
    GradeableQuestion,
    SubmittedAnswer,
    grade_answer,
)
from app.modules.quiz.domain.presentation import (
    deranged_order,
    order_choices,
    ordering_order,
    question_seed,
)
from app.modules.quiz.domain.review import ReviewCard, ReviewLog, quality_for

__all__ = ["ReviewService"]


class ReviewService:
    def __init__(self, uow: UnitOfWorkProtocol) -> None:
        self._uow = uow

    # ================================================================ queue
    async def due_cards(self, user_id: UserId, *, limit: int, strict: bool = False) -> DueCardsView:
        """The learner's due cards, soonest first, with their published questions.

        Reads ``limit + 1`` rows to answer "is there another page" without a second query,
        and loads the page's questions in one batched call. Cards whose question is no
        longer published are dropped from the page — the batch loader makes that safe
        rather than surfacing a card with nothing to show.

        ``strict`` turns an empty queue into ``REVIEW_NOTHING_DUE`` for a caller that would
        rather be told than shown a blank screen.
        """
        now = utc_now()
        rows = await self._uow.review_cards.list_due(user_id, now=now, limit=limit + 1)
        if strict and not rows:
            raise NotFoundError(
                "There is nothing to review right now.",
                code=ErrorCode.REVIEW_NOTHING_DUE,
            )
        has_more = len(rows) > limit
        window = rows[:limit]
        due_count = await self._uow.review_cards.count_due(user_id, now=now)

        questions = await self._uow.questions.list_published_by_ids(
            [ContentId(card.question_id) for card in window]
        )
        by_id = {question.id: question for question in questions}
        cards = tuple(
            _due_card(card, by_id[ContentId(card.question_id)])
            for card in window
            if ContentId(card.question_id) in by_id
        )
        return DueCardsView(cards=cards, has_more=has_more, due_count=due_count)

    async def preview(self, user_id: UserId) -> ReviewPreviewView:
        """How many cards are due, for badging the review entry point."""
        due_count = await self._uow.review_cards.count_due(user_id, now=utc_now())
        return ReviewPreviewView(due_count=due_count)

    # ================================================================ answer
    async def answer_card(
        self, user_id: UserId, card_id: uuid.UUID, *, answer: SubmittedAnswer
    ) -> ReviewedCardView:
        """Grade one review and advance the card's schedule.

        The card is loaded under its owner, so another account's id reads as absent. The
        question is re-read through the published view — a review of an unpublished question
        has nothing to grade against. Grading uses the first-attempt policy, so a correct
        review is worth full points; the verdict's grade then drives the SM-2 update and is
        recorded in the log.
        """
        now = utc_now()
        card = await self._uow.review_cards.get_for_user(card_id, user_id)
        if card is None:
            raise NotFoundError("There is no such review card.")

        question = await self._uow.questions.get_published(ContentId(card.question_id))
        if question is None:
            raise NotFoundError(
                "The question for this card is not available.",
                code=ErrorCode.QUESTION_NOT_FOUND,
            )

        graded = grade_answer(_gradeable(question), answer, attempt=FIRST_ATTEMPT)
        quality = quality_for(graded.is_correct)
        previous_interval = card.interval_days
        card.schedule(quality, at=now)

        await self._uow.review_cards.save(card)
        await self._uow.review_logs.add(
            ReviewLog(
                user_id=user_id,
                card_id=card.id,
                quality=quality,
                previous_interval_days=previous_interval,
                new_interval_days=card.interval_days,
                reviewed_at=now,
            )
        )
        await self._uow.commit()

        return ReviewedCardView(
            is_correct=graded.is_correct,
            correct_answer=graded.correct_answer,
            explanation=graded.explanation,
            points_awarded=graded.points_awarded,
            quality=quality,
            due_at=card.due_at,
            interval_days=card.interval_days,
            repetitions=card.repetitions,
            lapses=card.lapses,
            state=card.state.value,
        )

    # ================================================================= stats
    async def stats(self, user_id: UserId) -> ReviewStatsView:
        """The review screen's figures.

        The whole card set is read once and split in Python rather than issuing a separate
        ``COUNT`` per bucket: a learner has at most one card per published question, so the
        set is small and the split is cheaper than three round trips. Retention comes from
        the log aggregate, which spans every review ever taken.
        """
        now = utc_now()
        cards = await self._uow.review_cards.list_for_user(user_id)
        log_stats = await self._uow.review_logs.stats_for_user(user_id)
        due_today = sum(1 for card in cards if ensure_aware(card.due_at) <= now)
        return ReviewStatsView(
            total_cards=len(cards),
            due_today=due_today,
            future=len(cards) - due_today,
            reviewed_total=log_stats.total,
            retention_percent=log_stats.retention_percent,
        )


# ------------------------------------------------------------------ helpers
#: These two mirror ``quiz_service``'s private helpers on purpose. Importing across the
#: module's own private surface would couple the two services for no gain, and the
#: projection is low-risk: it is defined by omission, so a field added to ``QuestionView``
#: fails a type check here rather than leaking an answer.
def _gradeable(question: Question) -> GradeableQuestion:
    return GradeableQuestion(
        kind=question.kind,
        choices=question.choices,
        correct_index=question.correct_index,
        accepted_answers=question.accepted_answers,
        points=question.points,
        explanation=question.explanation,
    )


def _question_view(question: Question, *, seed: str) -> QuestionView:
    """A published question as the review queue serves it — with no answer key.

    A matching question is the one kind that needs to know *where* it is being shown from: its
    prompts and the bank they are paired against are two columns read side by side, so the bank
    has to be shuffled away from the pairing. A review has no session to seed that with, and
    the card is what the learner is answering, so the card stands in for one — the order is
    then stable for a given learner without being shared between two of them.
    """
    return QuestionView(
        id=question.id,
        position=question.position,
        kind=question.kind,
        skill=question.skill,
        prompt=question.prompt,
        choices=order_choices(question.choices, _bank_order(question, seed=seed)),
        points=question.points,
        image_url=question.image_url,
        audio_url=question.audio_url,
        prompts=pre_attempt_prompts(question.kind, question.accepted_answers),
    )


def _bank_order(question: Question, *, seed: str) -> tuple[int, ...]:
    """How to lay out a question's ``choices`` when serving it before an answer.

    Two kinds have an arrangement to avoid, and both of them are arrangements the stored order
    already is:

    * an **ordering** question, whose ``choices`` *are* the key in order, so serving them as
      stored hands the sequence over;
    * a **matching** question, whose two columns are read side by side, so the bank must not sit
      opposite the prompts it answers — the order its pairs were authored in.

    Every other kind is safe in any order, and is served as stored: the review queue is a read
    of the catalogue, and shuffling a multiple choice here for no reason would only make two
    screens of the same question disagree.
    """
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
    return tuple(range(len(question.choices)))


def _due_card(card: ReviewCard, question: Question) -> DueCardView:
    return DueCardView(
        card_id=card.id,
        due_at=card.due_at,
        state=card.state.value,
        interval_days=card.interval_days,
        repetitions=card.repetitions,
        question=_question_view(
            question,
            seed=question_seed(session_id=card.id, question_id=question.id, attempt=FIRST_ATTEMPT),
        ),
    )
