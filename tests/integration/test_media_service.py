"""The media upload flow against a real schema, the real repository and a real adapter.

What only this level can prove: that confirming an upload writes exactly one row and
re-confirming finds it rather than duplicating it, that the row records what the *bytes*
proved rather than what the request claimed, that an object which fails any check is deleted
as it is refused, and that the size ceiling and content-type policy are enforced against the
stored object rather than a header.

The storage adapter under test is :class:`LocalObjectStorage` writing into ``tmp_path`` — the
same code path a developer runs, not a fake. A fake would let the test agree with itself about
what "the object exists" means, which is the very thing confirmation is here to check.

The magic-number arithmetic itself is pinned in ``tests/unit/test_media_policy.py``; this file
is about the wiring around it.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import create_all, dispose_engine, get_session_factory
from app.core.errors import ErrorCode, NotFoundError, ValidationFailed
from app.core.registry import import_all_models
from app.core.types import UserId
from app.core.uow_sqlalchemy import SqlAlchemyUnitOfWork
from app.modules.media.application.dto import MediaView, PresignView
from app.modules.media.application.service import MediaService
from app.modules.media.domain.policy import MediaKind
from app.modules.media.infrastructure.models import MediaAssetModel
from app.modules.users.infrastructure.models import UserModel
from app.platform.storage.local import LocalObjectStorage

pytestmark = pytest.mark.integration

#: A real PNG header, so the sniffer names it rather than guessing from a name.
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 64
#: A shell script that a caller would like the server to believe is a PNG.
SCRIPT = b"#!/bin/sh\necho not-an-image\n"


# ------------------------------------------------------------------------ fixtures
@pytest_asyncio.fixture
async def uow() -> AsyncIterator[SqlAlchemyUnitOfWork]:
    import_all_models()
    await create_all()
    async with SqlAlchemyUnitOfWork(get_session_factory()) as open_uow:
        yield open_uow
    await dispose_engine()


@pytest_asyncio.fixture
async def storage(tmp_path: Path) -> LocalObjectStorage:
    return LocalObjectStorage(tmp_path)


@pytest.fixture
def service(uow: SqlAlchemyUnitOfWork, storage: LocalObjectStorage) -> MediaService:
    return MediaService(uow, storage, get_settings())


@pytest.fixture
def session(uow: SqlAlchemyUnitOfWork) -> AsyncSession:
    return uow.session


# -------------------------------------------------------------------- row builders
def _user_id(session: AsyncSession) -> UserId:
    """A real account, because ``media_assets.owner_id`` is a real foreign key."""
    row = UserModel(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@example.com",
        status="active",
        roles=["user"],
    )
    session.add(row)
    return UserId(row.id)


async def _mint(
    service: MediaService,
    user_id: UserId,
    kind: MediaKind,
    content_type: str,
) -> PresignView:
    from app.modules.media.application.dto import PresignCommand

    return await service.presign(user_id, PresignCommand(kind=kind, content_type=content_type))


async def _rows(session: AsyncSession) -> list[MediaAssetModel]:
    result = await session.execute(select(MediaAssetModel))
    return list(result.scalars().all())


# ------------------------------------------------------------------- presign
async def test_presign_refuses_a_type_the_kind_does_not_allow(
    service: MediaService, uow: SqlAlchemyUnitOfWork, session: AsyncSession
) -> None:
    user_id = _user_id(session)
    with pytest.raises(ValidationFailed) as caught:
        await _mint(service, user_id, MediaKind.AVATAR, "application/pdf")

    assert caught.value.code == ErrorCode.MEDIA_TYPE_NOT_ALLOWED
    assert caught.value.details["kind"] == "avatar"
    assert await _rows(session) == []


async def test_presign_hands_back_the_headers_the_signature_is_bound_to(
    service: MediaService, uow: SqlAlchemyUnitOfWork, session: AsyncSession
) -> None:
    user_id = _user_id(session)
    view = await _mint(service, user_id, MediaKind.AVATAR, "image/png")

    assert view.kind == "avatar"
    assert view.required_headers == {"Content-Type": "image/png"}
    assert view.media_key in view.upload_url
    assert view.expires_in_seconds > 0


# ------------------------------------------------------------------- confirm
async def test_confirm_records_what_the_bytes_proved(
    service: MediaService,
    storage: LocalObjectStorage,
    uow: SqlAlchemyUnitOfWork,
    session: AsyncSession,
) -> None:
    user_id = _user_id(session)
    grant = await _mint(service, user_id, MediaKind.AVATAR, "image/png")
    await storage.put(grant.media_key, PNG, content_type="image/png")

    view = await service.confirm(user_id, grant.media_key)

    rows = await _rows(session)
    assert len(rows) == 1
    assert view.content_type == "image/png"
    assert view.size_bytes == len(PNG)
    assert view.checksum_sha256 == hashlib.sha256(PNG).hexdigest()
    assert view.url.startswith("local://")
    assert rows[0].owner_id == user_id


async def test_confirm_believes_the_bytes_over_a_mislabelled_upload(
    service: MediaService,
    storage: LocalObjectStorage,
    uow: SqlAlchemyUnitOfWork,
    session: AsyncSession,
) -> None:
    """A PNG that claims to be a GIF is stored as the PNG it is.

    The declared type chose the key's extension, but the *row* records the sniffed type, so
    the metadata never repeats the client's lie.
    """
    user_id = _user_id(session)
    grant = await _mint(service, user_id, MediaKind.IMAGE, "image/gif")
    await storage.put(grant.media_key, PNG, content_type="image/gif")

    view = await service.confirm(user_id, grant.media_key)

    assert view.content_type == "image/png"
    assert view.kind == "image"


async def test_confirm_refuses_a_file_whose_bytes_betray_the_claim(
    service: MediaService,
    storage: LocalObjectStorage,
    uow: SqlAlchemyUnitOfWork,
    session: AsyncSession,
) -> None:
    user_id = _user_id(session)
    grant = await _mint(service, user_id, MediaKind.AVATAR, "image/png")
    await storage.put(grant.media_key, SCRIPT, content_type="image/png")

    with pytest.raises(ValidationFailed) as caught:
        await service.confirm(user_id, grant.media_key)

    assert caught.value.code == ErrorCode.MEDIA_TYPE_NOT_ALLOWED
    assert await _rows(session) == []
    assert not await storage.exists(grant.media_key), "the refused object should be removed"


async def test_confirm_refuses_an_object_over_the_kind_ceiling(
    service: MediaService,
    storage: LocalObjectStorage,
    uow: SqlAlchemyUnitOfWork,
    session: AsyncSession,
) -> None:
    """The ceiling is measured from the object, not read from a request header."""
    user_id = _user_id(session)
    capped = get_settings().model_copy(update={"media_max_image_bytes": 16})
    gated = MediaService(uow, storage, capped)

    grant = await _mint(service, user_id, MediaKind.AVATAR, "image/png")
    await storage.put(grant.media_key, PNG, content_type="image/png")  # larger than 16

    with pytest.raises(ValidationFailed) as caught:
        await gated.confirm(user_id, grant.media_key)

    assert caught.value.code == ErrorCode.MEDIA_UPLOAD_FAILED
    assert caught.value.details["maxBytes"] == 16
    assert await _rows(session) == []
    assert not await storage.exists(grant.media_key)


async def test_audio_uses_the_audio_ceiling_not_the_image_one(
    service: MediaService,
    storage: LocalObjectStorage,
    uow: SqlAlchemyUnitOfWork,
    session: AsyncSession,
) -> None:
    """A clip that exceeds the image ceiling is still fine for an audio kind."""
    user_id = _user_id(session)
    settings = get_settings().model_copy(
        update={"media_max_image_bytes": 4, "media_max_audio_bytes": 4096}
    )
    gated = MediaService(uow, storage, settings)

    grant = await _mint(service, user_id, MediaKind.PRONUNCIATION, "audio/wav")
    wav = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 40
    await storage.put(grant.media_key, wav, content_type="audio/wav")

    view = await gated.confirm(user_id, grant.media_key)

    assert view.kind == "pronunciation"
    assert view.content_type == "audio/wav"


async def test_confirm_refuses_a_key_this_service_never_issued(
    service: MediaService, uow: SqlAlchemyUnitOfWork, session: AsyncSession
) -> None:
    user_id = _user_id(session)
    with pytest.raises(ValidationFailed) as caught:
        await service.confirm(user_id, "../../etc/passwd")

    assert caught.value.code == ErrorCode.MEDIA_UPLOAD_FAILED
    assert await _rows(session) == []


async def test_confirm_refuses_when_nothing_was_uploaded(
    service: MediaService, uow: SqlAlchemyUnitOfWork, session: AsyncSession
) -> None:
    user_id = _user_id(session)
    grant = await _mint(service, user_id, MediaKind.IMAGE, "image/jpeg")

    with pytest.raises(ValidationFailed) as caught:
        await service.confirm(user_id, grant.media_key)

    assert caught.value.code == ErrorCode.MEDIA_UPLOAD_FAILED
    assert await _rows(session) == []


async def test_confirm_is_idempotent(
    service: MediaService,
    storage: LocalObjectStorage,
    uow: SqlAlchemyUnitOfWork,
    session: AsyncSession,
) -> None:
    """A retry after a dropped response returns the same row, never a duplicate.

    This is why ``storage_key`` is unique: the second call finds the first row by key rather
    than racing the constraint or inserting a second row for one object.
    """
    user_id = _user_id(session)
    grant = await _mint(service, user_id, MediaKind.AVATAR, "image/png")
    await storage.put(grant.media_key, PNG, content_type="image/png")

    first = await service.confirm(user_id, grant.media_key)
    second = await service.confirm(user_id, grant.media_key)

    assert first.id == second.id
    assert len(await _rows(session)) == 1


# ------------------------------------------------------------------- get
async def test_get_re_signs_a_read_url_for_a_stored_asset(
    service: MediaService,
    storage: LocalObjectStorage,
    uow: SqlAlchemyUnitOfWork,
    session: AsyncSession,
) -> None:
    user_id = _user_id(session)
    grant = await _mint(service, user_id, MediaKind.AVATAR, "image/png")
    await storage.put(grant.media_key, PNG, content_type="image/png")
    created: MediaView = await service.confirm(user_id, grant.media_key)

    fetched = await service.get(created.id)

    assert fetched.id == created.id
    assert fetched.url.startswith("local://")
    assert fetched.expires_in_seconds > 0


async def test_get_refuses_an_unknown_id(service: MediaService, uow: SqlAlchemyUnitOfWork) -> None:
    with pytest.raises(NotFoundError) as caught:
        await service.get(uuid.uuid4())

    assert caught.value.code == ErrorCode.MEDIA_NOT_FOUND
