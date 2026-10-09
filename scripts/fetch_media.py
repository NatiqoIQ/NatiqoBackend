"""Fetch curriculum media once, then serve it from this repository.

The image and audio aids declared in :mod:`scripts.seed_curriculum` start life as
third-party URLs. Shipping those to clients would make every lesson depend on an
external host staying up and staying permissive, which is not a dependency a learner's
lesson should have. This script downloads each aid once into ``media/`` at a stable,
slug-based path, records where each file came from in ``media/sources.json``, and can
rewrite the curriculum so the stored value is the same-origin path the API serves.

The manifest is what makes the script re-runnable after the seed has already been
rewritten: the slug identifies the file, so a re-run skips what is on disk and can
reproduce what is missing without the original URLs still living in the seed.

    uv run python -m scripts.fetch_media --rewrite-seed

Add ``--force`` to re-download. Commit the ``media/`` tree afterwards; it is content,
not a build artefact.
"""

from __future__ import annotations

import argparse
import ast
import json
from dataclasses import dataclass
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "scripts" / "seed_curriculum.py"
MEDIA_ROOT = ROOT / "media"
MANIFEST = MEDIA_ROOT / "sources.json"

#: Decorative, deterministic placeholder photography. Seeded by slug so a lesson never
#: changes picture between loads, and licence-free so the repo carries no attribution
#: obligation. Swap in word-relevant, licensed media by dropping files at the same paths.
IMAGE_WIDTH = 640
IMAGE_HEIGHT = 360
_IMAGE_TEMPLATE = "https://picsum.photos/seed/{slug}/{width}/{height}"

IMAGE_DIR = MEDIA_ROOT / "images"
AUDIO_DIR = MEDIA_ROOT / "audio"


@dataclass(frozen=True)
class _QuestionMedia:
    slug: str
    image_value: str | None
    image_span: tuple[int, int] | None
    audio_value: str | None
    audio_span: tuple[int, int] | None


def _line_start_offsets(source: str) -> list[int]:
    offsets = [0]
    for line in source.splitlines(keepends=True):
        offsets.append(offsets[-1] + len(line))
    return offsets


def _node_offset(offsets: list[int], node: ast.expr) -> tuple[int, int]:
    assert node.end_lineno is not None and node.end_col_offset is not None
    start = offsets[node.lineno - 1] + node.col_offset
    end = offsets[node.end_lineno - 1] + node.end_col_offset
    return start, end


def _keyword_constant(
    keywords: dict[str, ast.expr], offsets: list[int], name: str
) -> tuple[str | None, tuple[int, int] | None]:
    """The string value of a keyword argument and its source span, if it is a literal."""
    value_node = keywords.get(name)
    if isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
        return value_node.value, _node_offset(offsets, value_node)
    return None, None


def _scan(source: str) -> list[_QuestionMedia]:
    """Every ``SeedQuestion`` that carries an aid, with the span of each aid literal.

    The spans let :func:`_rewrite` swap a value in place without reformatting a file it
    does not otherwise touch.
    """
    offsets = _line_start_offsets(source)
    found: list[_QuestionMedia] = []
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "SeedQuestion"
        ):
            continue
        keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
        slug_node = keywords.get("slug")
        if not (isinstance(slug_node, ast.Constant) and isinstance(slug_node.value, str)):
            continue

        image_value, image_span = _keyword_constant(keywords, offsets, "image_url")
        audio_value, audio_span = _keyword_constant(keywords, offsets, "audio_url")
        found.append(
            _QuestionMedia(
                slug=slug_node.value,
                image_value=image_value,
                image_span=image_span,
                audio_value=audio_value,
                audio_span=audio_span,
            )
        )
    return found


def _load_manifest() -> dict[str, dict[str, str]]:
    if not MANIFEST.is_file():
        return {}
    loaded = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {str(slug): dict(entry) for slug, entry in loaded.items()}


def _download(client: httpx.Client, url: str, dest: Path, *, force: bool) -> str:
    if dest.is_file() and not force:
        return "skip"
    response = client.get(url)
    response.raise_for_status()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(response.content)
    return f"{len(response.content)}B"


def _rewrite(source: str, questions: list[_QuestionMedia]) -> tuple[str, int]:
    replacements: list[tuple[tuple[int, int], str]] = []
    for question in questions:
        if (
            question.image_span is not None
            and question.image_value is not None
            and question.image_value.startswith("http")
        ):
            replacements.append((question.image_span, f'"/media/images/{question.slug}.jpg"'))
        if (
            question.audio_span is not None
            and question.audio_value is not None
            and question.audio_value.startswith("http")
        ):
            replacements.append((question.audio_span, f'"/media/audio/{question.slug}.mp3"'))
    for (start, end), text in sorted(replacements, reverse=True):
        source = source[:start] + text + source[end:]
    return source, len(replacements)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="re-download existing files")
    parser.add_argument(
        "--rewrite-seed",
        action="store_true",
        help="point the curriculum at the same-origin media paths",
    )
    args = parser.parse_args(argv)

    source = SEED.read_text(encoding="utf-8")
    questions = _scan(source)
    manifest = _load_manifest()

    images = audios = 0
    failures: list[str] = []
    with httpx.Client(follow_redirects=True, timeout=60.0) as client:
        for question in questions:
            entry = manifest.setdefault(question.slug, {})
            image_src = entry.get("image") or _IMAGE_TEMPLATE.format(
                slug=question.slug, width=IMAGE_WIDTH, height=IMAGE_HEIGHT
            )
            entry["image"] = image_src
            audio_src = entry.get("audio")
            if question.audio_value and question.audio_value.startswith("http"):
                audio_src = question.audio_value
            if audio_src:
                entry["audio"] = audio_src

            if question.image_value is not None:
                try:
                    outcome = _download(
                        client, image_src, IMAGE_DIR / f"{question.slug}.jpg", force=args.force
                    )
                    images += outcome != "skip"
                except httpx.HTTPError as error:
                    failures.append(f"image {question.slug}: {error}")
            if question.audio_value is not None and audio_src:
                try:
                    outcome = _download(
                        client, audio_src, AUDIO_DIR / f"{question.slug}.mp3", force=args.force
                    )
                    audios += outcome != "skip"
                except httpx.HTTPError as error:
                    failures.append(f"audio {question.slug}: {error}")

    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    rewritten = 0
    if args.rewrite_seed:
        rewritten_source, rewritten = _rewrite(source, questions)
        if rewritten:
            SEED.write_text(rewritten_source, encoding="utf-8")

    print(f"questions with media: {len(questions)}")
    print(f"downloaded: {images} image(s), {audios} audio file(s)")
    print(f"manifest: {MANIFEST.relative_to(ROOT)} ({len(manifest)} entries)")
    if args.rewrite_seed:
        print(f"seed literals rewritten: {rewritten}")
    for failure in failures:
        print(f"  FAILED {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
