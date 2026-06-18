# AI-DLC State Tracking

## Project Information
- **Project Type**: Brownfield
- **Start Date**: 2026-06-17T10:56:38Z
- **Current Stage**: INCEPTION - Workflow Planning COMPLETE (awaiting plan approval + connection string)
- **Task**: One-off migration of the local dev Postgres DB to the newly created
  Supabase project, then wire the connection string into Terraform and run the
  eval harnesses against the migrated DB.

## Workspace State
- **Existing Code**: Yes (Python 3.13 FastAPI backend, Vite + React 19 + TS frontend, Terraform infra)
- **Build System**: pip (backend), npm (frontend), Terraform (infra)
- **Project Structure**: Monorepo (backend / frontend / infra)
- **Reverse Engineering Needed**: No (see rationale below)
- **Workspace Root**: /Users/lewisan/Desktop/uq_course_rag

## Adaptive Stage Decisions (this task)
This is an operational data-migration task, not new feature development. Depth is
MINIMAL. Stage applicability:

- Workspace Detection — DONE
- Reverse Engineering — SKIPPED. Rationale: no application logic changes; the
  system is already documented in CLAUDE.md and `infra/terraform/README.md`.
  Full architecture/API reverse engineering adds no value to a DB migration.
  (User may override and request it.)
- Requirements Analysis — IN PROGRESS (minimal depth; questions raised)
- User Stories — N/A (no user-facing feature change)
- Workflow Planning — PENDING (after answers)
- Application Design / Units Generation — N/A (no new components)
- Construction / Code Generation — N/A (only a Terraform variable value + run
  documented migration commands; no source code is written)
- Build and Test — Mapped to running the existing eval harnesses against the
  migrated DB (route_eval / answer_eval / kb_eval)

## Verified Facts (read-only, gathered 2026-06-17)
- Local DB (postgresql://localhost:5433/uq_courses): PostgreSQL 17.10, reachable
- Local pg client tools: 17.4 (pg_dump / pg_restore / psql, Homebrew)
- Supabase target: PostgreSQL 17.6; `vector` 0.8.0 available (HNSW supported)
- All PG17 → pg_dump/pg_restore cross-minor-version compatible
- Local schema: 5 tables (courses, kb_chunks, programs, program_course, program_exclude)
- Local extensions: plpgsql, vector 0.8.2
- Counts: courses 3050 total / 3050 embedded; kb_chunks 2521 (matches README red line)
- Vector indexes: idx_courses_embedding, idx_kb_chunks_embedding (both HNSW, cosine)
- Local DB size: 180 MB (under Supabase free-tier 500 MB)
- terraform.tfvars: present, gitignored; `database_url` NOT yet set
- `database_url` variable declared in variables.tf:64

## Extension Configuration
- Security Baseline — Enabled: YES (Q1=A). Full rule file loaded; enforced as blocking constraints.
- Property-Based Testing — Enabled: NO (Q2=A). Skipped — no business-logic code written this task.

## Answers (migration-questions.md)
- Q1=A Security Baseline enforced
- Q2=A skip PBT
- Q3=A user pastes session-pooler string in chat → AI writes to gitignored terraform.tfvars
- Q4=B empty rds.tf (already a no-resource comment stub; user deletes file in IDE)
- Q5=A run all three evals against migrated DB (providers confirmed available)

## Pre-flight (verified 2026-06-17T11:02:43Z)
- Supabase public schema: EMPTY (0 tables) → assumption A1 holds, restore is safe
- rds.tf: already a 4-line comment stub, no resources, no external refs

## Code Location Rules
- Application Code: Workspace root (NEVER in aidlc-docs/)
- Documentation: aidlc-docs/ only

## Stage Progress
- [x] Workspace Detection
- [x] Requirements Analysis (answers received)
- [x] Workflow Planning (execution-plan.md created)
- [x] Execute migration (vector enabled → dump → restore exit=0)
- [x] Verify counts (all 5 tables match exactly; both HNSW indexes rebuilt)
- [x] Wire connection string into Terraform (database_url set by user in tfvars)
- [x] Run eval harnesses — route_eval 110/110, answer_eval 40/40 (over Supabase); kb_eval 18%@3 (file-based, known no-rerank baseline, not a migration signal)
- [x] SECURITY cleanup — user confirmed keys reset (2026-06-17); tfvars updated with new credentials on their side
- [x] README annotated as migrated (infra/terraform/README.md migration section)
- [ ] Optional: delete infra/terraform/rds.tf in IDE (already an empty stub) — user's call

## Deployment (OPERATIONS) — 2026-06-17
- [x] Preflight: AWS creds (acct 239359658565), terraform 1.14.8, docker+buildx, host arm64, Bedrock gpt-oss-20b present in us-east-1, sensitive vars confirmed
- [x] Stage 1: terraform apply -target ECR (1 added)
- [x] Stage 2: docker build linux/arm64 + push to ECR (uq-course-qa:latest, digest sha256:10be32ed…). Hit a zsh `$ECR:latest` `:l`-modifier bug (tag mangled to uq-course-qaatest) — fixed with `${ECR}:latest`.
- [x] Stage 3: full apply. First attempt failed — proxy Lambda reserved_concurrent_executions=3 vs account concurrency limit 10 (unreserved floor 10). Fix (user chose B): set proxy_max_concurrency=-1 in tfvars (no standing reservation; cost still bounded by WAF rate-limit + $30 budget + account cap 10 + kill-switch). Re-apply: 9 added, 1 destroyed (tainted proxy recreated). `terraform plan` => No changes (converged, 27 resources).
- Outputs: agent_runtime_arn=arn:aws:bedrock-agentcore:us-east-1:239359658565:runtime/uq_course_qa-nGurf67nLy; site_url=https://d181g2evun1j6h.cloudfront.net; lambda_function_url=https://5d554tc4hsu7wgh4fg4mlxsi2y0lppwo.lambda-url.us-east-1.on.aws/; cloudfront_distribution_id=E3BSUU5ZE0BTHI; site_bucket=uq-course-qa-site-239359658565
- [x] Frontend: npm run build (had to `unset -f node npm` — zsh nvm lazy-load shadows PATH and recurses; absolute /Users/lewisan/.nvm/versions/node/v24.15.0/bin) + s3 sync dist/ to uq-course-qa-site-239359658565 (4 files) + CloudFront invalidation. Verified: site root HTTP 200 text/html (<title>UQ 课程问答</title>), JS asset HTTP 200. LIVE at https://d181g2evun1j6h.cloudfront.net (frontend uses relative /api/* → CloudFront forwards to Lambda proxy; no build-time API URL needed)
- [ ] Functional verify: API chain through CloudFront /api/* → Lambda proxy → AgentCore runtime → Bedrock/DeepInfra/Supabase — NOT yet tested (static site loads, but answering not confirmed). Note: AgentCore exposes /invocations+/ping; need to confirm the proxy maps /api/ask and /api/sim/* correctly.
- [ ] Confirm SNS email subscription (AWS sent a confirm link to zianwang9911@gmail.com) — pending user click

## Outcome
Migration COMPLETE and verified. Supabase serves identical data to local dev DB
(all 5 tables, embeddings, HNSW indexes). Deployed Runtime can reach it via the
session-pooler database_url. Eval gates: routing + end-to-end answer correctness
pass at 100% over Supabase. Only remaining items are user-side security rotation
and the optional rds.tf file deletion.
