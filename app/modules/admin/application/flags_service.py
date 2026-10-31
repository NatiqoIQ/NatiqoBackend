"""Runtime feature flags (F24).

A flag is a row, not a deploy. Reading one must not cost a query per request, so the
whole table is cached as one snapshot and the cache is invalidated when an operator
writes. A cache outage falls through to the database: flags are configuration, and a
missing cache is a slow read, not a reason to fail a request.

Rollout is bucketed by a stable hash of ``key`` and ``user_id``. That means the same
learner sees the same side of a 10% launch on every device and every restart — a random
coin per request would make a half-shipped feature look broken to the people in it.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.modules.admin.infrastructure.models import AuditLogModel, FeatureFlagModel
from app.platform.cache.port import CachePort

logger = get_logger(__name__)

_CACHE_KEY = "flags:snapshot:v1"
_DEFAULT_TTL_SECONDS = 30


@dataclass(frozen=True, slots=True)
class FeatureFlag:
    """A flag's policy, detached from the ORM row so the cache can hold it."""

    key: str
    enabled: bool
    rollout_percentage: int
    public: bool
    description: str | None
    updated_at: datetime | None = None

    def bucket(self, user_id: uuid.UUID | None) -> int:
        """Stable 0..99 bucket for ``(key, user)``.

        Anonymous callers are bucketed by key alone: the answer must not change between
        two requests from the same signed-out device. A percentage rollout is then a
        global switch for them, which is the honest behaviour — there is no one to
        assign consistently.
        """
        subject = f"{self.key}:{user_id}" if user_id is not None else self.key
        digest = hashlib.sha256(subject.encode("utf-8")).digest()
        return int.from_bytes(digest[:4], "big") % 100

    def is_on_for(self, user_id: uuid.UUID | None) -> bool:
        if not self.enabled:
            return False
        if self.rollout_percentage >= 100:
            return True
        if self.rollout_percentage <= 0:
            return False
        return self.bucket(user_id) < self.rollout_percentage


def _encode(flags: dict[str, FeatureFlag]) -> str:
    return json.dumps(
        {
            key: {
                **asdict(flag),
                "updated_at": flag.updated_at.isoformat() if flag.updated_at else None,
            }
            for key, flag in flags.items()
        },
        separators=(",", ":"),
    )


def _decode(raw: str) -> dict[str, FeatureFlag]:
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("flag snapshot is not an object")
    flags: dict[str, FeatureFlag] = {}
    for key, payload in parsed.items():
        updated = payload.get("updated_at")
        flags[key] = FeatureFlag(
            key=key,
            enabled=bool(payload["enabled"]),
            rollout_percentage=int(payload["rollout_percentage"]),
            public=bool(payload["public"]),
            description=payload.get("description"),
            updated_at=datetime.fromisoformat(updated) if updated else None,
        )
    return flags


class FeatureFlagService:
    """Read/evaluate flags, and write them with an audit row in the same transaction."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        cache: CachePort,
        *,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self._session_factory = session_factory
        self._cache = cache
        self._ttl_seconds = ttl_seconds

    async def _cached(self) -> dict[str, FeatureFlag] | None:
        try:
            raw = await self._cache.get(_CACHE_KEY)
        except Exception as exc:  # noqa: BLE001 - a cache outage must not fail the read
            logger.warning("flags.cache_read_failed", error=str(exc)[:200])
            return None
        if not raw:
            return None
        try:
            return _decode(raw)
        except (ValueError, KeyError, TypeError) as exc:
            logger.warning("flags.snapshot_corrupt", error=str(exc)[:200])
            return None

    async def _store_cache(self, flags: dict[str, FeatureFlag]) -> None:
        try:
            await self._cache.set(_CACHE_KEY, _encode(flags), ttl_seconds=self._ttl_seconds)
        except Exception as exc:  # noqa: BLE001 - a failed write is just a miss next time
            logger.warning("flags.cache_write_failed", error=str(exc)[:200])

    async def snapshot(self) -> dict[str, FeatureFlag]:
        cached = await self._cached()
        if cached is not None:
            return cached
        async with self._session_factory() as session:
            rows = (await session.execute(select(FeatureFlagModel))).scalars().all()
            flags = {
                row.key: FeatureFlag(
                    key=row.key,
                    enabled=row.enabled,
                    rollout_percentage=row.rollout_percentage,
                    public=row.public,
                    description=row.description,
                    updated_at=row.updated_at,
                )
                for row in rows
            }
        await self._store_cache(flags)
        return flags

    async def evaluate(
        self,
        key: str,
        *,
        user_id: uuid.UUID | None = None,
        default: bool = False,
    ) -> bool:
        flags = await self.snapshot()
        flag = flags.get(key)
        return default if flag is None else flag.is_on_for(user_id)

    async def public_flags(self, *, user_id: uuid.UUID | None = None) -> dict[str, bool]:
        flags = await self.snapshot()
        return {key: flag.is_on_for(user_id) for key, flag in flags.items() if flag.public}

    async def upsert(
        self,
        key: str,
        *,
        enabled: bool,
        rollout_percentage: int,
        public: bool,
        description: str | None,
        actor_id: uuid.UUID | None,
    ) -> FeatureFlag:
        """Create or update a flag and its audit row in one transaction."""
        async with self._session_factory() as session:
            row = await session.get(FeatureFlagModel, key)
            if row is None:
                row = FeatureFlagModel(
                    key=key,
                    enabled=enabled,
                    rollout_percentage=rollout_percentage,
                    public=public,
                    description=description,
                    updated_by=actor_id,
                )
                session.add(row)
            else:
                row.enabled = enabled
                row.rollout_percentage = rollout_percentage
                row.public = public
                row.description = description
                row.updated_by = actor_id
            session.add(
                AuditLogModel(
                    actor_id=actor_id,
                    action="flags.update",
                    entity_type="feature_flag",
                    entity_id=None,
                    summary=(
                        f"flag {key}: enabled={enabled}, rollout={rollout_percentage}%, "
                        f"public={public}"
                    ),
                    changes={
                        "key": key,
                        "enabled": enabled,
                        "rollout_percentage": rollout_percentage,
                        "public": public,
                    },
                )
            )
            await session.commit()
            await session.refresh(row)
            flag = FeatureFlag(
                key=row.key,
                enabled=row.enabled,
                rollout_percentage=row.rollout_percentage,
                public=row.public,
                description=row.description,
                updated_at=row.updated_at,
            )
        try:
            await self._cache.delete(_CACHE_KEY)
        except Exception as exc:  # noqa: BLE001 - stale for one TTL is acceptable
            logger.warning("flags.cache_invalidate_failed", error=str(exc)[:200])
        return flag


__all__ = ["FeatureFlag", "FeatureFlagService"]
