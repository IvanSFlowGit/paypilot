# Private-only VPC: no internet gateway, no NAT, no public subnet.
# Two subnets in two AZs because an RDS subnet group requires two AZs, not
# because the database is multi-AZ (it is not).

resource "aws_vpc" "this" {
  cidr_block           = "10.40.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = var.project }
}

resource "aws_subnet" "private" {
  count             = 2
  vpc_id            = aws_vpc.this.id
  cidr_block        = cidrsubnet(aws_vpc.this.cidr_block, 8, count.index)
  availability_zone = local.azs[count.index]

  tags = { Name = "${var.project}-private-${count.index}" }
}

resource "aws_security_group" "lambda" {
  name        = "${var.project}-lambda"
  description = "Decision Lambda: egress to Postgres only"
  vpc_id      = aws_vpc.this.id
}

resource "aws_security_group" "db" {
  name        = "${var.project}-db"
  description = "RDS: ingress from the decision Lambda only"
  vpc_id      = aws_vpc.this.id
}

resource "aws_vpc_security_group_egress_rule" "lambda_to_db" {
  security_group_id            = aws_security_group.lambda.id
  referenced_security_group_id = aws_security_group.db.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "Postgres"
}

resource "aws_vpc_security_group_ingress_rule" "db_from_lambda" {
  security_group_id            = aws_security_group.db.id
  referenced_security_group_id = aws_security_group.lambda.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  description                  = "Postgres from the decision Lambda"
}
