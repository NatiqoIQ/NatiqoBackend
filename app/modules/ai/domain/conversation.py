"""Tutor conversations (domain).

A conversation is what makes the AI tutor a *tutor* rather than a series of unrelated
answers: the learner's earlier turns are part of the context the next reply is written
from, and a learner returning to the app finds yesterday's thread instead of a blank page.

Two shapes, kept deliberately small:

* :class:`Conversation` — the thread. It belongs to one learner and may be anchored to the
  lesson that was open when it started, so the tutor's guidance can stay on-topic. The
  lesson is a bare id and not a foreign key: a conversation is the learner's record, and it
  must survive an editor retiring or revising the lesson it was about.
* :class:`Message` — one turn, either the learner's or the tutor's. Append-only: nothing
  edits a sentence that has already been said.

The port has no "update a message" verb for the same reason the usage log has none — the
transcript is evidence, and evidence you can rewrite is not evidence.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable

from app.core.types import UserId

__all__ = [
    "Conversation",
    "ConversationRepositoryPort",
    "Message",
    "MessageRole",
]


class MessageRole(StrEnum):
    """Who said a turn. The set is closed; the database enforces it with a CHECK."""

    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class Conversation:
    """One learner's tutor thread, optionally anchored to a lesson."""

    id: uuid.UUID
    user_id: UserId
    lesson_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Message:
    """One turn in a conversation. Written once, never updated."""

    id: uuid.UUID
    conversation_id: uuid.UUID
    role: str
    content: str
    created_at: datetime


@runtime_checkable
class ConversationRepositoryPort(Protocol):
    """Read/write access to tutor threads and their turns.

    Implementations must not commit: the unit of work owns the transaction, so a learner's
    turn and the tutor's reply either both land or neither does — a stored question with no
    answer would be a thread that lies about what was said.
    """

    async def add(self, conversation: Conversation) -> None: ...

    async def get_for_user(
        self, conversation_id: uuid.UUID, user_id: UserId
    ) -> Conversation | None:
        """Look a thread up under its owner.

        The ownership predicate is part of the query so another account's thread reads as
        absent, exactly as a review card does.
        """
        ...

    async def touch(self, conversation_id: uuid.UUID, *, at: datetime) -> None:
        """Record that a turn was appended, so the thread sorts by recent activity."""
        ...

    async def add_message(self, message: Message) -> None: ...

    async def list_messages(
        self, conversation_id: uuid.UUID, *, limit: int | None = None
    ) -> list[Message]:
        """A thread's turns in the order they were said.

        ``limit`` keeps the most recent ``limit`` turns (oldest-first within the window),
        which is what a reply needs: the latest exchange, not the first one.
        """
        ...
