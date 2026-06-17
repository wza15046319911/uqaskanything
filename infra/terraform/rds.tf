# Minimal RDS: db.t4g.micro / single-AZ / gp3 20G, in the default VPC (no extra VPC/NAT, cheapest + smallest).
# The pgvector extension is not created here; it comes along in the -Fc dump during pg_restore.

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

resource "random_password" "db" {
  length  = 24
  special = false
}

resource "aws_db_subnet_group" "qa" {
  name       = "${local.name_dns}-subnets"
  subnet_ids = data.aws_subnets.default.ids
}

resource "aws_security_group" "db" {
  name        = "${local.name_dns}-db"
  description = "RDS Postgres for AgentCore QA"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "Postgres"
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = var.db_ingress_cidrs
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_db_instance" "qa" {
  identifier     = local.name_dns
  engine         = "postgres"
  engine_version = var.engine_version
  instance_class = var.db_instance_class

  allocated_storage = var.db_allocated_storage
  storage_type      = "gp3"

  db_name  = var.db_name
  username = var.db_username
  password = random_password.db.result

  db_subnet_group_name   = aws_db_subnet_group.qa.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = true

  multi_az                     = false
  backup_retention_period      = var.backup_retention_days
  performance_insights_enabled = false
  monitoring_interval          = 0
  deletion_protection          = false
  skip_final_snapshot          = true
  apply_immediately            = true
}

locals {
  database_url = "postgresql://${var.db_username}:${random_password.db.result}@${aws_db_instance.qa.address}:5432/${var.db_name}"
}
