# AgentCore QA — Terraform deployment

Deploys the QA logic (behind `/api/ask`) to **Amazon Bedrock AgentCore Runtime**.
The simulator `/api/sim/*` is out of scope.

## Architecture (minimal + cheapest)

```
caller ──SigV4──> AgentCore Runtime (PUBLIC network mode, container=app.agent, 8080)
                     ├── Bedrock gpt-oss       (generation/planning, via execution-role SigV4)
                     ├── DeepInfra bge-m3        (query embedding, managed egress)
                     └── RDS PostgreSQL+pgvector (publicly reachable, SG + random password)
```

- **PUBLIC network mode**: the Runtime uses AWS-managed egress to reach DeepInfra + Bedrock
  directly. **No VPC / NAT gateway / subnets** — saves the ~$32/mo NAT and keeps Terraform small.
- All resources minimal: RDS `db.t4g.micro` / single-AZ / gp3 20G; ECR keeps last 3 images; no Secrets Manager.
- Rough monthly cost: RDS t4g.micro ≈ $12 + storage/ECR negligible; Bedrock/DeepInfra pay-per-use (short QA text, very low).

> ⚠️ **Security tradeoff**: in PUBLIC mode the Runtime egress IPs are not fixed, so for it to reach
> RDS you usually set `db_ingress_cidrs = ["0.0.0.0/0"]` (DB port open to the internet, protected by a
> 24-char random password + the fact this is **public course data**). It is the cost of the cheap PUBLIC mode.
> Not acceptable? Use the "Secure variant" below.

## Prerequisites

- AWS credentials (`aws configure` or env vars); **gpt-oss model access enabled in the Bedrock console** for the target region.
- A DeepInfra API key (`https://deepinfra.com` → API keys).
- Local `pg_dump` of your dev DB (`localhost:5433/uq_courses`), already embedded via `embed.py`.
- `docker` locally (to build the ARM64 image).

## Variables

Create `terraform.tfvars`:

```hcl
region            = "us-east-1"
deepinfra_api_key = "your DeepInfra key"
db_ingress_cidrs  = ["0.0.0.0/0"]   # see security tradeoff above
# bedrock_model   = "openai.gpt-oss-20b-1:0"   # cheaper option
```

## Deploy order (image/data are not pure TF, so it is staged)

```bash
cd infra/terraform
terraform init

# 1) Create ECR + RDS (and their deps) first
terraform apply -target=aws_ecr_repository.qa -target=aws_db_instance.qa

# 2) Build the ARM64 image and push to ECR
ECR=$(terraform output -raw ecr_repository_url)
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin "${ECR%/*}"
docker build --platform linux/arm64 -f ../../backend/Dockerfile.agentcore -t "$ECR:latest" ../../backend
docker push "$ECR:latest"

# 3) Migrate data (carries existing vectors, no re-embed; the pgvector extension comes along in the dump)
pg_dump "postgresql://postgres:uqrag@localhost:5433/uq_courses" -Fc -f uq_courses.dump
DBURL=$(terraform output -raw database_url)
pg_restore --no-owner -d "$DBURL" uq_courses.dump
psql "$DBURL" -c "SELECT count(*) total, count(embedding) embedded FROM courses;"   # the two should be equal

# 4) Create the Runtime (the image now exists, referenced by it)
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

A response with `courses` / `answer` means all three (Bedrock gpt-oss + DeepInfra bge-m3 + RDS pgvector) work.
Also test a program-type question (deterministic answer) and a kb-type one (uses vectors).

> Note: the exact invoke CLI shape (payload base64 / outfile) depends on your AWS CLI version;
> the starter toolkit also works: `agentcore invoke '{"question":"..."}'`.

## Must do before serving students (student-facing red line 6)

After switching to gpt-oss + DeepInfra, **run the eval to confirm no regression** — not just "it didn't crash":

```bash
cd ../../backend
LLM_BACKEND=bedrock BEDROCK_REGION=us-east-1 \
EMBED_BASE=https://api.deepinfra.com/v1/openai EMBED_API_KEY=... EMBED_MODEL=BAAI/bge-m3 \
DATABASE_URL="$DBURL" \
python -m app.pipelines.route_eval        # routing accuracy
python -m app.pipelines.answer_eval       # end-to-end answer correctness
python -m app.pipelines.kb_eval           # KB recall
```

Focus on: (1) gpt-oss json_mode stability for the planner (planning is the critical path);
(2) Chinese answer quality; (3) DeepInfra vs Ollama bge-m3 `sim` distribution does not drift
(the 0.50/0.62 thresholds were tuned on bge-m3).

> The llm backend already strips gpt-oss `<reasoning>` blocks and robustly extracts JSON
> (gpt-oss can emit reasoning + an extra `{` in json_object mode) — verified against the real model.

## Secure variant (private RDS)

To avoid exposing the DB port, go back to a VPC setup (≈ +$32/mo for NAT):
1. `network_configuration { network_mode = "VPC" ... }` with subnets + security group;
2. RDS `publicly_accessible = false`, SG allows only the Runtime security group;
3. Build a VPC + private subnets + 1 NAT gateway (for DeepInfra egress).

## Destroy

```bash
terraform destroy
```
(ECR `force_delete`, RDS `skip_final_snapshot`, so it tears down cleanly.)
