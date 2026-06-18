# AgentCore QA — Terraform deployment

Deploys the QA logic (behind `/api/ask`) to **Amazon Bedrock AgentCore Runtime**.
The simulator `/api/sim/*` is out of scope.

## Architecture (minimal + cheapest)

```
caller ──SigV4──> AgentCore Runtime (PUBLIC network mode, container=app.agent, 8080)
                     ├── Bedrock gpt-oss          (generation/planning, via execution-role SigV4)
                     ├── DeepInfra bge-m3          (query embedding, managed egress)
                     └── Supabase Postgres+pgvector (managed; reached over TLS, session pooler :5432)
```

- **PUBLIC network mode**: the Runtime uses AWS-managed egress to reach DeepInfra + Bedrock + Supabase
  directly. **No VPC / NAT gateway / subnets** — saves the ~$32/mo NAT and keeps Terraform small.
- **The DB is Supabase, not RDS** — a managed Postgres endpoint (TLS + credentials + connection pooler),
  so Terraform owns no database: no open security group, no `0.0.0.0/0`, no `db.t4g.micro` running 24/7.
  The connection string is passed in via `var.database_url` and injected into the Runtime.
- Other resources minimal: ECR keeps last 3 images; no Secrets Manager.
- Rough monthly cost: Supabase free tier (the whole DB is ~180 MB, well under the 500 MB limit) ≈ $0;
  Bedrock/DeepInfra pay-per-use (short QA text, very low). Note the free tier pauses on inactivity and has no SLA.

## Prerequisites

- AWS credentials (`aws configure` or env vars); **gpt-oss model access enabled in the Bedrock console** for the target region.
- A DeepInfra API key (`https://deepinfra.com` → API keys).
- A **Supabase project** with the `vector` extension enabled and the data already migrated
  (see "Migrate the database to Supabase" below). You need its **session-pooler** connection string.
- `docker` locally (to build the ARM64 image).

## Variables

Create `terraform.tfvars`:

```hcl
region            = "us-east-1"
deepinfra_api_key = "your DeepInfra key"
database_url      = "postgresql://postgres.<ref>:<pwd>@aws-0-<region>.pooler.supabase.com:5432/postgres"
# bedrock_model   = "openai.gpt-oss-20b-1:0"   # cheaper option
```

## Migrate the database to Supabase (one-off, before the first deploy)

> **✅ Done (2026-06-17).** The local dev DB has been migrated to Supabase and
> verified: all 5 tables match the source exactly (courses 3050/3050,
> kb_chunks 2521/2521, programs 335, program_course 66687, program_exclude 97),
> both HNSW indexes rebuilt, and `database_url` is set in `terraform.tfvars`.
> Routing + end-to-end answer evals pass 100% against the migrated DB. The steps
> below are kept for reference and for re-running the migration if needed.

The DB is Supabase, not Terraform-managed. Migrate the local dev DB once. Use the
**session-pooler** string (port 5432) — it keeps prepared statements, so the backend's
`psycopg.connect` calls need no change (the transaction pooler on 6543 would).

```bash
# 1) In the Supabase SQL editor, enable pgvector:
#    create extension if not exists vector;

# 2) Dump the local dev DB (-Fc carries the vectors + HNSW index defs; no re-embed)
pg_dump "postgresql://postgres:uqrag@localhost:5433/uq_courses" \
  -Fc --no-owner --no-privileges -f uq_courses.dump

# 3) Restore into Supabase via the session-pooler string
SUPA="postgresql://postgres.<ref>:<pwd>@aws-0-<region>.pooler.supabase.com:5432/postgres"
pg_restore --no-owner --no-privileges -d "$SUPA" uq_courses.dump

# 4) Verify the counts match (red line: not "didn't error" — the numbers must agree)
psql "$SUPA" -c "SELECT count(*) total, count(embedding) embedded FROM courses;"  # expect 3050 / 3050
psql "$SUPA" -c "SELECT count(*) FROM kb_chunks;"                                 # expect 2521
```

Put this same `SUPA` string into `terraform.tfvars` as `database_url`.

## Deploy order (image is not pure TF, so it is staged)

```bash
cd infra/terraform
terraform init

# 1) Create ECR first
terraform apply -target=aws_ecr_repository.qa

# 2) Build the ARM64 image and push to ECR
ECR=$(terraform output -raw ecr_repository_url)
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin "${ECR%/*}"
docker build --platform linux/arm64 -f ../../backend/Dockerfile.agentcore -t "$ECR:latest" ../../backend
docker push "$ECR:latest"

# 3) Create the Runtime (the image now exists, referenced by it)
terraform apply
```

## Verify

```bash
ARN=$(terraform output -raw agent_runtime_arn)
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-arn "$ARN" \
  --payload '{"question":"which courses are about machine learning"}' \
  /dev/stdout
```

A response with `courses` / `answer` means all three (Bedrock gpt-oss + DeepInfra bge-m3 + Supabase pgvector) work.
Also test a program-type question (deterministic answer) and a kb-type one (uses vectors).

> Note: the exact invoke CLI shape (payload base64 / outfile) depends on your AWS CLI version;
> the starter toolkit also works: `agentcore invoke '{"question":"..."}'`.

## Must do before serving students (student-facing red line 6)

After switching to gpt-oss + DeepInfra, **run the eval to confirm no regression** — not just "it didn't crash":

```bash
cd ../../backend
LLM_BACKEND=bedrock BEDROCK_REGION=us-east-1 \
EMBED_BASE=https://api.deepinfra.com/v1/openai EMBED_API_KEY=... EMBED_MODEL=BAAI/bge-m3 \
DATABASE_URL="$SUPA" \
python -m app.pipelines.route_eval        # routing accuracy
python -m app.pipelines.answer_eval       # end-to-end answer correctness
python -m app.pipelines.kb_eval           # KB recall
```

Focus on: (1) gpt-oss json_mode stability for the planner (planning is the critical path);
(2) Chinese answer quality; (3) DeepInfra vs Ollama bge-m3 `sim` distribution does not drift
(the 0.50/0.62 thresholds were tuned on bge-m3).

> The llm backend already strips gpt-oss `<reasoning>` blocks and robustly extracts JSON
> (gpt-oss can emit reasoning + an extra `{` in json_object mode) — verified against the real model.

## Database network controls

The DB is Supabase, so there is no self-managed DB port to expose — no security group,
no `0.0.0.0/0`. The Runtime reaches it over TLS via the pooler. For stricter control,
Supabase offers IP allowlisting / network restrictions and private networking on its paid
tiers; the Runtime egress IPs are not fixed in PUBLIC mode, so an allowlist there needs the
VPC network mode (a fixed NAT egress IP), which reintroduces the ~$32/mo NAT.

## Destroy

```bash
terraform destroy
```
(ECR `force_delete`, so it tears down cleanly. The Supabase project is separate — delete it
in the Supabase dashboard; `terraform destroy` does not touch your data.)
