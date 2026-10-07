"""Outbound adapters for external identity providers.

The port lives in :mod:`app.modules.users.domain.identity`; the concrete verifiers that
talk to Google's and Apple's published key sets live here, behind it.
"""

from app.platform.identity.jwks import JwksConfig, JwksTokenVerifier
from app.platform.identity.verifiers import (
    AppleIdentityVerifier,
    GoogleIdentityVerifier,
    build_verifiers,
)

__all__ = [
    "AppleIdentityVerifier",
    "GoogleIdentityVerifier",
    "JwksConfig",
    "JwksTokenVerifier",
    "build_verifiers",
]
