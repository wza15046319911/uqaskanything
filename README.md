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
  See [DEPLOY.md](DEPLOY.md).
- **AWS (QA only):** the QA logic runs on Amazon Bedrock AgentCore Runtime
  (`app.agent` entrypoint, [backend/Dockerfile.agentcore](backend/Dockerfile.agentcore)),
  with Bedrock `gpt-oss` for generation, DeepInfra bge-m3 for embeddings, and RDS
  PostgreSQL + pgvector. CloudFront + a signing Lambda front the static frontend
  and the QA endpoint; WAF rate-limits and a budget kill-switch cap cost. The
  full Terraform stack and deploy order are in [infra/terraform/](infra/terraform/).

## Repository layout

```
backend/    FastAPI service: api / services / core, plus scrapers + pipelines (offline CLIs)
frontend/   Vite + React 19 + TS app
infra/      Terraform for the AWS Bedrock AgentCore deployment
DEPLOY.md   self-hosted (VPS) deployment guide
CLAUDE.md   architecture notes and the deterministic-vs-LLM design rule
```
