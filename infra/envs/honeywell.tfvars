# ── Honeywell client environment ───────────────────────────────────────────────
# Deploy: make deploy ENV=honeywell
#
# Fill in REPLACE_WITH_* values before deploying.

env                  = "honeywell"
aws_region           = "ap-south-1"
cognito_user_pool_id = "REPLACE_WITH_HONEYWELL_USER_POOL_ID"
table_prefix         = "digilux_honeywell"
bucket_prefix        = "digilux-honeywell"
lambda_runtime       = "python3.12"
phase1_timeout       = 29
phase2_timeout       = 300
lambda_memory_mb     = 512
archive_expiry_days  = 2555   # ~7 years
admin_api_key_value  = ""     # leave empty → AWS auto-generates
alert_email          = ""     # set to Honeywell ops email to enable alarms

tags = {
  Client  = "honeywell"
  Product = "digilux-platform"
  Owner   = "platform-team"
}
