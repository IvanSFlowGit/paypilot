variable "region" {
  description = "AWS region for every resource in the slice."
  type        = string
  default     = "eu-west-2"
}

variable "project" {
  description = "Name prefix and tag for every resource."
  type        = string
  default     = "paypilot-decision"
}

variable "db_password_ssm_name" {
  description = "SSM SecureString parameter holding the RDS master password. Create it by hand before the first apply; it is never in this repository."
  type        = string
  default     = "/paypilot/decision/db_password"
}

variable "app_db_password_ssm_name" {
  description = "SSM SecureString holding the password of paypilot_app, the SELECT/INSERT-only role the decision function uses."
  type        = string
  default     = "/paypilot/decision/app_db_password"
}

variable "decision_api_token_ssm_name" {
  description = "SSM SecureString parameter holding the bearer token for /decide and /decisions. Create it by hand before the first apply."
  type        = string
  default     = "/paypilot/decision/api_token"
}

variable "db_instance_class" {
  description = "RDS instance class. Smallest general-purpose Graviton class by default."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_engine_version" {
  description = "Postgres major version. RDS picks the current minor."
  type        = string
  default     = "16"
}

variable "lambda_zip_path" {
  description = "Path to the zip built by build_lambda.sh."
  type        = string
  default     = "build/lambda.zip"
}

variable "throttle_rate_per_second" {
  description = "Steady-state request cap on the HTTP API stage. Bounds the cost of anyone hammering a leaked URL."
  type        = number
  default     = 5
}

variable "throttle_burst" {
  description = "Burst request cap on the HTTP API stage."
  type        = number
  default     = 10
}

variable "budget_alert_email" {
  description = "Address that receives the monthly budget alert and the security alarms (bad tokens, throttling). Leave empty to skip the budget and the email topic."
  type        = string
  default     = ""
}

variable "monthly_budget_usd" {
  description = "Monthly cost budget for the account, in USD (the unit AWS Budgets bills in)."
  type        = number
  default     = 30
}
