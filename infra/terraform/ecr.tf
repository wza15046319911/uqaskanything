resource "aws_ecr_repository" "qa" {
  name                 = local.name_dns
  image_tag_mutability = "MUTABLE"
  force_delete         = true # easy teardown

  image_scanning_configuration {
    scan_on_push = false # save cost
  }
}

resource "aws_ecr_lifecycle_policy" "qa" {
  repository = aws_ecr_repository.qa.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "only keep last 3 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 3
      }
      action = { type = "expire" }
    }]
  })
}
