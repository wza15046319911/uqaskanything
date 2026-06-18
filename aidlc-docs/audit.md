# AI-DLC Audit Log

## Initial User Request
**Timestamp**: 2026-06-17T10:56:38Z
**User Input**: "I have created the project. do the rest"
**AI Response**: Began AI-DLC workflow. Loaded core-workflow.md and mandatory common rules after user reminder. Performed Workspace Detection (brownfield). Gathered read-only facts on local DB and Supabase target.
**Context**: INCEPTION - Workspace Detection / Requirements Analysis. Task is the Supabase DB migration documented in infra/terraform/README.md.

---

## Workflow Reminder
**Timestamp**: 2026-06-17T10:56:38Z
**User Input**: "Did you load the AIDLC"
**AI Response**: Acknowledged the workflow had not yet been loaded. Read core-workflow.md and the mandatory common rule files (process-overview, session-continuity, content-validation, question-format-guide, welcome-message), workspace-detection, and the two extension opt-in files.
**Context**: Course-correction; AI-DLC workflow now driving the task.

---

## Requirements Analysis — Questions Raised
**Timestamp**: 2026-06-17T10:56:38Z
**User Input**: (pending)
**AI Response**: Created aidlc-docs/inception/requirements/requirements.md (minimal depth) and migration-questions.md with 5 questions (2 extension opt-ins + 3 task decisions). Awaiting answers.
**Context**: INCEPTION - Requirements Analysis. No code or data changes made yet.

---

## Requirements Analysis — Answers Received
**Timestamp**: 2026-06-17T11:02:43Z
**User Input**: "continue" + migration-questions.md answers — Q1: A (Security Baseline enforced); Q2: A (skip PBT); Q3: A (user pastes connection string in chat, AI writes it into gitignored terraform.tfvars); Q4: B (empty rds.tf contents); Q5: A (run route_eval + answer_eval + kb_eval against migrated DB; user confirms embedding + LLM providers available)
**AI Response**: Loaded full security-baseline.md (extension now Enabled). Read-only pre-flight: Supabase public schema is empty (assumption A1 confirmed — restore is safe). Found rds.tf is already a no-resource comment stub with no external references. Proceeding to Workflow Planning.
**Context**: INCEPTION - Requirements Analysis complete. No data/code changed yet.

---

## Execution — Migration started
**Timestamp**: 2026-06-17T11:09:46Z
**User Input**: "我把真实的串写入了tfvars，你继续工作" (user wrote the real connection string into terraform.tfvars themselves — secret never entered chat)
**AI Response**: Verified tfvars database_url (no password echoed): session pooler :5432, project ref matches, non-placeholder. Enabled `vector` 0.8.0 in Supabase `public`. Dumped local DB to /tmp/uq_courses.dump (31MB, 5 tables + HNSW indexes). Ran pg_restore.
**Context**: CONSTRUCTION - Migration steps 1-2 done.

---

## BLOCKER — pg_restore password authentication failed (SECURITY-15: surfaced, not swallowed)
**Timestamp**: 2026-06-17T11:09:46Z
**Finding**: pg_restore exit=1 — "FATAL: password authentication failed for user postgres". Diagnosis (no secret revealed): username `postgres.gdjzwqjzxwxgfqcsjzjo` is correct; password is 10 chars, fully alphanumeric (no URL-encoding issue) → the password value in tfvars does not match the Supabase DB password.
**AI Response**: Halted migration (fail-closed). Asked user to correct the password in terraform.tfvars or reset it in the Supabase dashboard (recommend alphanumeric). Local dump and vector extension are already in place — only a re-run of restore is needed once the password is fixed.
**Context**: CONSTRUCTION - Migration paused at step 3, awaiting correct password.

---

## Execution — Restore succeeded + counts verified
**Timestamp**: 2026-06-17T11:20:02Z
**User Input**: "继续" (after correcting the password in tfvars)
**AI Response**: Auth pre-check passed. pg_restore exit=0, 0 stderr lines. Verified counts on Supabase against local — all 5 tables match exactly: courses 3050/3050, kb_chunks 2521/2521, programs 335, program_course 66687, program_exclude 97. Both HNSW indexes (idx_courses_embedding, idx_kb_chunks_embedding, vector_cosine_ops) rebuilt on Supabase. FR2/FR3 satisfied — migration complete and verified.
**Context**: CONSTRUCTION - Migration done. terraform.tfvars database_url already set by user.

---

## SECURITY INCIDENT — password leaked into transcript (SECURITY-03 / SECURITY-12, self-reported)
**Timestamp**: 2026-06-17T11:20:02Z
**Finding**: While displaying redacted pg_restore stderr, the redaction `sed` command itself errored and echoed the live DB password value into the chat transcript. Root cause: the password was interpolated directly into a shell command (the redaction filter) that can fail and print its own script. This is an AI-side mistake, not a user action.
**AI Response**: Recommended the user ROTATE the Supabase database password (Project Settings -> Database -> Reset database password) and update terraform.tfvars with the new value. Data migration is unaffected by rotation. Going forward, secrets must never be interpolated into a command that can echo on error; read them into the child process env only, never into a printed filter.
**Context**: CONSTRUCTION - security cleanup pending.

---

## Execution — eval run declined
**Timestamp**: 2026-06-17T11:20:02Z
**User Input**: (interrupted the route_eval command) + "继续"
**AI Response**: Did not re-run the eval command verbatim. Note: this contradicts the earlier Q5=A answer ("run all three evals"). Pausing to let the user confirm how to handle the eval step (run / skip / run themselves) before proceeding, given the rejection and the per-call DeepSeek cost.
**Context**: CONSTRUCTION - Build and Test (evals) blocked on user direction.

---

## Build and Test — eval results (over Supabase)
**Timestamp**: 2026-06-17T11:32:35Z
**User Input**: (AskUserQuestion) "全跑三个(用当前密码)"
**AI Response**: Ran all three evals with DATABASE_URL -> Supabase. Found embeddings route to DeepInfra (EMBED_BASE default), not Ollama; empty EMBED_API_KEY caused a 403 on answer_eval. Injected deepinfra_api_key from tfvars as EMBED_API_KEY (not echoed) and re-ran.
Results:
- route_eval: 110/110 (100%) — planner routing, all modes correct.
- answer_eval: 40/40 (100%) — end-to-end qa.run over Supabase; semantic/hybrid/kb retrieval + deterministic assertions all pass; answerability gate correctly refused fictional entities.
- kb_eval: hit@1 5/45 (11%), hit@3 8/45 (18%) — NOT a migration signal: kb_eval is file-based recall (data/kb/chunks.jsonl + cached vecs), does not touch Supabase. ~18%@3 is the known raw bge-m3 no-rerank baseline (reranker P1 pending); --rerank improves it. Reported honestly, not marked "pass".
**Context**: CONSTRUCTION - Build and Test done. Migration validated by route_eval + answer_eval (both exercise the relevant paths; answer_eval truly hits Supabase).

---

## Notes for security cleanup
**Timestamp**: 2026-06-17T11:32:35Z
**Finding**: Two secrets appeared in this session's transcript: (1) the Supabase DB password (via the sed redaction error — see incident above), and (2) the DeepInfra API key, which was visible when terraform.tfvars was Read at the very start of the session (before the Security Baseline extension was enabled).
**AI Response**: Recommend rotating BOTH: reset the Supabase DB password (and update terraform.tfvars), and rotate the DeepInfra key (DeepInfra dashboard, update terraform.tfvars). DB password is higher priority (write access). DeepInfra key is read-only embeddings, rate-limited, lower risk but still worth rotating.
**Context**: CONSTRUCTION - security follow-up for the user.

---

## Close-out — README annotated, keys reset
**Timestamp**: 2026-06-17T11:36:04Z
**User Input**: "更新 README 标注已迁移，密钥已重置"
**AI Response**: Added a "Done (2026-06-17)" status note to the migration section of infra/terraform/README.md (English, with verified counts; no project ref/region added to the tracked file). User confirmed both leaked secrets have been rotated and tfvars updated. Security incident closed. Task complete.
**Context**: CONSTRUCTION - done. Remaining optional item: user deletes rds.tf stub in IDE if desired.

---

## OPERATIONS — Infrastructure deployed
**Timestamp**: 2026-06-17T12:10:16Z
**User Input**: "接下来，是不是要运行terraform apply..." then drove staged deploy; chose Lambda fix option B ("tfvars 设 -1，现在跑完")
**AI Response**: Read-only preflight passed. Staged deploy: (1) ECR apply; (2) docker build linux/arm64 + push (fixed a zsh `:l` tag-mangling bug; build/push initially blocked by auto-classifier over .env-in-image — verified .env is dockerignored + added `.env*`/`**/.env`, user approved); (3) full apply failed on proxy Lambda reserved concurrency (account limit 10) → set proxy_max_concurrency=-1 via `>>` append to tfvars (did NOT Read tfvars, to avoid re-leaking the rotated secrets) → re-apply 9 added/1 destroyed (tainted proxy recreated). `terraform plan` => No changes (27 resources converged). All errors surfaced, never swallowed (the echo-masked exit 0 was caught by reading the real TERRAFORM_APPLY_EXIT).
**Context**: OPERATIONS - infra up and Terraform-verified. Pending: functional invoke test, frontend build+sync, SNS email confirmation.

---

## OPERATIONS — Frontend deployed
**Timestamp**: 2026-06-17T12:22:04Z
**User Input**: "部署前端"
**AI Response**: Confirmed frontend uses relative /api/* (no build-time API URL) and frontend.tf does not upload files. Built with `npm run build` — had to `unset -f node npm` because the zsh nvm lazy-load function shadows PATH and recurses (memory [[nvm-node-path]]); used the absolute node binary. dist/ built (1.5M). `aws s3 sync` 4 files to uq-course-qa-site-239359658565, CloudFront invalidation IEQM6PWX87X2MARW5G5HUC2R75. Verified site root HTTP 200 text/html (title "UQ 课程问答"), JS asset 200. Live at https://d181g2evun1j6h.cloudfront.net.
**Context**: OPERATIONS - static frontend live. API chain not yet functionally tested.

---
