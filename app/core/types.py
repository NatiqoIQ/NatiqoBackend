"""Shared, framework-free types used by every module.

Keeping these in one place prevents each bounded context from inventing its own
incompatible spelling of the same concept.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, NewType

from pydantic import AfterValidator, BeforeValidator

from app.core.time import ensure_aware

UserId = NewType("UserId", uuid.UUID)
ContentId = NewType("ContentId", uuid.UUID)
SessionId = NewType("SessionId", uuid.UUID)

_MONEY_QUANT = Decimal("0.000001")
Money = Annotated[Decimal, AfterValidator(lambda v: v.quantize(_MONEY_QUANT))]
Timestamp = Annotated[datetime, AfterValidator(ensure_aware)]
EmailAddress = Annotated[str, AfterValidator(lambda v: v.strip().lower())]


def new_id() -> uuid.UUID:
    """Primary key factory. UUIDv4 keeps IDs non-enumerable and sharding-friendly."""
    return uuid.uuid4()


def _coerce_datetime(value: object) -> object:
    return value


AwareDateTime = Annotated[datetime, BeforeValidator(_coerce_datetime), AfterValidator(ensure_aware)]


class CEFRLevel(StrEnum):
    """Common European Framework of Reference levels."""

    A1 = "A1"
    A2 = "A2"
    B1 = "B1"
    B2 = "B2"
    C1 = "C1"
    C2 = "C2"

    @property
    def rank(self) -> int:
        return _CEFR_RANK[self]

    def is_at_most(self, other: CEFRLevel) -> bool:
        return self.rank <= other.rank

    @classmethod
    def parse(cls, value: str | CEFRLevel) -> CEFRLevel:
        if isinstance(value, cls):
            return value
        try:
            return cls(value.strip().upper())
        except ValueError as exc:
            raise ValueError(f"Unknown CEFR level: {value!r}") from exc


_CEFR_RANK: dict[CEFRLevel, int] = {
    CEFRLevel.A1: 1,
    CEFRLevel.A2: 2,
    CEFRLevel.B1: 3,
    CEFRLevel.B2: 4,
    CEFRLevel.C1: 5,
    CEFRLevel.C2: 6,
}


class EnglishVariant(StrEnum):
    BRITISH = "british"
    AMERICAN = "american"
    BOTH = "both"


class ContentLanguage(StrEnum):
    """Teaching languages. ``IQ`` is Iraqi Arabic, used to bridge to English."""

    EN = "en"
    AR = "ar"
    IQ = "iq"


class Locale(StrEnum):
    EN_GB = "en-GB"
    AR_IQ = "ar-IQ"
    EN = "en"


class UserRole(StrEnum):
    """Authorization roles. Ordered from least to most privileged."""

    USER = "user"
    SUPPORT = "support"
    CONTENT_EDITOR = "content_editor"
    CONTENT_REVIEWER = "content_reviewer"
    ADMIN = "admin"

    @property
    def rank(self) -> int:
        return _ROLE_RANK[self]

    def at_least(self, other: UserRole) -> bool:
        return self.rank >= other.rank


_ROLE_RANK: dict[UserRole, int] = {
    UserRole.USER: 0,
    UserRole.SUPPORT: 1,
    UserRole.CONTENT_EDITOR: 2,
    UserRole.CONTENT_REVIEWER: 3,
    UserRole.ADMIN: 4,
}


class UserStatus(StrEnum):
    ACTIVE = "active"
    PENDING_VERIFICATION = "pending_verification"
    SUSPENDED = "suspended"
    DELETED = "deleted"


class SkillKind(StrEnum):
    """The five tracked learning skills (plan §48 separates knowledge from performance)."""

    VOCABULARY = "vocabulary"
    GRAMMAR = "grammar"
    LISTENING = "listening"
    READING = "reading"
    WRITING = "writing"
    PRONUNCIATION = "pronunciation"


class QuestionKind(StrEnum):
    """How a practice question is answered.

    A kind is only listed here once the attempt engine can score it: ``grade_answer`` has a
    branch for every value here, and a test enumerates the enum to keep that promise honest.
    Adding a kind without its grading rule would mean shipping a question type that silently
    scores every learner wrong.
    """

    MULTIPLE_CHOICE = "multiple_choice"
    MULTI_SELECT = "multi_select"
    TRUE_FALSE = "true_false"
    FILL_BLANK = "fill_blank"
    WORD_BANK = "word_bank"
    ORDERING = "ordering"
    MATCHING = "matching"


class ContentState(StrEnum):
    """Editorial lifecycle shared by every authored content aggregate."""

    DRAFT = "draft"
    REVIEW = "review"
    APPROVED = "approved"
    PUBLISHED = "published"
    ARCHIVED = "archived"

    def can_transition_to(self, target: ContentState) -> bool:
        return target in _CONTENT_TRANSITIONS[self]


_CONTENT_TRANSITIONS: dict[ContentState, frozenset[ContentState]] = {
    ContentState.DRAFT: frozenset({ContentState.REVIEW, ContentState.ARCHIVED}),
    ContentState.REVIEW: frozenset(
        {ContentState.DRAFT, ContentState.APPROVED, ContentState.ARCHIVED}
    ),
    ContentState.APPROVED: frozenset(
        {ContentState.PUBLISHED, ContentState.REVIEW, ContentState.ARCHIVED}
    ),
    ContentState.PUBLISHED: frozenset({ContentState.ARCHIVED}),
    ContentState.ARCHIVED: frozenset({ContentState.DRAFT}),
}
