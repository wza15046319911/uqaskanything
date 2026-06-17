# Cost guardrails: a monthly budget alert (notify only) and an automatic kill switch.
# Kill switch: a CloudWatch alarm on proxy invocations -> SNS -> breaker Lambda that sets the
# proxy's reserved concurrency to 0, cutting off QA and LLM spend. Re-arm with `terraform apply`.

resource "aws_budgets_budget" "monthly" {
  name         = "${local.name_dns}-monthly"
  budget_type  = "COST"
  limit_amount = var.budget_limit
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}

data "archive_file" "breaker" {
  type        = "zip"
  source_file = "${path.module}/lambda/breaker.py"
  output_path = "${path.module}/lambda/breaker.zip"
}

resource "aws_iam_role" "breaker" {
  name               = "${local.name_dns}-breaker"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "breaker_logs" {
  role       = aws_iam_role.breaker.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

data "aws_iam_policy_document" "breaker" {
  statement {
    actions   = ["lambda:PutFunctionConcurrency"]
    resources = [aws_lambda_function.proxy.arn]
  }
}

resource "aws_iam_role_policy" "breaker" {
  name   = "${local.name_dns}-breaker"
  role   = aws_iam_role.breaker.id
  policy = data.aws_iam_policy_document.breaker.json
}

resource "aws_lambda_function" "breaker" {
  function_name    = "${local.name_dns}-breaker"
  role             = aws_iam_role.breaker.arn
  runtime          = "python3.13"
  handler          = "breaker.lambda_handler"
  filename         = data.archive_file.breaker.output_path
  source_code_hash = data.archive_file.breaker.output_base64sha256
  timeout          = 30

  environment {
    variables = {
      PROXY_FUNCTION_NAME = aws_lambda_function.proxy.function_name
    }
  }
}

resource "aws_sns_topic" "breaker" {
  name = "${local.name_dns}-breaker"
}

resource "aws_sns_topic_subscription" "breaker_lambda" {
  topic_arn = aws_sns_topic.breaker.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.breaker.arn
}

resource "aws_sns_topic_subscription" "breaker_email" {
  topic_arn = aws_sns_topic.breaker.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_lambda_permission" "breaker_sns" {
  statement_id  = "AllowSNSInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.breaker.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.breaker.arn
}

resource "aws_cloudwatch_metric_alarm" "invocation_spike" {
  alarm_name          = "${local.name_dns}-invocation-spike"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Invocations"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = var.kill_switch_invocations
  alarm_description   = "QA proxy invocations spiked; trip the kill switch (set proxy concurrency to 0)"
  alarm_actions       = [aws_sns_topic.breaker.arn]

  dimensions = {
    FunctionName = aws_lambda_function.proxy.function_name
  }
}
