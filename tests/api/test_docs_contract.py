"""The client documentation, held against the document it describes.

``GET /openapi.json`` is the generated source of truth, and the client guides are what a
human — and a hand-written client — actually reads. They are also where a payload example
rots quietly: the schema gains a field, the example keeps the old shape, and nothing fails
until an engineer implements the stale version. That has already happened once in this
repository, when ``prompts`` was added to the question schemas and the session example in
``mobile-app-architecture.md`` went on showing a question without it.

So every payload example in the client guides is checked against the component schema it
documents, and three checks keep that honest:

* **Every example is accounted for.** Each JSON value in every fenced ``json``/``jsonc``
  block of the client guides is either *pinned* to a schema or *exempted* with a reason.
  An example nobody pinned fails the ownership test rather than being skipped, so this file
  cannot decay into "the examples that still pass".
* **A pinned example matches its schema.** Field names exist, required fields are present,
  enums, types and numeric bounds hold, and at the paths marked ``complete`` the example
  lists *every* field the schema declares — both directions, so a schema field the guide
  forgot and a guide field the schema does not have are both failures.
* **A marker still resolves.** Pins and exemptions are found by a marker string, so moving
  or deleting the example they describe fails loudly instead of silently validating nothing.

What is deliberately *not* checked: ``format`` (``uuid``, ``date-time``) and string length
bounds. The guides use placeholders — ``"uuid"``, ``"…"``, ``"eyJhbGciOi..."`` — precisely
because a real id is noise in an example, and a test that demanded a sixteen-character token
everywhere would be one more reason to stop documenting payloads.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from app.main import app

pytestmark = pytest.mark.api

ROOT = Path(__file__).resolve().parents[2]

#: The guide a client engineer implements from. ``client-contract.md`` and
#: ``mobile-integration-ar.md`` were folded into this one and deleted: it is the only
#: surviving contract, and the presentation plan is deliberately absent — it is a design
#: note about work in progress, not a promise to a client.
CLIENT_DOCS: tuple[str, ...] = ("docs/mobile-app-architecture.md",)

_FENCE = re.compile(r"```(?:json|jsonc)\r?\n(.*?)```", re.DOTALL)


# ------------------------------------------------------------------ the shapes
@dataclass(frozen=True, slots=True)
class Example:
    """One JSON value inside a documentation block, located in the raw document text."""

    doc: str
    start: int
    end: int
    #: ``None`` when the text could not be decoded at all — an example abbreviated with a
    #: structural ellipsis, which is a thing a guide may do and a test may not hide.
    value: Any | None


@dataclass(frozen=True, slots=True)
class Pin:
    """An example that must satisfy a component schema.

    ``complete`` names the paths, relative to ``path``, whose objects are claimed to be
    exhaustive: there the example must list exactly the schema's fields. Everything outside
    those paths is still checked for field names, required fields, enums, types and numeric
    bounds — but an abbreviated nested object (a guide shows a ``session`` with three
    fields, the schema declares twelve) is shorthand, not drift.
    """

    doc: str
    marker: str
    schema: str
    path: str = "$"
    complete: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Exempt:
    """An example that is deliberately not pinned, and why.

    The reason is data rather than a comment: an exemption is a decision a reader should be
    able to argue with, and a blank one would just be a place to hide an example.
    """

    doc: str
    marker: str
    why: str


# ------------------------------------------------------------------- the corpus
#: Every payload example in the client guide, pinned to the schema it documents.
PINS: tuple[Pin, ...] = (
    # --------------------------------------------- docs/mobile-app-architecture.md
    Pin(
        doc=CLIENT_DOCS[0],
        marker='"details": { }',
        schema="ErrorResponse",
        complete=("$", "$.error"),
    ),
    Pin(
        doc=CLIENT_DOCS[0],
        marker='"answeredQuestionIds":[]',
        schema="SessionDetailResponse",
        complete=("$", "$.session", "$.questions[*]"),
    ),
    Pin(doc=CLIENT_DOCS[0], marker='"answer": { "choice":"b" }', schema="AnswerCommand"),
    Pin(doc=CLIENT_DOCS[0], marker='"choices":["a","b"]', schema="AnswerCommand"),
    Pin(doc=CLIENT_DOCS[0], marker='"order":["Hello!"', schema="AnswerCommand"),
    Pin(doc=CLIENT_DOCS[0], marker='"matching":["Monday => 1"', schema="AnswerCommand"),
    Pin(
        doc=CLIENT_DOCS[0],
        marker='"isComplete":false',
        schema="AnswerCommandResult",
        complete=("$", "$.result"),
    ),
    Pin(
        doc=CLIENT_DOCS[0],
        marker='"wordsLearnedToday": 5',
        schema="DailyChallengeResponse",
        complete=("$",),
    ),
)

#: Examples the corpus check accepts without a schema, each for a stated reason.
EXEMPTIONS: tuple[Exempt, ...] = (
    Exempt(
        doc=CLIENT_DOCS[0],
        marker='"scorePercent":100',
        why=(
            'Written with a structural ellipsis (`{ … , "status":"completed" }`) so the '
            "example stays about the envelope. An example the decoder cannot read cannot be "
            "validated, and validating a paraphrase of it would prove nothing."
        ),
    ),
)


# ------------------------------------------------------------------- extracting
def _strip_comments(text: str) -> tuple[str, list[int]]:
    """Remove ``//`` and ``/* */`` comments, keeping a map back to the raw offsets.

    The guides are ``jsonc`` because their examples carry prose, and a marker may point at
    a comment (``/* ... */`` abbreviates one list), so callers need offsets in the *original*
    document rather than in the stripped copy.
    """
    stripped: list[str] = []
    offsets: list[int] = []
    index = 0
    in_string = False
    while index < len(text):
        char = text[index]
        if in_string:
            stripped.append(char)
            offsets.append(index)
            if char == "\\" and index + 1 < len(text):
                stripped.append(text[index + 1])
                offsets.append(index + 1)
                index += 2
                continue
            in_string = char != '"'
            index += 1
            continue
        if char == '"':
            in_string = True
        elif text.startswith("//", index):
            while index < len(text) and text[index] != "\n":
                index += 1
            continue
        elif text.startswith("/*", index):
            closing = text.find("*/", index + 2)
            index = len(text) if closing == -1 else closing + 2
            continue
        stripped.append(char)
        offsets.append(index)
        index += 1
    return "".join(stripped), offsets


def _chunk_end(text: str, start: int) -> int:
    """Where an undecodable run of text ends: the next blank line, or the block's end."""
    match = re.search(r"\n[ \t]*\n", text[start:])
    return len(text) if match is None else start + match.end()


def _documents(stripped: str) -> list[tuple[int, int, Any | None]]:
    """Every JSON value in the stripped block, plus whatever could not be decoded.

    Decoding document by document rather than parsing the block as one value is what stops a
    single abbreviated example from hiding the four good ones beside it.
    """
    decoder = json.JSONDecoder()
    found: list[tuple[int, int, Any | None]] = []
    index = 0
    while index < len(stripped):
        while index < len(stripped) and stripped[index].isspace():
            index += 1
        if index >= len(stripped):
            break
        try:
            value, end = decoder.raw_decode(stripped, index)
        except json.JSONDecodeError:
            end = _chunk_end(stripped, index)
            found.append((index, end, None))
            index = end
            continue
        found.append((index, end, value))
        index = end
    return found


def _examples(doc: str) -> list[Example]:
    """Every example in one guide, located in raw-document offsets."""
    text = _text(doc)
    collected: list[Example] = []
    for block in _FENCE.finditer(text):
        stripped, offsets = _strip_comments(block.group(1))
        base = block.start(1)
        for start, end, value in _documents(stripped):
            collected.append(
                Example(
                    doc=doc,
                    start=base + offsets[start],
                    end=base + offsets[end - 1] + 1,
                    value=value,
                )
            )
    return collected


def _text(doc: str) -> str:
    return (ROOT / doc).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def schemas() -> dict[str, Any]:
    return app.openapi()["components"]["schemas"]


@pytest.fixture(scope="module")
def examples() -> dict[str, list[Example]]:
    return {doc: _examples(doc) for doc in CLIENT_DOCS}


# ------------------------------------------------------------------ the wire spec
def _segments(expression: str) -> tuple[str, ...]:
    """``$.items[*].units`` into ``("items", "*", "units")``."""
    if expression == "$":
        return ()
    assert expression.startswith("$"), f"unsupported path {expression!r}"
    remainder = expression[1:].removeprefix(".")
    parts: list[str] = []
    for chunk in remainder.split("."):
        name, _, tail = chunk.partition("[")
        if name:
            parts.append(name)
        if tail:
            assert tail == "*]", f"unsupported path segment {chunk!r}"
            parts.append("*")
    return tuple(parts)


def _is_complete(path: tuple[Any, ...], patterns: tuple[tuple[str, ...], ...]) -> bool:
    """Whether the object at ``path`` is one the example claims to list exhaustively.

    ``[*]`` in a pattern matches an array index, which is what makes ``$.questions[*]``
    reach the nested objects a guide is most likely to leave behind: a question shape that
    gained a field while the example beside it stayed as it was.
    """
    return any(
        len(pattern) == len(path)
        and all(
            expected == "*" or expected == str(actual)
            for expected, actual in zip(pattern, path, strict=True)
        )
        for pattern in patterns
    )


def _is_type(value: Any, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "boolean":
        return isinstance(value, bool)
    if name == "string":
        return isinstance(value, str)
    if name == "array":
        return isinstance(value, list)
    if name == "object":
        return isinstance(value, dict)
    if name == "null":
        return value is None
    return True  # an unknown type name is the schema's business, not the example's


def _resolve(schema: dict[str, Any], schemas: dict[str, Any]) -> dict[str, Any]:
    depth = 0
    while "$ref" in schema:
        schema = schemas[schema["$ref"].rsplit("/", 1)[-1]]
        depth += 1
        assert depth < 10, "cycling $ref"
    return schema


def _walk(
    value: Any,
    schema: dict[str, Any],
    path: tuple[Any, ...],
    *,
    complete: tuple[tuple[str, ...], ...],
    where: str,
    problems: list[str],
    schemas: dict[str, Any],
) -> None:
    """Check one value against one schema, collecting every problem rather than the first."""
    schema = _resolve(schema, schemas)

    for keyword in ("anyOf", "oneOf"):
        alternatives = schema.get(keyword)
        if not alternatives:
            continue
        for alternative in alternatives:
            branch_problems: list[str] = []
            _walk(
                value,
                alternative,
                path,
                complete=(),
                where=where,
                problems=branch_problems,
                schemas=schemas,
            )
            if not branch_problems:
                return
        problems.append(f"{where}: the value matches none of the {keyword} alternatives")
        return

    if "const" in schema and value != schema["const"]:
        problems.append(f"{where}: expected {schema['const']!r}, got {value!r}")
    enum = schema.get("enum")
    if enum and value not in enum:
        problems.append(f"{where}: {value!r} is not one of {enum}")

    declared = schema.get("type")
    names = [name for name in (declared if isinstance(declared, list) else [declared]) if name]
    if names and not any(_is_type(value, name) for name in names):
        problems.append(f"{where}: expected {declared}, got {type(value).__name__}")
        return

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        if _is_complete(path, complete):
            missing = sorted(set(properties) - set(value))
            extra = sorted(set(value) - set(properties))
            if missing:
                problems.append(f"{where}: the schema declares {missing}, the example omits them")
            if extra:
                problems.append(f"{where}: the example shows {extra}, the schema declares none")
        for key, item in value.items():
            if key in properties:
                _walk(
                    item,
                    properties[key],
                    (*path, key),
                    complete=complete,
                    where=f"{where}.{key}",
                    problems=problems,
                    schemas=schemas,
                )
                continue
            additional = schema.get("additionalProperties", None)
            if isinstance(additional, dict):
                _walk(
                    item,
                    additional,
                    (*path, key),
                    complete=(),
                    where=f"{where}.{key}",
                    problems=problems,
                    schemas=schemas,
                )
            elif additional is not True:
                problems.append(f"{where}: {key!r} is not a field this schema declares")
        for required in schema.get("required", []):
            if required not in value:
                problems.append(f"{where}: the schema requires {required!r}")

    if isinstance(value, list):
        items = schema.get("items")
        if isinstance(items, dict):
            for index, item in enumerate(value):
                _walk(
                    item,
                    items,
                    (*path, index),
                    complete=complete,
                    where=f"{where}[{index}]",
                    problems=problems,
                    schemas=schemas,
                )

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for keyword, holds in (
            ("minimum", lambda number, limit: number >= limit),
            ("maximum", lambda number, limit: number <= limit),
        ):
            limit = schema.get(keyword)
            if limit is not None and not holds(value, limit):
                problems.append(f"{where}: {value} is outside {keyword} {limit}")


def _select(value: Any, path: str, *, where: str, problems: list[str]) -> list[Any]:
    """The values a path expression points at — ``$[*]`` finds every item of an array."""
    current = [value]
    for segment in _segments(path):
        found: list[Any] = []
        for item in current:
            if segment == "*":
                if isinstance(item, list):
                    found.extend(item)
                else:
                    problems.append(f"{where}: {path} expects an array")
            elif isinstance(item, dict) and segment in item:
                found.append(item[segment])
            else:
                problems.append(f"{where}: {path} names no field {segment!r}")
        current = found
    return current


def _pinned_problems(pin: Pin, example: Example, schemas: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    where = f"{pin.doc} [{pin.marker}] at {pin.path}"
    if pin.schema not in schemas:
        return [f"{where}: there is no component schema named {pin.schema}"]
    complete = tuple(_segments(pattern) for pattern in pin.complete)
    for target in _select(example.value, pin.path, where=where, problems=problems):
        _walk(
            target,
            schemas[pin.schema],
            (),
            complete=complete,
            where=where,
            problems=problems,
            schemas=schemas,
        )
    return problems


# ---------------------------------------------------------------------- checks


def test_every_example_has_exactly_one_owner(examples: dict[str, list[Example]]) -> None:
    """No example is skipped, and none is claimed twice.

    This is the check that keeps the rest of the file meaningful: an example added to a
    guide fails here until it is pinned to a schema or exempted with a reason, and a marker
    that stops pointing at its example fails here rather than validating nothing.
    """
    unaccounted: list[str] = []
    ambiguous: list[str] = []
    for doc, found in examples.items():
        text = _text(doc)
        markers = [entry for entry in (*PINS, *EXEMPTIONS) if entry.doc == doc]
        for example in found:
            owners = [
                entry
                for entry in markers
                if (start := text.find(entry.marker)) != -1 and example.start <= start < example.end
            ]
            label = (
                f"{doc} at offset {example.start} "
                f"({type(example.value).__name__ if example.value is not None else 'undecodable'})"
            )
            if not owners:
                unaccounted.append(label)
            elif len(owners) > 1:
                ambiguous.append(f"{label}: {[entry.marker for entry in owners]}")

    assert not unaccounted, "examples neither pinned nor exempted:\n" + "\n".join(unaccounted)
    assert not ambiguous, "examples claimed more than once:\n" + "\n".join(ambiguous)


def test_each_marker_resolves_to_exactly_one_example(examples: dict[str, list[Example]]) -> None:
    """A marker that moved, was deleted, or became ambiguous must fail, not silently pass."""
    problems: list[str] = []
    for entry in (*PINS, *EXEMPTIONS):
        text = _text(entry.doc)
        occurrences = text.count(entry.marker)
        if occurrences != 1:
            problems.append(
                f"{entry.doc}: {entry.marker!r} appears {occurrences} times, expected once"
            )
            continue
        start = text.index(entry.marker)
        inside = [
            example for example in examples[entry.doc] if example.start <= start < example.end
        ]
        if len(inside) != 1:
            problems.append(
                f"{entry.doc}: {entry.marker!r} is not inside exactly one example "
                f"(found {len(inside)})"
            )

    assert not problems, "stale documentation markers:\n" + "\n".join(problems)


def _pin_id(pin: Pin) -> str:
    slug = re.sub(r"\W+", "-", pin.marker, flags=re.UNICODE)[:28].strip("-")
    return f"{Path(pin.doc).stem}:{pin.schema}:{slug}"


@pytest.mark.parametrize("pin", PINS, ids=_pin_id)
def test_the_example_matches_the_schema(
    pin: Pin, examples: dict[str, list[Example]], schemas: dict[str, Any]
) -> None:
    """The example documents the schema, field for field, in the shape it declares."""
    start = _text(pin.doc).find(pin.marker)
    assert start != -1, f"{pin.doc} no longer contains {pin.marker!r}; re-pin the example"
    owner = next(
        (example for example in examples[pin.doc] if example.start <= start < example.end),
        None,
    )
    assert owner is not None, f"{pin.marker!r} is not inside any payload example"
    assert owner.value is not None, f"{pin.marker} could not be decoded as JSON"

    problems = _pinned_problems(pin, owner, schemas)

    assert not problems, "\n".join(problems)
