# Migration — Requirements Clarification Questions

Please answer each question by filling in the letter after the `[Answer]:` tag.
If none of the options match, choose the last option (Other) and describe.

> SECURITY NOTE: Do NOT paste the Supabase password or full connection string
> into this file. Q3 only asks HOW you will deliver it. The secret itself goes
> straight into the gitignored `terraform.tfvars` (by you or by me after you
> paste it in chat).

## Question 1 — Security Baseline extension
Should the SECURITY baseline rules be enforced for this task as blocking
constraints? (We are handling a DB password / connection string, so this is
relevant.)

A) Yes — enforce all SECURITY rules as blocking constraints (recommended)
B) No — skip all SECURITY rules
X) Other (please describe after [Answer]: tag below)

[Answer]:A

## Question 2 — Property-Based Testing extension
Should property-based testing rules be enforced for this task?

A) No — skip PBT (recommended; this task writes no business-logic code, only runs a data migration + existing evals)
B) Yes — enforce all PBT rules as blocking constraints
C) Partial — only for pure functions / serialization round-trips
X) Other (please describe after [Answer]: tag below)

[Answer]:A

## Question 3 — How will you deliver the session-pooler connection string?
I cannot read your Supabase DB password; I need the session-pooler string
(port 5432) to run pg_restore and to fill `database_url`.

A) I will paste the full string in chat; you (Claude) write it into the gitignored terraform.tfvars and use it for the migration (fastest)
B) I will write it into infra/terraform/terraform.tfvars myself; you read it from there
C) I will export it as DATABASE_URL in the shell; you read it from the environment
X) Other (please describe after [Answer]: tag below)

[Answer]:A

## Question 4 — What to do with infra/terraform/rds.tf?
The DB is now Supabase, not RDS. The old rds.tf may be obsolete.

A) Leave rds.tf untouched for now (decide later)
B) Empty its contents (I cannot rm; you delete the file in your IDE afterwards if you want it gone)
C) It is already handled / not present — skip
X) Other (please describe after [Answer]: tag below)

[Answer]:B

## Question 5 — Eval scope after migration (red line 6)
Running evals against Supabase needs an embedding provider (Ollama bge-m3 local,
or DeepInfra) for query vectors, plus an LLM for the planner.

A) Run all three (route_eval + answer_eval + kb_eval) against the migrated DB now; I confirm the embedding + LLM providers are available
B) Run only the cheap routing/recall checks now (route_eval + kb_eval), defer answer_eval
C) Skip evals in this session; I will run them separately before serving students
X) Other (please describe after [Answer]: tag below)

[Answer]:A
