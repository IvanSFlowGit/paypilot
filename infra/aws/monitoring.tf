# Alarms for the two failure modes the 15 September security pass measured:
# callers without a valid token, and Lambda throttling (a new account's
# concurrency limit of 10 turned a 40-request burst into 28 throttles, served
# as 503). Both notify the same address as the budget, when one is supplied.
# Without an address the alarms still exist and show in the console.

locals {
  alert_actions = var.budget_alert_email == "" ? [] : [aws_sns_topic.alerts[0].arn]
}

resource "aws_sns_topic" "alerts" {
  count = var.budget_alert_email == "" ? 0 : 1
  name  = "${var.project}-alerts"
}

# AWS emails a confirmation link; nothing is delivered until it is clicked.
resource "aws_sns_topic_subscription" "alerts_email" {
  count     = var.budget_alert_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts[0].arn
  protocol  = "email"
  endpoint  = var.budget_alert_email
}

# The Lambda runtime prefixes each line with level, time and request id, so the
# JSON body is not the whole message and a JSON filter pattern would never
# match. A quoted term matches the event name inside the line.
resource "aws_cloudwatch_log_metric_filter" "token_rejected" {
  name           = "${var.project}-token-rejected"
  log_group_name = aws_cloudwatch_log_group.lambda.name
  pattern        = "\"decision_token_rejected\""

  metric_transformation {
    name          = "DecisionTokenRejected"
    namespace     = "PayPilot/Decision"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "token_rejected" {
  alarm_name          = "${var.project}-token-rejected"
  alarm_description   = "20 or more requests with a missing or wrong bearer token in 5 minutes."
  namespace           = "PayPilot/Decision"
  metric_name         = "DecisionTokenRejected"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 20
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alert_actions
}

resource "aws_cloudwatch_metric_alarm" "lambda_throttles" {
  alarm_name          = "${var.project}-throttles"
  alarm_description   = "Decision Lambda throttled: callers are getting 503."
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  dimensions          = { FunctionName = aws_lambda_function.decision.function_name }
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alert_actions
}
