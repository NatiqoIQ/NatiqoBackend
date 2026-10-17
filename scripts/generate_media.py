"""Generate the curriculum's image and audio aids, without a third-party media host.

:mod:`scripts.fetch_media` *downloads* aids that were authored as remote URLs, which works
only while the original host is up, permissive and reachable — none of which this project
controls, and none of which is true on a locked-down build machine. This script is the
counterpart for that situation: it produces the same files locally, so a lesson's media is
reproducible from the repository alone.

Two sources, chosen because each is reachable when the other is not:

**Images — Wikimedia Commons.** A word-relevant picture is fetched by searching Commons for
a concrete phrase and keeping the best freely-licensed raster hit. Relevance is not left to
the search ranking: candidates are scored on licence (public domain and CC0 first, because
they carry no attribution obligation), on whether the file name echoes the query, and on
resolution. Licences that *do* require attribution are recorded in the manifest rather than
being used silently. When nothing is good enough — or the network is down — a typographic
card is drawn instead, so no question is left without an image.

**Audio — the machine's own speech synthesiser.** ``System.Speech`` on Windows, driven
through PowerShell, writes one wave file per line; ``ffmpeg`` encodes it to the MP3 the
catalogue already points at. Speaking locally rather than fetching a fixed recording means
the audio covers whole sentences as easily as single words, which is what a listening item
needs.

The manifest at ``media/sources.json`` is shared with :mod:`scripts.fetch_media` and stays
readable by it: ``image`` remains the direct file URL, so a later online re-run reproduces
the same picture instead of downloading a page. Provenance the other script has no concept
of — licence, credit, the spoken text — is added under its own keys.

    uv run python -m scripts.generate_media --dry-run       # report the plan, write nothing
    uv run python -m scripts.generate_media --yes           # generate everything missing
    uv run python -m scripts.generate_media --yes --force   # regenerate every file
    uv run python -m scripts.generate_media --yes --kinds audio
    uv run python -m scripts.generate_media --yes --slug a2-shopping-money-q5

Commit the ``media/`` tree afterwards; like the curriculum itself, it is content rather than
a build artefact.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlencode

from scripts.seed_curriculum import CURRICULUM, SeedLesson, SeedQuestion, SeedUnit

ROOT = Path(__file__).resolve().parents[1]
MEDIA_ROOT = ROOT / "media"
IMAGE_DIR = MEDIA_ROOT / "images"
AUDIO_DIR = MEDIA_ROOT / "audio"
MANIFEST = MEDIA_ROOT / "sources.json"

#: Matches the size :mod:`scripts.fetch_media` downloads, so a client cannot tell which
#: script produced a given picture and a cached aid stays valid either way.
IMAGE_WIDTH = 640
IMAGE_HEIGHT = 360

#: Commons serves a thumbnail of any raster *or vector* file at this width. Asking for a
#: thumbnail rather than the original is what keeps an SVG diagram usable (the API
#: rasterises it) and keeps a 40-megapixel photograph from being decoded at full size.
THUMBNAIL_WIDTH = 800

#: How many Commons searches may be in flight at once. Deliberately modest: the API is a
#: shared free service and the bottleneck is latency, not our bandwidth.
SEARCH_WORKERS = 4

#: Licences that impose no attribution requirement, matched as a prefix of the Commons
#: short name. Anything else is still usable, but is recorded in the manifest so the
#: obligation is visible rather than lost.
_ATTRIBUTION_FREE_LICENCES = ("public domain", "cc0", "cc-zero")

#: Wikimedia rejects a request with no identifying user agent, and rejects the fingerprint
#: of some HTTP client libraries outright. ``curl`` is used for every call for exactly that
#: reason: it is the client the API is willing to answer.
_USER_AGENT = (
    "NatiqoCurriculumBot/1.0 (educational content seeding; https://github.com/qutaibatalal/Natiqo)"
)
_COMMONS_API = "https://commons.wikimedia.org/w/api.php"

#: Tried in order. ``System.Speech`` installs a British voice only on a machine whose
#: locale pack provides one, so an American voice is the honest fallback rather than a
#: silent failure.
_PREFERRED_VOICES = (
    "Microsoft Hazel Desktop",
    "Microsoft George Desktop",
    "Microsoft Zira Desktop",
    "Microsoft David Desktop",
)

#: Slightly slower than the default: the listener is an A1 learner, and prosody that races
#: ahead of comprehension is worse than prosody that sounds deliberate.
_SPEECH_RATE = -1

#: Searched in order; a machine usually has exactly one. The bold cut is preferred because
#: the card is a flashcard, and a thin face at 46 px on a phone is hard to read.
_CARD_FONTS = (
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)

_CARD_BACKGROUND = (30, 58, 138)
_CARD_ACCENT = (14, 165, 233)
_CARD_MARGIN = 40


# ----------------------------------------------------------------------- spec
@dataclass(frozen=True, slots=True)
class QuestionMedia:
    """What one question wants its aids to be, independent of how they are produced.

    ``speak`` is the text a learner hears; ``image_query`` is a concrete, searchable phrase
    for the picture (``"a red apple"``, not ``"fruit"``). Both belong next to the question,
    because only the author knows what the item is *teaching* — the prompt alone cannot
    tell a picture of a bank from a picture of a river bank.
    """

    slug: str
    speak: str | None
    image_query: str | None
    wants_image: bool
    wants_audio: bool


def _derived_speak(question: SeedQuestion) -> str | None:
    """The best guess at what to pronounce when the question does not declare it.

    The taught item is usually the answer the question is keyed on, so a keyed answer is
    the fallback: for a ``fill_blank`` that is the missing word and for a choice question
    it is the correct option. Nothing is derived from the prompt, because a prompt is an
    instruction more often than it is a phrase worth hearing.
    """
    if question.accepted_answers:
        return question.accepted_answers[0]
    if question.correct_index is not None and 0 <= question.correct_index < len(question.choices):
        return question.choices[question.correct_index]
    return None


def _derived_image_query(lesson: SeedLesson, unit: SeedUnit) -> str:
    """A topic phrase for the picture, from the lesson and the unit it sits in.

    The lesson title is a serviceable picture prompt for a topic like "Colours" or "Saying
    hello" — a good deal better than a slug, and only ever used when the question itself
    declares nothing.
    """
    return f"{lesson.title} {unit.title}".strip()


def collect(
    levels: frozenset[str] | None = None, slugs: frozenset[str] | None = None
) -> list[QuestionMedia]:
    """Every declared question that carries a media aid, in curriculum order.

    A question with no ``image_url`` and no ``audio_url`` is skipped rather than having aids
    invented for it: the seed decides which items are illustrated. ``levels`` narrows the
    sweep to named CEFR codes and ``slugs`` narrows it to named questions, which is what
    makes regenerating one level's — or one newly authored item's — media a small,
    reviewable operation rather than a rewrite of the whole tree.
    """
    collected: list[QuestionMedia] = []
    for level in CURRICULUM:
        if levels is not None and level.code.value not in levels:
            continue
        for unit in level.units:
            for lesson in unit.lessons:
                for question in lesson.questions:
                    if slugs is not None and question.slug not in slugs:
                        continue
                    wants_image = question.image_url is not None
                    wants_audio = question.audio_url is not None
                    if not (wants_image or wants_audio):
                        continue
                    declared_speak = getattr(question, "speak", None)
                    if wants_audio and not declared_speak:
                        declared_speak = _derived_speak(question)
                    declared_query = getattr(question, "image_query", None)
                    collected.append(
                        QuestionMedia(
                            slug=question.slug,
                            speak=declared_speak if wants_audio else None,
                            image_query=(declared_query or _derived_image_query(lesson, unit))
                            if wants_image
                            else None,
                            wants_image=wants_image,
                            wants_audio=wants_audio,
                        )
                    )
    return collected


# -------------------------------------------------------------------- process
class CommandRunner(Protocol):
    """Whatever runs an external process and fetches a URL.

    An interface rather than three free functions so a test — or ``--dry-run`` — can drive
    the whole pipeline without a network or a sound card.
    """

    def json(self, url: str) -> Any: ...

    def download(self, url: str, dest: Path) -> None: ...

    def run(self, argv: Sequence[str]) -> None: ...


@dataclass(slots=True)
class CurlRunner:
    """The real runner: every outbound call goes through ``curl``. See ``_USER_AGENT``."""

    timeout_seconds: int = 90

    def _argv(self, url: str) -> list[str]:
        return [
            "curl",
            "-sS",
            "-L",
            "-A",
            _USER_AGENT,
            "-m",
            str(self.timeout_seconds),
            url,
        ]

    def json(self, url: str) -> Any:
        completed = subprocess.run(  # noqa: S603 - a fixed argv, never built from input
            self._argv(url), capture_output=True, check=True
        )
        return json.loads(completed.stdout.decode("utf-8", "replace"))

    def download(self, url: str, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(  # noqa: S603 - a fixed argv, never built from input
            [*self._argv(url)[:-1], "-o", str(dest), url], check=True, capture_output=True
        )

    def run(self, argv: Sequence[str]) -> None:
        subprocess.run(list(argv), capture_output=True, check=True)  # noqa: S603


# --------------------------------------------------------------------- images
@dataclass(frozen=True, slots=True)
class ImageCandidate:
    """One Commons search hit, reduced to what the choice between hits depends on."""

    title: str
    file_url: str
    page_url: str
    licence: str
    credit: str
    width: int

    @property
    def is_attribution_free(self) -> bool:
        folded = self.licence.strip().lower()
        return any(folded.startswith(prefix) for prefix in _ATTRIBUTION_FREE_LICENCES)


def _plain_text(value: str) -> str:
    """Commons returns a credit as HTML. Plain text is what belongs in a manifest."""
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def _search_url(query: str) -> str:
    params = {
        "action": "query",
        "format": "json",
        "generator": "search",
        "gsrsearch": query,
        "gsrnamespace": "6",
        "gsrlimit": "15",
        "prop": "imageinfo",
        "iiprop": "url|extmetadata|mime|size",
        "iiurlwidth": str(THUMBNAIL_WIDTH),
    }
    return f"{_COMMONS_API}?{urlencode(params)}"


def parse_candidates(payload: Any, *, query: str) -> list[ImageCandidate]:
    """Rank a search response, best first.

    This ranking is what decides whether a lesson shows an apple or an Apple II computer, so
    it is explicit rather than trusting the API's own relevance order:

    1. a file name containing a query word wins, and it wins *first*. Search relevance alone
       is not enough — a query for ``"apple fruit"`` returns a laptop, and a merely
       permissively-licensed photograph of the wrong thing is worse than a usable one;
    2. an attribution-free licence breaks the tie, because public domain and CC0 carry no
       obligation the project has to honour later (anything else is still usable, and is
       recorded in the manifest with its credit);
    3. a larger picture breaks what is left, because a small one is upscaled into a blur.
    """
    words = {word for word in re.split(r"\W+", query.lower()) if len(word) > 2}
    pages = (payload or {}).get("query", {}).get("pages", {})
    candidates: list[ImageCandidate] = []
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        thumb = info.get("thumburl")
        if not thumb or info.get("mime") not in ("image/jpeg", "image/png"):
            continue
        title = str(page.get("title", ""))
        metadata = info.get("extmetadata", {})
        candidates.append(
            ImageCandidate(
                title=title,
                file_url=str(thumb),
                page_url=f"https://commons.wikimedia.org/wiki/{title.replace(' ', '_')}",
                licence=_plain_text(metadata.get("LicenseShortName", {}).get("value", "")),
                credit=_plain_text(metadata.get("Artist", {}).get("value", "")),
                width=int(info.get("width") or 0),
            )
        )

    def score(candidate: ImageCandidate) -> tuple[int, int, int]:
        name_words = {word for word in re.split(r"\W+", candidate.title.lower()) if word}
        return (
            1 if words & name_words else 0,
            1 if candidate.is_attribution_free else 0,
            min(candidate.width, 4000),
        )

    return sorted(candidates, key=score, reverse=True)


def _encode_image(source: Path, dest: Path) -> None:
    """Re-encode whatever Commons returned into one predictable 640x360 JPEG.

    A client caches a media aid by URL for the life of the app, so the bytes behind a slug
    have to be stable in shape as well as in content: cover-cropping to a fixed frame is
    what stops a portrait photograph from letterboxing inside a landscape card.
    """
    from PIL import Image, ImageOps

    with Image.open(source) as opened:
        opened.load()
        picture = ImageOps.exif_transpose(opened).convert("RGB")
        framed = ImageOps.fit(picture, (IMAGE_WIDTH, IMAGE_HEIGHT), method=Image.Resampling.LANCZOS)
        dest.parent.mkdir(parents=True, exist_ok=True)
        framed.save(dest, "JPEG", quality=82, optimize=True)


def _wrap(text: str, measure: Any, font: Any, max_width: int) -> str:
    """Break a label into lines that fit, preserving any break the author wrote."""
    lines: list[str] = []
    for paragraph in text.splitlines() or [""]:
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}".strip()
            if current and measure.textlength(candidate, font=font) > max_width:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current)
    return "\n".join(lines)


def _draw_card(dest: Path, label: str) -> None:
    """Draw the fallback card: the taught phrase, centred, on a branded field.

    This exists so the pipeline cannot fail closed. A question whose picture could not be
    sourced still ships something legible, and a card that states the phrase is a
    defensible aid for grammar and function items, where no photograph could be right.
    """
    from PIL import Image, ImageDraw, ImageFont

    def load_font(size: int) -> Any:
        for path in _CARD_FONTS:
            if Path(path).is_file():
                return ImageFont.truetype(path, size)
        return ImageFont.load_default(size)

    card = Image.new("RGB", (IMAGE_WIDTH, IMAGE_HEIGHT), _CARD_BACKGROUND)
    draw = ImageDraw.Draw(card)
    # A thin accent bar anchors the composition, so the card reads as designed rather than
    # as a failed download.
    draw.rectangle([0, IMAGE_HEIGHT - 10, IMAGE_WIDTH, IMAGE_HEIGHT], fill=_CARD_ACCENT)

    text = _wrap(label.strip() or "Natiqo", draw, load_font(46), IMAGE_WIDTH - 2 * _CARD_MARGIN)
    font = load_font(46)
    while getattr(font, "size", 20) > 22:
        box = draw.multiline_textbbox((0, 0), text, font=font, spacing=10, align="center")
        if box[3] - box[1] <= IMAGE_HEIGHT - 2 * _CARD_MARGIN:
            break
        font = load_font(getattr(font, "size", 22) - 2)
    draw.multiline_text(
        (IMAGE_WIDTH / 2, IMAGE_HEIGHT / 2 - 5),
        text,
        font=font,
        fill=(255, 255, 255),
        anchor="mm",
        align="center",
        spacing=10,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    card.save(dest, "JPEG", quality=88, optimize=True)


# ---------------------------------------------------------------------- audio
_SPEAK_SCRIPT = r"""
param([string]$Lines, [string]$OutDir, [string]$Voice, [int]$Rate)
Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$installed = @($synth.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name })
if ($Voice -and ($installed -contains $Voice)) { $synth.SelectVoice($Voice) }
elseif ($installed.Count -gt 0) { $synth.SelectVoice($installed[0]) }
$synth.Rate = $Rate
$utf8 = New-Object System.Text.UTF8Encoding $false
foreach ($line in [System.IO.File]::ReadAllLines($Lines, $utf8)) {
    if ([string]::IsNullOrWhiteSpace($line)) { continue }
    $parts = $line -split "`t", 2
    if ($parts.Count -lt 2 -or [string]::IsNullOrWhiteSpace($parts[1])) { continue }
    $synth.SetOutputToWaveFile((Join-Path $OutDir ($parts[0] + ".wav")))
    $synth.Speak($parts[1])
}
$synth.SetOutputToNull()
$synth.Dispose()
"""


def available_voices() -> list[str]:
    """The installed synthesiser voices, best first. Empty when speech is unavailable."""
    script = (
        "Add-Type -AssemblyName System.Speech; "
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
        "$s.GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name }"
    )
    try:
        argv = [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            script,
        ]
        completed = subprocess.run(  # noqa: S603 - a fixed argv, never built from input
            argv, capture_output=True, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return []
    found = [line.strip() for line in completed.stdout.decode("utf-8", "replace").splitlines()]
    installed = [name for name in found if name]
    preferred = [name for name in _PREFERRED_VOICES if name in installed]
    return preferred + [name for name in installed if name not in preferred]


def _encode_mp3(source: Path, dest: Path) -> None:
    """Transcode one wave file into the 64 kbps mono MP3 the catalogue already points at."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-ac",
        "1",
        "-ar",
        "44100",
        "-codec:a",
        "libmp3lame",
        "-b:a",
        "64k",
        str(dest),
    ]
    subprocess.run(argv, capture_output=True, check=True)  # noqa: S603 - a fixed argv


# ------------------------------------------------------------------- manifest
def load_manifest() -> dict[str, dict[str, str]]:
    if not MANIFEST.is_file():
        return {}
    loaded = json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {str(slug): {str(k): str(v) for k, v in entry.items()} for slug, entry in loaded.items()}


def write_manifest(manifest: dict[str, dict[str, str]]) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(
        json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ------------------------------------------------------------------------ run
@dataclass
class Report:
    """What a run did, in the shape an operator needs to read it."""

    images_written: int = 0
    images_from_commons: int = 0
    images_from_card: int = 0
    audios_written: int = 0
    skipped: int = 0
    failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def format(self, *, dry_run: bool) -> str:
        verb = "would write" if dry_run else "wrote"
        lines = [
            f"{verb} {self.images_written} image(s) "
            f"({self.images_from_commons} from Commons, {self.images_from_card} generated) "
            f"and {self.audios_written} audio file(s)",
            f"already present, left alone: {self.skipped}",
        ]
        lines.extend(f"note: {note}" for note in self.notes)
        lines.extend(f"FAILED {failure}" for failure in self.failures)
        return "\n".join(lines)


@dataclass(slots=True)
class _SearchCache:
    """Query to ranked candidates, shared across the search workers.

    A duplicate search is merely slower, never wrong, so the lock guards the dictionary
    against corruption rather than enforcing single-flight.
    """

    entries: dict[str, list[ImageCandidate]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, query: str, produce: Any) -> list[ImageCandidate]:
        with self.lock:
            cached = self.entries.get(query)
        if cached is not None:
            return cached
        candidates = produce()
        with self.lock:
            self.entries[query] = candidates
        return candidates


def _produce_image(
    spec: QuestionMedia,
    *,
    runner: CommandRunner,
    cache: _SearchCache,
    workdir: Path,
) -> dict[str, str]:
    """Produce one picture, returning the manifest fields that describe it.

    A Commons hit is preferred and the drawn card is the fallback at every step: a failed
    search, a failed download and an empty result list all end the same way, because a
    missing image costs a lesson more than an imperfect one.
    """
    dest = IMAGE_DIR / f"{spec.slug}.jpg"
    query = spec.image_query
    if query:
        try:
            candidates = cache.get(
                query, lambda: parse_candidates(runner.json(_search_url(query)), query=query)
            )
            if candidates:
                best = candidates[0]
                raw = workdir / f"{spec.slug}.src"
                runner.download(best.file_url, raw)
                _encode_image(raw, dest)
                return {
                    "image": best.file_url,
                    "image_page": best.page_url,
                    "image_license": best.licence,
                    "image_credit": best.credit,
                    "image_query": query,
                    "image_source": "commons",
                }
        except (OSError, ValueError, subprocess.SubprocessError):
            # Falling through to the card is the whole point of catching here.
            pass

    _draw_card(dest, spec.speak or spec.slug.replace("-", " "))
    return {
        "image": "",
        "image_page": "",
        "image_license": "generated",
        "image_credit": "Natiqo",
        "image_query": query or "",
        "image_source": "card",
    }


def _generate_audio(
    specs: list[QuestionMedia],
    *,
    runner: CommandRunner,
    report: Report,
    manifest: dict[str, dict[str, str]],
    workdir: Path,
    voice: str | None,
) -> None:
    """Synthesise every missing line in one synthesiser session, then encode each one.

    Batching the speech is not a micro-optimisation: starting a synthesiser costs about a
    third of a second, so once per line turns a three-hundred-item curriculum into minutes
    of process startup. Encoding stays per file, because that is the shape ``ffmpeg`` takes.
    """
    voices = available_voices()
    if not voices:
        report.failures.append("audio: no speech synthesiser is available on this machine")
        return
    if shutil.which("ffmpeg") is None:
        report.failures.append("audio: ffmpeg is not on PATH, so wave files cannot be encoded")
        return

    chosen = voice or voices[0]
    lines = workdir / "lines.tsv"
    lines.write_text(
        "".join(f"{spec.slug}\t{spec.speak}\n" for spec in specs if spec.speak),
        encoding="utf-8",
    )
    script = workdir / "speak.ps1"
    script.write_text(_SPEAK_SCRIPT, encoding="utf-8")

    try:
        runner.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-Lines",
                str(lines),
                "-OutDir",
                str(workdir),
                "-Voice",
                chosen,
                "-Rate",
                str(_SPEECH_RATE),
            ]
        )
    except (OSError, subprocess.SubprocessError) as error:
        report.failures.append(f"audio: the synthesiser failed ({error})")
        return

    for spec in specs:
        wave = workdir / f"{spec.slug}.wav"
        if not wave.is_file():
            report.failures.append(f"audio {spec.slug}: the synthesiser produced no wave file")
            continue
        try:
            _encode_mp3(wave, AUDIO_DIR / f"{spec.slug}.mp3")
        except (OSError, subprocess.SubprocessError) as error:
            report.failures.append(f"audio {spec.slug}: encoding failed ({error})")
            continue
        report.audios_written += 1
        manifest.setdefault(spec.slug, {}).update(
            {
                "audio_text": spec.speak or "",
                "audio_engine": f"sapi:{chosen}",
                "audio_rate": str(_SPEECH_RATE),
            }
        )
    report.notes.append(f"spoken by {chosen}")


def run(
    *,
    runner: CommandRunner,
    force: bool,
    kinds: frozenset[str],
    dry_run: bool,
    voice: str | None,
    levels: frozenset[str] | None = None,
    slugs: frozenset[str] | None = None,
) -> Report:
    """Generate every missing aid, or describe what would be generated.

    Writes are per file and resumable: a run cut short by a dropped connection or a crashed
    synthesiser keeps the files it finished, and the next run picks up from there. That is
    why existence on disk — not the manifest — decides what is skipped.
    """
    specs = collect(levels, slugs)
    report = Report()
    manifest = load_manifest()
    workdir = Path(tempfile.mkdtemp(prefix="natiqo-media-"))

    def missing(specs: list[QuestionMedia], path_for: Any) -> list[QuestionMedia]:
        pending = [spec for spec in specs if force or not path_for(spec).is_file()]
        report.skipped += len(specs) - len(pending)
        return pending

    try:
        image_specs = [spec for spec in specs if spec.wants_image]
        audio_specs = [spec for spec in specs if spec.wants_audio and spec.speak]
        image_work = (
            missing(image_specs, lambda spec: IMAGE_DIR / f"{spec.slug}.jpg")
            if "image" in kinds
            else []
        )
        audio_work = (
            missing(audio_specs, lambda spec: AUDIO_DIR / f"{spec.slug}.mp3")
            if "audio" in kinds
            else []
        )

        if dry_run:
            report.images_written = len(image_work)
            report.images_from_card = len(image_work)
            report.audios_written = len(audio_work)
            return report

        cache = _SearchCache()
        if image_work:
            with concurrent.futures.ThreadPoolExecutor(max_workers=SEARCH_WORKERS) as pool:
                pending = {
                    pool.submit(
                        _produce_image, spec, runner=runner, cache=cache, workdir=workdir
                    ): spec
                    for spec in image_work
                }
                for future in concurrent.futures.as_completed(pending):
                    spec = pending[future]
                    try:
                        entry = future.result()
                    except Exception as error:  # noqa: BLE001 - one bad item must not stop a run
                        report.failures.append(f"image {spec.slug}: {error}")
                        continue
                    report.images_written += 1
                    if entry.get("image_source") == "commons":
                        report.images_from_commons += 1
                    else:
                        report.images_from_card += 1
                    manifest.setdefault(spec.slug, {}).update(entry)

        if audio_work:
            _generate_audio(
                audio_work,
                runner=runner,
                report=report,
                manifest=manifest,
                workdir=workdir,
                voice=voice,
            )

        if report.images_written or report.audios_written:
            write_manifest(manifest)
        return report
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------- CLI
def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.generate_media",
        description="Generate the curriculum's image and audio aids locally.",
    )
    parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation.")
    parser.add_argument("--force", action="store_true", help="Regenerate files that exist.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be generated and write nothing.",
    )
    parser.add_argument(
        "--kinds",
        choices=("all", "image", "audio"),
        default="all",
        help="Restrict to one kind of aid. Default: both.",
    )
    parser.add_argument("--voice", default=None, help="Override the speech synthesiser voice.")
    parser.add_argument(
        "--level",
        action="append",
        default=None,
        metavar="CODE",
        help="Restrict to a CEFR level (A1, A2, ...). Repeatable. Default: every level.",
    )
    parser.add_argument(
        "--slug",
        action="append",
        default=None,
        metavar="SLUG",
        help="Restrict to one question slug. Repeatable. Default: every question.",
    )
    return parser


def _confirm(plan: str, *, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    # Refusing when there is no terminal is the safe reading: a generation that ran
    # unattended should be a deliberate --yes, not a surprise write of media files.
    if not sys.stdin.isatty():
        print("Refusing to write without confirmation. Re-run with --yes.", file=sys.stderr)
        return False
    print(plan)
    try:
        answer = input("Generate these files? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    kinds = frozenset({"image", "audio"} if args.kinds == "all" else {args.kinds})
    if os.name != "nt" and "audio" in kinds:
        # The synthesiser path is Windows-only. Saying so beats a stack trace halfway
        # through a run that has already written images.
        print("Note: audio synthesis requires Windows; generating images only.", file=sys.stderr)
        kinds = frozenset({"image"})

    levels = frozenset(code.upper() for code in args.level) if args.level else None
    slugs = frozenset(args.slug) if args.slug else None
    plan = run(
        runner=CurlRunner(),
        force=args.force,
        kinds=kinds,
        dry_run=True,
        voice=args.voice,
        levels=levels,
        slugs=slugs,
    )
    if args.dry_run:
        print(plan.format(dry_run=True))
        return 0
    if not _confirm(plan.format(dry_run=True), assume_yes=args.yes):
        print("Nothing written.", file=sys.stderr)
        return 1

    report = run(
        runner=CurlRunner(),
        force=args.force,
        kinds=kinds,
        dry_run=False,
        voice=args.voice,
        levels=levels,
        slugs=slugs,
    )
    print(report.format(dry_run=False))
    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
