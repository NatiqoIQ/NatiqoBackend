"""The media content-type policy: sniffing, the allow-lists and key minting.

This is the security-relevant half of the media module, and it is pure — no database, no
storage, no HTTP — so it is pinned here where a failure names the exact byte sequence that
broke.

Two properties matter most and both are about not trusting the caller:

* the sniffer names a file from its leading bytes, so a script labelled ``image/jpeg`` is
  refused, and
* a key is only accepted if it matches the grammar this service issues, so a traversal or a
  hand-made key never reaches the storage adapter.
"""

from __future__ import annotations

import re

from app.modules.media.domain.policy import (
    ALLOWED_CONTENT_TYPES,
    MEDIA_KINDS,
    MediaKind,
    build_storage_key,
    max_bytes_allowed,
    parse_storage_key,
    sniff_content_type,
)

# --------------------------------------------------------------- byte samples
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01" + b"\x00" * 8
PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 8
GIF = b"GIF89a\x01\x00\x01\x00" + b"\x00" * 8
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 8
WAV = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 8
OGG = b"OggS\x00\x02" + b"\x00" * 8
MP3_ID3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 8
MP3_FRAME = b"\xff\xfb\x90\x00" + b"\x00" * 12
M4A = b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 8
SHELL_SCRIPT = b"#!/bin/sh\necho not-media\n"


class TestSniffContentType:
    def test_it_names_every_image_the_pipeline_accepts(self) -> None:
        assert sniff_content_type(JPEG) == "image/jpeg"
        assert sniff_content_type(PNG) == "image/png"
        assert sniff_content_type(GIF) == "image/gif"
        assert sniff_content_type(WEBP) == "image/webp"

    def test_it_names_every_audio_the_pipeline_accepts(self) -> None:
        assert sniff_content_type(WAV) == "audio/wav"
        assert sniff_content_type(OGG) == "audio/ogg"
        assert sniff_content_type(MP3_ID3) == "audio/mpeg"
        assert sniff_content_type(MP3_FRAME) == "audio/mpeg"
        assert sniff_content_type(M4A) == "audio/mp4"

    def test_the_two_riff_containers_are_told_apart(self) -> None:
        """They share their first four bytes; only offset eight distinguishes them."""
        assert sniff_content_type(WEBP) == "image/webp"
        assert sniff_content_type(WAV) == "audio/wav"

    def test_a_non_media_payload_is_not_named(self) -> None:
        assert sniff_content_type(SHELL_SCRIPT) is None
        assert sniff_content_type(b"") is None
        assert sniff_content_type(b"\x00" * 32) is None

    def test_a_payload_that_only_looks_like_an_extension_is_still_refused(self) -> None:
        """The whole point: a leading ``.jpg`` in the name is not a leading JPEG in the
        bytes, and the bytes win."""
        assert sniff_content_type(b"hello.jpg") is None


class TestAllowedContentTypes:
    def test_every_kind_has_an_allow_list(self) -> None:
        assert set(ALLOWED_CONTENT_TYPES) == set(MediaKind)

    def test_an_avatar_may_not_be_an_animated_gif(self) -> None:
        """A UI decision, and the one place avatar and image deliberately differ."""
        assert "image/gif" not in ALLOWED_CONTENT_TYPES[MediaKind.AVATAR]
        assert "image/gif" in ALLOWED_CONTENT_TYPES[MediaKind.IMAGE]

    def test_no_kind_accepts_a_non_media_type(self) -> None:
        for allowed in ALLOWED_CONTENT_TYPES.values():
            assert all(t.startswith(("image/", "audio/")) for t in allowed)

    def test_kind_values_are_the_check_constraint_source(self) -> None:
        assert MEDIA_KINDS == ("avatar", "image", "audio", "pronunciation")


class TestBuildStorageKey:
    def test_the_key_carries_the_kind_and_the_extension(self) -> None:
        key = build_storage_key(MediaKind.AVATAR, "image/png")
        assert re.fullmatch(r"avatar/[0-9a-f]{32}\.png", key)

    def test_two_calls_never_collide(self) -> None:
        keys = {build_storage_key(MediaKind.IMAGE, "image/jpeg") for _ in range(100)}
        assert len(keys) == 100

    def test_wav_and_x_wav_share_the_canonical_extension(self) -> None:
        assert build_storage_key(MediaKind.AUDIO, "audio/wav").endswith(".wav")
        assert build_storage_key(MediaKind.AUDIO, "audio/x-wav").endswith(".wav")


class TestParseStorageKey:
    def test_it_round_trips_a_minted_key(self) -> None:
        key = build_storage_key(MediaKind.PRONUNCIATION, "audio/mpeg")
        assert parse_storage_key(key) == (MediaKind.PRONUNCIATION, "mp3")

    def test_it_refuses_a_key_this_service_did_not_issue(self) -> None:
        assert parse_storage_key("../../etc/passwd") is None
        assert parse_storage_key("avatar/not-a-uuid.png") is None
        assert parse_storage_key("avatar/" + "0" * 32 + ".png") is not None
        assert parse_storage_key("video/" + "0" * 32 + ".mp4") is None
        assert parse_storage_key("avatar/" + "0" * 32 + ".toolongextension") is None
        assert parse_storage_key("avatar/" + "0" * 31 + ".png") is None

    def test_the_uppercase_hex_is_refused(self) -> None:
        """Minted keys are lowercase; refusing the other case keeps one key from having two
        spellings for the unique constraint to disagree about."""
        assert parse_storage_key("avatar/" + "A" * 32 + ".png") is None


class TestMaxBytesAllowed:
    def test_images_use_the_image_ceiling_and_audio_the_audio_one(self) -> None:
        for kind in (MediaKind.AVATAR, MediaKind.IMAGE):
            assert max_bytes_allowed(kind, image_max=10, audio_max=99) == 10
        for kind in (MediaKind.AUDIO, MediaKind.PRONUNCIATION):
            assert max_bytes_allowed(kind, image_max=10, audio_max=99) == 99
