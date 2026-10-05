"""Logging push adapter.

Selected automatically when ``FCM_CREDENTIALS_JSON`` is absent. Deliveries are
still recorded in ``notification_deliveries`` so the UI and the audit trail stay
correct even though nothing leaves the process.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.core.logging import get_logger
from app.core.types import UserId
from app.platform.push.port import PushMessage, PushResult

logger = get_logger("push.logging")


class LoggingPush:
    async def send_to_user(
        self, user_id: UserId, tokens: Sequence[str], message: PushMessage
    ) -> PushResult:
        logger.info(
            "push.logged",
            user_id=str(user_id),
            push_type=str(message.type),
            title=message.title,
            token_count=len(tokens),
        )
        return PushResult(success_count=len(tokens), failure_count=0)

    async def send_to_users(
        self, recipients: dict[UserId, Sequence[str]], message: PushMessage
    ) -> PushResult:
        sent = 0
        for user_id, tokens in recipients.items():
            result = await self.send_to_user(user_id, tokens, message)
            sent += result.success_count
        return PushResult(success_count=sent, failure_count=0)

    async def validate_token(self, token: str) -> bool:
        return bool(token)

    async def ping(self) -> bool:
        return True
