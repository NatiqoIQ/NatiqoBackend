"""Activity seeder tests.

These run against a real SQLite schema, the real content seeder and the real repositories,
because what this seeder claims is mostly a property of the *database*: that a rerun writes
nothing, that a preview reports exactly what the run writes, and that every row it leaves
behind is one the app's own rules would have produced.

That last one is the theme of the file. A seeder is the easiest place in a codebase to
invent numbers, so the assertions here do not stop at "there are fourteen sessions" — they
re-grade the stored answers with the attempt engine's policy, check the SM-2 schedule chains
through its logs, and read the streak back through the progress surface a client calls.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from itertools import pairwise

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import create_all, dispose_engine, get_session_factory
from app.core.registry import import_all_models
from app.core.time import ensure_aware
from app.core.types import QuestionKind, UserId
from app.core.uow_sqlalchemy import SqlAlchemyUnitOfWork
from app.modules.ai.infrastructure.models import AiConversationModel, AiMessageModel
from app.modules.content.infrastructure.models import QuestionModel
from app.modules.gamification.application.service import GamificationService
from app.modules.gamification.infrastructure.models import (
    UserAchievementModel,
    UserStatsModel,
)
from app.modules.quiz.application.progress_service import ProgressService
from app.modules.quiz.application.review_service import ReviewService
from app.modules.quiz.domain.policy import GradeableQuestion, SubmittedAnswer, grade_answer
from app.modules.quiz.domain.review import ReviewState
from app.modules.quiz.infrastructure.models import (
    LessonProgressModel,
    QuizAnswerModel,
    QuizSessionModel,
)
from app.modules.quiz.infrastructure.review_models import ReviewCardModel, ReviewLogModel
from app.modules.users.infrastructure.models import UserModel
from scripts.seed import SeedAction, run_seed
from scripts.seed_content import run_seed as seed_content
from scripts.seed_personas import PERSONAS, SEED_NAMESPACE, SeedActivity

pytestmark = pytest.mark.integration

ADMIN_EMAIL = "admin@natiqo.app"
LEARNER_EMAIL = "amina@natiqo.app"

#: Every table the activity seeder can write to. Its idempotency and dry-run claims are made
#: against all of them, because a "wrote nothing" assertion that only covers the tables
#: someone remembered is the kind of assertion that hides the bug it was written for.
_ACTIVITY_MODELS = (
    QuizSessionModel,
    QuizAnswerModel,
    LessonProgressModel,
    ReviewCardModel,
    ReviewLogModel,
    AiConversationModel,
    AiMessageModel,
    UserStatsModel,
    UserAchievementModel,
)


# ------------------------------------------------------------------------ fixtures
@pytest_asyncio.fixture
async def schema() -> AsyncIterator[None]:
    """A fresh schema per test, the way every other integration suite builds one."""
    import_all_models()
    await create_all()
    yield
    await dispose_engine()


@pytest_asyncio.fixture
async def uow(schema: None) -> AsyncIterator[SqlAlchemyUnitOfWork]:
    """An *open* unit of work: the account seeder commits per persona on it."""
    async with SqlAlchemyUnitOfWork(get_session_factory()) as open_uow:
        yield open_uow


@pytest_asyncio.fixture
async def verify(schema: None) -> AsyncIterator[AsyncSession]:
    """A second connection, so assertions only ever see committed state."""
    async with get_session_factory()() as reader:
        yield reader


@pytest_asyncio.fixture
async def catalogue(schema: None) -> None:
    """The published course, which is what a learner's history is written against."""
    report = await seed_content(dry_run=False, include_drafts=False)
    assert report.written.total > 0


def _activity(key: str) -> SeedActivity:
    plan = next(persona for persona in PERSONAS if persona.key == key).activity
    assert plan is not None, f"{key} declares no activity plan"
    return plan


async def _user_id(verify: AsyncSession, email: str) -> UserId:
    row = await verify.scalar(select(UserModel).where(UserModel.email == email))
    assert row is not None, f"{email} was not seeded"
    return UserId(row.id)


async def _count(session: AsyncSession, model: type) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _counts(session: AsyncSession) -> dict[str, int]:
    return {model.__tablename__: await _count(session, model) for model in _ACTIVITY_MODELS}


# ================================================================ the history itself
@pytest.mark.asyncio
async def test_the_admin_can_be_shown_every_screen(catalogue, uow, settings, verify) -> None:
    """One account that answers the question "is this app actually alive?".

    Every number below is read back through the service a client calls rather than through
    SQL, so the test fails if the write and the read disagree as well as if the write is
    wrong.
    """
    report = await run_seed(uow, settings, only=["admin"])
    plan = _activity("admin")
    outcome = next(o for o in report.outcomes if o.key == "admin")
    admin_id = await _user_id(verify, ADMIN_EMAIL)

    assert outcome.action is SeedAction.CREATE
    assert outcome.activity.sessions == plan.lessons_completed + 2

    progress = ProgressService(uow)
    summary = await progress.summary(admin_id)
    assert summary.lessons_completed == plan.lessons_completed
    assert summary.lessons_in_progress >= 1
    assert summary.total_xp > 0
    assert summary.answered_count > summary.lessons_completed
    assert 0 < summary.accuracy_percent < 100
    # The streak is derived from the completion *dates*, so a plan that spreads its lessons
    # over nine days has to read back as a nine-day run.
    assert summary.current_streak_days == plan.study_span_days
    assert summary.longest_streak_days == plan.study_span_days
    assert summary.last_activity_at is not None

    # The map: the finished lessons are finished, the next one is half done, and the
    # lessons beyond it are still drawn.
    nodes = await progress.unlock_map(admin_id)
    assert len(nodes) > plan.lessons_completed
    assert sum(1 for node in nodes if node.state == "completed") == plan.lessons_completed
    assert any(node.state == "in_progress" for node in nodes)
    assert sum(1 for node in nodes if node.state == "available") > 0

    # The XP feed lists exactly the sittings that were completed.
    feed = await progress.xp_history(admin_id, limit=100)
    assert len(feed.items) == plan.lessons_completed
    assert all(event.xp_awarded > 0 for event in feed.items)

    review = ReviewService(uow)
    stats = await review.stats(admin_id)
    assert stats.total_cards > 0
    assert stats.reviewed_total == plan.reviews_reviewed * len(plan.review_days_ago)
    assert stats.retention_percent == 75
    # A queue with something in it, which is the state that screen exists to clear.
    assert stats.due_today > 0
    queue = await review.due_cards(admin_id, limit=10)
    assert queue.cards
    assert queue.due_count == stats.due_today
    assert queue.cards[0].question.prompt

    hub = await GamificationService(uow, settings).summary(admin_id)
    assert hub.stats.current_streak == plan.study_span_days
    assert hub.stats.longest_streak == plan.study_span_days
    # Claiming pays, so the hub total is the lessons' XP plus the claimed rewards.
    assert hub.stats.xp_total >= summary.total_xp
    assert hub.stats.gems > 0
    assert hub.lessons_completed == plan.lessons_completed
    assert hub.achievements_unlocked >= 3
    assert hub.achievements_unlocked < hub.achievements_total

    wall = await GamificationService(uow, settings).list_achievements(admin_id)
    claimed = [badge for badge in wall if badge.claimed_at is not None]
    assert len(claimed) == plan.claimed_achievements
    assert all(badge.unlocked for badge in claimed)
    assert all(not badge.claimable for badge in claimed)


@pytest.mark.asyncio
async def test_the_tutor_threads_are_readable_back(catalogue, uow, settings, verify) -> None:
    """A thread is only a thread if both sides of it are stored, and in order."""
    await run_seed(uow, settings, only=["admin"])
    plan = _activity("admin")

    threads = (
        await verify.scalars(select(AiConversationModel).order_by(AiConversationModel.created_at))
    ).all()
    assert len(threads) == len(plan.tutor_threads)

    for thread, declared in zip(threads, plan.tutor_threads, strict=True):
        messages = (
            await verify.scalars(
                select(AiMessageModel)
                .where(AiMessageModel.conversation_id == thread.id)
                .order_by(AiMessageModel.created_at, AiMessageModel.id)
            )
        ).all()
        # One reply per question, in the order they were asked.
        assert [message.role for message in messages] == ["user", "assistant"] * len(
            declared.learner_messages
        )
        assert [message.content for message in messages if message.role == "user"] == list(
            declared.learner_messages
        )
        assert all(message.content for message in messages if message.role == "assistant")
        # The thread's own clock is the last turn's: a list sorted by "recently active"
        # would otherwise float a thread above one that was actually spoken to later.
        assert ensure_aware(thread.updated_at) == max(
            ensure_aware(message.created_at) for message in messages
        )


@pytest.mark.asyncio
async def test_every_stored_answer_is_the_verdict_the_app_would_give(
    catalogue, uow, settings, verify
) -> None:
    """Re-grade the whole history from the answer key, from scratch.

    This is what keeps the seeder honest: if it ever stops asking the engine to grade and
    starts deciding for itself, the stored rows stop agreeing with the key and this fails.
    Points are compared as well as verdicts, because a row that is right about what the
    learner answered but wrong about what it was worth is just as broken.
    """
    await run_seed(uow, settings, only=["admin"])
    admin_id = await _user_id(verify, ADMIN_EMAIL)

    questions = {row.id: row for row in (await verify.scalars(select(QuestionModel))).all()}
    answers = (
        await verify.scalars(
            select(QuizAnswerModel)
            .join(QuizSessionModel, QuizSessionModel.id == QuizAnswerModel.session_id)
            .where(QuizSessionModel.user_id == admin_id)
        )
    ).all()
    assert answers, "nothing was answered"

    for answer in answers:
        question = questions[answer.question_id]
        submission = (
            SubmittedAnswer(choice=answer.given_answer["choice"])
            if "choice" in answer.given_answer
            else SubmittedAnswer(text=answer.given_answer["text"])
        )
        graded = grade_answer(
            GradeableQuestion(
                kind=QuestionKind(question.kind),
                choices=tuple(question.choices),
                correct_index=question.correct_index,
                accepted_answers=tuple(question.accepted_answers),
                points=question.points,
                explanation=question.explanation or "",
            ),
            submission,
        )
        assert graded.is_correct == answer.is_correct
        assert graded.points_awarded == answer.points_awarded
        assert answer.attempt == 1
        # The rationale is the copy the engine takes at grading time, not a re-worded one.
        assert answer.explanation == (question.explanation or "")

    assert any(not answer.is_correct for answer in answers), (
        "a history with no mistakes hides every screen that shows one"
    )


@pytest.mark.asyncio
async def test_the_review_schedule_chains_through_its_logs(
    catalogue, uow, settings, verify
) -> None:
    """Each reviewed card's numbers are SM-2's, and its log tells the same story."""
    await run_seed(uow, settings, only=["admin"])
    admin_id = await _user_id(verify, ADMIN_EMAIL)
    plan = _activity("admin")

    cards = (
        await verify.scalars(select(ReviewCardModel).where(ReviewCardModel.user_id == admin_id))
    ).all()
    reviewed = [card for card in cards if card.last_reviewed_at is not None]
    assert len(reviewed) == plan.reviews_reviewed
    assert all(card.state != ReviewState.NEW.value for card in reviewed)

    for card in reviewed:
        logs = (
            await verify.scalars(
                select(ReviewLogModel)
                .where(ReviewLogModel.card_id == card.id)
                .order_by(ReviewLogModel.reviewed_at)
            )
        ).all()
        assert len(logs) == len(plan.review_days_ago)
        assert logs[0].previous_interval_days == 0
        assert logs[-1].new_interval_days == card.interval_days
        for earlier, later in pairwise(logs):
            # Each review starts from where the last one left the card.
            assert later.previous_interval_days == earlier.new_interval_days
        # The due date is the last review plus the interval it produced, never a date
        # invented next to the schedule.
        assert ensure_aware(card.due_at) == ensure_aware(card.last_reviewed_at) + timedelta(
            days=card.interval_days
        )
        assert sum(1 for log in logs if log.quality < 3) == card.lapses


@pytest.mark.asyncio
async def test_the_ids_are_derived_from_the_persona(catalogue, uow, settings, verify) -> None:
    """The row ids are ``uuid5`` of the persona and the row's place in the plan.

    Asserted through the database rather than against the helper, because the derived id is
    what makes the next run recognise its own rows — and a helper that agreed with itself
    while the writer used something else would still be a seeder that duplicates.
    """
    await run_seed(uow, settings, only=["admin"])
    admin_id = await _user_id(verify, ADMIN_EMAIL)

    row = await verify.get(
        LessonProgressModel, uuid.uuid5(SEED_NAMESPACE, "activity:admin:progress:0")
    )
    assert row is not None
    assert row.user_id == admin_id
    assert (
        await verify.get(QuizSessionModel, uuid.uuid5(SEED_NAMESPACE, "activity:admin:session:0"))
        is not None
    )


@pytest.mark.asyncio
async def test_only_the_persona_that_declares_activity_gets_history(
    catalogue, uow, settings, verify
) -> None:
    """The pass is opt-in per persona, so the accounts without a plan are untouched by it."""
    await run_seed(uow, settings, only=["admin", "learner"])
    learner_id = await _user_id(verify, LEARNER_EMAIL)

    assert await _count(verify, LessonProgressModel) > 0
    assert (
        await verify.scalar(
            select(func.count())
            .select_from(LessonProgressModel)
            .where(LessonProgressModel.user_id == learner_id)
        )
        == 0
    )
    assert (
        await verify.scalar(
            select(func.count())
            .select_from(QuizSessionModel)
            .where(QuizSessionModel.user_id == learner_id)
        )
        == 0
    )


# ====================================================================== adoption
@pytest.mark.asyncio
async def test_adopting_a_legacy_admin_backfills_its_history(
    catalogue, uow, settings, verify
) -> None:
    """The case this exists for: a development database seeded before identities did.

    ``admin@natiqo.app`` is already there and no provider has ever signed in to it, so
    ``--adopt`` takes it over and the same run writes the history it was missing. A legacy
    demo database stops being a database of empty screens without being rebuilt.
    """
    legacy_id = UserId(uuid.uuid4())
    verify.add(
        UserModel(
            id=legacy_id,
            email=ADMIN_EMAIL,
            status="active",
            roles=["admin", "support"],
        )
    )
    await verify.commit()

    report = await run_seed(uow, settings, only=["admin"], adopt=True)
    outcome = next(o for o in report.outcomes if o.key == "admin")
    plan = _activity("admin")

    assert outcome.action is SeedAction.ENRICH
    assert outcome.user_id == legacy_id
    assert outcome.activity.rows > 0

    summary = await ProgressService(uow).summary(legacy_id)
    assert summary.lessons_completed == plan.lessons_completed
    assert summary.current_streak_days == plan.study_span_days
    assert (await ReviewService(uow).stats(legacy_id)).total_cards > 0
    threads = await verify.scalar(
        select(func.count())
        .select_from(AiConversationModel)
        .where(AiConversationModel.user_id == legacy_id)
    )
    assert threads == len(plan.tutor_threads)

    # And it is the seeder's account from now on, so a rerun is the ordinary no-op.
    second = await run_seed(uow, settings, only=["admin"])
    again = next(o for o in second.outcomes if o.key == "admin")
    assert again.action is SeedAction.ENRICH
    assert again.activity.rows == 0


# ====================================================================== reruns
@pytest.mark.asyncio
async def test_a_second_run_writes_nothing(catalogue, uow, settings, verify) -> None:
    """The convergence claim, measured against the database rather than the report."""
    await run_seed(uow, settings, only=["admin"])
    before = await _counts(verify)

    second = await run_seed(uow, settings, only=["admin"])
    after = await _counts(verify)

    outcome = next(o for o in second.outcomes if o.key == "admin")
    assert outcome.activity.rows == 0
    assert not outcome.changed
    assert before == after, "a rerun of the activity seed grew the database"


@pytest.mark.asyncio
async def test_a_lesson_a_real_attempt_already_finished_is_left_alone(
    catalogue, uow, settings, verify
) -> None:
    """A lesson this learner has already recorded keeps the record it has.

    Rows are matched by lesson, not by id, which is what protects an account that has
    actually used the app: it is filled in around, never overwritten.
    """
    await run_seed(uow, settings, only=["admin"])
    admin_id = await _user_id(verify, ADMIN_EMAIL)
    before = await _count(verify, LessonProgressModel)

    row = (
        await verify.scalars(
            select(LessonProgressModel)
            .where(LessonProgressModel.user_id == admin_id)
            .order_by(LessonProgressModel.completed_at)
        )
    ).first()
    assert row is not None
    original = (row.id, row.completed_at, row.xp_earned, row.best_score, row.attempts)

    await run_seed(uow, settings, only=["admin"])

    after = await verify.get(LessonProgressModel, row.id)
    assert after is not None
    assert (after.id, after.completed_at, after.xp_earned, after.best_score, after.attempts) == (
        original
    )
    assert await _count(verify, LessonProgressModel) == before


# ===================================================================== dry runs
@pytest.mark.asyncio
async def test_a_dry_run_writes_nothing(catalogue, uow, settings, verify) -> None:
    """Including the achievement catalogue, which only the write path reconciles."""
    report = await run_seed(uow, settings, only=["admin"], dry_run=True)

    outcome = next(o for o in report.outcomes if o.key == "admin")
    assert outcome.activity.rows > 0
    assert all(count == 0 for count in (await _counts(verify)).values())


@pytest.mark.asyncio
async def test_a_dry_run_predicts_the_run_it_previews(catalogue, uow, settings) -> None:
    """A preview that disagrees with the run it previews is worse than no preview.

    Compared field by field rather than as a whole, so a failure says *which* half of the
    plan the two passes disagree about.
    """
    preview = await run_seed(uow, settings, only=["admin"], dry_run=True)
    actual = await run_seed(uow, settings, only=["admin"])

    predicted = next(o for o in preview.outcomes if o.key == "admin").activity
    written = next(o for o in actual.outcomes if o.key == "admin").activity
    assert predicted.sessions == written.sessions
    assert predicted.answers == written.answers
    assert predicted.cards == written.cards
    assert predicted.reviews == written.reviews
    assert predicted.threads == written.threads
    assert predicted.messages == written.messages
    assert predicted.achievements == written.achievements
    assert predicted.stats_updated == written.stats_updated
    assert predicted == written


@pytest.mark.asyncio
async def test_a_dry_run_after_a_seed_predicts_nothing_further(catalogue, uow, settings) -> None:
    """The numbers must be a diff against what is stored, not the plan's own size."""
    await run_seed(uow, settings, only=["admin"])

    preview = await run_seed(uow, settings, only=["admin"], dry_run=True)
    actual = await run_seed(uow, settings, only=["admin"])

    before = next(o for o in preview.outcomes if o.key == "admin").activity
    after = next(o for o in actual.outcomes if o.key == "admin").activity
    assert before.rows == 0
    assert after == before


# ==================================================================== edge cases
@pytest.mark.asyncio
async def test_an_empty_catalogue_is_not_an_error(schema: None, uow, settings, verify) -> None:
    """A seeder run before the curriculum exists must still create the accounts.

    The history is anchored to published content, so with none published there is nothing
    to anchor it to — and an empty activity outcome, not a crash, is the right answer.
    """
    report = await run_seed(uow, settings, only=["admin"])

    outcome = next(o for o in report.outcomes if o.key == "admin")
    assert outcome.action is SeedAction.CREATE
    assert outcome.activity.rows == 0
    assert await _count(verify, UserModel) == 1


@pytest.mark.asyncio
async def test_a_question_with_no_answer_key_is_refused(catalogue, uow, settings, verify) -> None:
    """A choice question without a key cannot be practised, so the seed stops.

    The key is dropped straight into the row, because the domain aggregate refuses to hold
    a keyless choice question at all — which makes this the one state the seeder can meet
    in a database its own types say cannot exist. It must fail loudly rather than record
    an answer the learner never gave as wrong, and it does: the refusal comes from the
    content module's own reader, before the seeder has decided anything.
    """
    placements = await uow.lessons.list_published_ordered()
    first_lesson = await uow.questions.list_for_lessons([placements[0].lesson.id])
    assert any(question.choices for question in first_lesson), "no choice question to break"

    await verify.execute(
        update(QuestionModel)
        .where(
            QuestionModel.kind.in_(
                [QuestionKind.MULTIPLE_CHOICE.value, QuestionKind.TRUE_FALSE.value]
            )
        )
        .values(correct_index=None)
    )
    await verify.commit()

    with pytest.raises(ValueError, match="correct_index"):
        await run_seed(uow, settings, only=["admin"])


@pytest.mark.asyncio
async def test_an_unanswered_sitting_is_not_a_completed_one(
    catalogue, uow, settings, verify
) -> None:
    """The half-finished and abandoned sittings are noise the app has to draw correctly.

    Neither counts towards XP or the streak, both leave the lesson in progress, and the
    abandoned one is not in the XP feed — which is the whole reason to seed them.
    """
    await run_seed(uow, settings, only=["admin"])
    admin_id = await _user_id(verify, ADMIN_EMAIL)
    plan = _activity("admin")

    statuses = [row[0] for row in (await verify.execute(select(QuizSessionModel.status))).all()]
    assert statuses.count("abandoned") == plan.abandoned_sessions
    assert statuses.count("active") == int(plan.lesson_in_progress)
    assert statuses.count("completed") == plan.lessons_completed

    summary = await ProgressService(uow).summary(admin_id)
    # The unfinished lessons are in progress, and the streak counts only the finished ones.
    assert summary.lessons_in_progress == statuses.count("abandoned") + statuses.count("active")
    assert summary.current_streak_days == plan.study_span_days
    feed = await ProgressService(uow).xp_history(admin_id, limit=100)
    assert all(event.completed_at is not None for event in feed.items)


@pytest.mark.asyncio
async def test_the_report_names_what_the_activity_pass_wrote(catalogue, uow, settings) -> None:
    """The operator has to be able to see the second half of the seed in the report."""
    report = await run_seed(uow, settings, only=["admin"], dry_run=True)
    rendered = report.format()

    outcome = next(o for o in report.outcomes if o.key == "admin")
    assert "activity" in rendered
    assert outcome.activity.format() in rendered
    assert report.activity_rows == outcome.activity.rows


def test_the_plan_is_not_a_secret() -> None:
    """What a persona declares is data, and the plan is readable without a database."""
    plan = _activity("admin")

    assert plan.lessons_completed > 0
    assert plan.study_span_days >= max(plan.review_days_ago, default=0)
    assert plan.review_pass_pattern
    assert plan.score_pattern
    assert all(0 <= score <= 100 for score in plan.score_pattern)
    assert plan.tutor_threads
    assert all(thread.learner_messages for thread in plan.tutor_threads)
