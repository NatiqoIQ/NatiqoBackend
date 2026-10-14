"""Unit tests for the HTTP cache policy helpers.

The endpoint-level behaviour is covered in ``tests/api/test_endpoints.py``. What is tested
here is the parts an endpoint test cannot reach: the comparison rules a real cache or proxy
can exercise against us, and the shape of the policy string we publish.
"""

from __future__ import annotations

import pytest
from starlette.datastructures import Headers
from starlette.requests import Request

from app.core.http_cache import (
    CACHE_POLICY_ATTR,
    CachePolicy,
    cacheable,
    matches_etag,
    policy_for,
)


def build_request(method: str = "GET", endpoint: object | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/api/v1/content/tree",
            "headers": Headers({}).raw,
            "endpoint": endpoint,
        }
    )


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, False),
        ("", False),
        ('"abc"', True),
        # A weak validator describes the same body. Accepting it is what stops a client that
        # stored one through an intermediary from re-downloading on every single launch.
        ('W/"abc"', True),
        # RFC 9110 allows a list, and clients that hold several resources commonly send one.
        ('"one", "abc", "two"', True),
        ('"one", "two"', False),
        # `*` means "I hold any current representation", which is a genuine match.
        ("*", True),
    ],
)
def test_a_conditional_request_is_matched_against_what_the_client_holds(
    header: str | None, expected: bool
) -> None:
    assert matches_etag(header, '"abc"') is expected


def test_an_empty_cache_control_is_impossible_for_a_default_policy() -> None:
    """Without a revalidation window the header must still say something usable."""
    assert CachePolicy(max_age=60).cache_control == "public, max-age=60"
    assert (
        CachePolicy(max_age=60, stale_while_revalidate=600).cache_control
        == "public, max-age=60, stale-while-revalidate=600"
    )


def test_the_policy_is_read_from_the_route_and_nowhere_else() -> None:
    """The marker is the whole allowlist. A route without one is not cacheable.

    Resolved from the endpoint rather than the path precisely so that a newly added
    authenticated route cannot become cacheable by accident, by path shape, or by a
    forgotten entry in a list someone has to remember to update.
    """
    unmarked = policy_for(build_request(endpoint=object()))
    assert unmarked is None

    @cacheable(max_age=30)
    async def marked() -> None: ...

    assert policy_for(build_request(endpoint=marked)) == CachePolicy(max_age=30)
    assert getattr(marked, CACHE_POLICY_ATTR) == CachePolicy(max_age=30)


def test_a_request_with_no_resolved_endpoint_is_never_cacheable() -> None:
    """A 404 from the router has no handler, so there is nothing to declare a policy.

    Guessing here would mean inferring cacheability from the URL, which is the exact
    coupling that makes an allowlist rot.
    """
    assert policy_for(build_request(endpoint=None)) is None


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_only_safe_methods_are_ever_cacheable(method: str) -> None:
    @cacheable(max_age=30)
    async def marked() -> None: ...

    assert policy_for(build_request(method=method, endpoint=marked)) is None
