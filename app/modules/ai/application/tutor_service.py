"""AI tutor use cases: explain an answer, chat, read a thread, read the quota.

Three rules shape this module, and they come straight from the plan's treatment of AI as an
enhancement rather than a dependency (plan §59):

* **Never a hard dependency.** The platform :class:`AIService` owns every fallback, so with a
  disabled provider — or an exhausted budget — ``explain`` still returns the authored
  rationale and ``ask`` still returns a useful, non-AI reply. This service never sees an
  "AI is down" branch because there isn't one to write.
* **Never authoritative.** The service asks the platform to *phrase* an explanation; it never
  lets AI award points, move a schedule or grade anything. The verdict comes from the attempt
  engine's own policy, applied here on the server exactly as ``/quiz`` and ``/review`` do it.
* **Always accountable.** Chat is persisted so a learner returning to the app finds their
  thread, and the budget is read from the same usage log the platform writes to, so a quota
  the UI shows is the quota the provider enforced.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from app.core.errors import ErrorCode, NotFoundError, ValidationFailed
from app.core.time import ensure_aware, utc_now
from app.core.types import CEFRLevel, ContentId, ContentLanguage, UserId, new_id
from app.core.uow import UnitOfWorkProtocol
from app.modules.ai.application.dto import (
    AiQuotaView,
    ConversationView,
    ExplanationView,
    MessageView,
    TutorReplyView,
)
from app.modules.ai.domain.budget import BudgetPort
from app.modules.ai.domain.conversation import Conversation, Message, MessageRole
from app.modules.content.domain.entities import Question
from app.modules.quiz.domain.policy import (
    FIRST_ATTEMPT,
    GradeableQuestion,
    SubmittedAnswer,
    grade_answer,
)
from app.platform.ai.port import AIFeature, AIServicePort, TutorContext

__all__ = ["AiTutorService"]

#: How many recent turns of a thread are handed to the model as context. The platform trims
#: this again on its side; the bound here keeps the query from loading a long thread whole.
HISTORY_WINDOW = 20


class AiTutorService:
    def __init__(
        self,
        uow: UnitOfWorkProtocol,
        ai_service: AIServicePort,
        budget: BudgetPort,
    ) -> None:
        self._uow = uow
        self._ai = ai_service
        self._budget = budget

    # ============================================================== explain
    async def explain(
        self, user_id: UserId, *, question_id: uuid.UUID, answer: SubmittedAnswer
    ) -> ExplanationView:
        """Grade one answer and return the verdict with a rationale.

        Grading happens here, on the server, using the identical policy the attempt engine
        uses: the client posts a bare answer and never learns the key until it has committed
        one. The question is re-read through the published view, so an unpublished question
        has nothing to explain — the same guard ``/review`` keeps.
        """
        question = await self._uow.questions.get_published(ContentId(question_id))
        if question is None:
            raise NotFoundError(
                "That question is not available.",
                code=ErrorCode.QUESTION_NOT_FOUND,
            )

        graded = grade_answer(_gradeable(question), answer, attempt=FIRST_ATTEMPT)
        context = await self._context(user_id)
        question_text = question.prompt
        authored_explanation = question.explanation or None
        learner_answer = _learner_answer(answer)
        correct_answer = _correct_answer_text(graded.correct_answer)
        #: End the read transaction *before* the upstream call. Everything the provider
        #: needs is already copied into locals: holding the transaction open across a
        #: call that can take ten seconds pins a pooled connection for no reason, and at
        #: 700 RPS a handful of slow providers would exhaust the whole pool and stall every
        #: unrelated endpoint. Committing a read-only transaction only returns the
        #: connection; nothing is written.
        await self._uow.commit()
        result = await self._ai.explain_answer(
            context=context,
            question_text=question_text,
            learner_answer=learner_answer,
            correct_answer=correct_answer,
            is_correct=graded.is_correct,
            explanation_en=authored_explanation,
            # A question carries a single authored rationale today, so it grounds the model
            # in every explanation language rather than leaving Iraqi learners ungrounded.
            explanation_iq=authored_explanation,
        )
        return ExplanationView(
            is_correct=graded.is_correct,
            correct_answer=graded.correct_answer,
            explanation=str(result.get("text") or authored_explanation),
            source=str(result.get("source", "authored")),
        )

    # ================================================================== chat
    async def ask(
        self,
        user_id: UserId,
        *,
        message: str,
        conversation_id: uuid.UUID | None = None,
        lesson_id: uuid.UUID | None = None,
    ) -> TutorReplyView:
        """Append a turn to a thread and return the tutor's reply.

        A first message opens the thread; a later one continues it, and the caller must own
        it or it reads as absent. The learner's turn is persisted before the reply is
        requested, so a provider failure still leaves an honest transcript of what was asked.
        """
        text = message.strip()
        if not text:
            raise ValidationFailed("A message is required.", details={"field": "message"})

        asked_at = utc_now()
        history: list[dict[str, str]] = []
        if conversation_id is not None:
            conversation = await self._uow.ai_conversations.get_for_user(conversation_id, user_id)
            if conversation is None:
                raise NotFoundError(
                    "There is no such conversation.",
                    code=ErrorCode.CONVERSATION_NOT_FOUND,
                )
            prior = await self._uow.ai_conversations.list_messages(
                conversation.id, limit=HISTORY_WINDOW
            )
            history = [{"role": turn.role, "content": turn.content} for turn in prior]
            # A new turn must sort after every turn already in the thread. The system clock
            # can return the same instant twice — Windows in particular ticks far coarser
            # than a microsecond — and a transcript ordered by ``created_at`` would then fall
            # to an arbitrary id tiebreak and read out of order. Advancing past the last
            # stored instant keeps the order total and deterministic.
            if prior:
                asked_at = _after(ensure_aware(prior[-1].created_at), asked_at)
        else:
            conversation = Conversation(
                id=new_id(),
                user_id=user_id,
                lesson_id=lesson_id,
                created_at=asked_at,
                updated_at=asked_at,
            )
            await self._uow.ai_conversations.add(conversation)

        await self._uow.ai_conversations.add_message(
            Message(
                id=new_id(),
                conversation_id=conversation.id,
                role=MessageRole.USER.value,
                content=text,
                created_at=asked_at,
            )
        )

        context = await self._context(user_id)
        #: Same rule as ``explain``: persist the learner's turn and release the connection
        #: before asking the model. The reply is written in a second, short transaction.
        #: The alternative — one transaction spanning the provider call — is what turns a
        #: slow model into a database connection leak under load.
        await self._uow.commit()
        result = await self._ai.tutor_reply(context=context, history=history, message=text)
        reply = str(result.get("reply") or "")

        #: A distinct instant for the reply, so the two turns never share a ``created_at``.
        #: Ordering the transcript by timestamp is only meaningful if the timestamps differ:
        #: with one shared value the order would fall to an arbitrary id tiebreak.
        replied_at = _after(asked_at, utc_now())
        await self._uow.ai_conversations.add_message(
            Message(
                id=new_id(),
                conversation_id=conversation.id,
                role=MessageRole.ASSISTANT.value,
                content=reply,
                created_at=replied_at,
            )
        )
        await self._uow.ai_conversations.touch(conversation.id, at=replied_at)
        await self._uow.commit()

        return TutorReplyView(
            conversation_id=conversation.id,
            reply=reply,
            source=str(result.get("source", "authored")),
        )

    # =============================================================== history
    async def history(self, user_id: UserId, conversation_id: uuid.UUID) -> ConversationView:
        """A thread's turns, oldest first, scoped to the caller."""
        conversation = await self._uow.ai_conversations.get_for_user(conversation_id, user_id)
        if conversation is None:
            raise NotFoundError(
                "There is no such conversation.",
                code=ErrorCode.CONVERSATION_NOT_FOUND,
            )
        messages = await self._uow.ai_conversations.list_messages(conversation.id)
        return ConversationView(
            id=conversation.id,
            lesson_id=conversation.lesson_id,
            messages=tuple(
                MessageView(
                    id=turn.id,
                    role=turn.role,
                    content=turn.content,
                    created_at=turn.created_at,
                )
                for turn in messages
            ),
        )

    # ================================================================== quota
    async def quota(self, user_id: UserId) -> AiQuotaView:
        """The learner's AI budget for today, and whether the feature is even live."""
        status = await self._budget.status(user_id, feature=AIFeature.TUTOR_CHAT)
        return AiQuotaView(
            enabled=self._ai.enabled,
            allowed=status.allowed,
            limit_usd=float(status.limit_usd),
            spent_usd=float(status.spent_usd),
            remaining_usd=float(status.remaining_usd),
            reason=status.reason,
        )

    # ============================================================== plumbing
    async def _context(self, user_id: UserId) -> TutorContext:
        """The one thing the tutor is allowed to know about the learner.

        Level and explanation language come from the learner's own profile and settings, so a
        reply is written at the right CEFR band and in the language they chose to be taught in.
        A missing profile or settings row (which sign-in does not produce, but a hand-written
        fixture might) falls back to the safest defaults rather than failing the request.
        """
        profile = await self._uow.users.get_profile(user_id)
        settings = await self._uow.users.get_settings(user_id)
        return TutorContext(
            user_id=user_id,
            level=profile.current_level if profile is not None else CEFRLevel.A1,
            explanation_language=(
                settings.explanation_language if settings is not None else ContentLanguage.EN
            ),
        )


# ------------------------------------------------------------------ helpers
def _after(previous: datetime, moment: datetime) -> datetime:
    """``moment`` advanced to strictly follow ``previous`` if the clock did not.

    The system clock has a granularity coarser than its resolution — on Windows two reads a
    fraction of a millisecond apart routinely return the same value — so an append-only
    transcript ordered by timestamp needs this nudge to stay a total order. One microsecond
    is below any human notion of "when" and is only ever applied on a tie.
    """
    if moment <= previous:
        return previous + timedelta(microseconds=1)
    return moment


#: Mirrors ``review_service``'s private helper on purpose: importing across the quiz module's
#: own private surface would couple the two services for no gain, and the projection is
#: defined by omission, so it cannot leak the key it copies.
def _gradeable(question: Question) -> GradeableQuestion:
    return GradeableQuestion(
        kind=question.kind,
        choices=question.choices,
        correct_index=question.correct_index,
        accepted_answers=question.accepted_answers,
        points=question.points,
        explanation=question.explanation,
    )


def _learner_answer(answer: SubmittedAnswer) -> str:
    """What the learner submitted, as one string for the prompt."""
    if answer.choice is not None and answer.choice.strip():
        return answer.choice
    return answer.text or ""


def _correct_answer_text(correct_answer: dict[str, str]) -> str:
    """The answer key as the client sent it, for the model to compare against."""
    if "choice" in correct_answer:
        return correct_answer["choice"]
    if "text" in correct_answer:
        return correct_answer["text"]
    return ""
