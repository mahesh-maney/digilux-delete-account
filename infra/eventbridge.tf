# ── EventBridge Scheduler — daily INACTIVE sweep ───────────────────────────────
# Runs at 02:00 UTC every day.
# Scans digilux_honeywell_user_data for status=INACTIVE + archivePending=true
# and runs the full Phase 2 archive pipeline for each user found.

resource "aws_scheduler_schedule" "daily_sweep" {
  name       = local.sweep_schedule_name
  group_name = "default"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = "cron(0 2 * * ? *)"
  schedule_expression_timezone = "UTC"
  description                  = "Digilux account deletion — daily sweep for INACTIVE users pending archive (${var.env})"

  target {
    arn      = aws_lambda_function.phase2.arn
    role_arn = aws_iam_role.eventbridge_scheduler.arn

    input = jsonencode({
      source = "aws.scheduler"
    })

    retry_policy {
      maximum_retry_attempts = 2
    }
  }
}

resource "aws_lambda_permission" "phase2_eventbridge" {
  statement_id  = "AllowEventBridgeSchedulerInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.phase2.function_name
  principal     = "scheduler.amazonaws.com"
  source_arn    = aws_scheduler_schedule.daily_sweep.arn
}
