# Complete Construction Plan — Question Presentation & Type Diversification

**Objective:** add every planned question kind (`multi_select`, `word_bank`, `ordering`,
`matching`) plus a media-first presentation mode to the quiz engine, server-graded, with client
rendering guidance — delivered as a sequence of small, independently shippable PRs.

**Scope:** backend (grading, validation, contracts, seeding, tests) + client *guidance*
(documentation of rendering/answering rules). No Flutter code in this plan.
**Language/location:** English, `docs/` (decided).
**Execution mode:** branch + PR (git and `gh` available; repo has **no CI workflows** — every gate
runs locally before the PR is opened).

---

## 1. Repo pre-flight (verified facts)

| Fact | Value |
| --- | --- |
| Branch | `push-to-new-branch`, remote `origin git@github.com:qutaibatalal/Natiqo.git` |
| CI | none — verification is local commands only |
| Commit style | terse (`v5`, `v6`) — PR titles carry the meaning |
| alembic head | `b1f7c2e9a4d6` (add quiz_answer_attempt) |
| Local DB | `local.db` at `a710e92dc173` — **behind head** |
| Commands | `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, `uv run pyright` |

**Standard verification block** (repeated in every step; run from repo root):

```powershell
uv run alembic upgrade head
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run pyright
```

---

## 2. Invariants — verified after every step, no exceptions

1. **The answer key never leaves before the answer.** Pre-attempt payloads are assembled by
   omission; keys arrive only on `GradedAnswerResponse`. Any new key field is added to
   `ANSWER_KEY_FIELDS` (`tests/api/test_endpoints.py`) in the same PR — the pin test is the
   leak detector.
2. **No kind without a grading rule.** `QuestionKind` gains an entry only in the same change that
   adds its `grade_answer` branch and its `question_shape_problems` branch.
3. **Server grades, always.** The client posts a bare answer and never computes correctness.
4. **Deterministic presentation.** Anything shuffled derives from
   `f"{session_id}:{question_id}:{attempt}"` via `app/modules/quiz/domain/presentation.py`.
5. **Wire contract:** camelCase via `WireModel`, `extra="forbid"`; generated client models must be
   regenerated in the release that adds a field.
6. **Catalogue cacheable, sessions dynamic** — per-attempt data is never shared-cacheable.
7. **`explanation` mandatory** on authored questions, post-attempt only.
8. **Validation returns every problem at once**, in report order.
9. **Unreachable fall-through:** for every published value of `QuestionKind`, `grade_answer` must
   not raise `QUESTION_TYPE_UNSUPPORTED` — asserted by a test that enumerates the enum.

---

## 3. Current state (context for a cold-start agent)

Three kinds exist: `multiple_choice` (choices 2–6 + `correct_index`), `true_false` (exactly 2 +
`correct_index`), `fill_blank` (`accepted_answers`, folded by
`normalise_accepted_answers`). Machinery every step plugs into:

- `QuestionKind` — `app/core/types.py:152` (docstring states the "graded-before-listed" rule).
- `question_shape_problems` — `app/modules/content/domain/policy.py:87`
  (`MIN_CHOICES=2`, `MAX_CHOICES=6`, `TRUE_FALSE_CHOICES=2`).
- `grade_answer` — `app/modules/quiz/domain/policy.py:200`; dispatches to `_grade_choice` /
  `_grade_free_text`; fall-through raises `QUESTION_TYPE_UNSUPPORTED`.
- `GradeableQuestion` / `SubmittedAnswer` / `GradedAnswer` — framework-free grading inputs;
  `SubmittedAnswer` today has exactly `choice: str | None` and `text: str | None`.
- `QuestionView` (`quiz/application/dto.py:29`) → `SessionQuestionResponse`
  (`quiz/presentation/schemas.py:58`); `AnswerPayload` (`schemas.py:51`);
  `GradedAnswerResponse` (`schemas.py:92`) carries `correct_answer: dict[str, str]` +
  `explanation`.
- Reshuffle: `app/modules/quiz/domain/presentation.py` (`question_seed`, `choice_order`,
  `order_choices`, `order_correct_index`); applied in `quiz_service.py` after a wrong attempt.
- Leak pins: `PUBLIC_QUESTION_FIELDS` / `ANSWER_KEY_FIELDS` in
  `tests/api/test_endpoints.py`, mirrored in `tests/unit/test_content_domain.py`.
- Media aids `image_url`/`audio_url` already exist on aggregate, `QuestionView`, and wire schema.
- Authoring input: `SeedQuestion` in `scripts/seed_curriculum.py`; seeders
  `scripts/seed_curriculum.py`, `scripts/seed_content.py`.
- Client contract docs: `docs/client-contract.md`, `docs/mobile-integration-ar.md` (§ wire
  examples), `docs/mobile-app-architecture.md` §6.3 (wire shapes) + §8 (per-kind rendering),
  `README.md` (states three types).

---

## 4. Dependency graph & parallelism

```
Step 0 (baseline)
   └─▶ Step 1 (multi_select)
          └─▶ Step 2 (word_bank)
                 └─▶ Step 3 (ordering)
                        └─▶ Step 4 (matching)
                               └─▶ Step 5 (media-first, docs+policy)
                                      └─▶ Step 6 (docs consolidation & client handoff)
```

**Strictly serial: steps 1–4** — they all edit the same four files (`app/core/types.py`,
`content/domain/policy.py`, `quiz/domain/policy.py`, `quiz/presentation/schemas.py` +
`dto.py`), so parallel branches would conflict on both code and the leak pins.
**Step 5** depends on 1–4 only for doc-merge cleanliness; **Step 6** depends on everything.

**Model tier:** steps 1–4 = strongest model (grading correctness is the product); steps 0, 5, 6 =
default (mechanical/docs).

**Rollback:** every step is one revertible PR. **One migration proved necessary, and none of it is
a new column** — `choices` and `accepted_answers` (JSON columns) hold all four new kinds' data, but
the `CHECK` on `questions.kind` was fixed at the three original kinds when the catalogue table was
created, so it had to be widened for the new kinds to be storable at all: `d4c9a17b6e02`
(`down_revision = b1f7c2e9a4d6`). Its rollback is covered by `tests/integration/test_migrations.py`
(`upgrade head` → `alembic check` → `downgrade base`). If a step ever demands a new column, stop and
re-plan (see §7).

---

## 5. Steps

### Step 0 — Baseline green (no PR; direct commit if anything is stale)

- **Goal:** the standard verification block is green before any feature work.
- **Tasks:**
  1. `uv run alembic upgrade head` (brings `local.db` from `a710e92dc173` → `b1f7c2e9a4d6`).
  2. Run the full verification block.
  3. If anything fails, fix or stop and report — do not start Step 1 on a red suite.
- **Exit criteria:** all five commands succeed; `alembic current` shows `b1f7c2e9a4d6`.
- **Rollback:** n/a.

### Step 1 — `multi_select`

- **Goal:** questions with several correct options, graded by exact set equality.
- **Context brief:** reuse `choices` (2–6 authored options). Store the key as the *texts* of the
  correct options in `accepted_answers` (already normalised — no migration, single source of
  truth). `correct_index` must be unset. The pre-attempt shape is identical to
  `multiple_choice` except `kind`, so clients switch on `kind` only.
- **Changes:**
  - `app/core/types.py` — add `MULTI_SELECT = "multi_select"`; update docstring.
  - `content/domain/policy.py` — branch: 2–`MAX_CHOICES` choices; ≥2 accepted answers; every
    accepted answer ∈ `choices`; `correct_index is None`; `choices` non-empty.
  - `quiz/domain/policy.py` — `SubmittedAnswer` gains `choices: tuple[str, ...] = ()`;
    `_grade_multi_select`: fold submitted texts, compare as sets against `accepted_answers`;
    empty → 422 `VALIDATION_ERROR` on `answer.choices`; any unmatched entry → 422 (never a
    silent wrong); `correct_answer={"choices": ", ".join(sorted(key))}`.
  - `quiz/presentation/schemas.py` — `AnswerPayload` gains `choices: list[str] | None = None`.
  - `quiz_service.py` — build `GradeableQuestion` with `accepted_answers` from the aggregate;
    pre-attempt view unchanged apart from `kind`.
  - `scripts/seed_curriculum.py` — ≥1 `multi_select` seed example.
- **Wire:**
  ```jsonc
  // pre-attempt
  { "id": "…", "kind": "multi_select", "prompt": "…",
    "choices": ["Verb", "Noun", "Adjective", "Adverb"], "points": 1 }
  // answer
  { "op": "answer", "questionId": "…", "answer": { "choices": ["Verb", "Noun"] } }
  // verdict (after answer only)
  { "isCorrect": true, "correctAnswer": { "choices": "Adjective, Noun" }, "explanation": "…", … }
  ```
- **Tests:** unit — set-equality, order-insensitivity, fold, empty→422, unknown-entry→422,
  shape problems (1 accepted answer rejected, accepted ∉ choices rejected); API — pre-attempt
  payload contains no `ANSWER_KEY_FIELDS` member; `AnswerPayload.choices` round-trip; enum
  fall-through test still passes for the new value; leak-pin update for any new key field.
- **Verification:** standard block.
- **Exit criteria:** all gates in §2 green; seeded `multi_select` answerable end-to-end
  (start → answer → complete).
- **Rollback:** revert PR; no data migration, but seeded rows of kind `multi_select` would fail
  validation on re-seed — rerun seeders after revert.
- **Shipped.** `MULTI_SELECT` reachable end-to-end, graded by exact set equality over folded texts
  (`accepted_answers` holds the correct option *texts*; `correct_index` must be unset);
  `AnswerPayload` gained `choices`; the verdict is `correctAnswer.choices` (comma-joined, served
  order). No new key field, so the leak pin is unchanged. Ten seeded `multi_select` items.

### Step 2 — `word_bank`

- **Goal:** fill-in-the-blank where the learner assembles the answer from offered tiles.
- **Context brief:** same aggregate fields as `fill_blank` (`accepted_answers` = the key).
  Relax the current "`fill_blank` must not offer choices" rule: for `word_bank`, `choices` holds
  the *tiles* (bank), never the key. The bank is authored as accepted answers + distractors.
  Shuffled per attempt with the existing seeded reshuffle.
- **Changes:**
  - `QuestionKind` — add `WORD_BANK = "word_bank"`.
  - `content/domain/policy.py` — new branch: `accepted_answers` ≥1; `choices` 2–`MAX_CHOICES`
    tiles; every accepted answer present among `choices`; `correct_index is None`.
  - `quiz/domain/policy.py` — route `WORD_BANK` to `_grade_free_text` (same folded-membership
    rule; one grading rule, two presentation kinds).
  - `quiz_service.py` — shuffle `choices` for the pre-attempt view with the existing seed.
  - Seed: ≥1 `word_bank` example (sentence with a blank, 4–6 tiles).
- **Wire:** pre-attempt `{ "kind": "word_bank", "prompt": "She ____ to school.", "choices": ["go", "went", "goes", "going"] }`
  (shuffled); answer `{ "answer": { "text": "goes" } }` — **unchanged answer contract**, no model
  regeneration.
- **Tests:** unit — key-never-in-payload, tile-shuffle determinism (same seed ⇒ same order),
  accepted-answer-not-in-bank rejected at authoring; API — reshuffled choices differ across
  attempts but grade identically; enum fall-through.
- **Verification:** standard block. **Exit criteria:** as Step 1. **Rollback:** as Step 1.
- **Shipped.** `WORD_BANK` shares `fill_blank`'s folded free-text grading — one rule, two
  presentation kinds — with `choices` holding the tiles (key ⊆ tiles), shuffled per attempt by the
  existing seed. The answer contract is unchanged (`{"text": …}`), so no client model regeneration
  beyond the enum. Nine seeded `word_bank` items.

### Step 3 — `ordering`

- **Goal:** drag-to-sequence items, graded against the authored order.
- **Context brief:** `choices` stores items **in correct order** (index = position). The
  pre-attempt payload sends them reshuffled; the key (the correct permutation) is unrecoverable
  from a shuffle because the seed is never sent. The learner submits *texts*, not indices —
  indices refer to the shuffled array and would be meaningless after a retry reshuffle.
- **Changes:**
  - `QuestionKind` — add `ORDERING = "ordering"`.
  - `content/domain/policy.py` — new branch: 2–`MAX_CHOICES` items; `correct_index is None`;
    `accepted_answers` must be empty (the order *is* the key).
  - `quiz/domain/policy.py` — `SubmittedAnswer` gains `order: tuple[str, ...] = ()`;
    `_grade_ordering`: element-wise comparison after the fold; length mismatch or unknown item →
    422; **all-or-nothing** (partial credit explicitly out of scope);
    `correct_answer={"order": " → ".join(items)}`.
  - `AnswerPayload` — add `order: list[str] | None = None`.
  - `quiz_service.py` — reshuffle `choices` for pre-attempt via existing machinery.
  - Seed: ≥1 `ordering` example (e.g. sentence reordering, 4 items).
- **Wire:**
  ```jsonc
  // pre-attempt — shuffled, key absent
  { "kind": "ordering", "prompt": "Put the events in order.", "choices": ["Third", "First", "Fourth", "Second"] }
  // answer — texts in the learner's order
  { "answer": { "order": ["First", "Second", "Third", "Fourth"] } }
  // verdict
  { "correctAnswer": { "order": "First → Second → Third → Fourth" } }
  ```
- **Tests:** unit — exact order correct, one swap wrong, shuffled input grades correctly,
  unknown item → 422, length mismatch → 422; API — pre-attempt payload has no key; retry
  reshuffle still grades; enum fall-through.
- **Verification:** standard block. **Exit criteria:** as Step 1. **Rollback:** as Step 1.
- **Shipped.** `ORDERING` graded element-wise over folded texts against `choices` (the authored
  order is the key); all-or-nothing, with an unknown item or a length mismatch a `422`.
  `AnswerPayload` gained `order`; the verdict is `correctAnswer.order` (" → "-joined). The
  catalogue withholds the authored order — `pre_attempt_choices` returns `()` for ordering — so
  the key cannot be read pre-attempt; only a session or a review, which carry a seed, serve the
  shuffled items. Twelve seeded `ordering` items.

### Step 4 — `matching`

- **Goal:** pair left-hand prompts with right-hand options; order of pairs never matters.
- **Context brief:** pairs are the unit. One encoding everywhere: pair strings
  `"left => right"` stored in `accepted_answers` (the key), right-hand values duplicated into
  `choices` as the draggable bank (shuffled), left-hand values exposed pre-attempt as a new
  public field `prompts` (not secret — goes into `PUBLIC_QUESTION_FIELDS`, never
  `ANSWER_KEY_FIELDS`). Rejected alternative: a `pairs` column — needs a migration for the same
  information.
- **Changes:**
  - `QuestionKind` — add `MATCHING = "matching"`.
  - `content/domain/policy.py` — new branch: 2–`MAX_PAIRS=4` pairs (new constant); parse every
    `"left => right"` entry; no duplicate lefts; no duplicate rights (duplicates make grading
    ambiguous and the UI lie); `choices` = the distinct rights; `correct_index is None`.
  - `quiz/domain/policy.py` — `SubmittedAnswer` gains `matching: tuple[str, ...] = ()`;
    `_grade_matching`: set equality of pair strings vs `accepted_answers` (fold both sides);
    malformed/unknown pair → 422;
    `correct_answer={"pairs": "left1 ⇒ right1; left2 ⇒ right2"}`.
  - `AnswerPayload` — add `matching: list[str] | None = None`.
  - `QuestionView` + `SessionQuestionResponse` — add `prompts: list[str] = []`
    (empty for other kinds).
  - `quiz_service.py` — split stored pairs into `prompts` (authored order) + shuffled
    `choices`.
  - Leak pins: add `prompts` to `PUBLIC_QUESTION_FIELDS`; any new key material to
    `ANSWER_KEY_FIELDS`.
  - Seed: ≥1 `matching` example (3 pairs).
- **Wire:**
  ```jsonc
  // pre-attempt
  { "kind": "matching", "prompt": "Match the word to its meaning.",
    "prompts": ["Dog", "Cat"], "choices": ["kelb", "qitt", "far"] }   // rights shuffled
  // answer — same pair encoding the author used
  { "answer": { "matching": ["Dog => kelb", "Cat => qitt"] } }
  ```
- **Tests:** unit — set equality (pair order irrelevant), duplicate-right rejected at authoring,
  malformed pair → 422, fold applied; API — `prompts` present pre-attempt and contains no key,
  key absent from payload, enum fall-through.
- **Verification:** standard block. **Exit criteria:** as Step 1. **Rollback:** as Step 1.
- **Shipped**, with three details settled differently from the brief above — each because the
  brief's own wire example turned out to be unreachable as written:
  1. **The key is not folded at rest** (same field, same encoding). The brief stored the pairs in
     `accepted_answers`, which the domain folds — and the left-hand halves of those pairs *are*
     the `prompts`, so folding would serve `"Monday"` as `"monday"`. A matching key is compared
     half by half rather than looked up whole, so folding at rest bought grading nothing; it is
     deduplicated at rest and folded at comparison instead (`dedupe_strings`, `_split_pair`).
  2. **The bank is withheld from the catalogue** and served only by a session or a review, both
     of which have a seed to shuffle it out of the authored order. That is the existing
     `pre_attempt_choices` rule, extended by one kind: `prompts` is public (a prompt says what is
     asked, not what the answer is) while the bank can be *paired back* by position.
  3. **The shuffle is a derangement**, not a shuffle (`presentation.deranged_order`): the one
     arrangement to avoid is the alignment where each option faces its prompt, and a plain
     shuffle is allowed to reproduce exactly that. Rows above carry the general form: no prompt
     is served at a position whose bank entry answers it.
  Four seeds (A1 numbers and days, A2 money, B1 jobs) rather than one, so the kind is exercised
  against a bank with a distractor and against capitalised prompts.

### Step 5 — media-first presentation mode (docs + one policy call)

- **Goal:** define how image-first and audio-first questions are authored and rendered. **Not a
  new kind** — an existing kind with `image_url`/`audio_url`, which already flow through
  `QuestionView` and the wire.
- **Blocked-on:** content owners must answer §6 questions before any validation rule is enforced.
- **Changes (validation rule only after approval):**
  - `content/domain/policy.py` — if approved: `skill == listening ⇒ audio_url required`.
  - Docs regardless: rendering guidance.
- **Client guidance to document:**
  - Media renders **above** the prompt as the primary stimulus.
  - Audio: visible replay control, no autoplay after submission, never auto-advance.
  - Images: alt text derived from `prompt`; the payload must remain fully answerable without
    seeing the media (assistive-tech floor).
  - The media is a stimulus, never the answer key — no information exists *only* in media that
    the text payload lacks, except the intended comprehension target.
- **Tests (if rule approved):** shape problem when `listening` lacks `audio_url`; existing
  published fixtures updated.
- **Verification:** standard block. **Exit criteria:** guidance published; rule (if approved)
  enforced with a test. **Rollback:** revert the policy line; docs stay valid.
- **Shipped — guidance only; the optional rule was not adopted.** Media-first rendering guidance
  is published in `docs/mobile-app-architecture.md` §6.3/§8 and `docs/mobile-integration-ar.md`.
  `listening ⇒ audio_url required` was **not** made a hard publication rule, so no shape-problem
  was added — the policy keeps only the existing URL-format check (`_media_url_problems`). Every
  seeded question now ships an image and an audio aid, produced locally by
  `scripts/generate_media.py` (Commons-sourced images with a drawn fallback; MP3 audio via the
  machine synthesiser + `ffmpeg`), with provenance recorded in `media/sources.json`.

### Step 6 — documentation consolidation & client handoff

- **Goal:** one coherent client-facing contract after all kinds exist.
- **Tasks:**
  1. `README.md` — question-type list: five kinds (+ media-first note); drop "three types".
  2. `docs/client-contract.md` — `AnswerPayload` full shape (`choice`/`text`/`choices`/`order`/
      `matching`), which fields apply to which kind, regeneration note for generated clients.
  3. `docs/mobile-integration-ar.md` — per-kind request/response example (Arabic doc, existing
      section numbering).
  4. `docs/mobile-app-architecture.md` §6.3 (wire shapes table) + §8 (per-`kind` rendering rules
      for all five kinds: chip toggles, tile-fill, drag list, pair columns, media-first).
  5. `docs/backend-roadmap-ar.md` — mark type-diversification done / remove pending mentions.
  6. `app/core/types.py` docstring — final list of kinds.
  7. Remove this plan's §5 checkboxes → mark steps shipped (audit trail kept in PRs).
- **Verification:** standard block + read-through that every doc example matches its schema
  (paste from schema output, not from memory). That read-through is now mechanical:
  `tests/api/test_docs_contract.py` compares each payload example in the client guides
  against the component schema it documents, and fails on an example that is neither pinned
  to a schema nor exempted with a reason — which is how the `prompts` field, added to the
  question schemas in Step 4, was caught missing from the session example in
  `mobile-app-architecture.md`.
- **Exit criteria:** a client engineer can implement all five kinds from the docs alone, and no
  doc anywhere still says "three question types".
- **Rollback:** revert PR.
- **Shipped.** `README.md` lists all kinds plus the media pipeline; `docs/client-contract.md`
  carries the full `AnswerPayload` (`choice`/`text`/`choices`/`order`/`matching`);
  `docs/mobile-app-architecture.md` §6.3 (wire shapes) + §8 (per-kind rendering) and
  `docs/mobile-integration-ar.md` cover every kind; the only remaining "three types" wording lives
  in this historical plan. `tests/api/test_docs_contract.py` pins each payload example to its
  component schema — which is how `prompts` was caught missing from the session example after
  Step 4.

---

## 6. Open questions (block Step 5 only)

1. Is `listening ⇒ audio_url required` a hard publication rule or a soft editorial guideline?
2. Do image-first questions need an authored alt-text field? (None exists; `prompt` is the only
   free text today.)

Steps 1–4 are decided and need no further approval.

---

## 7. Plan mutation protocol

- **Split** a step only if a PR exceeds ~400 changed lines: split by (validation+grading) then
  (wire+tests), never by kind.
- **Insert** a migration step only if a phase proves impossible with `choices`/
  `accepted_answers`; a migration step must state its `down_revision` and a rollback test before
  it starts. **One was inserted, exactly here:** `d4c9a17b6e02` ("widen the question kind check"),
  `down_revision = b1f7c2e9a4d6`, rollback covered by `tests/integration/test_migrations.py`. It
  carries no column — it drops and recreates `ck_questions_valid_kind` to admit all seven kinds,
  because the constraint was fixed at the original three when the catalogue table was created.
- **Skip** a step only by removing it from the graph here and noting why (audit trail = this
  file's git history).
- **Reorder** is forbidden for steps 1–4 (shared-file conflicts, §4).

## 8. Definition of done (whole plan)

1. All five kinds (plus media-first guidance) live; enum fall-through test proves no kind is
   ungradable.
2. Standard verification block green; leak pins green; every new key field listed in
   `ANSWER_KEY_FIELDS`.
3. One seeded example per kind, answerable end-to-end.
4. §6 Step 6 documents reconciled — no stale "three types" anywhere.
5. Each step merged as its own PR in graph order; this file updated to record shipment.
