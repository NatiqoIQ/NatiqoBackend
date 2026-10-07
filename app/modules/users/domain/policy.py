"""Email and calendar policy.

These are rules, not plumbing: they must hold whether the caller is the HTTP API, an
admin script or a future CLI. They live in the domain so the presentation layer cannot
quietly relax them.

Credential policy is deliberately absent. Sign-in is delegated to Google and Apple, so
the application never sees, stores or validates a password — the provider is the sole
authority on how a learner proves who they are.
"""

from __future__ import annotations

import re
from email.utils import parseaddr

from app.core.time import is_valid_timezone
from app.core.types import ContentLanguage

#: Learners bridge from Arabic, so Arabic is the default bridge language.
DEFAULT_NATIVE_LANGUAGE = ContentLanguage.AR

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


def normalise_email(raw: str) -> str:
    """Lowercase and trim. The database also stores a unique index on this value,
    so normalisation must be deterministic or duplicate accounts appear."""
    return raw.strip().lower()


def is_valid_email(email: str) -> bool:
    """Syntax check only.

    A provider asserts that an address is real and, usually, that it is verified; this
    only guards the shape before it reaches an indexed column.
    """
    if not email or len(email) > 254:
        return False
    # `rsplit("@", 1)` returns a one-item list for input with no "@", so unpacking it
    # would raise ValueError and turn a malformed address into a 500 instead of a
    # validation failure. `rpartition` always yields three items, so this is total.
    local, at_sign, host = email.rpartition("@")
    if not at_sign or not local or not host or len(local) > 64:
        return False
    _, domain = parseaddr(email)
    return bool(domain) and bool(_EMAIL_RE.match(email))


def is_valid_timezone_name(name: str) -> bool:
    """IANA zone names only. Abbreviations like ``+03:00`` or ``AST`` are rejected
    because they are ambiguous across daylight-saving boundaries."""
    return bool(name) and is_valid_timezone(name)
