data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# The deploy user may only create roles that carry this boundary, so no role it
# makes can be granted more than logging and VPC networking, whatever policy is
# attached. The boundary is managed outside Terraform: see infra/aws/iam/.
data "aws_iam_policy" "role_boundary" {
  name = "${var.project}-role-boundary"
}

resource "aws_iam_role" "lambda" {
  name                 = "${var.project}-lambda"
  assume_role_policy   = data.aws_iam_policy_document.lambda_assume.json
  permissions_boundary = data.aws_iam_policy.role_boundary.arn
}

# Logs plus the ENI permissions a VPC-attached function needs. Nothing else:
# the function reads no SSM, no S3, no Secrets Manager.
resource "aws_iam_role_policy_attachment" "lambda_vpc" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.project}"
  retention_in_days = 7
}

resource "aws_cloudwatch_log_group" "bootstrap" {
  name              = "/aws/lambda/${var.project}-bootstrap"
  retention_in_days = 7
}

# Holds the master password. Applies the schema, creates paypilot_app with
# SELECT and INSERT only, then logs in as that role and proves UPDATE, DELETE,
# TRUNCATE and CREATE are refused. No API route targets it; Terraform invokes it.
resource "aws_lambda_function" "bootstrap" {
  function_name    = "${var.project}-bootstrap"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "app.decision_bootstrap.handler"
  filename         = var.lambda_zip_path
  source_code_hash = filebase64sha256(var.lambda_zip_path)
  memory_size      = 256
  timeout          = 60

  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }

  environment {
    variables = {
      PGHOST          = aws_db_instance.this.address
      PGPORT          = tostring(aws_db_instance.this.port)
      PGDATABASE      = aws_db_instance.this.db_name
      PGUSER          = aws_db_instance.this.username
      PGPASSWORD      = data.aws_ssm_parameter.db_password.value
      PGSSLROOTCERT   = "/var/task/rds-ca.pem"
      APP_DB_PASSWORD = data.aws_ssm_parameter.app_db_password.value
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.bootstrap,
    aws_iam_role_policy_attachment.lambda_vpc,
  ]
}

# Re-runs whenever the bootstrap code, the schema or the app password version
# changes. A failed grant check raises inside the function, which fails the apply.
resource "aws_lambda_invocation" "bootstrap" {
  function_name = aws_lambda_function.bootstrap.function_name
  input = jsonencode({
    code_sha256      = aws_lambda_function.bootstrap.source_code_hash
    schema_sha256    = filesha256("${path.module}/../../app/decision_schema.sql")
    app_password_ver = data.aws_ssm_parameter.app_db_password.version
  })
}

resource "aws_lambda_function" "decision" {
  function_name    = var.project
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  architectures    = ["arm64"]
  handler          = "app.lambda_handler.handler"
  filename         = var.lambda_zip_path
  source_code_hash = filebase64sha256(var.lambda_zip_path)
  memory_size      = 256
  timeout          = 10

  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }

  environment {
    variables = {
      PGHOST             = aws_db_instance.this.address
      PGPORT             = tostring(aws_db_instance.this.port)
      PGDATABASE         = aws_db_instance.this.db_name
      PGUSER             = "paypilot_app"
      PGPASSWORD         = data.aws_ssm_parameter.app_db_password.value
      PGSSLROOTCERT      = "/var/task/rds-ca.pem"
      DECISION_API_TOKEN = data.aws_ssm_parameter.decision_api_token.value
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda,
    aws_iam_role_policy_attachment.lambda_vpc,
    aws_lambda_invocation.bootstrap,
  ]
}
