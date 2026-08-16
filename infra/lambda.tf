# ── Lambda code packaging ──────────────────────────────────────────────────────
# archive_file zips the source directories at plan/apply time.
# source_code_hash ensures Lambda is only re-deployed when code actually changes.

data "archive_file" "phase1" {
  type        = "zip"
  source_dir  = "${path.root}/../phase1"
  output_path = "${path.root}/.builds/phase1.zip"
}

data "archive_file" "phase2" {
  type        = "zip"
  source_dir  = "${path.root}/../phase2"
  output_path = "${path.root}/.builds/phase2.zip"
}


# ── Phase 1 Lambda ─────────────────────────────────────────────────────────────
# Triggered by DELETE /account (user-facing).
# Revokes access immediately; marks user INACTIVE + archivePending=true.

resource "aws_lambda_function" "phase1" {
  function_name    = local.phase1_name
  role             = aws_iam_role.phase1.arn
  runtime          = var.lambda_runtime
  handler          = "lambda_function.lambda_handler"
  filename         = data.archive_file.phase1.output_path
  source_code_hash = data.archive_file.phase1.output_base64sha256
  timeout          = var.phase1_timeout
  memory_size      = var.lambda_memory_mb

  environment {
    variables = {
      USER_POOL_ID         = var.cognito_user_pool_id
      TABLE_USER_DATA      = local.table.user_data
      TABLE_DEVICE_DATA    = local.table.device_data
      TABLE_DELETION_AUDIT = local.table.deletion_audit
    }
  }

  # Log group must exist before Lambda tries to write to it
  depends_on = [aws_cloudwatch_log_group.phase1]
}


# ── Phase 2 Lambda ─────────────────────────────────────────────────────────────
# Triggered by EventBridge daily sweep OR admin POST /admin/archive.
# Runs the full archive → verify → hard-delete pipeline.

resource "aws_lambda_function" "phase2" {
  function_name    = local.phase2_name
  role             = aws_iam_role.phase2.arn
  runtime          = var.lambda_runtime
  handler          = "lambda_function.lambda_handler"
  filename         = data.archive_file.phase2.output_path
  source_code_hash = data.archive_file.phase2.output_base64sha256
  timeout          = var.phase2_timeout
  memory_size      = var.lambda_memory_mb

  environment {
    variables = {
      TABLE_USER_DATA                  = local.table.user_data
      TABLE_DEVICE_DATA                = local.table.device_data
      TABLE_SCENE_DATA                 = local.table.scene_data
      TABLE_USER_DEVICE_DETAILS        = local.table.user_device_details
      TABLE_USER_DEVICE_MAPPING        = local.table.user_device_mapping
      TABLE_USER_SUBUSER_DETAIL        = local.table.user_subuser_detail
      TABLE_USER_SUBUSER_MAPPING       = local.table.user_subuser_mapping
      TABLE_SUBUSER_ROLE_DATA          = local.table.subuser_role_data
      TABLE_ADMIN_OTP_DATA             = local.table.admin_otp_data
      TABLE_ALEXA_LWA_TOKENS           = local.table.alexa_lwa_tokens
      TABLE_DEVICE_STATE               = local.table.device_state
      TABLE_ENTITY_STATE               = local.table.entity_state
      TABLE_AUTOMATION_EVENT           = local.table.automation_event
      TABLE_AUTOMATION_SCHEDULE_DIRECT = local.table.automation_schedule_direct
      TABLE_AUTOMATION_SCHEDULE_CTRL   = local.table.automation_schedule_controller
      TABLE_DELETION_AUDIT             = local.table.deletion_audit
      ARCHIVE_BUCKET                   = local.archive_bucket
      METADATA_BUCKET                  = local.metadata_bucket
    }
  }

  depends_on = [aws_cloudwatch_log_group.phase2]
}
