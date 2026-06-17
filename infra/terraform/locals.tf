locals {
  name_dns = replace(var.project, "_", "-") # ECR/RDS/SG/IAM AWS resource names: hyphens (RDS disallows underscores)
  name_us  = replace(var.project, "-", "_") # AgentCore runtime name: underscores (disallows hyphens)
}
