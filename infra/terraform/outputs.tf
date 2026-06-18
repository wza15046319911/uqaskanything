output "ecr_repository_url" {
  description = "push the image here"
  value       = aws_ecr_repository.qa.repository_url
}

output "agent_runtime_id" {
  description = "AgentCore Runtime id"
  value       = aws_bedrockagentcore_agent_runtime.qa.agent_runtime_id
}

output "agent_runtime_arn" {
  description = "for invocation: aws bedrock-agentcore invoke-agent-runtime --agent-runtime-arn <this>"
  value       = aws_bedrockagentcore_agent_runtime.qa.agent_runtime_arn
}

output "site_url" {
  description = "open this in the browser (CloudFront)"
  value       = "https://${aws_cloudfront_distribution.site.domain_name}"
}

output "site_bucket" {
  description = "aws s3 sync frontend/dist to this bucket"
  value       = aws_s3_bucket.site.bucket
}

output "cloudfront_distribution_id" {
  description = "for cache invalidation after uploading the frontend"
  value       = aws_cloudfront_distribution.site.id
}

output "lambda_function_url" {
  description = "proxy Function URL (CloudFront /api/* origin; direct access has no CORS)"
  value       = aws_lambda_function_url.proxy.function_url
}
