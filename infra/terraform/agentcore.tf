# AgentCore Runtime: PUBLIC network mode (managed egress, reaches DeepInfra + Bedrock directly, no VPC/NAT).
# Secrets (DeepInfra key / DB connection string) go straight into the runtime env (minimal: no Secrets Manager).

resource "aws_bedrockagentcore_agent_runtime" "qa" {
  agent_runtime_name = local.name_us
  role_arn           = aws_iam_role.runtime.arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = "${aws_ecr_repository.qa.repository_url}:${var.image_tag}"
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  environment_variables = {
    LLM_BACKEND    = "bedrock"
    BEDROCK_MODEL  = var.bedrock_model
    BEDROCK_REGION = var.region
    EMBED_BASE     = var.embed_base
    EMBED_MODEL    = var.embed_model
    EMBED_API_KEY  = var.deepinfra_api_key
    DATABASE_URL   = local.database_url
  }

  depends_on = [aws_iam_role_policy.runtime]
}
