# Plan — Bilingual Answers (match question language)

**Goal:** answer in the same language as the question. A Chinese question gets a
Chinese answer; an English question gets an English answer. Scope chosen by the
user: **full i18n** — every answer, including the deterministic high-risk ones
(prerequisites, census, fees, program facts), must follow the question language.

**Detection source of truth:** the question text, NOT the frontend UI toggle.
("用中文问就中文答,用英文问就英文答" — the question language wins even if the UI
is set to the other language.)

---

## Current state (why this is large)

The whole answer path is hardcoded Chinese, and it splits into two kinds of
output with very different effort:

1. **LLM-generated answers** (`semantic` / `hybrid` / `kb` / `course_detail`
   intro). Easy: each prompt says "用简洁中文回答". Swap to "answer in the
   question's language" / select a per-language SYSTEM prompt.
   - Note: `KB_SYSTEM` in `answer.py:222` is written the *opposite* way — it
     explicitly forces Chinese even for English questions
     ("无论问题用中文还是英文提出,一律用简洁中文回答"). This reverse rule must be removed.

2. **Deterministic, code-built answers** (the bulk of the work). These are NOT
   single strings — they are sentence-building logic: conditional clauses,
   `、` list joins, Chinese numerals (`二选一`), counters (`等 N 门`), and the
   `必修/选修` labels. Full i18n means re-expressing this *logic* per language,
   not swapping strings. And these are exactly the student-facing red-line
   answers (red line 1: high-cost facts must be deterministic, not free LLM
   generation), so the English wording needs human review (red line 6).

### Detection placement (deterministic — global rule 12)

Language detection is a deterministic classification, so it is code, not an LLM
call. A pure function: contains a CJK character → `zh`, else → `en`. Detected
once at the `qa.run` / `run_stream` entry, then `lang` is threaded down. It must
reach `_retrieve`, because the deterministic answers (`prog_answer`,
`det_answer`, `status_note`, `_empty_note`) are already built there.

---

## Architecture decision

### New module: `app/core/i18n.py`

- `detect_lang(text: str) -> Literal["zh", "en"]` — any CJK char → `zh`, else
  `en`. (Edge case: an otherwise-English question containing one Chinese course
  name resolves to `zh`. Acceptable; documented. Revisit with a ratio threshold
  only if it bites.)
- Small per-language helpers for the logic-heavy builders:
  - `label_req(req, lang)` — 必修/选修 ↔ compulsory/elective
  - `join_list(items, lang)` — `、` vs `, `
  - `choice_word(k, lang)` — 二选一 vs "choose 1 of N"
  - counters / "等 N 门" equivalents as needed
- `MESSAGES = {"zh": {...}, "en": {...}}` — registry for the **fixed** sentences
  (the ones with no branching logic): `EMPTY_MSG`, `KB_REFUSE`, `EMPTY_ANSWER`,
  the census template, KB fallbacks, the "以官方课程页为准 / per the official
  course page" disclaimers, the sources block label, etc. Access via a `t(key,
  lang, **fmt)` helper.

Rationale: fixed sentences belong in a flat registry (clean, reviewable in one
place). Logic-heavy builders cannot be a registry — they keep their code but
branch on `lang` via the helpers. This keeps the deterministic single-source-of-
truth property (red line 1): the English answer is built from the same
structured facts by code, never by translating the Chinese answer with an LLM
(which would reintroduce mistranslation risk on numbers/dates/prereq logic).

### Threading `lang`

- `qa.run(conn, question, generate=True, lang=None)` → `lang = lang or
  detect_lang(question)`.
- `qa.run_stream(conn, question, lang=None)` → same.
- `_retrieve(conn, question, lang)` → pass `lang` into every `_ans_*`,
  `_empty_note`, `_status_note`, and into the `answer.*` calls.
- `answer.*` functions (`answer`, `answer_stream`, `answer_kb`,
  `answer_kb_stream`, `answer_course_detail`, `answer_course_detail_stream`,
  `detail_structured_answer` and its sub-builders, `kb_sources_block`,
  `fixed_kb_body`, `EMPTY_ANSWER`/`KB_REFUSE` usage) take `lang`.
- API layer (`app/api/ask.py`): no new request field needed — detect
  server-side from the question. (Confirm ask.py just forwards `question`.)

---

## Phases (checkpoint after each — global rule 17)

Each checkpoint runs `pytest` from `backend/` and the relevant eval, then stops
for review before continuing.

### Phase 1 — Foundation
- Create `app/core/i18n.py` (`detect_lang` + helpers + `MESSAGES` skeleton).
- Thread `lang` through `qa.run` / `run_stream` / `_retrieve` and the `answer.*`
  signatures (default `lang="zh"` so behavior is unchanged until callers pass
  it).
- Checkpoint: `pytest` green, `from app.main import app` imports, existing
  Chinese answers byte-identical (no English wired in yet).

### Phase 2 — LLM soft answers bilingual
- `answer.py`: per-language SYSTEM prompts for the 5 generators; remove the
  reverse "always Chinese" rule in `KB_SYSTEM`.
- Bilingual `KB_REFUSE`, `EMPTY_ANSWER`, the census `_CENSUS_ANSWER` template,
  the sources block label ("来源(UQ 官方页面...)" → "Sources (official UQ
  pages...)"), the KB empty-answer fallbacks.
- `_KB_EMPTY_MARKERS` / `is_empty_kb_answer`: add the English empty-answer
  markers so an English "no info" answer is also caught and retried.
- Checkpoint: ask one EN + one ZH question per mode manually; verify language
  matches and citations/guardrails still hold.

### Phase 3 — Deterministic answers bilingual
- `qa.py`: `_ans_c2p`, `_ans_p2c`, `_ans_p2c_structured`, `_fmt_group`,
  `_ans_program_filter`, `_ans_low_burden`, `_ans_permit`, `_empty_note`,
  `_status_note` / `_status_unknown_note`, `EMPTY_MSG` — all via `lang` +
  helpers.
- `answer.py`: `detail_structured_answer` family (`_detail_prereq`,
  `_detail_assessment`, `_detail_units`, `_detail_semester`,
  `_assessment_type_answer`, `_fmt_course`, the `_REQ_TYPE_LABEL` /
  `_ASSESSMENT_TYPES` label tables) bilingual.
- Checkpoint: `pytest`; spot-check each deterministic path in both languages.

### Phase 4 — Eval + human-review gate (red line 6)
- Run `route_eval` (routing unaffected, regression guard), `answer_eval`, and
  `kb_eval` — once with Chinese fixtures, once with English variants of the
  high-risk questions.
- Collect every high-risk English string (census, prerequisite disclaimers,
  fee/withdrawal notes, program permit answers) into one section of the i18n
  module, clearly marked `# REVIEW: human-verify EN wording before serving`,
  and list them in this plan for the user to check line by line.
- Do NOT consider the feature shippable until the human review passes — weak
  "didn't crash" checks do not count (red line 6, global rule 16).

---

## Success criteria (global rule 11 — loop until verified)

1. For every mode, a ZH question yields a fully-ZH answer and an EN question
   yields a fully-EN answer (no mixed-language leakage, including the disclaimers
   and source labels).
2. Deterministic high-risk answers stay deterministic and fact-identical across
   languages (same prereq logic, same numbers, same dates) — verified, not
   assumed.
3. `pytest` stays green; routing eval shows no regression.
4. Every answer still carries its official source link (red line 2) in both
   languages.
5. High-risk EN wording reviewed by a human before serving (red line 6).

---

## Phase 4 — EN wording human-review checklist (red line 6 — HARD GATE)

Status: Phases 1–4 implemented and verified — pytest 165 pass (3 pre-existing env
failures only), zh answers byte-identical, all regression + EN-variant evals green
(see Eval section below). **One gate remains: human sign-off on the EN wording below
(red line 6). Not shippable until that passes.**

Review the EN strings against the zh source for: same facts/numbers/dates, correct
register for students, every high-risk answer keeps its official-source pointer.

### A. Fixed sentences — `app/core/i18n.py` MESSAGES (one place, easy to edit)
- `census` (EN) — HIGH RISK (fees + deadlines). Verify it matches the zh meaning
  exactly and tells the student to confirm on mySI-net / important dates.
- `kb_refuse`, `kb_fallback_body` — refusal + official pointer wording.
- `program_not_found`, `course_not_found`, `detail_see_card`, `empty_answer`,
  `empty_msg`, `kb_sources_header`.

### B. Deterministic builders — `app/services/qa.py` (logic-built, EN branch inline)
- `_ans_permit` — "No. … gives no credit …" / "Yes. … (as a/an … course)" / the
  general-elective uncertainty sentence. HIGH RISK (enrolment/credit).
- banned-course notes — "This program excludes (no credit): …" and
  "Another N programs explicitly exclude this course (no credit), e.g. …". HIGH RISK.
- `_ans_c2p` / `_ans_p2c` / `_ans_p2c_structured` / `_fmt_group` /
  `_ans_program_filter` — counts, "compulsory/elective", "choose 1 of N",
  "and N more", "pick a direction then use the course planner …".
- `_ans_low_burden` — the "system does not judge difficulty/pass rate …" disclaimer.
- `_empty_note` — "All courses indexed here are taught In Person …" / campus-absent.
- `_status_unknown_note` — "another N courses … not counted … check each against
  its course profile (ECP)" + the midterm/group phrases.
- major note — "Note: this program has majors/directions; its core courses are
  determined after choosing a direction — use the course planner …".

### C. Single-course detail — `app/services/answer.py` (EN branch inline)
- `_detail_prereq` — "… prerequisites: {raw}. Refer to the official course profile
  (ECP)." HIGH RISK (prereq logic; raw is quoted verbatim, not translated).
- `_detail_assessment`, `_detail_units`, `_detail_semester`,
  `_assessment_type_answer`, `_ASSESSMENT_TYPES` EN labels.

### D. LLM system prompts — `app/services/answer.py` (drive answer language)
- `SYSTEM_EN`, `KB_SYSTEM_EN`, `COURSE_DETAIL_SYSTEM_EN`. The reverse "always
  Chinese" rule was removed from `KB_SYSTEM`. KB_SYSTEM_EN governs high-risk KB
  answers (fees/dates) — verify it still says "rely on the official page, mind timeliness".

### Eval (regression + EN variants) — DONE
All run from `backend/` with local embedding override
(`EMBED_BASE=http://localhost:11434/v1 EMBED_MODEL=bge-m3`, generation via DeepSeek):
- route_eval (zh) — **110/110 (100%)**. Routing unaffected (wording-only change).
- answer_eval `answers.jsonl` (40, zh + 4 existing EN) — **40/40 (100%)**. The EN
  refuse fixture ("dragon taming") now answers an EN refusal and is correctly
  recognized — see the eval-script fix below.
- answer_eval `answers_en.jsonl` (14 new EN high-risk variants — census, prereq,
  permit, c2p, p2c core + direction structure, program-filter, filter row-level,
  semantic, hybrid, 2 EN refuse) — **14/14 (100%)**. Real correctness, not "didn't crash".
- answerability_eval (`kb_refuse.jsonl`) — wrong-refuse = 0 (red line) ✓. "leaked 14"
  fictional entities is the pre-existing reranker-absent baseline (not from this change).
- relevance_scan (`course_relevance.jsonl`, zh) — all pass.
- pytest — 165 passed / 3 pre-existing env failures (DeepInfra 403 ×2, one KeyError).

**Eval-script fix (regression the feature introduced):** bilingual refuse/empty broke
the string-equality checks `ans == answer.KB_REFUSE` / `== answer.EMPTY_ANSWER` for EN
answers. Added lang-agnostic `answer.is_kb_refuse` / `answer.is_empty_answer` (match any
language via `i18n.MESSAGES[...].values()`); updated `answer_eval.py`, `llm_judge_eval.py`,
`relevance_scan.py`, and `relevance_scan._has_no_match_caveat` (now also detects the EN
caveat). `answerability_eval.py` needed no change (decides at the retrieval/gate level).

## Phase 5 — Adversarial review (multi-agent) + fixes

Ran a 7-lens adversarial review (zh-regression, fact-divergence, language-leakage,
untranslated-leak, en-grammar/fidelity, threading/edge-cases, eval-integrity) plus an
independent dynamic CJK-leak scan over every en builder branch. Verdict: no fact
divergence, no zh regression, no dropped source pointers, all high-risk facts stay
code-built. Four real defects found and FIXED:

1. [MED] `answer._course_detail_facts` / `_assessments_for_llm` fed hardcoded Chinese
   labels (课程简介/学分/先修要求…) into the EN course-detail LLM prompt — the EN prompt
   says "copy verbatim", so a weak model could echo Chinese. FIX: threaded `lang` +
   `_CDF_LABELS` per-language dict (Description/Units/Prerequisites (verbatim)/…).
2. [MED] `answer.guard_citations` appended a hardcoded Chinese warning line
   ("[警告] 已剔除越界…") to EN answers on the hallucination/safety path. FIX: bilingual
   warning ("[Warning] Removed out-of-bound…"); threaded `lang` from both call sites;
   updated `answer_eval` to split on `[警告]|[Warning]` (regex `_WARN_SPLIT`).
3. [LOW] `qa._gen_facts` used Chinese dict key `命中总数` dumped into the EN answer-LLM
   facts JSON. FIX: neutral key `total` (already in `_TOTAL_KEYS`); `所属program总数` → `total_programs`.
4. [LOW] `qa._ans_p2c` EN read "(incl. 1 choose 1 of 2 groups)". FIX: "pick one from each
   group: …" + "(incl. N pick-one group(s))".

Closing proof (completeness critic's suggested check): ran every EN path end-to-end
(course-detail intro LLM, semantic/filter large result sets, all 14 EN fixtures) and
grepped each real answer for CJK — **0 Chinese characters in any EN answer**. zh paths
byte-identical (self-test + pytest 165 pass). Note: the multi-agent verify sub-stage hit
a workflow-script bug (passed promises to parallel()); findings were instead re-verified
by me by reading the cited code directly before fixing.

## Open / flagged

- **EN wording review** is a hard gate, not optional (red line 6). The plan
  produces the strings; a human signs off before they reach students.
- **Detection edge case**: any CJK char → `zh`. Documented; ratio threshold
  deferred unless it misfires in eval.
- **Frontend**: UI localization (`frontend/src/i18n.ts`, `locales/`) is separate
  and already in progress; this plan covers the *answer content* only. No new
  API request field — the server detects from the question.
