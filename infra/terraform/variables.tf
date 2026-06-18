# All resources use minimal size + cheapest config; the reachability/security tradeoff is in db_ingress_cidrs and the README.

variable "region" {
  type    = string
  default = "us-west-2" # region where gpt-oss + AgentCore are available
}

variable "project" {
  type    = string
  default = "uq-course-qa" # base name; AWS resources use the hyphen form, the AgentCore runtime the underscore form (see locals.tf)
}

variable "image_tag" {
  type    = string
  default = "latest"
}

variable "proxy_max_concurrency" {
  type    = number
  default = 3 # reserved Lambda concurrency: hard ceiling on simultaneous LLM calls = spend ceiling (free)
}

variable "waf_rate_limit" {
  type    = number
  default = 200 # WAF per-IP request cap over a 5-minute window; over this the IP is blocked at the edge
}

variable "alert_email" {
  type = string
  # no default: where budget alerts + kill-switch notifications are sent (must confirm the SNS email)
}

variable "budget_limit" {
  type    = number
  default = 30 # monthly USD budget; alerts at 80% (actual), 100% (actual + forecasted)
}

variable "kill_switch_invocations" {
  type    = number
  default = 200 # proxy invocations per 5-min window that trip the kill switch (sets proxy concurrency to 0)
}

variable "bedrock_model" {
  type    = string
  default = "openai.gpt-oss-120b-1:0" # set openai.gpt-oss-20b-1:0 to save cost
}

variable "embed_base" {
  type    = string
  default = "https://api.deepinfra.com/v1/openai"
}

variable "embed_model" {
  type    = string
  default = "BAAI/bge-m3"
}

variable "deepinfra_api_key" {
  type      = string
  sensitive = true
  # no default: must be provided explicitly (TF_VAR_deepinfra_api_key or tfvars)
}

variable "database_url" {
  type      = string
  sensitive = true
  # no default: the Supabase Postgres connection string (session pooler, port 5432 — supports prepared statements).
  # postgresql://postgres.<ref>:<pwd>@aws-0-<region>.pooler.supabase.com:5432/postgres
  # Injected into the Runtime as DATABASE_URL. The DB lives in Supabase (managed Postgres + pgvector);
  # see README for the one-off data migration from the local dev DB.
}
