locals {
  # ── Resource name prefix ──────────────────────────────────────────────────────
  name_prefix = "${var.env}-delete-account"

  # ── DynamoDB table names ──────────────────────────────────────────────────────
  # All tables with prefix _except_ deletion_audit (which this module creates).
  # The rest already exist in each environment — referenced only for IAM ARNs.
  table = {
    user_data                      = "${var.table_prefix}_user_data"
    device_data                    = "${var.table_prefix}_device_data"
    scene_data                     = "${var.table_prefix}_scene_data"
    user_device_details            = "${var.table_prefix}_user_device_details"
    user_device_mapping            = "${var.table_prefix}_user_device_mapping"
    user_subuser_detail            = "${var.table_prefix}_user_subuser_detail"
    user_subuser_mapping           = "${var.table_prefix}_user_subuser_mapping"
    subuser_role_data              = "${var.table_prefix}_subuser_role_data"
    admin_otp_data                 = "${var.table_prefix}_admin_otp_data"
    alexa_lwa_tokens               = "${var.table_prefix}_alexa_lwa_tokens"
    device_state                   = "${var.table_prefix}_device_state"
    entity_state                   = "${var.table_prefix}_entity_state"
    automation_event               = "${var.table_prefix}_automation_event"
    automation_schedule_direct     = "${var.table_prefix}_automation_schedule_direct"
    automation_schedule_controller = "${var.table_prefix}_automation_schedule_controller"
    deletion_audit                 = "${var.table_prefix}_deletion_audit"
  }

  # Helper: ARN pattern for any table in this region/account
  table_arn_prefix = "arn:aws:dynamodb:${var.aws_region}:${data.aws_caller_identity.current.account_id}:table"

  # ── S3 bucket names ────────────────────────────────────────────────────────────
  archive_bucket  = "${var.bucket_prefix}-archive"
  metadata_bucket = "${var.bucket_prefix}-metadata"

  # ── Lambda function names ──────────────────────────────────────────────────────
  phase1_name = "${local.name_prefix}-phase1"
  phase2_name = "${local.name_prefix}-phase2"

  # ── EventBridge ───────────────────────────────────────────────────────────────
  sweep_schedule_name = "${local.name_prefix}-daily-sweep"

  # ── API Gateway ───────────────────────────────────────────────────────────────
  api_name = "${local.name_prefix}-api"

  # ── Cognito User Pool ARN ─────────────────────────────────────────────────────
  user_pool_arn = "arn:aws:cognito-idp:${var.aws_region}:${data.aws_caller_identity.current.account_id}:userpool/${var.cognito_user_pool_id}"

  # ── Common tags applied to every resource ────────────────────────────────────
  common_tags = merge(var.tags, {
    Environment = var.env
    Feature     = "account-deletion"
    ManagedBy   = "terraform"
    Project     = "digilux-honeywell"
  })
}
