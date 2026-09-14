resource "aws_db_subnet_group" "this" {
  name       = var.project
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_instance" "this" {
  identifier     = var.project
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  allocated_storage = 20
  storage_type      = "gp3"
  storage_encrypted = true

  db_name  = "paypilot"
  username = "paypilot"
  password = data.aws_ssm_parameter.db_password.value

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.db.id]
  publicly_accessible    = false
  multi_az               = false

  backup_retention_period = 1
  # The whole point of this stack is apply, destroy, apply again. A final
  # snapshot or deletion protection would make the destroy step fail or leave
  # a billable snapshot behind.
  skip_final_snapshot = true
  deletion_protection = false
  apply_immediately   = true
}
