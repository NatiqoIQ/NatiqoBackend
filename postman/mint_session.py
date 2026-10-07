"""Mint a session for the Postman run, standing in for the identity provider.

    uv run python ./postman/mint_session.py someone@example.com

The API verifies an ID token against Google's or Apple's published keys, so there is no
token this repository could hand a test client that the real server would accept — and
adding an endpoint, or a request header, that makes it accept one would put an
account-takeover path into development to make testing convenient.

So the collection gets its bearer token the way ``scripts/seed.py`` gets its accounts:
by calling the real ``login_with_identity`` use case with a stub verifier, against the
database the server under test is reading. Everything except the signature check is
production code — account creation, the identity row, profile and settings defaults,
and the first refresh chain all travel the same path a real sign-in does.

``run.ps1`` sets ``DATABASE_URL`` for the whole run, so this script inherits the same
file the server opened and the tokens it writes are recognised by it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from app.core.config import Settings, get_settings
from app.core.database import dispose_engine, make_uow
from app.core.logging import get_logger
from app.core.registry import import_all_models
from app.modules.users.application.auth_service import AuthService
from app.modules.users.application.dto import DeviceContext, SocialLoginCommand
from app.modules.users.domain.identity import (
    IdentityProvider,
    IdentityVerificationError,
    VerifiedIdentity,
)
from app.modules.users.domain.policy import normalise_email

logger = get_logger("postman.mint_session")

#: Where ``run.ps1`` looks for the tokens. Named rather than a bare path so the two
#: files cannot disagree about it.
OUTPUT_PATH = Path(__file__).with_name("session.json")

#: Seeded identities live under ``google``; this one does too, so a minted account is
#: indistinguishable in shape from a real sign-in and no extra provider needs
#: configuring for the collection to run.
PROVIDER = IdentityProvider.GOOGLE

#: A device is attached so ``/me/devices`` has a row to return. The push token is
#: deliberately not an FCM-shaped value: nothing may try to deliver to it.
DEVICE = DeviceContext(platform="android", push_token="pm-device-token-0001", app_version="1.0.0")


class MintedIdentityVerifier:
    """A verifier that vouches for exactly the identity this script was told to mint.

    ``verify`` still refuses anything else, so the object it stands in for keeps its
    shape: the use case is given a verifier, not a bypass.
    """

    provider = PROVIDER

    def __init__(self, identity: VerifiedIdentity) -> None:
        self._identity = identity

    async def verify(self, id_token: str) -> VerifiedIdentity:
        if id_token != self._identity.subject:
            raise IdentityVerificationError("unexpected_token")
        return self._identity


async def run(email: str, subject: str, settings: Settings) -> dict[str, str]:
    """Sign the account in and return the fields ``run.ps1`` needs."""
    import_all_models()

    normalised = normalise_email(email)
    identity = VerifiedIdentity(
        provider=PROVIDER,
        subject=subject,
        email=normalised,
        email_verified=True,
        display_name="Postman Runner",
    )

    uow = make_uow()
    try:
        async with uow:
            service = AuthService(
                uow, settings, verifiers={PROVIDER: MintedIdentityVerifier(identity)}
            )
            result = await service.login_with_identity(
                SocialLoginCommand(
                    provider=PROVIDER,
                    id_token=subject,
                    device=DEVICE,
                    display_name=identity.display_name,
                )
            )
    finally:
        await dispose_engine()

    return {
        "email": normalised,
        "accessToken": result.tokens.access_token,
        "refreshToken": result.tokens.refresh_token,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email", help="the address to sign in with; created on first use")
    parser.add_argument(
        "--subject",
        default=None,
        help="the provider subject to key the account on (default: derived from the email)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=OUTPUT_PATH,
        help=f"where to write the tokens (default: {OUTPUT_PATH.name})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    subject = args.subject or f"postman:{normalise_email(args.email)}"

    try:
        session = asyncio.run(run(args.email, subject, get_settings()))
    except Exception as exc:  # noqa: BLE001 - a failed mint must print, not traceback
        print(f"Could not mint a session for {args.email}: {exc}", file=sys.stderr)
        return 1

    args.output.write_text(json.dumps(session, indent=2) + "\n", encoding="utf-8")
    logger.info("postman.session_minted", email=session["email"], output=str(args.output))
    print(f"Minted a session for {session['email']} -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
