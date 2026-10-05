"""Firebase Cloud Messaging adapter (production).

The Firebase Admin SDK is imported lazily so that environments without FCM
credentials — including CI — never need it installed.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from typing import Any

from app.core.logging import get_logger
from app.core.types import UserId
from app.platform.push.port import PushMessage, PushResult

logger = get_logger("push.fcm")

#: FCM collapses a burst to at most 500 distinct registration tokens per multicast.
_MULTICAST_CHUNK = 500


class FcmPush:
    """Sends via the Firebase Admin SDK.

    The SDK is reached through the module-level ``messaging`` functions with an explicit
    ``app=`` argument. ``App.messaging()`` existed in firebase-admin 4/5 but was removed
    in 6.0, so calling it raises ``AttributeError`` against a current install.
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    @classmethod
    def from_settings(cls) -> FcmPush:
        import firebase_admin
        from firebase_admin import credentials

        from app.core.config import get_settings

        settings = get_settings()
        if not settings.fcm_credentials_json:
            raise RuntimeError("FCM_CREDENTIALS_JSON is not configured")
        cred = credentials.Certificate(json.loads(settings.fcm_credentials_json))
        try:
            app = firebase_admin.get_app(settings.fcm_project_id or "natiqo")
        except ValueError:
            app = firebase_admin.initialize_app(cred, {"projectId": settings.fcm_project_id})
        logger.info("push.fcm_initialised", project=settings.fcm_project_id)
        return cls(app)

    @staticmethod
    def _data(message: PushMessage) -> dict[str, str]:
        data = {k: str(v) for k, v in message.data.items() if not k.startswith("__")}
        data["type"] = str(message.type)
        if message.target:
            data["target"] = message.target
        return data

    async def send_to_user(
        self, user_id: UserId, tokens: Sequence[str], message: PushMessage
    ) -> PushResult:
        if not tokens:
            return PushResult(success_count=0, failure_count=0)

        def _send() -> PushResult:
            from firebase_admin import messaging as fb_messaging

            success = 0
            failures = 0
            invalid: list[str] = []
            for start in range(0, len(tokens), _MULTICAST_CHUNK):
                chunk = list(tokens[start : start + _MULTICAST_CHUNK])
                batch = fb_messaging.MulticastMessage(
                    tokens=chunk,
                    notification=fb_messaging.Notification(title=message.title, body=message.body),
                    data=self._data(message),
                )
                response = fb_messaging.send_each_for_multicast(batch, app=self._app)
                success += response.success_count
                failures += response.failure_count
                for index, result in enumerate(response.responses):
                    if not result.success and _is_registration_error(result.exception):
                        invalid.append(chunk[index])
            return PushResult(
                success_count=success, failure_count=failures, invalid_tokens=tuple(invalid)
            )

        return await asyncio.to_thread(_send)

    async def send_to_users(
        self, recipients: dict[UserId, Sequence[str]], message: PushMessage
    ) -> PushResult:
        success = 0
        failures = 0
        invalid: list[str] = []
        for user_id, tokens in recipients.items():
            result = await self.send_to_user(user_id, tokens, message)
            success += result.success_count
            failures += result.failure_count
            invalid.extend(result.invalid_tokens)
        return PushResult(
            success_count=success, failure_count=failures, invalid_tokens=tuple(invalid)
        )

    async def validate_token(self, token: str) -> bool:
        return isinstance(token, str) and len(token) > 20

    async def ping(self) -> bool:
        return True


def _is_registration_error(exception: Any) -> bool:
    """FCM marks dead tokens with sender-id / unregistered messaging errors."""
    code = getattr(exception, "code", "") or ""
    return any(marker in str(code).lower() for marker in ("registration-token", "unregistered"))
