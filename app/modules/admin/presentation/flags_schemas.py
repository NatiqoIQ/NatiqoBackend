"""Wire schemas for runtime feature flags."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from app.core.responses import WireModel


class FlagResponse(WireModel):
    key: str
    enabled: bool
    rollout_percentage: int
    public: bool
    description: str | None = None
    updated_at: datetime | None = None


class FlagUpdateRequest(WireModel):
    """A full PUT, not a PATCH: a flag's policy is four small values that read together."""

    enabled: bool
    rollout_percentage: int = Field(default=100, ge=0, le=100)
    public: bool = False
    description: str | None = Field(default=None, max_length=200)


class PublicFlagsResponse(WireModel):
    """Evaluated for the caller, so a client does not reimplement the bucketing."""

    flags: dict[str, bool]


__all__ = ["FlagResponse", "FlagUpdateRequest", "PublicFlagsResponse"]
