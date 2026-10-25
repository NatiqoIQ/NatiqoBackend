"""A persona's learning history, written against the real course.

**Why this exists.** ``scripts.seed`` writes accounts and ``scripts.seed_content`` writes
the curriculum, and between them a freshly seeded environment still has nothing to *look
at*: progress is empty, the review queue is empty, every achievement is locked and the
tutor has never been spoken to. This module closes that gap for the personas that declare a
:class:`~scripts.seed_personas.SeedActivity`, by walking the published course the way a
learner would and writing what came of it.

**How it is written.** The whole history is *planned* — as domain entities — before a single
row is written, the way ``seed_content`` builds its tree before it inserts it. Two things
follow from that, and both are why the shape was chosen:

* ``--dry-run`` reports the exact numbers a real run writes, because the real run writes
  exactly the plan it just previewed rather than deciding as it goes.
* A catalogue the seeder cannot practise is refused before anything is written. A choice
  question with no answer key is a content bug, and it fails here — loudly, with the
  lesson's title — instead of becoming a row nobody can grade.

**Nothing here re-implements a rule.** The answer is graded by the attempt engine's own
policy (``quiz.domain.policy``), the points are what that policy awarded, XP is their sum,
the schedule is SM-2 (``quiz.domain.review``), the streak is the shared calendar in
``app.core.time``, the league and the achievement wall are read with the predicates the hub
uses, and a tutor reply is what ``app.platform.ai.service`` says when no model is available.
The seeder decides *when* things happened and how well they went; it never decides what
anything is worth.

**Why a rerun is harmless.** Every id is ``uuid5`` of the persona and the row's place in the
plan, so a second run recognises its own rows. Three guards keep it from touching work it
did not do:

* a lesson that already has a progress row is left alone — a real attempt, or an earlier
  run, is not re-seeded and not overwritten;
* a question that already has a review card keeps the schedule it has;
* a persona with no declared activity short-circuits to an all-zero outcome, so this module
  changes nothing for the five accounts that do not use it.

The one case worth naming: a run interrupted halfway leaves whole lessons written and whole
lessons untouched, because the guards are per lesson. The next run finishes the rest and
reports zero for what was already there.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta

from app.core.config import Settings
from app.core.time import ensure_aware, get_timezone, local_date, streak_runs, utc_now
from app.core.types import ContentId, QuestionKind, UserId
from app.core.uow import UnitOfWorkProtocol
from app.modules.ai.domain.conversation import Conversation, Message, MessageRole
from app.modules.content.domain.entities import Lesson, LessonPlacement, Question
from app.modules.content.domain.policy import MATCH_ARROW as _MATCH_ARROW
from app.modules.gamification.domain.catalog import ACHIEVEMENTS
from app.modules.gamification.domain.entities import LeagueTier, UserAchievement, UserStats
from app.modules.gamification.domain.policy import (
    condition_metric,
    is_condition_met,
    league_for_xp,
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
from app.modules.quiz.domain.review import (
    REVIEW_INITIAL_INTERVAL_DAYS,
    ReviewCard,
    ReviewLog,
    new_card,
    quality_for,
)
from app.platform.ai.port import TutorContext
from app.platform.ai.providers import DisabledProvider
from app.platform.ai.service import AIService
from scripts.seed_personas import SEED_NAMESPACE, SeedActivity, SeedPersona

__all__ = ["ActivityOutcome", "seed_activity"]

#: The answer a deliberately wrong free-text question is given. It cannot collide with a
#: real key: accepted answers are normalised words and phrases from the curriculum.
_WRONG_TEXT = "not sure"

#: How far apart two sittings in one day are. Chosen so a busy day still ends before
#: midnight and so the timestamps of a day's sittings never share an instant.
_SITTING_GAP_MINUTES = 25

#: How long after the learner's turn the tutor answers, and how long they then take to
#: write the next one. Distinct instants keep the transcript a total order.
_REPLY_DELAY_SECONDS = 45


@dataclass(frozen=True, slots=True)
class ActivityOutcome:
    """What the activity seeder wrote, by kind of row.

    Counts are what *changed*, not what exists, for the same reason
    :class:`~scripts.seed.PersonaOutcome` counts that way: a second run reporting zero is
    the seeder telling its operator that the environment already holds this history.
    """

    sessions: int = 0
    answers: int = 0
    cards: int = 0
    reviews: int = 0
    threads: int = 0
    messages: int = 0
    achievements: int = 0
    stats_updated: int = 0

    @property
    def rows(self) -> int:
        return (
            self.sessions
            + self.answers
            + self.cards
            + self.reviews
            + self.threads
            + self.messages
            + self.achievements
            + self.stats_updated
        )

    @property
    def changed(self) -> bool:
        return self.rows > 0

    def format(self) -> str:
        """The breakdown, for the one persona in a report that has one."""
        return (
            f"{self.sessions} sessions, {self.answers} answers, {self.cards} cards, "
            f"{self.reviews} reviews, {self.threads} threads, "
            f"{self.achievements} achievements"
        )


@dataclass(frozen=True, slots=True)
class _Completion:
    """One finished lesson, as the projection needs to see it."""

    lesson_id: uuid.UUID
    completed_at: datetime
    xp: int


@dataclass(frozen=True, slots=True)
class _PlannedSession:
    session: QuizSession
    answers: tuple[QuizAnswer, ...]
    progress: LessonProgress


@dataclass(frozen=True, slots=True)
class _PlannedReview:
    card: ReviewCard
    logs: tuple[ReviewLog, ...]


@dataclass(frozen=True, slots=True)
class _PlannedThread:
    conversation: Conversation
    messages: tuple[Message, ...]
    creates_conversation: bool


@dataclass(frozen=True, slots=True)
class _Plan:
    """Everything one persona's history consists of, before any of it is written."""

    sessions: tuple[_PlannedSession, ...] = ()
    cards: tuple[ReviewCard, ...] = ()
    reviews: tuple[_PlannedReview, ...] = ()
    threads: tuple[_PlannedThread, ...] = ()
    completions: tuple[_Completion, ...] = ()
    achievements: tuple[UserAchievement, ...] = ()
    stats: UserStats | None = None


def _outcome(plan: _Plan) -> ActivityOutcome:
    return ActivityOutcome(
        sessions=len(plan.sessions),
        answers=sum(len(row.answers) for row in plan.sessions),
        cards=len(plan.cards) + len(plan.reviews),
        reviews=sum(len(review.logs) for review in plan.reviews),
        threads=sum(1 for thread in plan.threads if thread.creates_conversation),
        messages=sum(len(thread.messages) for thread in plan.threads),
        achievements=len(plan.achievements),
        stats_updated=1 if plan.stats is not None else 0,
    )


async def seed_activity(
    uow: UnitOfWorkProtocol,
    settings: Settings,
    persona: SeedPersona,
    user_id: UserId | None,
    *,
    dry_run: bool,
) -> ActivityOutcome:
    """Plan this persona's learning history, write it unless ``dry_run``, and report it.

    ``user_id`` is ``None`` only for a dry run of an account that does not exist yet: the
    plan is then built against a placeholder owner, which is what makes the preview of a
    fresh account exact — nothing it looks for can be found.
    """
    if persona.activity is None:
        return ActivityOutcome()
    if user_id is None and not dry_run:
        raise ValueError("A real activity seed needs the account it is writing for.")

    plan = await _ActivityPlanner(uow, settings, persona, user_id).plan()
    if not dry_run:
        await _write(uow, plan, owner=user_id)
    return _outcome(plan)


class _ActivityPlanner:
    """Builds one persona's history from the published catalogue.

    A class rather than a pile of functions because every step reads what the earlier steps
    decided: the lessons to finish decide the completion dates, the completion dates decide
    which cards exist and which of them can have been reviewed, and the whole set decides
    the stats the projection is rebuilt from.
    """

    def __init__(
        self,
        uow: UnitOfWorkProtocol,
        settings: Settings,
        persona: SeedPersona,
        user_id: UserId | None,
    ) -> None:
        self._uow = uow
        self._settings = settings
        self._persona = persona
        self._plan: SeedActivity = persona.activity  # type: ignore[assignment]
        #: The account every row belongs to. A placeholder is used when the account does
        #: not exist yet, which is only ever the case for a preview: nothing is written
        #: under it, and every lookup against it correctly finds nothing.
        self._user_id: UserId = user_id or UserId(
            uuid.uuid5(SEED_NAMESPACE, f"pending:{persona.key}")
        )
        self._today: date = local_date(utc_now(), persona.timezone)
        self._ai = AIService(DisabledProvider())

    # ------------------------------------------------------------------ plan
    async def plan(self) -> _Plan:
        placements = await self._uow.lessons.list_published_ordered()
        questions = await self._questions_for(placements)
        # A lesson with no questions cannot be attempted — the engine refuses to open a
        # session over one — so it is not part of the plan at all, and the seeder does not
        # silently leave a hole where the learner's history should be.
        practisable = [row for row in placements if questions.get(row.lesson.id)]
        if not practisable:
            return _Plan()

        done_count = min(self._plan.lessons_completed, len(practisable))
        planned = practisable[:done_count]
        rest = practisable[done_count:]

        sessions: list[_PlannedSession] = []
        completions: list[_Completion] = []
        cards: list[ReviewCard] = []
        instants = self._completion_instants(len(planned))
        for index, placement in enumerate(planned):
            if await self._has_progress(placement.lesson.id):
                continue
            built = self._completed_session(
                placement,
                questions[placement.lesson.id],
                index=index,
                score_percent=self._score_at(index),
                at=instants[index],
            )
            sessions.append(built)
            completions.append(
                _Completion(
                    lesson_id=placement.lesson.id,
                    completed_at=built.session.completed_at or instants[index],
                    xp=built.session.xp_awarded,
                )
            )
            for answer in built.answers:
                if answer.is_correct:
                    card = await self._open_card(answer.question_id, at=answer.answered_at)
                    if card is not None:
                        cards.append(card)

        sessions.extend(await self._unfinished_sessions(rest, questions, len(planned)))

        rules = _ReviewRules(
            days_ago=self._plan.review_days_ago,
            pass_pattern=self._plan.review_pass_pattern,
        )
        reviews = self._reviewed_cards(cards, rules)
        reviewed_ids = {review.card.id for review in reviews}
        threads = await self._threads(planned)
        achievements, stats = await self._projection(completions)

        return _Plan(
            sessions=tuple(sessions),
            cards=tuple(card for card in cards if card.id not in reviewed_ids),
            reviews=reviews,
            threads=threads,
            completions=tuple(completions),
            achievements=achievements,
            stats=stats,
        )

    # ------------------------------------------------------------- catalogue
    async def _questions_for(
        self, placements: list[LessonPlacement]
    ) -> dict[ContentId, list[Question]]:
        """Every lesson's questions in one read, grouped and in position order."""
        rows = await self._uow.questions.list_for_lessons([row.lesson.id for row in placements])
        grouped: dict[ContentId, list[Question]] = defaultdict(list)
        for question in rows:
            grouped[question.lesson_id].append(question)
        return grouped

    # -------------------------------------------------------------- sessions
    def _completion_instants(self, count: int) -> list[datetime]:
        """When each completion happened, oldest first, in the learner's own calendar.

        Days are spread evenly from ``study_span_days`` ago to today, with the sittings of
        one day an hour apart, so the streak the account shows is a consequence of real
        session times rather than a number written next to them.
        """
        offsets = _day_offsets(count, self._plan.study_span_days)
        seen: dict[int, int] = defaultdict(int)
        instants: list[datetime] = []
        for index, offset in enumerate(offsets):
            slot = seen[offset]
            seen[offset] += 1
            minutes = slot * _SITTING_GAP_MINUTES
            instants.append(
                # Clamped into the past: a seed run before the declared hour would
                # otherwise write today's sittings into the future, and a history that
                # has not happened yet is not a history.
                _before_now(
                    _local_instant(
                        self._today - timedelta(days=offset),
                        hour=min(23, self._plan.first_hour + minutes // 60),
                        minute=minutes % 60,
                        timezone=self._persona.timezone,
                    ),
                    step=index,
                )
            )
        return instants

    def _score_at(self, index: int) -> int:
        pattern = self._plan.score_pattern
        if not pattern:  # pragma: no cover - the fixture always declares one
            return 100
        return max(0, min(100, pattern[index % len(pattern)]))

    def _completed_session(
        self,
        placement: LessonPlacement,
        questions: list[Question],
        *,
        index: int,
        score_percent: int,
        at: datetime,
    ) -> _PlannedSession:
        """Grade a whole lesson the way the engine would, without touching the database."""
        lesson = placement.lesson
        correct_target = round(score_percent * len(questions) / 100)
        misses = _misses(len(questions), len(questions) - correct_target)
        span = timedelta(minutes=_sitting_minutes(lesson, self._settings))

        session_id = self._id(f"session:{index}")
        started_at = at - span
        answers: list[QuizAnswer] = []
        correct_count = 0
        xp = 0
        for position, question in enumerate(questions):
            answer = self._graded(
                question,
                session_id=session_id,
                lesson_index=index,
                position=position,
                correct=position not in misses,
                at=started_at + span * (position + 1) / (len(questions) + 1),
            )
            correct_count += 1 if answer.is_correct else 0
            xp += answer.points_awarded
            answers.append(answer)

        session = QuizSession(
            id=session_id,
            user_id=self._user_id,
            mode=SessionMode.LESSON,
            lesson_id=lesson.id,
            question_ids=tuple(question.id for question in questions),
            started_at=started_at,
            expires_at=started_at + timedelta(seconds=self._settings.quiz_session_ttl_seconds),
            status=SessionStatus.COMPLETED,
            completed_at=at,
            correct_count=correct_count,
            xp_awarded=xp,
        )
        progress = LessonProgress(
            id=self._id(f"progress:{index}"),
            user_id=self._user_id,
            lesson_id=lesson.id,
            state=ProgressState.COMPLETED,
            best_score=round(correct_count / len(questions) * 100),
            attempts=1,
            xp_earned=xp,
            completed_at=at,
        )
        return _PlannedSession(session=session, answers=tuple(answers), progress=progress)

    async def _unfinished_sessions(
        self,
        rest: list[LessonPlacement],
        questions: dict[ContentId, list[Question]],
        offset: int,
    ) -> list[_PlannedSession]:
        """The two ways a sitting ends without a score: left open, and given up on.

        Both write a progress row, because that is what starting a session does in the app
        (``QuizService._start_progress``). Those rows are the states a learner-facing screen
        has to be able to draw: a lesson half done, and a lesson abandoned days ago.
        """
        rows: list[_PlannedSession] = []
        wanted: list[tuple[LessonPlacement, SessionStatus, int]] = []
        if self._plan.lesson_in_progress and rest:
            wanted.append((rest[0], SessionStatus.ACTIVE, 0))
        for step in range(self._plan.abandoned_sessions):
            if len(rest) > step + 1:
                wanted.append((rest[step + 1], SessionStatus.ABANDONED, step + 1))

        for placement, status, step in wanted:
            lesson = placement.lesson
            if await self._has_progress(lesson.id):
                continue
            items = questions[lesson.id]
            # Older than the lessons around it: an unfinished lesson is exactly the kind of
            # row that sits untouched for a while, and dating it in the future would make
            # the progress screen's ordering lie.
            at = _before_now(
                _local_instant(
                    self._today - timedelta(days=step),
                    hour=min(23, self._plan.first_hour + 2),
                    minute=step,
                    timezone=self._persona.timezone,
                ),
                step=step,
            )
            index = offset + step
            session_id = self._id(f"session:{index}")
            answered = items[: max(1, len(items) // 2)]
            answers = tuple(
                self._graded(
                    question,
                    session_id=session_id,
                    lesson_index=index,
                    position=position,
                    correct=position % 2 == 0,
                    at=at + timedelta(seconds=30 * (position + 1)),
                )
                for position, question in enumerate(answered)
            )
            rows.append(
                _PlannedSession(
                    session=QuizSession(
                        id=session_id,
                        user_id=self._user_id,
                        mode=SessionMode.LESSON,
                        lesson_id=lesson.id,
                        question_ids=tuple(question.id for question in items),
                        started_at=at,
                        expires_at=at + timedelta(seconds=self._settings.quiz_session_ttl_seconds),
                        status=status,
                        completed_at=None if status is SessionStatus.ACTIVE else at,
                    ),
                    answers=answers,
                    progress=LessonProgress(
                        id=self._id(f"progress:{index}"),
                        user_id=self._user_id,
                        lesson_id=lesson.id,
                        state=ProgressState.IN_PROGRESS,
                    ),
                )
            )
        return rows

    async def _has_progress(self, lesson_id: uuid.UUID) -> bool:
        """Whether this lesson already carries a record, whoever wrote it."""
        return await self._uow.lesson_progress.get(self._user_id, lesson_id) is not None

    def _graded(
        self,
        question: Question,
        *,
        session_id: uuid.UUID,
        lesson_index: int,
        position: int,
        correct: bool,
        at: datetime,
    ) -> QuizAnswer:
        """One answer, graded by the policy the attempt engine grades with.

        The verdict and the points are the policy's, not this script's: the submission only
        decides what the learner *sent*, and a question whose key is missing fails here
        rather than being quietly recorded as a wrong answer.
        """
        graded = grade_answer(
            _gradeable(question),
            _submitted(question, correct=correct),
            attempt=FIRST_ATTEMPT,
        )
        return QuizAnswer(
            id=self._id(f"answer:{lesson_index}:{position}"),
            session_id=session_id,
            question_id=question.id,
            position=question.position,
            given_answer=_given_answer(question, correct=correct),
            is_correct=graded.is_correct,
            points_awarded=graded.points_awarded,
            explanation=graded.explanation,
            attempt=FIRST_ATTEMPT,
            answered_at=at,
        )

    # ---------------------------------------------------------------- review
    async def _open_card(self, question_id: uuid.UUID, *, at: datetime) -> ReviewCard | None:
        """The card a correct answer creates, or ``None`` when one already exists.

        Mirrors ``QuizService._ensure_review_card``: a card is opened once per question, on
        the first correct answer, and the answer that opened it is when it started waiting.
        An existing card is returned as ``None`` so the plan never rewrites a schedule a
        learner has been living with.
        """
        existing = await self._uow.review_cards.get_for_question(self._user_id, question_id)
        if existing is not None:
            return None
        return replace(
            new_card(self._user_id, question_id, now=at),
            id=self._id(f"card:{question_id}"),
        )

    def _reviewed_cards(
        self, cards: list[ReviewCard], rules: _ReviewRules
    ) -> tuple[_PlannedReview, ...]:
        """Give the earliest cards a real SM-2 history, and leave the rest alone.

        Only cards older than the first review can have been reviewed, and only the first
        ``reviews_reviewed`` of those are: the rest stay due, which is what a learner who
        skipped a few days actually has in their queue.
        """
        reviewed: list[_PlannedReview] = []
        for card in cards:
            if len(reviewed) >= self._plan.reviews_reviewed:
                break
            moments = rules.moments(self._today, self._persona.timezone, len(reviewed))
            opened_at = ensure_aware(card.due_at) - timedelta(days=REVIEW_INITIAL_INTERVAL_DAYS)
            if any(moment <= opened_at for moment in moments):
                # The card did not exist yet when the review is supposed to have happened:
                # a card opens a day before it is first due, so an earlier review is
                # fiction, and a history that contains one would not be a history.
                continue
            logs: list[ReviewLog] = []
            for step, moment in enumerate(moments):
                passed = rules.passed(len(reviewed), step)
                previous = card.interval_days
                card.schedule(quality_for(passed), at=moment)
                logs.append(
                    ReviewLog(
                        user_id=self._user_id,
                        card_id=card.id,
                        quality=quality_for(passed),
                        previous_interval_days=previous,
                        new_interval_days=card.interval_days,
                        id=self._id(f"review:{card.question_id}:{step}"),
                        reviewed_at=moment,
                    )
                )
            reviewed.append(_PlannedReview(card=card, logs=tuple(logs)))
        return tuple(reviewed)

    # ---------------------------------------------------------------- tutor
    async def _threads(self, planned: list[LessonPlacement]) -> tuple[_PlannedThread, ...]:
        """The tutor conversations, written turn by turn with the app's own replies."""
        threads: list[_PlannedThread] = []
        for index, declared in enumerate(self._plan.tutor_threads):
            conversation_id = self._id(f"thread:{index}")
            anchor = self._lesson_at(planned, declared.lesson_index)
            touched = _before_now(
                _local_instant(
                    self._today - timedelta(days=declared.days_ago),
                    hour=min(23, self._plan.first_hour + 1),
                    minute=index * 7,
                    timezone=self._persona.timezone,
                ),
                step=index,
            )
            existing = await self._uow.ai_conversations.get_for_user(conversation_id, self._user_id)
            present = {
                message.id
                for message in await self._uow.ai_conversations.list_messages(conversation_id)
            }
            messages: list[Message] = []
            moment = touched
            #: The instant of the most recent turn, which is what the thread's own clock
            #: means — the same thing ``tutor_service`` records when it appends a reply.
            updated_at = touched
            for turn, text in enumerate(declared.learner_messages):
                asked = self._id(f"thread:{index}:asked:{turn}")
                if asked not in present:
                    messages.append(
                        Message(
                            id=asked,
                            conversation_id=conversation_id,
                            role=MessageRole.USER.value,
                            content=text,
                            created_at=moment,
                        )
                    )
                    updated_at = moment
                moment += timedelta(seconds=_REPLY_DELAY_SECONDS)
                answered = self._id(f"thread:{index}:replied:{turn}")
                if answered not in present:
                    messages.append(
                        Message(
                            id=answered,
                            conversation_id=conversation_id,
                            role=MessageRole.ASSISTANT.value,
                            content=await self._reply(text),
                            created_at=moment,
                        )
                    )
                    updated_at = moment
                moment += timedelta(seconds=_REPLY_DELAY_SECONDS * 2)
            if not messages:
                # The whole thread is already stored, which is the normal state after a
                # first run. Nothing to repair and nothing to report.
                continue
            threads.append(
                _PlannedThread(
                    conversation=Conversation(
                        id=conversation_id,
                        user_id=self._user_id,
                        lesson_id=anchor,
                        created_at=touched,
                        updated_at=updated_at,
                    ),
                    messages=tuple(messages),
                    # A thread interrupted mid-write is completed rather than restarted:
                    # the conversation row is only added when it is genuinely absent.
                    creates_conversation=existing is None,
                )
            )
        return tuple(threads)

    async def _reply(self, message: str) -> str:
        """What the tutor answers, without a model and without a network call.

        The provider is the disabled one on purpose: a seeder must never be able to bill a
        provider or depend on one being reachable, and the text it produces here is the
        same text the app hands a learner whenever AI is switched off or over budget.
        """
        context = TutorContext(
            user_id=self._user_id,
            level=self._persona.current_level,
            explanation_language=self._persona.explanation_language,
        )
        result = await self._ai.tutor_reply(context=context, history=(), message=message)
        return str(result.get("reply") or "")

    def _lesson_at(self, planned: list[LessonPlacement], index: int | None) -> uuid.UUID | None:
        if index is None or index < 0 or index >= len(planned):
            return None
        return planned[index].lesson.id

    # ----------------------------------------------------------- projection
    async def _projection(
        self, completions: list[_Completion]
    ) -> tuple[tuple[UserAchievement, ...], UserStats | None]:
        """Rebuild the stats row and the achievement wall from the history.

        Both are *projections*: the hub derives them from ``lesson_progress`` and the
        session times when no row exists, so the only question a seeder has to answer is
        what that derivation says. It says what this returns — the same walk, with the same
        predicates, over the same rows.
        """
        history = await self._history(completions)
        if not history:
            return (), None

        unlocked: dict[str, tuple[datetime, int]] = {}
        claimed = await self._claimed_codes()
        xp_total = 0
        days: list[date] = []
        tier = LeagueTier.BRONZE
        promoted_at: datetime | None = None
        for done, row in enumerate(history):
            xp_total += row.xp
            days.append(local_date(row.completed_at, self._persona.timezone))
            _, longest = streak_runs(days, today=self._today)
            for achievement in ACHIEVEMENTS:
                if achievement.code in unlocked:
                    continue
                if is_condition_met(
                    achievement.condition,
                    xp_total=xp_total,
                    longest_streak=longest,
                    lessons_completed=done + 1,
                ):
                    unlocked[achievement.code] = (
                        row.completed_at,
                        condition_metric(
                            achievement.condition,
                            xp_total=xp_total,
                            longest_streak=longest,
                            lessons_completed=done + 1,
                        ),
                    )
            earned = league_for_xp(xp_total)
            if earned is not tier:
                tier = earned
                promoted_at = row.completed_at

        # The plan's own claims are added to whatever the account has already claimed, so a
        # demo account that has been used in the app does not have its rewards taken away.
        order = [achievement.code for achievement in ACHIEVEMENTS if achievement.code in unlocked]
        claimed |= set(order[: self._plan.claimed_achievements])
        by_code = {achievement.code: achievement for achievement in ACHIEVEMENTS}
        xp_total += sum(by_code[code].xp_reward for code in claimed)
        gems = sum(by_code[code].gems_reward for code in claimed)

        current, longest = streak_runs(days, today=self._today)
        desired = UserStats(
            user_id=self._user_id,
            xp_total=xp_total,
            gems=gems,
            current_streak=current,
            longest_streak=longest,
            last_activity_date=days[-1],
            league_tier=tier,
            league_updated_at=promoted_at,
        )
        return await self._unlock(unlocked, order), await self._stats_if_changed(desired)

    async def _history(self, completions: list[_Completion]) -> list[_Completion]:
        """Every finished lesson, planned and stored, oldest first and counted once."""
        rows = await self._uow.lesson_progress.list_for_user(self._user_id)
        stored = {
            row.lesson_id: _Completion(
                lesson_id=row.lesson_id,
                completed_at=ensure_aware(row.completed_at),
                xp=row.xp_earned,
            )
            for row in rows
            if row.state is ProgressState.COMPLETED and row.completed_at is not None
        }
        for completion in completions:
            # The stored row wins where the two overlap: it is the one the app would read.
            stored.setdefault(completion.lesson_id, completion)
        return sorted(stored.values(), key=lambda row: row.completed_at)

    async def _claimed_codes(self) -> set[str]:
        records = await self._uow.user_achievements.list_for_user(self._user_id)
        return {record.achievement_code for record in records if record.is_claimed}

    async def _unlock(
        self, unlocked: dict[str, tuple[datetime, int]], order: list[str]
    ) -> tuple[UserAchievement, ...]:
        """One row per achievement the history earns and the account does not have yet."""
        records: list[UserAchievement] = []
        claimed = set(order[: self._plan.claimed_achievements])
        claimed |= await self._claimed_codes()
        for code in order:
            if await self._uow.user_achievements.get(self._user_id, code) is not None:
                continue
            unlocked_at, progress = unlocked[code]
            records.append(
                UserAchievement(
                    id=self._id(f"achievement:{code}"),
                    user_id=self._user_id,
                    achievement_code=code,
                    unlocked_at=unlocked_at,
                    progress=progress,
                    claimed_at=unlocked_at if code in claimed else None,
                )
            )
        return tuple(records)

    async def _stats_if_changed(self, desired: UserStats) -> UserStats | None:
        """``desired`` when it differs from the stored row, so a rerun writes nothing."""
        row = await self._uow.user_stats.get(self._user_id)
        if row is None:
            return desired
        unchanged = (
            row.xp_total == desired.xp_total
            and row.gems == desired.gems
            and row.current_streak == desired.current_streak
            and row.longest_streak == desired.longest_streak
            and row.last_activity_date == desired.last_activity_date
            and row.league_tier == desired.league_tier
        )
        return None if unchanged else desired

    # ------------------------------------------------------------------ ids
    def _id(self, suffix: str) -> uuid.UUID:
        """The deterministic id of one planned row.

        ``uuid5`` of the persona's key and the row's place in the plan: a row the seeder
        wrote last time has the same id this time, which is what makes the guards cheap and
        the rerun a no-op.
        """
        return uuid.uuid5(SEED_NAMESPACE, f"activity:{self._persona.key}:{suffix}")


@dataclass(frozen=True, slots=True)
class _ReviewRules:
    """How the demo's review history is spaced and graded."""

    days_ago: tuple[int, ...]
    pass_pattern: tuple[bool, ...]

    def moments(self, today: date, timezone: str, card_index: int) -> tuple[datetime, ...]:
        """When each review of card ``card_index`` happened, oldest first."""
        return tuple(
            _local_instant(
                today - timedelta(days=offset),
                hour=20,
                minute=min(59, card_index * 3 + step),
                timezone=timezone,
            )
            for step, offset in enumerate(self.days_ago)
        )

    def passed(self, card_index: int, step: int) -> bool:
        if not self.pass_pattern:  # pragma: no cover - the fixture always declares one
            return True
        return self.pass_pattern[(card_index + step) % len(self.pass_pattern)]


# ------------------------------------------------------------------- writing
async def _write(uow: UnitOfWorkProtocol, plan: _Plan, *, owner: UserId | None) -> None:
    """Insert the plan. Commits nothing: the caller owns the transaction.

    Every row is written with ``save`` rather than ``add``, because the two are the same
    thing here (the id is either absent or the seeder's own) and ``save`` is the one that
    cannot turn a rerun into a primary-key failure.
    """
    for row in plan.sessions:
        await uow.quiz_sessions.save(row.session)
        for answer in row.answers:
            await uow.quiz_answers.save(answer)
        await uow.lesson_progress.save(row.progress)
    for card in plan.cards:
        await uow.review_cards.save(card)
    for review in plan.reviews:
        await uow.review_cards.save(review.card)
        for log in review.logs:
            await uow.review_logs.add(log)
    for thread in plan.threads:
        if thread.creates_conversation:
            await uow.ai_conversations.add(thread.conversation)
        for message in thread.messages:
            await uow.ai_conversations.add_message(message)
    if plan.achievements:
        # The join table references the catalogue, and the service that unlocks
        # achievements reconciles it from the same constant. Done here, after the plan was
        # built, so a preview never writes it.
        await uow.achievements.upsert_catalog(ACHIEVEMENTS)
        for record in plan.achievements:
            await uow.user_achievements.add(record)
    if plan.stats is not None and owner is not None:
        row = await uow.user_stats.get(owner)
        if row is None:
            await uow.user_stats.add(plan.stats)
        else:
            _apply_stats(plan.stats, row)
            await uow.user_stats.save(row)


def _apply_stats(desired: UserStats, row: UserStats) -> None:
    """Copy the values history decides onto the stored row, touching nothing else.

    ``freezes_available`` is deliberately left as it stands: a freeze is a purchase the
    seeder has no opinion about, and overwriting one would take away something the account
    actually holds.
    """
    row.xp_total = desired.xp_total
    row.gems = desired.gems
    row.current_streak = desired.current_streak
    row.longest_streak = desired.longest_streak
    row.last_activity_date = desired.last_activity_date
    row.league_tier = desired.league_tier
    if desired.league_updated_at is not None:
        row.league_updated_at = desired.league_updated_at
    row.updated_at = utc_now()


# ------------------------------------------------------------------- helpers
def _day_offsets(count: int, span: int) -> list[int]:
    """How many days before today each of ``count`` completions happened, oldest first.

    Spread evenly and ending today, so the run the streak reads is the run the plan
    intends: a completion on every day of the span whenever there are at least that many
    completions to place. A single completion is today's, whatever the span says.
    """
    if count <= 0:
        return []
    if span <= 1:
        return [0] * count
    return [round((count - 1 - index) * (span - 1) / (count - 1)) for index in range(count)]


def _misses(total: int, wrong: int) -> frozenset[int]:
    """Which questions are answered wrongly, spread across the lesson.

    Evenly spaced rather than a prefix, because a screen where every mistake is the last
    question in the lesson is a screen that never shows a mistake in the middle.
    """
    if wrong <= 0:
        return frozenset()
    return frozenset((step * total) // wrong for step in range(min(wrong, total)))


def _sitting_minutes(lesson: Lesson, settings: Settings) -> int:
    """How long a sitting took: the lesson's own estimate, inside the session lease.

    Clamped, because a completion after its lease expired is a session the engine would
    have refused, and the demo should not contain a state the product cannot reach.
    """
    lease = max(2, settings.quiz_session_ttl_seconds // 60)
    return max(1, min(lesson.estimated_minutes, lease - 1))


def _local_instant(day: date, *, hour: int, minute: int, timezone: str) -> datetime:
    """A UTC instant for a wall-clock moment in the learner's own timezone."""
    local = datetime.combine(day, time(hour, minute), tzinfo=get_timezone(timezone))
    return local.astimezone(UTC)


#: How far behind the clock a clamped instant is left. Nothing in the app reads a moment
#: closer to "now" than a client's own request time, so a few minutes of slack is invisible.
_CLAMP_MARGIN = timedelta(minutes=5)


def _before_now(instant: datetime, *, step: int) -> datetime:
    """``instant`` if it has already happened, otherwise just behind the clock.

    ``step`` separates the clamped instants by a second so that a run at 03:00 does not
    give a day's sittings one shared timestamp: the XP feed orders by ``(completed_at,
    id)`` and would still be correct, but "four lessons at exactly 03:00" reads as a bug.
    """
    latest = utc_now() - _CLAMP_MARGIN + timedelta(seconds=step)
    return min(instant, latest)


def _gradeable(question: Question) -> GradeableQuestion:
    """The question as grading sees it — the same projection ``QuizService`` makes.

    The key is read here and nowhere else in this module: the seeder grades through the
    engine's policy rather than deciding the verdict itself, so a question that is
    unanswerable fails the seed instead of being written as a wrong answer.
    """
    return GradeableQuestion(
        kind=question.kind,
        choices=question.choices,
        correct_index=question.correct_index,
        accepted_answers=question.accepted_answers,
        points=question.points,
        explanation=question.explanation,
    )


def _submitted(question: Question, *, correct: bool) -> SubmittedAnswer:
    """What the learner typed, tapped or arranged, given whether they meant to be right.

    One branch per kind, because a submission is *shaped* by its kind and a mis-shaped one is
    a validation error rather than a wrong answer: a single choice for a single-option
    question, a set for a multi-select, typed text for the free-text kinds, the items in
    sequence for an ordering question, and the pairs the learner built for a matching one.
    The seeder speaks the engine's vocabulary so the grader can refuse it — a fake that
    always looked acceptable would hide the very content bug this module exists to catch.

    Where the key lives differs by kind, and that is the whole reason the old
    choices-or-text shortcut was wrong: only a choice question keeps it in ``correct_index``,
    a multi-select and a matching question keep theirs in ``accepted_answers``, and an
    ordering question's key *is* its ``choices`` in authored order.
    """
    if question.kind in (QuestionKind.MULTIPLE_CHOICE, QuestionKind.TRUE_FALSE):
        index = _right_index(question) if correct else _wrong_index(question)
        return SubmittedAnswer(choice=question.choices[index])
    if question.kind is QuestionKind.MULTI_SELECT:
        return SubmittedAnswer(choices=_selections(question, correct=correct))
    if question.kind in (QuestionKind.FILL_BLANK, QuestionKind.WORD_BANK):
        if correct:
            return SubmittedAnswer(text=_typed_key(question))
        return SubmittedAnswer(text=_WRONG_TEXT)
    if question.kind is QuestionKind.ORDERING:
        return SubmittedAnswer(order=_arrangement(question, correct=correct))
    return SubmittedAnswer(matching=_matches(question, correct=correct))


def _given_answer(question: Question, *, correct: bool) -> dict[str, str]:
    """The same submission in the shape ``quiz_answers.given_answer`` stores.

    Mirrors ``QuizService._given_answer``: one key per submission field, joined with the
    separator the engine uses, so the stored record and the verdict it was marked against
    read the same way.
    """
    answer = _submitted(question, correct=correct)
    if answer.choice is not None:
        return {"choice": answer.choice}
    if answer.choices:
        return {"choices": "; ".join(answer.choices)}
    if answer.order:
        return {"order": " → ".join(answer.order)}
    if answer.matching:
        return {"pairs": "; ".join(answer.matching)}
    return {"text": answer.text or ""}


def _typed_key(question: Question) -> str:
    """The first accepted spelling of a free-text answer."""
    if not question.accepted_answers:
        raise _unkeyed(question)
    return question.accepted_answers[0]


def _selections(question: Question, *, correct: bool) -> tuple[str, ...]:
    """A multi-select submission: the whole key, or a set that cannot be the key.

    A wrong answer is built from the options the key does not name, so it is wrong by set
    equality rather than by luck. When every option is correct there is no distractor to
    send, and the honest wrong answer is an unfinished one — the key minus an option — which
    is a real thing a learner does on that screen.
    """
    key = tuple(question.accepted_answers)
    if not key:
        raise _unkeyed(question)
    if correct:
        return key
    folded = {answer.strip().lower() for answer in key}
    distractors = tuple(
        choice for choice in question.choices if choice.strip().lower() not in folded
    )
    if distractors:
        return distractors
    if len(key) > 1:
        return key[:-1]
    raise ValueError(
        f"Question {question.id!r} of kind {question.kind!r} has a single correct option, so "
        "no answer can be wrong on it. Publish a question with a distractor or a second "
        "correct option."
    )


def _arrangement(question: Question, *, correct: bool) -> tuple[str, ...]:
    """An ordering submission: the authored sequence, or it with two neighbours swapped.

    The key is ``choices`` in the order they were authored, and grading is element-wise, so
    swapping any adjacent pair is wrong for certain while still sending every item exactly
    once — which is the validity rule the grader enforces before it judges anything.
    """
    key = tuple(question.choices)
    if len(key) < 2:
        raise ValueError(
            f"Question {question.id!r} of kind {question.kind!r} has {len(key)} items, so "
            "there is no order to get wrong. Publish at least two items."
        )
    if correct:
        return key
    return (key[1], key[0], *key[2:])


def _matches(question: Question, *, correct: bool) -> tuple[str, ...]:
    """A matching submission: the authored pairs, or one pair pointed at another option.

    The verdict is set equality, so a wrong answer has to differ as a *set* and not merely
    in order. Rotating the pairs would look wrong but would be identical when two prompts
    share an option, so the wrong answer keeps every real pair but the first, and points
    that one prompt at a different option from the bank — a genuinely unfinished pairing the
    grader accepts as a submission before marking it wrong.
    """
    pairs = tuple(question.accepted_answers)
    if not pairs:
        raise _unkeyed(question)
    if correct:
        return pairs
    left, arrow, right = pairs[0].partition(_MATCH_ARROW)
    if not arrow:
        raise ValueError(
            f"Question {question.id!r} stores {pairs[0]!r} as a matching pair, which has no "
            f"{_MATCH_ARROW!r} in it. Publish the pair as 'left => right'."
        )
    taken = right.strip().lower()
    for choice in question.choices:
        if choice.strip().lower() != taken:
            return (f"{left.strip()} {_MATCH_ARROW} {choice.strip()}", *pairs[1:])
    raise ValueError(
        f"Question {question.id!r} of kind {question.kind!r} offers no option other than the "
        "one its first prompt answers, so no pairing can be wrong on it."
    )


def _unkeyed(question: Question) -> ValueError:
    return ValueError(
        f"Question {question.id!r} of kind {question.kind!r} has no answer key, so it "
        "cannot be practised. Publish a keyed question or leave it out of the lesson."
    )


def _right_index(question: Question) -> int:
    if question.correct_index is None:
        raise _unkeyed(question)
    return question.correct_index


def _wrong_index(question: Question) -> int:
    """The next choice round the ring: deterministic, and never the key."""
    return (_right_index(question) + 1) % len(question.choices)
