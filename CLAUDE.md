# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.
## ALWAYS USE AIDLC.
see ./core-workflow.md

## Repo-specific rule

In this particular repository, you can directly push code to the `main` branch.

## What this is

UQ course assistant: a RAG question-answering service plus a deterministic
degree-planning simulator. Backend is FastAPI (Python 3.13) serving JSON only;
frontend is a separate Vite + React 19 + TypeScript app. The backend does NOT
serve HTML — the two run as separate processes.

Audience is real UQ students making real decisions (prerequisites, fees, census
dates, deadlines). Read `backend/.claude/rules/student-facing.md` before touching
any answer path — a wrong answer can cost a student money or a deadline.

## Commands

Backend (run all from the `backend/` directory):
- Install: `python3 -m pip install -r requirements-dev.txt`
- API: `uvicorn app.main:app --port 8077` (or `python -m app.main`)
- Tests: `pytest` — single file `pytest tests/test_simulator.py`, single test
  `pytest tests/test_simulator.py::test_name -q`
- Run a service CLI directly: `python -m app.services.qa "CS有哪些课没有考试"`,
  `python -m app.pipelines.build_db`, `python -m app.scrapers.scraper` etc.

Frontend (run all from the `frontend/` directory; use absolute node/npm paths —
nvm lazy-load breaks in non-interactive shells):
- Dev: `npm run dev` (Vite proxies `/api` → `http://127.0.0.1:8077`, so the
  backend must be running on 8077)
- Build: `npm run build` (`tsc -b && vite build`)
- Lint / format: `npm run lint`, `npm run format` (ESLint + Prettier are the
  source of truth — change the config, don't fight the output)

## Architecture

### Two product surfaces, two routers
`app/main.py` only assembles routers and starts the app. `app/api/ask.py` is the
RAG QA endpoint; `app/api/sim.py` is the simulator. Routers stay thin and delegate
to `app/services/`. Layer direction is strict: `api → services → core`; CLIs live
in `scrapers/` and `pipelines/`. Always import through the package
(`from app.services import qa`) — never relative imports.

### QA flow (`/api/ask`, `/api/ask/stream`)
`qa.run` → `planner.plan` classifies the question into a `mode`, then routes:
- `filter` / `semantic` / `hybrid` → `retrieval.*` over the `courses` table
  (structured WHERE, pgvector semantic, or RRF hybrid)
- `program` → `program_lookup` (course→programs, program→courses, permit checks).
  These answers are **deterministic and built in code** (`_ans_c2p` / `_ans_p2c`
  / `_ans_permit` in `qa.py`), NOT LLM-generated — zero hallucination on
  enrolment facts.
- `course_detail` → single-course structured lookup
- `kb` → `retrieval.kb_search` over the scraped knowledge base (FAQ / academic
  calendar / policy). Reached either by the planner up front, or as a fallback
  when course retrieval is weak — see the `KB_PREFER_SIM` / `KB_STRONG_SIM`
  similarity thresholds and `_DATE_INTENT` guard in `qa.py`.
- `empty` → graceful "too broad" message

Only `semantic` / `hybrid` / `course_detail` / `kb` answers go through the LLM
(`app/services/answer.py`), and those are grounded in retrieved rows with a
citation guard (`guard_citations`). The LLM never decides a high-risk fact.

### Simulator flow (`/api/sim/*`)
Fully deterministic, no LLM in state computation. The client holds the full state
and the server replays it statelessly each request — `sim_state` rebuilds a
`simulator.PlanSimulator` from `program_id` + selections every call. Core pieces:
- `services/simulator.py` — the rule-tree engine (parses a program's degree rules,
  tracks selections, OR-branches, level caps, equivalence groups)
- `services/scheduler.py` — auto-places selected courses into semester slots
  (prerequisites earlier, offering semester, units cap, incompatibility,
  year-long courses spanning two slots)
- `api/sim.py::_validate` — deterministic timetable validation for manual placement
- `services/sim_advise.py` — the ONE LLM-assisted simulator path: deterministic
  candidate pool, LLM only ranks + explains, wrapped in dual guardrails

Offering data: S1 comes from `courses.semester`; S2 membership comes from
`S2_CODES` (loaded in `config.py` from `data/s2_course_codes.txt`).

### Deterministic vs LLM boundary (project-wide invariant)
Routing, thresholds, guards, prerequisites, fees, dates — anything where the
answer is the same every time — must be explicit code. The LLM only classifies,
drafts wording, or resolves ambiguity. This is the central design rule; see
`backend/.claude/rules/lessons-learned.md` and `student-facing.md`.

### Config
`app/core/config.py` is the single source for `DSN`, `DATA_DIR`, and `S2_CODES`.
Never re-declare `DSN = os.environ.get(...)` in a module — import it. Data files
live in `backend/data/`; CLI default paths are relative to `backend/`.

## Data pipeline (offline, ordered)
Scrapers produce JSONL in `data/`, pipelines load it into Postgres:
1. `scrapers/collect_ids.py` → `scrapers/scraper.py` → course JSONL
2. `pipelines/build_db.py` — create tables + load `courses.jsonl` (embedding left null)
3. `pipelines/embed.py` — fill `embedding` via local Ollama bge-m3 (1024-d), build HNSW index
4. `scrapers/program_scraper.py` → `pipelines/build_programs.py` — programs + rules JSONB
5. `scrapers/scrape_aux_rules.py` → `pipelines/build_aux.py` — exclude lists / aux rules
6. Knowledge base: `scrapers/kb_discover.py` → `kb_fetch*.py` → `pipelines/kb_parse.py`
   → `pipelines/kb_build.py` (chunk + embed FAQ/calendar/policy pages)

Eval (run before serving real students — not just "didn't crash"):
`pipelines/route_eval.py` (planner routing accuracy), `pipelines/answer_eval.py`
(end-to-end answer correctness), `pipelines/kb_eval.py` (KB recall). Fixtures in
`data/eval/`.

## Stack & environment
- Postgres + pgvector on port **5433**, database `uq_courses`
- LLM: local Ollama (bge-m3 embeddings + qwen2.5-coder:7b generation) by default;
  set `DEEPSEEK_API_KEY` in `backend/.env` to route both planner + answer through
  DeepSeek instead. `LLM_ENABLED=false` forces local even with a key. See
  `backend/.env.example`.
- Deployment notes: `DEPLOY.md`

## Conventions
- Backend keeps Chinese module-level docstrings (purpose + 用法). Keep that style;
  do not add per-line comments. Code identifiers stay English.
- Never swallow errors: routers return
  `JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=...)` with the
  real message. Surface skip counts and reasons in pipeline output, never silent
  defaults.
- Frontend: function declarations for exported functions, arrow functions only for
  private/internal use; `fetch` stays in `src/api/`, out of components; components
  own their props types. Details in `frontend/.claude/rules/code-style.md`.
- Per-package rules and lessons live under `backend/.claude/rules/` and
  `frontend/.claude/rules/` — read them before non-trivial work in either side.
