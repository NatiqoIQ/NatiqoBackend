"""Optional secret-manager bootstrap (F23).

The deployment already supports ``secretRef`` and External Secrets, but a process still
reads its DSN, Redis URL and signing keys from the environment. That is fine until an
operator wants nothing sensitive in the pod spec at all: this module lets the process pull
one JSON object from AWS Secrets Manager at startup and turn each key into an environment
variable *before* :class:`Settings` is constructed.

The contract is deliberately small:

* ``SECRETS_BACKEND=aws`` must be set in the process environment (not ``.env``): the
  bootstrap runs before pydantic reads the env file, so a value only in ``.env`` cannot
  select the backend.
* The secret's value must be a JSON object whose keys are environment-variable names and
  whose values are strings.
* Values from the secret **override** the environment for the keys they contain. The
  secret manager is the source of truth; a stale ``DATABASE_URL`` left in a pod manifest
  must not silently win.
* Failure to fetch is fatal. A process that starts with half its secrets is worse than one
  that refuses to start, because the failure would surface later as a database error.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from app.core.logging import get_logger

logger = get_logger("core.secrets")

__all__ = [
    "apply_secret_payload",
    "bootstrap_secrets",
    "load_aws_secret",
    "parse_secret_payload",
    "reset_secrets_cache",
]

#: Only names that look like environment variables are accepted. A JSON key such as
#: ``"jwt.secret"`` is a configuration mistake that would otherwise be set and ignored.
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")

_loaded = False


def parse_secret_payload(raw: str) -> dict[str, str]:
    """Parse and validate the secret's JSON object.

    Raises ``ValueError`` for anything that is not an object of string values, so a
    malformed secret fails at startup with a message naming the problem instead of a
    confusing pydantic error about a missing DSN.
    """
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("secret payload is not valid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("secret payload must be a JSON object of env names to strings")
    parsed: dict[str, str] = {}
    for key, value in payload.items():
        if not isinstance(key, str) or not _ENV_NAME.fullmatch(key):
            raise ValueError(f"secret key {key!r} is not an environment variable name")
        if not isinstance(value, str):
            raise ValueError(f"secret value for {key} must be a string")
        parsed[key] = value
    return parsed


def apply_secret_payload(
    payload: dict[str, str], *, environ: dict[str, str] | None = None
) -> list[str]:
    """Apply the payload to the environment and return the keys that were written.

    The return value is used for a log line; the values are never logged.
    """
    target = os.environ if environ is None else environ
    for key, value in payload.items():
        target[key] = value
    return sorted(payload)


def load_aws_secret(secret_id: str, region: str | None = None) -> dict[str, str]:
    """Fetch one secret from AWS Secrets Manager and parse it."""
    import boto3

    client = boto3.client("secretsmanager", region_name=region)
    response = client.get_secret_value(SecretId=secret_id)
    raw = response.get("SecretString")
    if not isinstance(raw, str):
        raise ValueError(f"secret {secret_id} has no SecretString payload")
    return parse_secret_payload(raw)


def bootstrap_secrets() -> None:
    """Load the AWS secret once, before settings are built.

    A no-op unless ``SECRETS_BACKEND=aws``. Called from :func:`app.core.config.get_settings`
    so every entry point — API, worker, scripts — gets the same behaviour without each one
    remembering to call it.
    """
    global _loaded
    if os.environ.get("SECRETS_BACKEND", "env").strip().lower() != "aws":
        return
    if _loaded:
        return
    secret_id = os.environ.get("AWS_SECRET_ID", "").strip()
    if not secret_id:
        raise RuntimeError("SECRETS_BACKEND=aws requires AWS_SECRET_ID")
    payload = load_aws_secret(secret_id, os.environ.get("AWS_REGION") or None)
    keys = apply_secret_payload(payload)
    _loaded = True
    logger.info("secrets.loaded", source="aws", keys=keys)


def reset_secrets_cache() -> None:
    """Forget that a secret was loaded. Tests only; a process loads once."""
    global _loaded
    _loaded = False
