"""Content catalogue: the read model behind "what can I study?".

The module owns the ``Level → Unit → Lesson → Question`` tree, the rules about what may
become visible, and the read-only endpoints that serve it. Authoring and publishing
operations are deliberately not here yet; they arrive as their own slice so the
learner-facing contract can settle first.
"""

from app.modules.content.domain.entities import (
    InvalidContentState,
    Lesson,
    Level,
    Question,
    Unit,
)
from app.modules.content.domain.ports import (
    LessonRepositoryPort,
    LevelRepositoryPort,
    QuestionRepositoryPort,
    UnitRepositoryPort,
)

__all__ = [
    "InvalidContentState",
    "Lesson",
    "LessonRepositoryPort",
    "Level",
    "LevelRepositoryPort",
    "Question",
    "QuestionRepositoryPort",
    "Unit",
    "UnitRepositoryPort",
]
