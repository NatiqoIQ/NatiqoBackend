"""Mint a password-reset token for the Postman run.

`POST /auth/forgot-password` deliberately returns no token and logs no token: in
production the only copy travels by email. That leaves a Postman run with no way to
reach `/auth/reset-password`.

This script stands in for the mail provider and nothing else. It calls the real
`AuthService.forgot_password` with a capturing delivery adapter and writes the raw
token to `reset_token.json`, which is exactly the value a learner would paste out of
their inbox. The endpoint under test therefore receives an authentic, unexpired,
correctly-hashed token rather than a fabricated one.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.core.database import dispose_engine, make_uow
from app.modules.users.application.auth_service import AuthService
from app.modules.users.application.dto import ForgotPasswordCommand
from app.modules.users.infrastructure.delivery import (
    InMemoryPasswordResetDelivery,
)

OUT = Path(__file__).with_name("reset_token.json")


async def main() -> int:
    email = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PM_EMAIL", "")
    if not email:
        print("usage: mint_reset_token.py <email>", file=sys.stderr)
        return 2

    settings = get_settings()
    delivery = InMemoryPasswordResetDelivery()

    async with make_uow() as uow:
        service = AuthService(uow, settings, reset_delivery=delivery)
        await service.forgot_password(ForgotPasswordCommand(email=email, ip_address="127.0.0.1"))

    if not delivery.sent:
        print(
            f"no reset token minted for {email!r}: the address has no authenticatable account",
            file=sys.stderr,
        )
        return 1

    sent = delivery.sent[-1]
    OUT.write_text(
        json.dumps(
            {
                "email": email,
                "token": sent.raw_token,
                "expires_at": sent.expires_at.isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    await dispose_engine()
    print(f"wrote {OUT.name} for {email}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
