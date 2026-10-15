"""The audit trail: a durable record of every editorial mutation.

The admin surface is protected twice over. The role check stops the *wrong* caller; the
audit row answers the question a role check cannot — who changed this, and to what — after
the fact. A permission model that cannot be audited is a permission model nobody can
review, and "which editor unpublished this lesson" is exactly the question an incident
review asks.

The action strings are namespaced (``content.lesson.publish``) rather than free text so a
log query can group by them without guessing at spelling, and so a future read surface can
filter on a stable vocabulary.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.core.types import UserId

__all__ = [
    "ENTITY_LESSON",
    "ENTITY_LEVEL",
    "ENTITY_QUESTION",
    "ENTITY_UNIT",
    "ENTITY_USER",
    "AuditAction",
    "AuditEntry",
]

#: Entity labels written into ``audit_logs.entity_type``. Kept short and singular so a
#: row reads like a sentence: ``actor 12 content.lesson.publish lesson a1b2``.
ENTITY_LEVEL = "level"
ENTITY_UNIT = "unit"
ENTITY_LESSON = "lesson"
ENTITY_QUESTION = "question"
ENTITY_USER = "user"


class AuditAction(StrEnum):
    """Every mutation the admin surface records."""

    LEVEL_CREATE = "content.level.create"
    LEVEL_UPDATE = "content.level.update"
    LEVEL_DELETE = "content.level.delete"

    UNIT_CREATE = "content.unit.create"
    UNIT_UPDATE = "content.unit.update"
    UNIT_DELETE = "content.unit.delete"

    LESSON_CREATE = "content.lesson.create"
    LESSON_UPDATE = "content.lesson.update"
    LESSON_DELETE = "content.lesson.delete"
    LESSON_PUBLISH = "content.lesson.publish"
    LESSON_REORDER_QUESTIONS = "content.lesson.reorder_questions"

    QUESTION_CREATE = "content.question.create"
    QUESTION_UPDATE = "content.question.update"
    QUESTION_DELETE = "content.question.delete"

    USER_ROLES_UPDATE = "user.roles.update"


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One recorded mutation.

    ``changes`` is a small, JSON-serialisable snapshot of what the write carried — not a
    full before/after diff, which would double the table for a completeness nobody reads.
    It holds the fields an investigator would need to reconstruct intent: the new state,
    the moved position, the assigned roles.
    """

    actor_id: UserId | None
    action: str
    entity_type: str
    entity_id: uuid.UUID | None
    summary: str
    changes: dict[str, Any] = field(default_factory=dict)
