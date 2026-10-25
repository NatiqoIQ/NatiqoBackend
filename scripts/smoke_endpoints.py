"""Call every route this API serves, once, with the data it needs.

A test suite proves the routes it knows about, on a database it built itself. This is the
other question: *is the environment in front of me answering all of itself, with data?* A
deployed box can pass every test and still serve an empty review queue, a catalogue whose
questions carry no media, or an achievements screen with no catalogue behind it — each of
which is a data problem, not a code problem, and none of which a unit test can see.

So the route list is not written here. It is read from the application's own OpenAPI
document, and every route in it must have a check registered below: **a route added without
one fails the run** with ``uncovered``. That is the point of the script — it cannot quietly
fall behind the API. The reverse is tolerated: a check for a route this build does not mount
(the admin surface, for instance, is absent from a deployment that does not ship it) is
reported as ``skipped`` rather than failing.

**How it gets a session.** By calling the real sign-in use case with a stub verifier, the
same way ``postman/mint_session.py`` and ``scripts/seed.py`` do: the account is found by the
seeder's own ``(provider, subject)`` pair, so the sweep signs in as a seeded persona without
a provider round-trip, without a password, and without a bypass that a deployment would have
to be trusted not to enable. It needs the database those accounts live in, which is why it
runs next to the API rather than from a laptop.

**What it writes.** It is not read-only, and that is deliberate — a route that is never
allowed to write is a route nobody has tested. Per run it: issues a few sessions, answers
one due review card (advancing its schedule), starts/answers/completes a quiz session and
abandons another, creates draft admin content and deletes it again, and signs a throwaway
``smoke+…@natiqo.app`` account in to exercise device revocation, a role change and account
closing before removing that account. ``--claim`` additionally claims one achievement
reward, which is a visible state change, so it is opt-in. Re-running ``scripts/seed.py``
restores the declared demo state apart from a claimed achievement.

    uv run python -m scripts.smoke_endpoints                    # in-process, dev database
    uv run python -m scripts.smoke_endpoints --base-url http://127.0.0.1:8000
    uv run python -m scripts.smoke_endpoints --persona learner --only review --claim

The exit status is 0 only when nothing failed, nothing answered with an empty payload where
data is expected, and no route went uncovered — so it can gate a deploy as it stands.
"""

from __future__ import annotations

import argparse
import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx
from sqlalchemy import text

from app.api.deps import get_storage
from app.core.config import get_settings
from app.core.database import dispose_engine, get_session_factory, make_uow
from app.core.registry import import_all_models
from app.main import app as application
from app.modules.users.application.auth_service import AuthService
from app.modules.users.application.dto import DeviceContext, SocialLoginCommand
from app.modules.users.domain.identity import (
    IdentityProvider,
    IdentityVerificationError,
    VerifiedIdentity,
)
from scripts.seed import seed_subject
from scripts.seed_personas import PERSONAS, SeedPersona

#: The provider the seeder and the session minter both use. Which one it is does not
#: matter — nothing here verifies a signature — only that it matches the identity row the
#: persona already holds, because that pair *is* how an account is recognised.
_PROVIDER = IdentityProvider.GOOGLE

#: Sent with every sign-in, so the sweep's own session is a device row it may revoke.
_DEVICE_JSON: dict[str, Any] = {"platform": "android", "app_version": "0.0.0-smoke"}
_DEVICE = DeviceContext(platform="android", app_version="0.0.0-smoke", user_agent="smoke/0.0.0")

#: The reward-less account the destructive checks run against. Closed and removed at the
#: end of the run, so one sweep does not grow the admin user list it just asserted on.
_DISPOSABLE_DOMAIN = "natiqo.app"

#: A minimal JPEG — the magic number plus filler. The media pipeline names a file from its
#: leading bytes rather than its label, so this is a complete, valid upload as far as the
#: sniffer is concerned, and the sweep never ships a real photograph to prove a route works.
_TINY_JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000100010000ffdb004300ffd9")


class Verdict(StrEnum):
    """What one check concluded. Only the last three fail the run."""

    OK = "ok"
    REFUSED = "refused"
    """The documented answer for this environment — a premium gate, a spent token, a
    taken CEFR code. Not a pass in the sense of a happy path, and not a problem either."""
    SKIPPED = "skipped"
    """Needs an opt-in flag, or covers a route this build does not mount."""
    EMPTY = "empty"
    """Answered, but carried no data where the environment is supposed to have some."""
    FAILED = "failed"
    """Wrong status, wrong shape, or an exception."""
    UNCOVERED = "uncovered"


_FAILING = (Verdict.EMPTY, Verdict.FAILED, Verdict.UNCOVERED)


@dataclass(frozen=True, slots=True)
class Session:
    """One signed-in account: what the checks need to call the API as it."""

    email: str
    user_id: uuid.UUID
    access_token: str
    refresh_token: str


@dataclass(slots=True)
class Outcome:
    method: str
    path: str
    description: str
    status: int | None = None
    verdict: Verdict = Verdict.FAILED
    note: str = ""


class _MintedVerifier:
    """Stands in for a provider's key check; see ``postman/mint_session.py`` for why."""

    #: Part of the verifier port: which provider's tokens this one answers for.
    provider = _PROVIDER

    def __init__(self, identity: VerifiedIdentity) -> None:
        self._identity = identity

    async def verify(self, id_token: str) -> VerifiedIdentity:
        if id_token != self._identity.subject:
            raise IdentityVerificationError("unexpected_token")
        return self._identity


async def _sign_in(email: str, subject: str, display_name: str) -> Session:
    """Sign an account in through the real use case, creating it on first sight."""
    import_all_models()
    identity = VerifiedIdentity(
        provider=_PROVIDER,
        subject=subject,
        email=email,
        email_verified=True,
        display_name=display_name,
    )
    uow = make_uow()
    async with uow:
        service = AuthService(uow, get_settings(), verifiers={_PROVIDER: _MintedVerifier(identity)})
        result = await service.login_with_identity(
            SocialLoginCommand(
                provider=_PROVIDER,
                id_token=subject,
                device=_DEVICE,
                display_name=display_name,
            )
        )
    return Session(
        email=email,
        user_id=result.user.id,
        access_token=result.tokens.access_token,
        refresh_token=result.tokens.refresh_token,
    )


# ------------------------------------------------------------------ the context
@dataclass(slots=True)
class Context:
    """One run's state: the client, the sessions, and the ids checks pass to each other.

    Checks run in the order they are registered and may rely on that — the admin cycle
    creates a unit, uses it, and deletes it, in that order. Everything a check *needs* it
    asks for by name through :meth:`require`, so a missing prerequisite reads as a failed
    check with a name rather than as a ``KeyError``.
    """

    client: httpx.AsyncClient
    persona: SeedPersona
    claim: bool
    keep_disposable: bool
    persona_session: Session
    dev_session: Session | None = None
    disposable_session: Session | None = None
    last_status: int | None = None
    #: The last segment of the route being checked, so one check can serve two routes
    #: (Google and Apple refuse for the same reason but are two entries in the table).
    path_hint: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    # ---------------------------------------------------------------- requests
    def bearer(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        token = kwargs.pop("bearer", self.persona_session.access_token)
        if token:
            headers = dict(kwargs.pop("headers", {}))
            headers.setdefault("Authorization", f"Bearer {token}")
            kwargs["headers"] = headers
        response = await self.client.request(method, path, **kwargs)
        self.last_status = response.status_code
        return response

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request("POST", path, **kwargs)

    async def patch(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request("PATCH", path, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self.request("DELETE", path, **kwargs)

    # ---------------------------------------------------------------- fixtures
    def require(self, key: str) -> Any:
        if key not in self.data:
            raise LookupError(f"{key} was never captured — an earlier check must have failed")
        return self.data[key]

    async def levels(self) -> list[dict[str, Any]]:
        if "levels" not in self.data:
            body = _json(await self.get("/api/v1/content/tree"))
            self.data["levels"] = body.get("items", [])
        return self.data["levels"]

    async def lessons(self) -> list[dict[str, Any]]:
        if "lessons" not in self.data:
            levels = await self.levels()
            self.data["lessons"] = [
                lesson
                for level in levels
                for unit in level.get("units", [])
                for lesson in unit.get("lessons", [])
            ]
        return self.data["lessons"]

    async def disposable(self) -> Session:
        """The throwaway account, created on first use."""
        if self.disposable_session is None:
            handle = uuid.uuid4().hex[:10]
            self.disposable_session = await _sign_in(
                f"smoke+{handle}@{_DISPOSABLE_DOMAIN}",
                f"smoke:{handle}",
                "Smoke Runner",
            )
            self.data["disposable_id"] = self.disposable_session.user_id
        return self.disposable_session


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {"items": body}


def _items(body: dict[str, Any]) -> list[dict[str, Any]]:
    value = body.get("items", body.get("cards", []))
    return value if isinstance(value, list) else []


def _code(response: httpx.Response) -> str:
    body = _json(response)
    error = body.get("error")
    return str(error.get("code", "")) if isinstance(error, dict) else ""


def _find_key(body: Any, key: str) -> Any:
    """The first value stored under ``key`` anywhere in a nested payload."""
    if isinstance(body, dict):
        if key in body:
            return body[key]
        for value in body.values():
            found = _find_key(value, key)
            if found is not None:
                return found
    elif isinstance(body, list):
        for value in body:
            found = _find_key(value, key)
            if found is not None:
                return found
    return None


def _answer_for(question: dict[str, Any]) -> dict[str, str]:
    """An answer shaped like the question: a choice where there is one, text otherwise."""
    choices = question.get("choices") or []
    return {"choice": choices[0]} if choices else {"text": "smoke"}


# ------------------------------------------------------------------ verdicts
def _ok(note: str = "") -> tuple[Verdict, str]:
    return Verdict.OK, note


def _refused(note: str) -> tuple[Verdict, str]:
    return Verdict.REFUSED, note


def _skipped(note: str) -> tuple[Verdict, str]:
    return Verdict.SKIPPED, note


def _empty(note: str) -> tuple[Verdict, str]:
    return Verdict.EMPTY, note


def _failed(note: str) -> tuple[Verdict, str]:
    return Verdict.FAILED, note


Check = Callable[[Context], Awaitable[tuple[Verdict, str]]]


@dataclass(frozen=True, slots=True)
class Route:
    """One check, pinned to the route it answers for."""

    method: str
    path: str
    description: str
    check: Check


# ------------------------------------------------------------------- probes
async def _check_health(ctx: Context) -> tuple[Verdict, str]:
    response = await ctx.get("/health", bearer="")
    if response.status_code != 200:
        return _failed(f"liveness answered {response.status_code}")
    return _ok(f"status={_json(response).get('status', '?')}")


async def _check_ready(ctx: Context) -> tuple[Verdict, str]:
    response = await ctx.get("/ready", bearer="")
    if response.status_code != 200:
        return _failed(f"readiness answered {response.status_code}")
    return _ok("database and cache reachable")


async def _check_provider_refused(ctx: Context) -> tuple[Verdict, str]:
    """A provider route with no verifier behind it must refuse, not 500."""
    provider = "google" if ctx.path_hint == "google" else "apple"
    response = await ctx.post(
        f"/api/v1/auth/{provider}", json={"idToken": "not-a-real-id-token"}, bearer=""
    )
    status = response.status_code
    if status >= 500:
        return _failed(f"{provider} sign-in answered {status}")
    if status in (400, 401, 403):
        return _refused(f"{status} {_code(response) or 'refused'} — no client ids configured")
    return _failed(f"expected a refusal, got {status}")


async def _check_dev_login(ctx: Context) -> tuple[Verdict, str]:
    settings = get_settings()
    response = await ctx.post(
        "/api/v1/auth/dev-login",
        json={
            "username": settings.dev_admin_user,
            "password": settings.dev_admin_password.get_secret_value(),
            "device": _DEVICE_JSON,
        },
        bearer="",
    )
    if response.status_code != 200:
        return _failed(f"the development door answered {response.status_code} {_code(response)}")
    body = _json(response)
    user = body.get("user", {})
    if not user.get("isAdmin"):
        return _failed("the development account is not an admin")
    ctx.dev_session = Session(
        email=str(user.get("email", "")),
        user_id=uuid.UUID(str(user["id"])),
        access_token=body["tokens"]["accessToken"],
        refresh_token=body["tokens"]["refreshToken"],
    )
    return _ok(f"{ctx.dev_session.email} is an admin")


async def _check_refresh(ctx: Context) -> tuple[Verdict, str]:
    spent = ctx.persona_session.refresh_token
    response = await ctx.post("/api/v1/auth/refresh", json={"refreshToken": spent}, bearer="")
    if response.status_code != 200:
        return _failed(f"rotation answered {response.status_code} {_code(response)}")
    tokens = _json(response).get("tokens", {})
    rotated = Session(
        email=ctx.persona_session.email,
        user_id=ctx.persona_session.user_id,
        access_token=tokens.get("accessToken", ""),
        refresh_token=tokens.get("refreshToken", ""),
    )
    if not rotated.access_token:
        return _failed("rotation returned no access token")
    me = await ctx.get("/api/v1/me", bearer=rotated.access_token)
    if me.status_code != 200:
        return _failed(f"the rotated access token was refused ({me.status_code})")
    replay = await ctx.post("/api/v1/auth/refresh", json={"refreshToken": spent}, bearer="")
    if replay.status_code not in (400, 401, 403):
        return _failed(f"the spent refresh token answered {replay.status_code}")
    ctx.persona_session = rotated
    return _ok("rotated; the new token works and the spent one is refused")


async def _check_logout(ctx: Context) -> tuple[Verdict, str]:
    """Logout must leave the token useless. A repeated logout answering 200 is fine:
    revocation is idempotent, and refusing the second call would make a retried logout
    — the one a client sends when it never saw the first response — look like a failure.
    What is *not* fine is a token that still mints sessions afterwards."""
    if ctx.dev_session is None:
        return _failed("no development session to revoke")
    first = await ctx.post(
        "/api/v1/auth/logout", json={"refreshToken": ctx.dev_session.refresh_token}, bearer=""
    )
    if first.status_code != 200:
        return _failed(f"logout answered {first.status_code} {_code(first)}")
    spent = await ctx.post(
        "/api/v1/auth/refresh", json={"refreshToken": ctx.dev_session.refresh_token}, bearer=""
    )
    if spent.status_code not in (400, 401, 403):
        return _failed(f"the revoked refresh token still mints sessions ({spent.status_code})")
    return _ok("revoked; the token cannot mint a session any more")


async def _check_me(ctx: Context) -> tuple[Verdict, str]:
    response = await ctx.get("/api/v1/me")
    if response.status_code != 200:
        return _failed(f"profile answered {response.status_code}")
    body = _json(response)
    if body.get("email") != ctx.persona.email:
        return _failed(f"wrong account: {body.get('email')} != {ctx.persona.email}")
    declared = ctx.persona.is_admin
    if bool(body.get("isAdmin")) != declared:
        return _failed(f"isAdmin {body.get('isAdmin')} != {declared}")
    if not body.get("profile"):
        return _empty("the account has no profile row")
    return _ok(f"{body['email']} isAdmin={body.get('isAdmin')} profile+settings present")


async def _check_patch_me(ctx: Context) -> tuple[Verdict, str]:
    before = _find_key(_json(await ctx.get("/api/v1/me")), "dailyGoalMinutes")
    if not isinstance(before, int):
        return _empty("no daily goal to change — the profile has no settings")
    target = before + 1
    changed = await ctx.patch("/api/v1/me", json={"dailyGoalMinutes": target})
    if changed.status_code != 200:
        return _failed(f"the update answered {changed.status_code} {_code(changed)}")
    stored = _find_key(_json(changed), "dailyGoalMinutes")
    if stored != target:
        return _failed(f"the goal was not stored: {stored} != {target}")
    restored = await ctx.patch("/api/v1/me", json={"dailyGoalMinutes": before})
    if restored.status_code != 200 or _find_key(_json(restored), "dailyGoalMinutes") != before:
        return _failed("the goal could not be restored")
    return _ok(f"dailyGoalMinutes {before} -> {target} -> {before}")


async def _check_devices(ctx: Context) -> tuple[Verdict, str]:
    response = await ctx.get("/api/v1/me/devices")
    if response.status_code != 200:
        return _failed(f"devices answered {response.status_code}")
    devices = _items(_json(response))
    if not devices:
        return _empty("no device rows — every persona declares at least one, so the seed is stale")
    return _ok(f"{len(devices)} devices ({', '.join(str(d.get('platform')) for d in devices)})")


async def _check_revoke_device(ctx: Context) -> tuple[Verdict, str]:
    session = await ctx.disposable()
    listing = _items(_json(await ctx.get("/api/v1/me/devices", bearer=session.access_token)))
    if not listing:
        return _empty("the throwaway account has no device row of its own to revoke")
    target = str(listing[0]["id"])
    response = await ctx.delete(f"/api/v1/me/devices/{target}", bearer=session.access_token)
    if response.status_code not in (200, 204):
        return _failed(f"revoking answered {response.status_code} {_code(response)}")
    left = _items(_json(await ctx.get("/api/v1/me/devices", bearer=session.access_token)))
    if any(str(device["id"]) == target for device in left):
        return _failed("the revoked device is still listed")
    return _ok(f"revoked; {len(left)} device(s) left on the throwaway account")


async def _check_tree(ctx: Context) -> tuple[Verdict, str]:
    response = await ctx.get("/api/v1/content/tree", bearer="")
    if response.status_code != 200:
        return _failed(f"the catalogue answered {response.status_code}")
    lessons = await ctx.lessons()
    if not lessons:
        return _empty("the tree served no lessons — run `python -m scripts.seed_content`")
    questions = sum(int(lesson.get("questionCount", 0)) for lesson in lessons)
    if questions == 0:
        return _empty("no lessons carry questions")
    return _ok(f"{len(await ctx.levels())} levels, {len(lessons)} lessons, {questions} questions")


async def _check_lesson(ctx: Context) -> tuple[Verdict, str]:
    lesson = (await ctx.lessons())[0]
    response = await ctx.get(f"/api/v1/content/lessons/{lesson['id']}", bearer="")
    if response.status_code != 200:
        return _failed(f"the lesson answered {response.status_code}")
    body = _json(response)
    questions = body.get("questions", [])
    if not questions:
        return _empty("the lesson served no questions")
    without = [q for q in questions if not (q.get("imageUrl") and q.get("audioUrl"))]
    if without:
        return _empty(f"{len(without)}/{len(questions)} questions carry no media aid")
    for key in ("imageUrl", "audioUrl"):
        asset = await ctx.get(str(questions[0][key]), bearer="")
        if asset.status_code != 200:
            return _failed(f"{questions[0][key]} answered {asset.status_code}")
    return _ok(f"{len(questions)} questions, image and audio both served")


async def _check_progress(ctx: Context) -> tuple[Verdict, str]:
    response = await ctx.get("/api/v1/progress")
    if response.status_code != 200:
        return _failed(f"progress answered {response.status_code}")
    body = _json(response)
    if not body.get("lessonsCompleted"):
        return _empty("no completed lessons — run `python -m scripts.seed --only <persona>`")
    if not body.get("xp"):
        return _empty("the XP feed is empty")
    return _ok(
        f"{body['lessonsCompleted']} lessons, {body.get('accuracyPercent')}% accuracy, "
        f"{len(body['xp'])} XP events, streak {body.get('currentStreakDays')}"
    )


async def _check_unlocks(ctx: Context) -> tuple[Verdict, str]:
    response = await ctx.get("/api/v1/progress/unlocks")
    if response.status_code != 200:
        return _failed(f"the unlock map answered {response.status_code}")
    body = _json(response)
    entries = _items(body)
    lessons = len(await ctx.lessons())
    if not entries:
        return _empty("the unlock map is empty")
    if len(entries) != lessons:
        return _failed(f"{len(entries)} entries for {lessons} published lessons")
    return _ok(f"{len(entries)} entries match the {lessons} published lessons")


async def _check_start_session(ctx: Context) -> tuple[Verdict, str]:
    lesson = (await ctx.lessons())[0]
    response = await ctx.post(
        "/api/v1/quiz/sessions", json={"lessonId": lesson["id"], "mode": "lesson"}
    )
    if response.status_code not in (200, 201):
        return _failed(f"starting a session answered {response.status_code} {_code(response)}")
    body = _json(response)
    session = body.get("session", {})
    questions = body.get("questions", [])
    if not questions:
        return _empty("the session carries no questions")
    if session.get("lessonId") != lesson["id"]:
        return _failed("the session is not the lesson that was asked for")
    ctx.data["session_id"] = session["id"]
    ctx.data["session_questions"] = questions
    ctx.data["lesson_id"] = lesson["id"]
    return _ok(f"session {str(session['id'])[:8]} over {len(questions)} questions")


async def _check_resume_session(ctx: Context) -> tuple[Verdict, str]:
    session_id = ctx.require("session_id")
    response = await ctx.get(f"/api/v1/quiz/sessions/{session_id}")
    if response.status_code != 200:
        return _failed(f"resuming answered {response.status_code} {_code(response)}")
    body = _json(response)
    if not body.get("questions"):
        return _empty("the resumed session has no questions")
    if not body.get("session", {}).get("status"):
        return _failed("the session carries no status")
    return _ok(f"{len(body['questions'])} questions, status={body['session']['status']}")


async def _check_session_commands(ctx: Context) -> tuple[Verdict, str]:
    """Every command an open session accepts: answer each question, complete, abandon."""
    session_id = ctx.require("session_id")
    questions = ctx.require("session_questions")
    graded: dict[str, Any] = {}
    for question in questions:
        response = await ctx.post(
            f"/api/v1/quiz/sessions/{session_id}",
            json={"op": "answer", "questionId": question["id"], "answer": _answer_for(question)},
        )
        if response.status_code != 200:
            return _failed(f"the answer command answered {response.status_code} {_code(response)}")
        graded = _json(response).get("result", {})
    if not isinstance(graded.get("isCorrect"), bool):
        return _failed(f"the verdict carries no isCorrect: {sorted(graded)}")
    if not graded.get("explanation"):
        return _empty("the graded answer came back without its explanation")
    if graded.get("answeredCount") != len(questions):
        return _failed(f"{graded.get('answeredCount')} of {len(questions)} answers were counted")
    completed = await ctx.post(f"/api/v1/quiz/sessions/{session_id}", json={"op": "complete"})
    if completed.status_code != 200:
        return _failed(f"the complete command answered {completed.status_code} {_code(completed)}")
    lesson_id = ctx.require("lesson_id")
    fresh = await ctx.post("/api/v1/quiz/sessions", json={"lessonId": lesson_id, "mode": "lesson"})
    if fresh.status_code not in (200, 201):
        return _failed(f"the second session answered {fresh.status_code}")
    fresh_id = _json(fresh).get("session", {}).get("id")
    abandoned = await ctx.post(f"/api/v1/quiz/sessions/{fresh_id}", json={"op": "abandon"})
    if abandoned.status_code != 200:
        return _failed(f"the abandon command answered {abandoned.status_code} {_code(abandoned)}")
    return _ok(
        f"answered all {len(questions)} (last graded {graded['isCorrect']}, explained), "
        "completed, and a second session abandoned"
    )


async def _check_due(ctx: Context) -> tuple[Verdict, str]:
    response = await ctx.get("/api/v1/review/due", params={"limit": 20})
    if response.status_code != 200:
        return _failed(f"the due queue answered {response.status_code}")
    body = _json(response)
    cards = body.get("cards", [])
    if not cards:
        return _empty("nothing is due — the seeded plan reviews cards, so the schedule is stale")
    ctx.data["card_id"] = cards[0]["cardId"]
    ctx.data["card_question"] = cards[0].get("question", {})
    ctx.data["due_count"] = body.get("dueCount")
    return _ok(f"{body.get('dueCount')} due, first card {str(cards[0]['cardId'])[:8]}")


async def _check_review_preview(ctx: Context) -> tuple[Verdict, str]:
    response = await ctx.get("/api/v1/review/preview")
    if response.status_code != 200:
        return _failed(f"the preview answered {response.status_code}")
    due = _json(response).get("dueCount")
    stats = _json(await ctx.get("/api/v1/review/stats")).get("dueToday")
    if due is None:
        return _empty("the preview carries no count")
    if due != stats:
        return _failed(f"the preview says {due} due, the stats say {stats}")
    return _ok(f"dueCount={due}, consistent with the stats screen")


async def _check_review_stats(ctx: Context) -> tuple[Verdict, str]:
    response = await ctx.get("/api/v1/review/stats")
    if response.status_code != 200:
        return _failed(f"the stats answered {response.status_code}")
    body = _json(response)
    if not body.get("totalCards"):
        return _empty("no cards exist — nothing has ever been answered correctly")
    return _ok(
        f"{body['totalCards']} cards, {body.get('dueToday')} due, "
        f"{body.get('reviewedTotal')} reviewed, retention {body.get('retentionPercent')}%"
    )


async def _check_review_answer(ctx: Context) -> tuple[Verdict, str]:
    card_id = ctx.require("card_id")
    payload = _answer_for(ctx.require("card_question"))
    response = await ctx.post(f"/api/v1/review/cards/{card_id}/answer", json=payload)
    if response.status_code != 200:
        return _failed(f"answering a card answered {response.status_code} {_code(response)}")
    body = _json(response)
    if not isinstance(body.get("isCorrect"), bool):
        return _failed(f"the verdict carries no isCorrect: {sorted(body)}")
    if body.get("intervalDays") is None or body.get("dueAt") is None:
        return _empty("the answer did not advance the card's schedule")
    return _ok(
        f"graded (correct={body['isCorrect']}, quality={body.get('quality')}), "
        f"next due in {body.get('intervalDays')}d"
    )


async def _check_ai_quota(ctx: Context) -> tuple[Verdict, str]:
    response = await ctx.get("/api/v1/ai/quota")
    if response.status_code != 200:
        return _failed(f"the quota answered {response.status_code}")
    body = _json(response)
    if not body.get("limitUsd"):
        return _empty("the quota carries no limit — the AI settings are not configured")
    spent = body.get("spentUsd") or 0
    note = f"limit ${body['limitUsd']}, spent ${spent}, enabled={body.get('enabled')}"
    if not spent:
        return _empty(f"{note} — no metered call has been recorded today")
    return _ok(note)


async def _check_explain(ctx: Context) -> tuple[Verdict, str]:
    question = ctx.require("session_questions")[0]
    response = await ctx.post(
        "/api/v1/ai/explain",
        json={"questionId": question["id"], "answer": _answer_for(question)},
    )
    if response.status_code != 200:
        return _failed(f"the explainer answered {response.status_code} {_code(response)}")
    body = _json(response)
    if not body.get("explanation"):
        return _empty("the explanation came back empty")
    return _ok(f"{len(str(body['explanation']))} chars from {body.get('source')}")


async def _check_tutor_ask(ctx: Context) -> tuple[Verdict, str]:
    response = await ctx.post(
        "/api/v1/ai/tutor", json={"message": "When do I use the past simple?", "lessonId": None}
    )
    if response.status_code != 200:
        return _failed(f"the tutor answered {response.status_code} {_code(response)}")
    body = _json(response)
    if not body.get("conversationId") or not body.get("reply"):
        return _empty("the tutor returned no conversation or no reply")
    ctx.data["conversation_id"] = body["conversationId"]
    return _ok(f"thread {str(body['conversationId'])[:8]} replied via {body.get('source')}")


async def _check_tutor_read(ctx: Context) -> tuple[Verdict, str]:
    conversation_id = ctx.require("conversation_id")
    response = await ctx.get(f"/api/v1/ai/tutor/{conversation_id}")
    if response.status_code != 200:
        return _failed(f"reading the thread answered {response.status_code} {_code(response)}")
    messages = _json(response).get("messages", [])
    if len(messages) < 2:
        return _empty(f"the thread holds {len(messages)} message(s): the reply was never stored")
    return _ok(f"{len(messages)} messages in the thread")


async def _check_gamification(ctx: Context) -> tuple[Verdict, str]:
    response = await ctx.get("/api/v1/gamification/summary")
    if response.status_code != 200:
        return _failed(f"the hub answered {response.status_code}")
    body = _json(response)
    if not body.get("achievementsTotal"):
        return _empty("the achievement catalogue is empty — nothing reconciles it on a fresh box")
    if not body.get("lessonsCompleted"):
        return _empty("no completed lessons behind the hub")
    return _ok(
        f"{body['lessonsCompleted']} lessons, "
        f"{body.get('achievementsUnlocked')}/{body['achievementsTotal']} achievements"
    )


async def _check_achievements(ctx: Context) -> tuple[Verdict, str]:
    response = await ctx.get("/api/v1/achievements")
    if response.status_code != 200:
        return _failed(f"the catalogue answered {response.status_code}")
    body = _json(response)
    items = _items(body)
    if not items:
        return _empty("the achievement catalogue served no entries")
    unlocked = [a for a in items if a.get("unlocked")]
    if not unlocked:
        return _empty("no achievement is unlocked — the seeded history unlocks several")
    ctx.data["achievements"] = items
    claimable = [a for a in items if a.get("claimable")]
    return _ok(f"{body.get('total')} entries, {len(unlocked)} unlocked, {len(claimable)} claimable")


async def _check_claim(ctx: Context) -> tuple[Verdict, str]:
    items = ctx.data.get("achievements") or _items(_json(await ctx.get("/api/v1/achievements")))
    if not items:
        return _empty("nothing to claim: the catalogue is empty")
    locked = next((a for a in items if not a.get("unlocked")), None)
    if locked is not None:
        refused = await ctx.post(f"/api/v1/achievements/{locked['code']}/claim", json={})
        if refused.status_code not in (400, 404, 409):
            return _failed(f"a locked achievement answered {refused.status_code}")
    claimed = next((a for a in items if a.get("claimedAt")), None)
    if claimed is not None:
        again = await ctx.post(f"/api/v1/achievements/{claimed['code']}/claim", json={})
        if again.status_code not in (400, 404, 409):
            return _failed(f"an already-claimed achievement answered {again.status_code}")
    claimable = next((a for a in items if a.get("claimable")), None)
    if claimable is None:
        return _skipped("nothing is left to claim; re-seed the persona to restore the plan")
    if not ctx.claim:
        return _skipped(
            f"{claimable['code']} is claimable — pass --claim to pay it out (a real state change)"
        )
    paid = await ctx.post(f"/api/v1/achievements/{claimable['code']}/claim", json={})
    if paid.status_code != 200:
        return _failed(f"claiming answered {paid.status_code} {_code(paid)}")
    body = _json(paid)
    if not (body.get("xpAwarded") or body.get("gemsAwarded")):
        return _empty("the claim paid neither XP nor gems")
    return _ok(
        f"claimed {claimable['code']} for {body.get('xpAwarded')} XP and "
        f"{body.get('gemsAwarded')} gems (state changed)"
    )


async def _check_streak(ctx: Context) -> tuple[Verdict, str]:
    response = await ctx.get("/api/v1/streak")
    if response.status_code != 200:
        return _failed(f"the streak answered {response.status_code}")
    body = _json(response)
    if not body.get("currentStreak"):
        return _empty("the streak is zero — the seeded history needs lessons on consecutive days")
    return _ok(
        f"{body['currentStreak']} day(s), longest {body.get('longestStreak')}, "
        f"{body.get('freezesAvailable')} freeze(s), premium={body.get('premium')}"
    )


async def _check_freeze(ctx: Context) -> tuple[Verdict, str]:
    settings = get_settings()
    response = await ctx.post("/api/v1/streak/freeze")
    if settings.premium_enabled:
        if response.status_code == 200:
            return _ok("a freeze was spent")
        return _refused(
            f"{response.status_code} {_code(response)} — premium is on, "
            "and a freeze is a purchase the seeder deliberately never grants"
        )
    if response.status_code != 402 or _code(response) != "PREMIUM_REQUIRED":
        return _failed(
            f"expected 402 PREMIUM_REQUIRED, got {response.status_code} {_code(response)}"
        )
    return _refused("402 PREMIUM_REQUIRED — the documented answer while premium is switched off")


# ------------------------------------------------------------------- media
async def _check_media_presign(ctx: Context) -> tuple[Verdict, str]:
    response = await ctx.post(
        "/api/v1/media/presign", json={"kind": "image", "content_type": "image/jpeg"}
    )
    if response.status_code not in (200, 201):
        return _failed(f"asking for a grant answered {response.status_code} {_code(response)}")
    body = _json(response)
    key = str(body.get("mediaKey", ""))
    if not key or not body.get("uploadUrl"):
        return _empty("the grant carries no key or no upload URL")
    if body.get("requiredHeaders", {}).get("Content-Type") != "image/jpeg":
        return _failed("the grant does not bind the content type it was asked for")
    ctx.data["media_key"] = key
    ctx.data["media_upload_url"] = body["uploadUrl"]
    ctx.data["media_headers"] = body.get("requiredHeaders", {})
    return _ok(f"key {key} for {body.get('expiresInSeconds')}s")


async def _upload_media(ctx: Context, key: str) -> str:
    """Put the bytes where the grant says to.

    The local adapter has no signed URL to PUT to — its own docstring says an upload there
    *is* the adapter call — while S3 hands back a real URL and gets a real HTTP PUT. Both
    paths end with the bytes in the same place, which is all ``confirm`` ever asks about.
    """
    upload_url = str(ctx.require("media_upload_url"))
    if upload_url.startswith("local://"):
        await get_storage().put(key, _TINY_JPEG, content_type="image/jpeg")
        return "filesystem adapter"
    response = await ctx.client.put(
        upload_url, content=_TINY_JPEG, headers=ctx.require("media_headers")
    )
    if response.status_code >= 300:
        raise RuntimeError(f"the upload answered {response.status_code}")
    return f"PUT {response.status_code}"


async def _check_media_confirm(ctx: Context) -> tuple[Verdict, str]:
    key = ctx.require("media_key")
    transport = await _upload_media(ctx, key)
    response = await ctx.post("/api/v1/media/confirm", json={"mediaKey": key})
    if response.status_code not in (200, 201):
        return _failed(f"confirming answered {response.status_code} {_code(response)}")
    body = _json(response)
    if not body.get("id"):
        return _failed("the confirmed asset came back without an id")
    if body.get("sizeBytes") != len(_TINY_JPEG):
        return _failed(f"the stored size is {body.get('sizeBytes')}, not {len(_TINY_JPEG)}")
    if body.get("contentType") != "image/jpeg":
        return _failed(f"the sniffed type came back as {body.get('contentType')}")
    ctx.data["media_id"] = body["id"]
    forged = await ctx.post("/api/v1/media/confirm", json={"mediaKey": "image/deadbeef.jpg"})
    if forged.status_code not in (400, 404, 422):
        return _failed(f"a key this server never issued answered {forged.status_code}")
    return _ok(f"uploaded via the {transport}; {body['sizeBytes']}B {body['contentType']} recorded")


async def _check_media_read(ctx: Context) -> tuple[Verdict, str]:
    media_id = ctx.require("media_id")
    response = await ctx.get(f"/api/v1/media/{media_id}")
    if response.status_code != 200:
        return _failed(f"reading the asset answered {response.status_code} {_code(response)}")
    body = _json(response)
    if not body.get("url"):
        return _empty("the asset came back without a signed URL")
    unknown = await ctx.get(f"/api/v1/media/{uuid.uuid4()}")
    if unknown.status_code != 404:
        return _failed(f"an unknown asset id answered {unknown.status_code}")
    return _ok(
        f"{body.get('kind')} {body.get('sizeBytes')}B, checksum {str(body.get('checksumSha256'))[:12]}…"
    )


# ------------------------------------------------------------------- admin
async def _check_admin_levels(ctx: Context) -> tuple[Verdict, str]:
    response = await ctx.get("/api/v1/admin/content/levels", params={"limit": 50})
    if response.status_code != 200:
        return _failed(f"listing levels answered {response.status_code} {_code(response)}")
    items = _items(_json(response))
    if not items:
        return _empty("no levels exist, drafts included")
    drafts = [level for level in items if level.get("state") != "published"]
    ctx.data["admin_level_id"] = items[0]["id"]
    ctx.data["admin_level_description"] = items[0].get("description")
    return _ok(f"{len(items)} levels ({len(drafts)} not published)")


async def _check_admin_level_create(ctx: Context) -> tuple[Verdict, str]:
    """Every CEFR code is taken in a seeded database, so the documented answer is 409."""
    response = await ctx.post(
        "/api/v1/admin/content/levels",
        json={"code": "A1", "title": "Smoke level", "description": "created by the sweep"},
    )
    if response.status_code == 409:
        return _refused("409 — all six CEFR codes are already published, which is the seeded state")
    if response.status_code in (200, 201):
        body = _json(response)
        ctx.data["created_level_id"] = body.get("id")
        return _ok(f"created {body.get('code')} {body.get('title')} ({body.get('state')})")
    return _failed(f"creating a level answered {response.status_code} {_code(response)}")


async def _check_admin_level_delete(ctx: Context) -> tuple[Verdict, str]:
    """Deleting a level takes its units and lessons with it, so the sweep never deletes a
    seeded one: it deletes the level it created, when a free CEFR code let it create one,
    and otherwise proves the refusal an unknown id gets."""
    created = ctx.data.pop("created_level_id", None)
    if created is not None:
        response = await ctx.delete(f"/api/v1/admin/content/levels/{created}")
        if response.status_code not in (200, 204):
            return _failed(f"deleting the level it created answered {response.status_code}")
        gone = await ctx.get(f"/api/v1/admin/content/levels/{created}")
        if gone.status_code != 404:
            return _failed(f"the deleted level still answers {gone.status_code}")
        return _ok("deleted the level it created; a re-read is 404")
    response = await ctx.delete(f"/api/v1/admin/content/levels/{uuid.uuid4()}")
    if response.status_code != 404:
        return _failed(f"an unknown level id answered {response.status_code} {_code(response)}")
    return _refused(
        "404 for an unknown id — all six CEFR codes are seeded, so no level here is deletable"
    )


async def _check_admin_level_read(ctx: Context) -> tuple[Verdict, str]:
    level_id = ctx.require("admin_level_id")
    response = await ctx.get(f"/api/v1/admin/content/levels/{level_id}")
    if response.status_code != 200:
        return _failed(f"reading a level answered {response.status_code}")
    body = _json(response)
    if not body.get("code") or not body.get("title"):
        return _empty("the level came back without its code or title")
    return _ok(f"{body['code']} {body['title']} ({body.get('state')})")


async def _check_admin_level_update(ctx: Context) -> tuple[Verdict, str]:
    level_id = ctx.require("admin_level_id")
    before = ctx.require("admin_level_description")
    marker = f"smoke {uuid.uuid4().hex[:8]}"
    changed = await ctx.patch(
        f"/api/v1/admin/content/levels/{level_id}", json={"description": marker}
    )
    if changed.status_code != 200:
        return _failed(f"updating a level answered {changed.status_code} {_code(changed)}")
    if _json(changed).get("description") != marker:
        return _failed("the new description was not stored")
    restored = await ctx.patch(
        f"/api/v1/admin/content/levels/{level_id}", json={"description": before}
    )
    if restored.status_code != 200 or _json(restored).get("description") != before:
        return _failed("the original description could not be restored")
    return _ok("description changed and restored")


async def _check_admin_units(ctx: Context) -> tuple[Verdict, str]:
    level_id = ctx.require("admin_level_id")
    response = await ctx.get(f"/api/v1/admin/content/levels/{level_id}/units", params={"limit": 50})
    if response.status_code != 200:
        return _failed(f"listing units answered {response.status_code} {_code(response)}")
    items = _items(_json(response))
    if not items:
        return _empty("the level has no units, drafts included")
    return _ok(
        f"{len(items)} units under {_json(await ctx.get(f'/api/v1/admin/content/levels/{level_id}')).get('code')}"
    )


async def _check_admin_unit_create(ctx: Context) -> tuple[Verdict, str]:
    level_id = ctx.require("admin_level_id")
    response = await ctx.post(
        f"/api/v1/admin/content/levels/{level_id}/units",
        json={"title": "Smoke unit", "description": "created and deleted by the sweep"},
    )
    if response.status_code not in (200, 201):
        return _failed(f"creating a unit answered {response.status_code} {_code(response)}")
    body = _json(response)
    if not body.get("id"):
        return _failed("the created unit came back without an id")
    ctx.data["admin_unit_id"] = body["id"]
    return _ok(
        f"unit {str(body['id'])[:8]} at position {body.get('position')} ({body.get('state')})"
    )


async def _check_admin_unit_read(ctx: Context) -> tuple[Verdict, str]:
    unit_id = ctx.require("admin_unit_id")
    response = await ctx.get(f"/api/v1/admin/content/units/{unit_id}")
    if response.status_code != 200:
        return _failed(f"reading the new unit answered {response.status_code}")
    body = _json(response)
    if body.get("title") != "Smoke unit":
        return _failed(f"the wrong unit came back: {body.get('title')}")
    return _ok(f"{body['title']} position {body.get('position')} ({body.get('state')})")


async def _check_admin_unit_update(ctx: Context) -> tuple[Verdict, str]:
    unit_id = ctx.require("admin_unit_id")
    response = await ctx.patch(
        f"/api/v1/admin/content/units/{unit_id}", json={"title": "Smoke unit, renamed"}
    )
    if response.status_code != 200:
        return _failed(f"updating the unit answered {response.status_code} {_code(response)}")
    if _json(response).get("title") != "Smoke unit, renamed":
        return _failed("the new title was not stored")
    return _ok("title changed")


async def _check_admin_lessons(ctx: Context) -> tuple[Verdict, str]:
    unit_id = ctx.require("admin_unit_id")
    response = await ctx.get(f"/api/v1/admin/content/units/{unit_id}/lessons", params={"limit": 50})
    if response.status_code != 200:
        return _failed(f"listing lessons answered {response.status_code} {_code(response)}")
    return _ok(f"{len(_items(_json(response)))} lesson(s) under the new unit")


async def _check_admin_lesson_create(ctx: Context) -> tuple[Verdict, str]:
    unit_id = ctx.require("admin_unit_id")
    response = await ctx.post(
        f"/api/v1/admin/content/units/{unit_id}/lessons",
        json={
            "title": "Smoke lesson",
            "summary": "Created and deleted by the endpoint sweep.",
            "estimatedMinutes": 5,
        },
    )
    if response.status_code not in (200, 201):
        return _failed(f"creating a lesson answered {response.status_code} {_code(response)}")
    body = _json(response)
    if not body.get("id"):
        return _failed("the created lesson came back without an id")
    ctx.data["admin_lesson_id"] = body["id"]
    return _ok(f"lesson {str(body['id'])[:8]} ({body.get('state')})")


async def _check_admin_lesson_read(ctx: Context) -> tuple[Verdict, str]:
    lesson_id = ctx.require("admin_lesson_id")
    response = await ctx.get(f"/api/v1/admin/content/lessons/{lesson_id}")
    if response.status_code != 200:
        return _failed(f"reading the new lesson answered {response.status_code}")
    body = _json(response)
    if body.get("title") != "Smoke lesson":
        return _failed(f"the wrong lesson came back: {body.get('title')}")
    return _ok(f"{body['title']} with {len(body.get('questions', []))} questions")


async def _check_admin_lesson_update(ctx: Context) -> tuple[Verdict, str]:
    lesson_id = ctx.require("admin_lesson_id")
    response = await ctx.patch(
        f"/api/v1/admin/content/lessons/{lesson_id}", json={"estimatedMinutes": 7}
    )
    if response.status_code != 200:
        return _failed(f"updating the lesson answered {response.status_code} {_code(response)}")
    if _json(response).get("estimatedMinutes") != 7:
        return _failed("the new estimate was not stored")
    return _ok("estimatedMinutes changed to 7")


async def _check_admin_question_create(ctx: Context) -> tuple[Verdict, str]:
    lesson_id = ctx.require("admin_lesson_id")
    response = await ctx.post(
        f"/api/v1/admin/content/lessons/{lesson_id}/questions",
        json={
            "kind": "multiple_choice",
            "skill": "grammar",
            "prompt": "Which one is correct?",
            "choices": ["She goes", "She go", "She going", "She gone"],
            "correctIndex": 0,
            "explanation": "Third person singular takes -s.",
        },
    )
    if response.status_code not in (200, 201):
        return _failed(f"creating a question answered {response.status_code} {_code(response)}")
    body = _json(response)
    if not body.get("id"):
        return _failed("the created question came back without an id")
    ctx.data["admin_question_id"] = body["id"]
    return _ok(f"question {str(body['id'])[:8]} at position {body.get('position')}")


async def _check_admin_question_reorder(ctx: Context) -> tuple[Verdict, str]:
    lesson_id = ctx.require("admin_lesson_id")
    question_id = ctx.require("admin_question_id")
    response = await ctx.post(
        f"/api/v1/admin/content/lessons/{lesson_id}/questions/reorder",
        json={"questionIds": [question_id]},
    )
    if response.status_code != 200:
        return _failed(f"reordering answered {response.status_code} {_code(response)}")
    return _ok("the single question was reordered onto position 1")


async def _check_admin_question_update(ctx: Context) -> tuple[Verdict, str]:
    question_id = ctx.require("admin_question_id")
    response = await ctx.patch(f"/api/v1/admin/content/questions/{question_id}", json={"points": 2})
    if response.status_code != 200:
        return _failed(f"updating the question answered {response.status_code} {_code(response)}")
    if _json(response).get("points") != 2:
        return _failed("the new weight was not stored")
    return _ok("points changed to 2")


async def _check_admin_lesson_publish(ctx: Context) -> tuple[Verdict, str]:
    lesson_id = ctx.require("admin_lesson_id")
    response = await ctx.post(f"/api/v1/admin/content/lessons/{lesson_id}/publish")
    if response.status_code == 409:
        return _refused(f"409 {_code(response)} — a lesson under a draft unit stays unpublished")
    if response.status_code != 200:
        return _failed(f"publishing answered {response.status_code} {_code(response)}")
    if _json(response).get("state") != "published":
        return _failed(f"the lesson is still {_json(response).get('state')}")
    return _ok("lesson published")


async def _check_admin_question_delete(ctx: Context) -> tuple[Verdict, str]:
    lesson_id = ctx.require("admin_lesson_id")
    question_id = ctx.require("admin_question_id")
    response = await ctx.delete(f"/api/v1/admin/content/questions/{question_id}")
    if response.status_code not in (200, 204):
        return _failed(f"deleting the question answered {response.status_code} {_code(response)}")
    after = _json(await ctx.get(f"/api/v1/admin/content/lessons/{lesson_id}"))
    if any(str(q["id"]) == str(question_id) for q in after.get("questions", [])):
        return _failed("the deleted question is still on the lesson")
    ctx.data.pop("admin_question_id", None)
    return _ok("deleted and gone from the lesson")


async def _check_admin_lesson_delete(ctx: Context) -> tuple[Verdict, str]:
    unit_id = ctx.require("admin_unit_id")
    lesson_id = ctx.require("admin_lesson_id")
    response = await ctx.delete(f"/api/v1/admin/content/lessons/{lesson_id}")
    if response.status_code not in (200, 204):
        return _failed(f"deleting the lesson answered {response.status_code} {_code(response)}")
    left = _items(_json(await ctx.get(f"/api/v1/admin/content/units/{unit_id}/lessons")))
    if any(str(row["id"]) == str(lesson_id) for row in left):
        return _failed("the deleted lesson is still listed")
    ctx.data.pop("admin_lesson_id", None)
    return _ok("deleted and gone from the unit")


async def _check_admin_unit_delete(ctx: Context) -> tuple[Verdict, str]:
    level_id = ctx.require("admin_level_id")
    unit_id = ctx.require("admin_unit_id")
    response = await ctx.delete(f"/api/v1/admin/content/units/{unit_id}")
    if response.status_code not in (200, 204):
        return _failed(f"deleting the unit answered {response.status_code} {_code(response)}")
    gone = await ctx.get(f"/api/v1/admin/content/units/{unit_id}")
    if gone.status_code != 404:
        return _failed(f"the deleted unit still answers {gone.status_code}")
    left = _items(_json(await ctx.get(f"/api/v1/admin/content/levels/{level_id}/units")))
    if any(str(row["id"]) == str(unit_id) for row in left):
        return _failed("the deleted unit is still listed")
    ctx.data.pop("admin_unit_id", None)
    return _ok("deleted; a re-read is 404 and the level no longer lists it")


async def _check_admin_users(ctx: Context) -> tuple[Verdict, str]:
    response = await ctx.get("/api/v1/admin/users", params={"limit": 50})
    if response.status_code != 200:
        return _failed(f"listing users answered {response.status_code} {_code(response)}")
    body = _json(response)
    items = _items(body)
    if not items:
        return _empty("no accounts to moderate")
    suspended = [u for u in items if u.get("status") != "active"]
    return _ok(f"{body.get('total', len(items))} accounts ({len(suspended)} not active)")


async def _check_admin_user_admin(ctx: Context) -> tuple[Verdict, str]:
    session = await ctx.disposable()
    response = await ctx.post(
        "/api/v1/admin/users", json={"userId": str(session.user_id), "isAdmin": True}
    )
    if response.status_code != 200:
        return _failed(f"granting admin answered {response.status_code} {_code(response)}")
    is_admin = _json(response).get("isAdmin")
    if is_admin is not True:
        return _failed(f"the admin flag came back as {is_admin}")
    return _ok("the throwaway account was promoted (and it is removed afterwards)")


async def _check_close_account(ctx: Context) -> tuple[Verdict, str]:
    """Last check, on the throwaway account: the persona's own account must survive."""
    session = await ctx.disposable()
    response = await ctx.delete("/api/v1/me", bearer=session.access_token)
    if response.status_code not in (200, 204):
        return _failed(f"closing the account answered {response.status_code} {_code(response)}")
    after = await ctx.get("/api/v1/me", bearer=session.access_token)
    if after.status_code not in (401, 403, 404):
        return _failed(f"the closed account still signs in ({after.status_code})")
    return _ok("closed; its token no longer works")


#: Every route the application serves, in the order the checks must run. The admin cycle
#: is ordered by dependency (create, use, delete); the destructive account check is last.
_ROUTES: tuple[tuple[str, str, str, Check], ...] = (
    ("GET", "/health", "liveness", _check_health),
    ("GET", "/ready", "readiness", _check_ready),
    (
        "POST",
        "/api/v1/auth/google",
        "provider sign-in refuses an unchecked token",
        _check_provider_refused,
    ),
    (
        "POST",
        "/api/v1/auth/apple",
        "provider sign-in refuses an unchecked token",
        _check_provider_refused,
    ),
    (
        "POST",
        "/api/v1/auth/dev-login",
        "the development door issues the admin session",
        _check_dev_login,
    ),
    ("GET", "/api/v1/content/tree", "the published catalogue", _check_tree),
    ("GET", "/api/v1/content/lessons/{lesson_id}", "a lesson with its media aids", _check_lesson),
    ("POST", "/api/v1/auth/refresh", "rotation and replay refusal", _check_refresh),
    ("POST", "/api/v1/auth/logout", "revocation", _check_logout),
    ("GET", "/api/v1/me", "the signed-in learner", _check_me),
    ("PATCH", "/api/v1/me", "profile and settings update", _check_patch_me),
    ("GET", "/api/v1/me/devices", "signed-in devices", _check_devices),
    ("DELETE", "/api/v1/me/devices/{device_id}", "device revocation", _check_revoke_device),
    ("GET", "/api/v1/progress", "the progress overview", _check_progress),
    ("GET", "/api/v1/progress/unlocks", "the unlock map", _check_unlocks),
    ("POST", "/api/v1/quiz/sessions", "start a session", _check_start_session),
    ("GET", "/api/v1/quiz/sessions/{session_id}", "resume a session", _check_resume_session),
    (
        "POST",
        "/api/v1/quiz/sessions/{session_id}",
        "answer, complete, abandon",
        _check_session_commands,
    ),
    ("GET", "/api/v1/review/due", "the due queue", _check_due),
    ("GET", "/api/v1/review/preview", "the due badge", _check_review_preview),
    ("GET", "/api/v1/review/stats", "the review statistics", _check_review_stats),
    ("POST", "/api/v1/review/cards/{card_id}/answer", "advance a card", _check_review_answer),
    ("POST", "/api/v1/media/presign", "grant a direct upload", _check_media_presign),
    ("POST", "/api/v1/media/confirm", "confirm an upload", _check_media_confirm),
    ("GET", "/api/v1/media/{media_id}", "describe a stored asset", _check_media_read),
    ("GET", "/api/v1/ai/quota", "the metered budget", _check_ai_quota),
    ("POST", "/api/v1/ai/explain", "explain a graded answer", _check_explain),
    ("POST", "/api/v1/ai/tutor", "ask the tutor", _check_tutor_ask),
    ("GET", "/api/v1/ai/tutor/{conversation_id}", "read a thread back", _check_tutor_read),
    ("GET", "/api/v1/gamification/summary", "the hub", _check_gamification),
    ("GET", "/api/v1/achievements", "the achievement catalogue", _check_achievements),
    ("POST", "/api/v1/achievements/{code}/claim", "claim a reward", _check_claim),
    ("GET", "/api/v1/streak", "the streak detail", _check_streak),
    ("POST", "/api/v1/streak/freeze", "the premium gate", _check_freeze),
    ("GET", "/api/v1/admin/content/levels", "levels, drafts included", _check_admin_levels),
    ("POST", "/api/v1/admin/content/levels", "create a level", _check_admin_level_create),
    (
        "DELETE",
        "/api/v1/admin/content/levels/{level_id}",
        "delete a level",
        _check_admin_level_delete,
    ),
    ("GET", "/api/v1/admin/content/levels/{level_id}", "read a level", _check_admin_level_read),
    (
        "PATCH",
        "/api/v1/admin/content/levels/{level_id}",
        "update a level",
        _check_admin_level_update,
    ),
    ("GET", "/api/v1/admin/content/levels/{level_id}/units", "a level's units", _check_admin_units),
    (
        "POST",
        "/api/v1/admin/content/levels/{level_id}/units",
        "create a unit",
        _check_admin_unit_create,
    ),
    ("GET", "/api/v1/admin/content/units/{unit_id}", "read a unit", _check_admin_unit_read),
    ("PATCH", "/api/v1/admin/content/units/{unit_id}", "update a unit", _check_admin_unit_update),
    (
        "GET",
        "/api/v1/admin/content/units/{unit_id}/lessons",
        "a unit's lessons",
        _check_admin_lessons,
    ),
    (
        "POST",
        "/api/v1/admin/content/units/{unit_id}/lessons",
        "create a lesson",
        _check_admin_lesson_create,
    ),
    ("GET", "/api/v1/admin/content/lessons/{lesson_id}", "read a lesson", _check_admin_lesson_read),
    (
        "PATCH",
        "/api/v1/admin/content/lessons/{lesson_id}",
        "update a lesson",
        _check_admin_lesson_update,
    ),
    (
        "POST",
        "/api/v1/admin/content/lessons/{lesson_id}/questions",
        "add a question",
        _check_admin_question_create,
    ),
    (
        "POST",
        "/api/v1/admin/content/lessons/{lesson_id}/questions/reorder",
        "reorder questions",
        _check_admin_question_reorder,
    ),
    (
        "PATCH",
        "/api/v1/admin/content/questions/{question_id}",
        "update a question",
        _check_admin_question_update,
    ),
    (
        "POST",
        "/api/v1/admin/content/lessons/{lesson_id}/publish",
        "publish a lesson",
        _check_admin_lesson_publish,
    ),
    (
        "DELETE",
        "/api/v1/admin/content/questions/{question_id}",
        "delete a question",
        _check_admin_question_delete,
    ),
    (
        "DELETE",
        "/api/v1/admin/content/lessons/{lesson_id}",
        "delete a lesson",
        _check_admin_lesson_delete,
    ),
    ("DELETE", "/api/v1/admin/content/units/{unit_id}", "delete a unit", _check_admin_unit_delete),
    ("GET", "/api/v1/admin/users", "list accounts", _check_admin_users),
    ("POST", "/api/v1/admin/users", "grant admin", _check_admin_user_admin),
    ("DELETE", "/api/v1/me", "close the throwaway account", _check_close_account),
)


def _routes() -> tuple[Route, ...]:
    return tuple(
        Route(method, path, description, check) for method, path, description, check in _ROUTES
    )


def _served() -> set[tuple[str, str]]:
    """Every (method, path) in this build's OpenAPI document."""
    spec = application.openapi()
    return {
        (method.upper(), path)
        for path, operations in spec["paths"].items()
        for method in operations
    }


async def _cleanup_disposable(ctx: Context) -> str:
    """Remove the throwaway account, so a sweep does not grow the user list it checks."""
    if ctx.disposable_session is None:
        return "no throwaway account was created"
    if ctx.keep_disposable:
        return f"kept {ctx.disposable_session.email} (--keep-disposable)"
    user_id = ctx.disposable_session.user_id
    # Children first: the account's own rows, then the account. This is the one write the
    # script makes outside the API, and it exists so repeated sweeps do not leave closed
    # accounts in the very user list the admin check asserts on.
    statements = (
        "delete from refresh_tokens where user_id = :id",
        "delete from user_devices where user_id = :id",
        "delete from user_identities where user_id = :id",
        "delete from user_settings where user_id = :id",
        "delete from user_profiles where user_id = :id",
        "delete from user_stats where user_id = :id",
        "delete from users where id = :id",
    )
    import_all_models()
    session_factory = get_session_factory()
    async with session_factory() as session:
        for statement in statements:
            await session.execute(text(statement), {"id": user_id})
        await session.commit()
    return f"removed {ctx.disposable_session.email}"


async def _cleanup_created(ctx: Context) -> str:
    """Delete draft content a failed admin cycle left behind."""
    leftovers = [
        key
        for key in ("admin_question_id", "admin_lesson_id", "admin_unit_id", "created_level_id")
        if key in ctx.data
    ]
    if not leftovers:
        return "no draft content left behind"
    removed: list[str] = []
    for key in leftovers:
        kind = key.removeprefix("admin_").removesuffix("_id")
        resource = {
            "question": "questions",
            "lesson": "lessons",
            "unit": "units",
            "created_level": "levels",
        }[kind]
        response = await ctx.delete(f"/api/v1/admin/content/{resource}/{ctx.data[key]}")
        removed.append(f"{kind}:{response.status_code}")
    return "cleaned up " + ", ".join(removed)


async def _cleanup_media(ctx: Context) -> str:
    """Remove the asset the media checks uploaded: the object and its row.

    There is no delete endpoint for media yet, so this is the second write the script makes
    outside the API. Without it every sweep would leave an orphaned object and a row in the
    table it just proved works.
    """
    key = ctx.data.get("media_key")
    media_id = ctx.data.get("media_id")
    if not key:
        return "no media asset was uploaded"
    await get_storage().delete(str(key))
    if media_id is None:
        return "the uploaded object was removed"
    import_all_models()
    session_factory = get_session_factory()
    async with session_factory() as session:
        await session.execute(text("delete from media_assets where id = :id"), {"id": media_id})
        await session.commit()
    return f"removed the uploaded asset {str(media_id)[:8]}"


async def _run(args: argparse.Namespace) -> int:
    persona = next((p for p in PERSONAS if p.key == args.persona), None)
    if persona is None:
        print(f"Unknown persona {args.persona!r}. Known: {', '.join(p.key for p in PERSONAS)}")
        return 1

    session = await _sign_in(persona.email, seed_subject(persona.key), persona.display_name)

    transport = (
        httpx.ASGITransport(app=application)
        if args.base_url is None
        else httpx.AsyncHTTPTransport()
    )
    base_url = args.base_url or "http://smoke.test"

    served = _served()
    checks = [route for route in _routes() if not args.only or args.only in route.path]
    uncovered = sorted(served - {(r.method, r.path) for r in _routes()})

    outcomes: list[Outcome] = []
    async with httpx.AsyncClient(transport=transport, base_url=base_url, timeout=60.0) as client:
        ctx = Context(
            client=client,
            persona=persona,
            claim=args.claim,
            keep_disposable=args.keep_disposable,
            persona_session=session,
        )
        for route in checks:
            if (route.method, route.path) not in served:
                outcomes.append(
                    Outcome(
                        route.method,
                        route.path,
                        route.description,
                        None,
                        Verdict.SKIPPED,
                        "this build does not mount the route",
                    )
                )
                continue
            ctx.path_hint = route.path.rstrip("/").split("/")[-1]
            ctx.last_status = None
            try:
                verdict, note = await route.check(ctx)
            except Exception as error:  # noqa: BLE001 - one check must not stop the sweep
                verdict, note = Verdict.FAILED, f"{type(error).__name__}: {error}"
            outcomes.append(
                Outcome(route.method, route.path, route.description, ctx.last_status, verdict, note)
            )
        cleanup = [
            await _cleanup_created(ctx),
            await _cleanup_media(ctx),
            await _cleanup_disposable(ctx),
        ]

    for method, path in uncovered:
        outcomes.append(
            Outcome(
                method,
                path,
                "no check registered",
                None,
                Verdict.UNCOVERED,
                "add a check to _ROUTES",
            )
        )

    _report(outcomes, cleanup)
    await dispose_engine()
    return 1 if any(o.verdict in _FAILING for o in outcomes) else 0


def _report(outcomes: Sequence[Outcome], cleanup: Sequence[str]) -> None:
    width = max((len(f"{o.method} {o.path}") for o in outcomes), default=0)
    print(f"\n{'ROUTE':<{width}}  {'ST':>3}  {'VERDICT':<9}  DETAIL")
    for outcome in outcomes:
        route = f"{outcome.method} {outcome.path}"
        status = "-" if outcome.status is None else str(outcome.status)
        print(f"{route:<{width}}  {status:>3}  {outcome.verdict.value:<9}  {outcome.note}")
    counts: dict[Verdict, int] = {}
    for outcome in outcomes:
        counts[outcome.verdict] = counts.get(outcome.verdict, 0) + 1
    summary = ", ".join(
        f"{verdict.value}={count}"
        for verdict, count in sorted(counts.items(), key=lambda item: item[0].value)
    )
    print(f"\n{len(outcomes)} routes checked: {summary}")
    for line in cleanup:
        print(f"cleanup: {line}")
    failing = [o for o in outcomes if o.verdict in _FAILING]
    if failing:
        print(f"\n{len(failing)} route(s) need attention:")
        for outcome in failing:
            print(f"  {outcome.verdict.value:<9} {outcome.method} {outcome.path} — {outcome.note}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.smoke_endpoints",
        description="Call every route the API serves and report what it answers with.",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help=(
            "A running server to test, e.g. http://127.0.0.1:8000. Omit to call the app "
            "in-process over ASGI against the same database this script reads."
        ),
    )
    parser.add_argument(
        "--persona",
        default="admin",
        help="Which seeded persona to sign in as. One of: "
        + ", ".join(persona.key for persona in PERSONAS),
    )
    parser.add_argument(
        "--only",
        default=None,
        help="Only check routes whose path contains this text. Coverage is then not enforced.",
    )
    parser.add_argument(
        "--claim",
        action="store_true",
        help="Pay out one claimable achievement. Opt-in: claiming is a visible state change.",
    )
    parser.add_argument(
        "--keep-disposable",
        action="store_true",
        help="Leave the throwaway smoke account in place instead of removing it.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
