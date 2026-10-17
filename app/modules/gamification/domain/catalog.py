"""The shipped achievement catalogue.

Defined in code, not only in the database, for the same reason the model registry is an
explicit list: the set of achievements is a reviewable artifact. The table
(``achievements``) mirrors this so admin tooling and translations can reach it without a
deploy, and the service reconciles the table from this constant on first use, so the table
can never silently drift from the code that evaluates it.

Adding an achievement is adding one entry here. Nothing else needs a branch.
"""

from __future__ import annotations

from app.modules.gamification.domain.entities import Achievement

__all__ = ["ACHIEVEMENTS"]


ACHIEVEMENTS: tuple[Achievement, ...] = (
    Achievement(
        code="first_lesson",
        title_key="achievement.first_lesson.title",
        description_key="achievement.first_lesson.description",
        icon_key="seedling",
        condition={"type": "lessons_completed", "count": 1},
        xp_reward=20,
        gems_reward=5,
        sort_order=10,
    ),
    Achievement(
        code="ten_lessons",
        title_key="achievement.ten_lessons.title",
        description_key="achievement.ten_lessons.description",
        icon_key="books",
        condition={"type": "lessons_completed", "count": 10},
        xp_reward=50,
        gems_reward=10,
        sort_order=20,
    ),
    Achievement(
        code="twenty_five_lessons",
        title_key="achievement.twenty_five_lessons.title",
        description_key="achievement.twenty_five_lessons.description",
        icon_key="library",
        condition={"type": "lessons_completed", "count": 25},
        xp_reward=120,
        gems_reward=25,
        sort_order=30,
    ),
    Achievement(
        code="xp_century",
        title_key="achievement.xp_century.title",
        description_key="achievement.xp_century.description",
        icon_key="spark",
        condition={"type": "xp", "total": 100},
        xp_reward=0,
        gems_reward=10,
        sort_order=40,
    ),
    Achievement(
        code="xp_veteran",
        title_key="achievement.xp_veteran.title",
        description_key="achievement.xp_veteran.description",
        icon_key="medal",
        condition={"type": "xp", "total": 500},
        xp_reward=0,
        gems_reward=30,
        sort_order=50,
    ),
    Achievement(
        code="streak_three",
        title_key="achievement.streak_three.title",
        description_key="achievement.streak_three.description",
        icon_key="flame",
        condition={"type": "streak", "days": 3},
        xp_reward=25,
        gems_reward=5,
        sort_order=60,
    ),
    Achievement(
        code="streak_week",
        title_key="achievement.streak_week.title",
        description_key="achievement.streak_week.description",
        icon_key="flame",
        condition={"type": "streak", "days": 7},
        xp_reward=75,
        gems_reward=20,
        sort_order=70,
    ),
    Achievement(
        code="streak_month",
        title_key="achievement.streak_month.title",
        description_key="achievement.streak_month.description",
        icon_key="flame",
        condition={"type": "streak", "days": 30},
        xp_reward=250,
        gems_reward=60,
        sort_order=80,
    ),
)
