# Edge protection for the public QA endpoint: per-IP rate limit + Amazon IP reputation list.
# Blocks floods/abuse at CloudFront before requests reach the Lambda proxy or burn LLM calls.
# CLOUDFRONT scope requires the us-east-1 provider (see versions.tf).

resource "aws_wafv2_web_acl" "site" {
  provider = aws.us_east_1
  name     = "${local.name_dns}-waf"
  scope    = "CLOUDFRONT"

  default_action {
    allow {}
  }

  rule {
    name     = "ip-reputation"
    priority = 1
    override_action {
      none {}
    }
    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesAmazonIpReputationList"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name_dns}-ip-reputation"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "rate-limit"
    priority = 2
    action {
      block {}
    }
    statement {
      rate_based_statement {
        limit              = var.waf_rate_limit
        aggregate_key_type = "IP"
      }
    }
    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.name_dns}-rate-limit"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${local.name_dns}-waf"
    sampled_requests_enabled   = true
  }
}
