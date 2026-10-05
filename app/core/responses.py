"""Response envelopes shared by every endpoint.

Two guarantees the Flutter client relies on:

1. Errors always look like ``{"error": {"code", "message", "details?", "requestId"}}``.
   Clients branch on ``code``; ``message`` is for humans only.
2. Collections are always paginated with a stable cursor shape.

Every model in the API inherits :class:`WireModel`, which is what makes the JSON camelCase.
That is a client-facing wire decision, not a Python naming preference, so it is declared once
here instead of hand-writing ``Field(alias=...)`` on each field — which is exactly the kind
of thing that gets forgotten on the one model nobody tests.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


class WireModel(BaseModel):
    """Base for every model that crosses the HTTP boundary.

    Serialises to camelCase, because the JSON is a contract with a Flutter client whose
    lints treat ``nextCursor`` as correct and ``next_cursor`` as a naming bug — the Python
    convention and the Dart convention disagree exactly on multi-word names.

    ``populate_by_name`` keeps every field constructible and parseable by its Python name,
    which matters twice over: tests and services build models with keyword arguments, and
    requests are accepted under *either* spelling. That last part is the migration story —
    snake_case clients keep working, so this does not have to land as a single breaking
    deploy alongside a client release.

    Serialisation is validated on the way out, so a field whose alias cannot be generated
    fails loudly here rather than silently appearing under its Python name.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        serialize_by_alias=True,
    )


class ErrorBody(WireModel):
    code: str = Field(description="Stable machine-readable error code")
    message: str = Field(description="Human-readable description (do not parse)")
    details: dict[str, Any] | None = Field(default=None)
    request_id: str | None = Field(default=None)


class ErrorResponse(WireModel):
    """The single error shape returned by the API."""

    error: ErrorBody


class PageMeta(WireModel):
    limit: int
    has_more: bool
    next_cursor: str | None = None


class Page[T](WireModel):
    """Cursor-paginated collection."""

    items: list[T]
    meta: PageMeta


def build_page[T](
    items: list[T],
    *,
    limit: int,
    has_more: bool,
    next_cursor: str | None = None,
) -> Page[T]:
    return Page(
        items=items,
        meta=PageMeta(limit=limit, has_more=has_more, next_cursor=next_cursor),
    )
