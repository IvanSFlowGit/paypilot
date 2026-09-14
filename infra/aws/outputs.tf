output "api_base_url" {
  description = "Base URL for PAYPILOT_CONTRACT_BASE_URL."
  value       = aws_apigatewayv2_stage.default.invoke_url
}

output "function_name" {
  value = aws_lambda_function.decision.function_name
}

output "db_endpoint" {
  description = "Private address; reachable only from inside the VPC."
  value       = aws_db_instance.this.address
}
