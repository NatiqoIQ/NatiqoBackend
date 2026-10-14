"""Quiz domain: the aggregates, the grading rule and the persistence ports.

Framework-free. Nothing here imports FastAPI, SQLAlchemy or a cache client, so the rule
that decides right from wrong can be exercised on its own.
"""

from app.modules.quiz.domain.entities import (
    LessonProgress,
    ProgressState,
    QuizAnswer,
    QuizSession,
    SessionMode,
    SessionStatus,
)
from app.modules.quiz.domain.policy import (
    GradeableQuestion,
    GradedAnswer,
    SubmittedAnswer,
    grade_answer,
)
from app.modules.quiz.domain.ports import (
    LessonProgressRepositoryPort,
    QuizAnswerRepositoryPort,
    QuizSessionRepositoryPort,
)

__all__ = [
    "GradeableQuestion",
    "GradedAnswer",
    "LessonProgress",
    "LessonProgressRepositoryPort",
    "ProgressState",
    "QuizAnswer",
    "QuizAnswerRepositoryPort",
    "QuizSession",
    "QuizSessionRepositoryPort",
    "SessionMode",
    "SessionStatus",
    "SubmittedAnswer",
    "grade_answer",
]
