# ── API endpoints ──────────────────────────────────────────────────────────────

output "api_base_url" {
  description = "Base URL of the Account Deletion REST API"
  value       = "https://${aws_api_gateway_rest_api.main.id}.execute-api.${var.aws_region}.amazonaws.com/${aws_api_gateway_stage.main.stage_name}"
}

output "phase1_endpoint" {
  description = "DELETE /account — user-facing deletion endpoint"
  value       = "https://${aws_api_gateway_rest_api.main.id}.execute-api.${var.aws_region}.amazonaws.com/${aws_api_gateway_stage.main.stage_name}/account"
}

output "phase2_admin_endpoint" {
  description = "POST /admin/archive — admin force-archive endpoint"
  value       = "https://${aws_api_gateway_rest_api.main.id}.execute-api.${var.aws_region}.amazonaws.com/${aws_api_gateway_stage.main.stage_name}/admin/archive"
}

# ── Admin API key ──────────────────────────────────────────────────────────────

output "admin_api_key" {
  description = "X-Api-Key value for POST /admin/archive. Treat as a secret."
  value       = aws_api_gateway_api_key.admin.value
  sensitive   = true
}

# ── Lambda ─────────────────────────────────────────────────────────────────────

output "phase1_lambda_name" {
  description = "Phase 1 Lambda function name"
  value       = aws_lambda_function.phase1.function_name
}

output "phase1_lambda_arn" {
  description = "Phase 1 Lambda ARN"
  value       = aws_lambda_function.phase1.arn
}

output "phase2_lambda_name" {
  description = "Phase 2 Lambda function name"
  value       = aws_lambda_function.phase2.function_name
}

output "phase2_lambda_arn" {
  description = "Phase 2 Lambda ARN"
  value       = aws_lambda_function.phase2.arn
}

# ── Storage ────────────────────────────────────────────────────────────────────

output "archive_bucket" {
  description = "S3 archive bucket name"
  value       = aws_s3_bucket.archive.bucket
}

output "deletion_audit_table" {
  description = "DynamoDB deletion audit table name"
  value       = aws_dynamodb_table.deletion_audit.name
}

# ── Monitoring ─────────────────────────────────────────────────────────────────

output "phase1_log_group" {
  description = "CloudWatch log group for Phase 1 Lambda"
  value       = aws_cloudwatch_log_group.phase1.name
}

output "phase2_log_group" {
  description = "CloudWatch log group for Phase 2 Lambda"
  value       = aws_cloudwatch_log_group.phase2.name
}

output "eventbridge_schedule_name" {
  description = "EventBridge Scheduler rule name (daily sweep)"
  value       = aws_scheduler_schedule.daily_sweep.name
}
