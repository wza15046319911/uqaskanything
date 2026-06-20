# UQ Course Assistant

A question-answering and degree-planning assistant for University of Queensland
students. It has two parts: a **RAG QA service** that answers questions about
courses, programs, fees, prerequisites and academic-calendar policy, and a
**deterministic degree-planning simulator** that lets a student build a valid
study plan semester by semester.

The audience is real students making real decisions, so the design follows one
strict rule: **anything where the answer is always the same — routing,
thresholds, prerequisites, fees, dates, enrolment facts — is plain code, not the
LLM.** The model only classifies the question, drafts wording, or resolves
ambiguity. It never decides a high-risk fact.

## Architecture

```
                         frontend (Vite + React 19 + TS)
                                     │  /api/*
                                     ▼
                          FastAPI (JSON only, Python 3.13)
                ┌────────────────────┴────────────────────┐
        /api/ask  (RAG QA)                        /api/sim/* (simulator)
                │                                          │
        planner.plan → mode                    fully deterministic, no LLM:
        ├─ filter / semantic / hybrid          state lives on the client and is
        │     → retrieval over `courses`        replayed statelessly each request
        │       (structured WHERE / pgvector    ├─ simulator.py  rule-tree engine
        │        semantic / RRF hybrid)         ├─ scheduler.py  auto-places courses
        ├─ program  → deterministic, in-code    │                into semester slots
        │     enrolment facts (no LLM)          └─ sim_advise.py the one LLM-assisted
        ├─ course_detail → single-course lookup                  path (ranks + explains
        ├─ kb      → vector search over the                      a deterministic pool)
        │     scraped knowledge base
        └─ empty   → graceful "too broad"

  Postgres + pgvector  ·  local Ollama (bge-m3 + qwen2.5) or DeepSeek / Bedrock
```

- **Two surfaces, two routers.** [backend/app/main.py](backend/app/main.py) only
  assembles routers. [api/ask.py](backend/app/api/ask.py) is QA;
  [api/sim.py](backend/app/api/sim.py) is the simulator. Layering is strict:
  `api → services → core`; scrapers and pipelines are offline CLIs.
- **Only `semantic` / `hybrid` / `course_detail` / `kb` answers reach the LLM**,
  and those are grounded in retrieved rows with a citation guard. `program`
  answers (course→programs, program→courses, permit checks) are built in code,
  so there is zero hallucination on enrolment facts.
- **The simulator never uses an LLM for state computation.** The server rebuilds
  the plan from `program_id` + selections on every call and validates timetables
  deterministically.

## Stack

- Backend: FastAPI, Python 3.13, Postgres + pgvector
- Frontend: Vite + React 19 + TypeScript (separate process; backend serves JSON only)
- Embeddings: bge-m3 (1024-d) — local Ollama, or DeepInfra / SiliconFlow over the
  OpenAI-compatible API
- Generation / planning: local Ollama `qwen2.5-coder:7b` by default; set
  `DEEPSEEK_API_KEY` to route through DeepSeek, or `LLM_BACKEND=bedrock` for
  Amazon Bedrock `gpt-oss`

## Cost & abuse protection

The three endpoints that call a paid LLM / embedding API — `/api/ask`,
`/api/ask/stream`, `/api/sim/advise` — sit behind one deterministic front gate
([backend/app/core/ratelimit.py](backend/app/core/ratelimit.py)). The LLM plays
no part in any allow/deny decision. Three layers, in order:

1. **Cloudflare Turnstile** human check — on only when `TURNSTILE_SECRET` is set.
   The frontend mints a fresh single-use token per request
   ([frontend/src/lib/turnstile.ts](frontend/src/lib/turnstile.ts), invisible
   widget) and sends it as `cf-turnstile-response`. Network errors fail open so a
   Cloudflare hiccup can't take the whole site down; the rate limit and daily cap
   still backstop the bill.
2. **Per-IP rate limit** — fixed 60s window, `RL_PER_MIN` requests per IP. Behind
   the Cloudflare proxy the real client IP is read from `CF-Connecting-IP`.
3. **Daily budget circuit breaker** — global `LLM_DAILY_CAP` paid requests per
   UTC day; once hit, the batch returns a "busy, try the official site" message.

Set each knob to `0` (or leave `TURNSTILE_SECRET` empty) to disable that layer —
that is the default for local dev and tests. See `backend/.env.example`.

## Quick start

Backend (from `backend/`):

```bash
python3 -m pip install -r requirements-dev.txt
uvicorn app.main:app --port 8077      # or: python -m app.main
pytest                                # run the tests
```

Frontend (from `frontend/`):

```bash
npm install
npm run dev      # Vite proxies /api → http://127.0.0.1:8077
npm run build    # tsc -b && vite build
```

The backend expects Postgres + pgvector on port **5433**, database `uq_courses`.

Or bring up the whole stack (pgvector db + backend + frontend) at once:

```bash
docker compose up        # db :5433, backend :8077, frontend :5173
```

## Data pipeline (offline, ordered)

Scrapers produce JSONL in `data/`, pipelines load it into Postgres:

1. `scrapers/collect_ids.py` → `scrapers/scraper.py` → course JSONL
2. `pipelines/build_db.py` — create tables, load `courses.jsonl`
3. `pipelines/embed.py` — fill embeddings, build the HNSW index
4. `scrapers/program_scraper.py` → `pipelines/build_programs.py` — programs + rules
5. `scrapers/scrape_aux_rules.py` → `pipelines/build_aux.py` — exclude lists / aux rules
6. Knowledge base: `kb_discover.py` → `kb_fetch*.py` → `pipelines/kb_parse.py`
   → `pipelines/kb_build.py` (chunk + embed FAQ / calendar / policy pages)

Before serving real students, run the evals (not just "it didn't crash"):
`pipelines/route_eval.py` (routing accuracy), `pipelines/answer_eval.py`
(end-to-end answer correctness), `pipelines/kb_eval.py` (KB recall).

## Deployment

- **Self-hosted (e.g. a Hong Kong VPS for users in China):** Postgres + pgvector
  in Docker, DeepSeek for generation, SiliconFlow bge-m3 for embeddings, nginx
  serving the built frontend and reverse-proxying `/api`. No blocked dependency.
  `docker compose up` brings up the full stack locally (see `docker-compose.yml`).
- **AWS (QA only):** the QA logic runs on Amazon Bedrock AgentCore Runtime
  (`app.agent` entrypoint, [backend/Dockerfile.agentcore](backend/Dockerfile.agentcore)),
  with Bedrock `gpt-oss` for generation, DeepInfra bge-m3 for embeddings, and RDS
  PostgreSQL + pgvector. CloudFront + a signing Lambda front the static frontend
  and the QA endpoint; WAF rate-limits and a budget kill-switch cap cost. The
  full Terraform stack and deploy order are in [infra/terraform/](infra/terraform/).

## Repository layout

```
backend/             FastAPI service: api / services / core, plus scrapers + pipelines (offline CLIs)
frontend/            Vite + React 19 + TS app
eval/                RAG eval harness (RAGAS + DeepEval LLM-as-judge over /api/ask)
infra/               Terraform for the AWS Bedrock AgentCore deployment
aidlc-docs/          AIDLC workflow state and audit docs
docker-compose.yml   local stack: pgvector db + backend + frontend
core-workflow.md     the AIDLC workflow definition (followed for development)
CLAUDE.md            architecture notes and the deterministic-vs-LLM design rule
```

The `eval/` harness runs LLM-as-judge metrics (RAGAS + DeepEval) over the live
`/api/ask` endpoint and complements the **deterministic** evals under
`backend/app/pipelines/`: the pipeline evals assert sources / course-code sets /
refusal, while `eval/` quantifies faithfulness, relevancy and context precision.
The two judge frameworks need conflicting dependencies, so each runs in its own
venv — see [eval/README.md](eval/README.md).
