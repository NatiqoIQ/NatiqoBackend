"""What a media run would touch, exercised without a network or a sound card.

The generation itself is side effects on ``media/``: Commons searches, a speech synthesiser
and a JPEG encoder. None of those belongs in a test — the first is unreachable on a build
machine, the second only exists on Windows, and rewriting the shipped tree from a test would
make the suite change content. What *is* worth pinning is the rule that decides which files a
run would write, because that rule is what makes regenerating media after an author adds a
question a small, reviewable operation rather than a rewrite of 372 files.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

import pytest

from scripts import generate_media
from scripts.generate_media import QuestionMedia, collect, run

pytestmark = pytest.mark.unit

#: A question in the shipped curriculum that declares both aids, so a scoped run has
#: something to find.
KNOWN_SLUG = "a2-shopping-money-q5"

#: A question from another level, for asserting that a filter really excludes.
OTHER_LEVEL_SLUG = "a1-numbers-age-q1"


class _OfflineRunner:
    """A runner that fails if a plan is ever allowed to reach the network.

    ``--dry-run`` is the only mode these tests use, and it must decide what it *would* write
    without asking Commons anything: a plan that searched would be a plan that had already
    started doing the work.
    """

    def json(self, url: str) -> Any:
        raise AssertionError(f"a plan must not search: {url}")

    def download(self, url: str, dest: Path) -> None:
        raise AssertionError(f"a plan must not download: {url}")

    def run(self, argv: Sequence[str]) -> None:
        raise AssertionError(f"a plan must not run a process: {argv}")


class TestCollect:
    def test_a_slug_filter_keeps_only_the_named_question(self) -> None:
        scoped = collect(slugs=frozenset({KNOWN_SLUG}))

        assert [spec.slug for spec in scoped] == [KNOWN_SLUG]
        assert scoped[0] == QuestionMedia(
            slug=KNOWN_SLUG,
            speak="Change, a receipt, cash, a kilo.",
            image_query="euro coins banknotes",
            wants_image=True,
            wants_audio=True,
        )

    def test_a_slug_filter_that_matches_nothing_collects_nothing(self) -> None:
        assert collect(slugs=frozenset({"no-such-question"})) == []

    def test_a_level_filter_is_a_strict_subset_of_the_whole_sweep(self) -> None:
        everything = {spec.slug for spec in collect()}
        a2_only = {spec.slug for spec in collect(levels=frozenset({"A2"}))}

        assert a2_only < everything
        assert KNOWN_SLUG in a2_only
        assert OTHER_LEVEL_SLUG not in a2_only

    def test_the_two_filters_intersect(self) -> None:
        """Naming one slug and the wrong level is an empty plan, not a widened one."""
        assert collect(levels=frozenset({"A1"}), slugs=frozenset({KNOWN_SLUG})) == []


@pytest.fixture
def empty_media_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, Path]]:
    """Point the plan at a fresh tree, so what is on disk is decided by the test.

    A plan reads the two media directories to decide what is missing, so without this the
    result would depend on whether the developer has already run the generator — the kind of
    test that passes on a clean checkout and fails on a working machine.
    """
    images = tmp_path / "images"
    audios = tmp_path / "audio"
    images.mkdir()
    audios.mkdir()
    monkeypatch.setattr(generate_media, "IMAGE_DIR", images)
    monkeypatch.setattr(generate_media, "AUDIO_DIR", audios)
    yield images, audios


def _plan(**overrides: Any) -> Any:
    defaults: dict[str, Any] = {
        "runner": _OfflineRunner(),
        "force": False,
        "kinds": frozenset({"image", "audio"}),
        "dry_run": True,
        "voice": None,
        "slugs": frozenset({KNOWN_SLUG}),
    }
    return run(**{**defaults, **overrides})


class TestRunPlan:
    def test_a_scoped_dry_run_plans_exactly_the_named_questions_aids(
        self, empty_media_tree: tuple[Path, Path]
    ) -> None:
        report = _plan()

        assert report.images_written == 1
        assert report.audios_written == 1
        assert report.skipped == 0
        assert report.failures == []

    def test_a_plan_leaves_files_already_on_disk_alone(
        self, empty_media_tree: tuple[Path, Path]
    ) -> None:
        """Existence on disk — not the manifest — is what decides a skip."""
        images, audios = empty_media_tree
        (images / f"{KNOWN_SLUG}.jpg").write_bytes(b"jpeg")
        (audios / f"{KNOWN_SLUG}.mp3").write_bytes(b"mp3")

        report = _plan()

        assert report.images_written == 0
        assert report.audios_written == 0
        assert report.skipped == 2

    def test_force_plans_those_files_again(self, empty_media_tree: tuple[Path, Path]) -> None:
        images, audios = empty_media_tree
        (images / f"{KNOWN_SLUG}.jpg").write_bytes(b"jpeg")
        (audios / f"{KNOWN_SLUG}.mp3").write_bytes(b"mp3")

        report = _plan(force=True)

        assert report.images_written == 1
        assert report.audios_written == 1

    def test_an_unscoped_plan_covers_the_whole_curriculum(
        self, empty_media_tree: tuple[Path, Path]
    ) -> None:
        report = _plan(kinds=frozenset({"image"}), slugs=None)

        assert report.images_written == len(collect())
        assert report.audios_written == 0

    def test_one_kind_at_a_time_is_respected(self, empty_media_tree: tuple[Path, Path]) -> None:
        report = _plan(kinds=frozenset({"audio"}))

        assert report.images_written == 0
        assert report.audios_written == 1
