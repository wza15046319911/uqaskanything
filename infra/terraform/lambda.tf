# Signing proxy Lambda + public Function URL. Browser -> CloudFront /api/* -> here -> AgentCore.
# Bundles a recent boto3 (the Lambda runtime's built-in boto3 is too old for bedrock-agentcore);
# the lambda/build dir is populated by `pip install boto3 -t build` before apply (see README).

data "archive_file" "proxy" {
  type        = "zip"
  source_dir  = "${path.module}/lambda/build"
  output_path = "${path.module}/lambda/proxy.zip"
}

# Shared secret CloudFront injects as x-origin-secret; the handler rejects requests without it.
# Server-side only (CloudFront config + Lambda env), never reaches the browser. Closes the raw
# Function URL bypass without OAC/IAM (which has a POST-body signing caveat for Lambda origins).
resource "random_password" "origin_secret" {
  length  = 40
  special = false
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "proxy" {
  name               = "${local.name_dns}-proxy"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "proxy_logs" {
  role       = aws_iam_role.proxy.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "proxy_invoke" {
  statement {
    actions = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources = [
      aws_bedrockagentcore_agent_runtime.qa.agent_runtime_arn,
      "${aws_bedrockagentcore_agent_runtime.qa.agent_runtime_arn}/*",
    ]
  }
}

resource "aws_iam_role_policy" "proxy_invoke" {
  name   = "${local.name_dns}-proxy-invoke"
  role   = aws_iam_role.proxy.id
  policy = data.aws_iam_policy_document.proxy_invoke.json
}

resource "aws_lambda_function" "proxy" {
  function_name    = "${local.name_dns}-proxy"
  role             = aws_iam_role.proxy.arn
  runtime          = "python3.13"
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.proxy.output_path
  source_code_hash = data.archive_file.proxy.output_base64sha256
  timeout          = 130 # cold start + QA latency (planner + answer + embedding)
  memory_size      = 256

  reserved_concurrent_executions = var.proxy_max_concurrency # spend ceiling: throttles excess instead of fanning out LLM calls

  environment {
    variables = {
      AGENT_RUNTIME_ARN = aws_bedrockagentcore_agent_runtime.qa.agent_runtime_arn
      BEDROCK_REGION    = var.region
      ORIGIN_SECRET     = random_password.origin_secret.result
    }
  }
}

resource "aws_lambda_function_url" "proxy" {
  function_name      = aws_lambda_function.proxy.function_name
  authorization_type = "NONE" # auth is enforced in-handler via the x-origin-secret header; direct hits without it get 403
}
