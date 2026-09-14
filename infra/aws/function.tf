data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.project}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
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
      PGUSER             = aws_db_instance.this.username
      PGPASSWORD         = data.aws_ssm_parameter.db_password.value
      PGSSLROOTCERT      = "/var/task/rds-ca.pem"
      DECISION_API_TOKEN = data.aws_ssm_parameter.decision_api_token.value
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda,
    aws_iam_role_policy_attachment.lambda_vpc,
  ]
}
