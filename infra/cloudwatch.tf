# ── Log groups ─────────────────────────────────────────────────────────────────

resource "aws_cloudwatch_log_group" "phase1" {
  name              = "/aws/lambda/${local.phase1_name}"
  retention_in_days = 90
}

resource "aws_cloudwatch_log_group" "phase2" {
  name              = "/aws/lambda/${local.phase2_name}"
  retention_in_days = 90
}


# ── SNS topic for alerts (created only if alert_email is provided) ─────────────

resource "aws_sns_topic" "alerts" {
  count = var.alert_email != "" ? 1 : 0
  name  = "${local.name_prefix}-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  count     = var.alert_email != "" ? 1 : 0
  topic_arn = aws_sns_topic.alerts[0].arn
  protocol  = "email"
  endpoint  = var.alert_email
}

locals {
  alert_topic_arn = var.alert_email != "" ? aws_sns_topic.alerts[0].arn : null
}


# ── Alarm: Phase 1 Lambda errors ───────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "phase1_errors" {
  count               = var.alert_email != "" ? 1 : 0
  alarm_name          = "${local.name_prefix}-phase1-errors"
  alarm_description   = "Phase 1 account deletion Lambda has errors"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 2
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.alert_topic_arn]

  dimensions = {
    FunctionName = aws_lambda_function.phase1.function_name
  }
}


# ── Alarm: Phase 2 Lambda errors ───────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "phase2_errors" {
  count               = var.alert_email != "" ? 1 : 0
  alarm_name          = "${local.name_prefix}-phase2-errors"
  alarm_description   = "Phase 2 archive worker Lambda has errors — user data may not be deleted"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Errors"
  namespace           = "AWS/Lambda"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.alert_topic_arn]

  dimensions = {
    FunctionName = aws_lambda_function.phase2.function_name
  }
}


# ── Metric filter: archive verification failures ────────────────────────────────
# Fires whenever the Phase 2 log contains "verification failed".
# This means hard-delete was aborted — source data is still alive.

resource "aws_cloudwatch_log_metric_filter" "verification_failures" {
  name           = "${local.name_prefix}-verify-failures"
  pattern        = "\"verification failed\""
  log_group_name = aws_cloudwatch_log_group.phase2.name

  metric_transformation {
    name          = "VerificationFailures"
    namespace     = "DigiluxDeletion/${var.env}"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "verification_failures" {
  count               = var.alert_email != "" ? 1 : 0
  alarm_name          = "${local.name_prefix}-verification-failures"
  alarm_description   = "Archive verification failed — hard-delete ABORTED, source data is preserved. Investigate immediately."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "VerificationFailures"
  namespace           = "DigiluxDeletion/${var.env}"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.alert_topic_arn]
}


# ── Metric filter: PHASE2_PARTIAL audit events ─────────────────────────────────
# Fires when Phase 2 completes but some individual deletes failed.

resource "aws_cloudwatch_log_metric_filter" "partial_deletes" {
  name           = "${local.name_prefix}-partial-deletes"
  pattern        = "\"HARD_DELETE_PARTIAL\""
  log_group_name = aws_cloudwatch_log_group.phase2.name

  metric_transformation {
    name          = "PartialDeletes"
    namespace     = "DigiluxDeletion/${var.env}"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "partial_deletes" {
  count               = var.alert_email != "" ? 1 : 0
  alarm_name          = "${local.name_prefix}-partial-deletes"
  alarm_description   = "Phase 2 completed with partial failures — some rows were not deleted"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "PartialDeletes"
  namespace           = "DigiluxDeletion/${var.env}"
  period              = 300
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = [local.alert_topic_arn]
}
