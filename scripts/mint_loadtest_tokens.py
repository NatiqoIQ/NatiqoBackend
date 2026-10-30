"""Mint a pool of long-lived sessions for load testing.

    uv run python -m scripts.mint_loadtest_tokens --count 5000 --output loadtest/tokens.json

The capacity tests in ``loadtest/k6/natiqo-mixed.js`` need one account per virtual
user. A per-IP *or* per-user rate limit cannot be measured honestly against a single
shared session, and the development sign-in creates exactly one account. This script
fills that gap the same way ``postman/mint_session.py`` does: it calls the real
``AuthService.login_with_identity`` use case with a stub verifier, so account creation,
the identity row, profile and settings defaults, and the first refresh chain all travel
production code. Only the provider's signature check is stood in for — there is no
network and no credential in the repository that a real Google/Apple verifier would
accept.

**This is a load-test tool, not an authentication bypass.** Two properties enforce
that:

* the tokens are minted directly against the database rather than through an HTTP
  endpoint, so no route exists that would accept a forged provider token;
* ``APP_ENV=production`` is refused unless ``LOADTEST_MINT_ALLOW_PRODUCTION=1`` is set
  explicitly, because a long-lived token for a real account is exactly the kind of
  artefact that must never be produced by accident on a live database.

Accounts are deterministic (``loadtest-00001@loadtest.invalid``), so rerunning the
script logs the same accounts in again rather than creating more. The output file
contains live credentials: it is written with mode ``0600`` and should never be
committed. The repository's ``.gitignore`` already covers ``loadtest/tokens.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from app.core.config import Settings, get_settings
from app.core.database import dispose_engine, make_uow
from app.core.logging import configure_logging, get_logger
from app.core.registry import import_all_models
from app.modules.users.application.auth_service import AuthService
from app.modules.users.application.dto import DeviceContext, SocialLoginCommand
from app.modules.users.domain.identity import (
    IdentityProvider,
    IdentityVerificationError,
    VerifiedIdentity,
)
from app.modules.users.domain.policy import normalise_email

logger = get_logger("scripts.mint_loadtest_tokens")

PROVIDER = IdentityProvider.GOOGLE

#: RFC 2606 reserves ``.invalid``: no mail can ever leave for these addresses.
DEFAULT_DOMAIN = "loadtest.invalid"

#: Enough for a soak test and short enough that a leaked file ages out on its own.
DEFAULT_TTL_SECONDS = 8 * 60 * 60

MAX_COUNT = 50_000


class _MintedIdentityVerifier:
    """Stands in for the provider, vouching for exactly one identity."""

    provider = PROVIDER

    def __init__(self, identity: VerifiedIdentity) -> None:
        self._identity = identity

    async def verify(self, id_token: str) -> VerifiedIdentity:
        if id_token != self._identity.subject:
            raise IdentityVerificationError("unexpected_token")
        return self._identity


async def _mint_one(index: int, *, args: argparse.Namespace, settings: Settings) -> dict[str, str]:
    """Sign one load-test account in and return its tokens."""
    email = normalise_email(args.prefix + f"-{index:05d}@" + args.domain)
    subject = f"{args.prefix}-{index:05d}"
    identity = VerifiedIdentity(
        provider=PROVIDER,
        subject=subject,
        email=email,
        email_verified=True,
        display_name="Load Test",
    )
    # No push token and no app version: `_issue_session` only creates a device row
    # when one of them is present, and a load-test account has no install to record.
    command = SocialLoginCommand(
        provider=PROVIDER,
        id_token=subject,
        device=DeviceContext(platform="android"),
        display_name="Load Test",
    )

    uow = make_uow()
    async with uow:
        service = AuthService(
            uow,
            settings,
            verifiers={PROVIDER: _MintedIdentityVerifier(identity)},
        )
        result = await service.login_with_identity(command)

    return {
        "email": email,
        "accessToken": result.tokens.access_token,
        "refreshToken": result.tokens.refresh_token,
    }


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    if settings.app_env == "production" and os.environ.get("LOADTEST_MINT_ALLOW_PRODUCTION") != "1":
        logger.error(
            "loadtest.refusing_production",
            detail=(
                "Refusing to mint long-lived tokens against a production database. "
                "Set LOADTEST_MINT_ALLOW_PRODUCTION=1 in an isolated load-test "
                "environment if this is genuinely intended."
            ),
        )
        return 2

    # Access tokens outlive the default 15 minutes so a multi-hour soak does not spend
    # its time refreshing. The refresh token gets the same window, so a client that
    # does refresh keeps working for the whole run.
    runtime_settings = settings.model_copy(
        update={
            "access_token_ttl_seconds": args.ttl_seconds,
            "refresh_token_ttl_seconds": args.ttl_seconds,
        }
    )

    configure_logging("INFO", "console")
    import_all_models()

    tokens: list[dict[str, str]] = []
    failed = 0
    lock = asyncio.Semaphore(args.concurrency)

    async def guarded(index: int) -> None:
        nonlocal failed
        async with lock:
            try:
                tokens.append(await _mint_one(index, args=args, settings=runtime_settings))
            except Exception as exc:  # noqa: BLE001 - one bad account must not stop the run
                failed += 1
                logger.error("loadtest.mint_failed", index=index, error=str(exc)[:300])

    try:
        await asyncio.gather(*(guarded(index) for index in range(1, args.count + 1)))
    finally:
        await dispose_engine()

    if not tokens:
        logger.error("loadtest.no_tokens_minted")
        return 1

    tokens.sort(key=lambda row: row["email"])
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # Written through a temporary file so an interrupted run cannot leave a half
    # JSON file that k6 would fail to parse.
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(tokens, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(output)

    logger.info(
        "loadtest.tokens_minted",
        minted=len(tokens),
        failed=failed,
        output=str(output),
        ttl_seconds=args.ttl_seconds,
    )
    return 0 if failed == 0 else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--count",
        type=int,
        default=500,
        help=f"how many accounts to mint (1..{MAX_COUNT}, default 500)",
    )
    parser.add_argument(
        "--output",
        default="loadtest/tokens.json",
        help="where to write the token file (default loadtest/tokens.json)",
    )
    parser.add_argument(
        "--prefix",
        default="loadtest",
        help="local part of the generated addresses (default loadtest)",
    )
    parser.add_argument(
        "--domain",
        default=DEFAULT_DOMAIN,
        help=f"domain of the generated addresses (default {DEFAULT_DOMAIN})",
    )
    parser.add_argument(
        "--ttl-seconds",
        type=int,
        default=DEFAULT_TTL_SECONDS,
        help=f"token lifetime in seconds (default {DEFAULT_TTL_SECONDS}, max 604800)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="parallel sign-ins (default 8; keep at or below DATABASE_POOL_SIZE)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.count <= MAX_COUNT:
        parser.error(f"--count must be between 1 and {MAX_COUNT}")
    if not 60 <= args.ttl_seconds <= 604_800:
        parser.error("--ttl-seconds must be between 60 and 604800 (7 days)")
    if not 1 <= args.concurrency <= 32:
        parser.error("--concurrency must be between 1 and 32")
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:  # pragma: no cover - operator pressed Ctrl-C
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
