"""Credential and email policy.

These are rules, not plumbing: they must hold whether the caller is the HTTP API, an
admin script or a future CLI. They live in the domain so the presentation layer cannot
quietly relax them.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from email.utils import parseaddr

from app.core.time import is_valid_timezone
from app.core.types import ContentLanguage

#: Learners bridge from Arabic, so Arabic is the default bridge language.
DEFAULT_NATIVE_LANGUAGE = ContentLanguage.AR

#: The floor is length plus variety rather than a symbol requirement — a passphrase
#: beats "P@ssw0rd!" for a real person to remember, and symbols exclude people who
#: type in Arabic script.
_COMMON_PASSWORDS = frozenset(
    {
        "password",
        "password1",
        "password123",
        "12345678",
        "123456789",
        "1234567890",
        "qwertyuiop",
        "letmein123",
        "welcome123",
        "natiqo123",
        "iloveyou1",
        "admin1234",
    }
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


@dataclass(frozen=True, slots=True)
class PasswordProblem:
    """One reason a password was rejected."""

    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def normalise_email(raw: str) -> str:
    """Lowercase and trim. The database also stores a unique index on this value,
    so normalisation must be deterministic or duplicate accounts appear."""
    return raw.strip().lower()


def is_valid_email(email: str) -> bool:
    """Syntax check only.

    Deliverability is proven by the verification email, not by guessing at SMTP.
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


def check_password(
    password: str, *, min_length: int = 10, email: str | None = None
) -> list[PasswordProblem]:
    """Validate a candidate password. Returns every problem, not just the first,
    so the client can show a complete checklist instead of drip-feeding rejections."""
    problems: list[PasswordProblem] = []
    normalised = unicodedata.normalize("NFKC", password)

    if len(normalised) < min_length:
        problems.append(PasswordProblem("TOO_SHORT", f"Use at least {min_length} characters."))
    if len(password) > 256:
        # Argon2 input is bounded; rejecting early avoids wasting CPU on an attack.
        problems.append(PasswordProblem("TOO_LONG", "Use at most 256 characters."))
    if not any(char.islower() for char in normalised):
        problems.append(PasswordProblem("NO_LOWERCASE", "Add a lowercase letter."))
    if not any(char.isupper() for char in normalised):
        problems.append(PasswordProblem("NO_UPPERCASE", "Add an uppercase letter."))
    if not any(char.isdigit() for char in normalised):
        problems.append(PasswordProblem("NO_DIGIT", "Add a number."))
    if len(set(normalised)) < 5:
        problems.append(PasswordProblem("TOO_REPETITIVE", "Use a less repetitive password."))
    if normalised.lower() in _COMMON_PASSWORDS:
        problems.append(PasswordProblem("TOO_COMMON", "That password is too easy to guess."))
    if email:
        local_part = normalise_email(email).split("@")[0]
        # Compare on letters and digits only. A literal substring test is trivially
        # defeated by swapping a separator — "Alina-Haddad-2026" does not contain
        # "alina.haddad", yet it is plainly derived from the very same address.
        squashed = "".join(char for char in normalised.lower() if char.isalnum())
        squashed_local = "".join(char for char in local_part if char.isalnum())
        if len(squashed_local) >= 4 and squashed_local in squashed:
            # Leaking the email local-part into the password makes credential stuffing
            # against a breached email list far cheaper.
            problems.append(
                PasswordProblem("CONTAINS_EMAIL", "Do not use your email address in your password.")
            )
    return problems
