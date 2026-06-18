# Requirements — Supabase DB Migration (minimal depth)

## Intent
Migrate the local dev Postgres database (`uq_courses` on :5433) into the newly
created Supabase project, so the AgentCore/Bedrock cloud deployment can reach a
managed Postgres + pgvector endpoint. Then store the connection string in
Terraform and confirm the migrated DB serves the existing eval harnesses.

## Functional Requirements
- FR1: Enable the `vector` extension on the Supabase target.
- FR2: Move the full schema + data (5 tables, embeddings, HNSW index defs) with
  no re-embedding, using `pg_dump -Fc` → `pg_restore` over the session pooler (:5432).
- FR3: Verify row counts on the target match the source exactly:
  courses 3050 total / 3050 embedded; kb_chunks 2521. (Red line: numbers must
  agree, not "did not error".)
- FR4: Put the session-pooler connection string into `terraform.tfvars` as
  `database_url` (gitignored).
- FR5: Run the eval harnesses (route_eval / answer_eval / kb_eval) against the
  migrated DB; per student-facing red line 6, these must pass before the DB is
  considered serving-ready.

## Non-Functional / Constraints
- NFR1 (security): The connection string contains a DB password. It must never be
  committed. It goes only into gitignored files (terraform.tfvars) or an
  environment variable. Do not write it into any tracked file, including aidlc-docs.
- NFR2: No destructive commands. The local source DB is read-only in this task;
  rds.tf removal (if chosen) is done by emptying content, not `rm` (per user's
  global rule 1).
- NFR3: Migration is one-off and idempotent enough to retry: a failed restore
  must surface all errors (never swallowed).

## Assumptions
- A1: The Supabase project is empty (fresh). If `courses`/`kb_chunks` already
  exist there, restore behaviour must be reconsidered before running.
- A2: Running the eval harnesses requires an embedding provider for query
  vectors (local Ollama bge-m3 or DeepInfra). This dependency must be available
  when FR5 runs; otherwise FR5 is blocked and reported, not silently skipped.

## Open Decisions
See `migration-questions.md`.
