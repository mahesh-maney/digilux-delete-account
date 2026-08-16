# ── Digilux internal environment ──────────────────────────────────────────────
# Deploy: make deploy ENV=digilux

env                  = "digilux"
aws_region           = "ap-south-1"
cognito_user_pool_id = "ap-south-1_KJpJMEzyM"
table_prefix         = "digilux_honeywell"
bucket_prefix        = "digilux-honeywell"
lambda_runtime       = "python3.12"
phase1_timeout       = 29
phase2_timeout       = 300
lambda_memory_mb     = 512
archive_expiry_days  = 2555   # ~7 years
admin_api_key_value  = ""     # leave empty → AWS auto-generates
alert_email          = ""     # set to your ops email to enable alarms

tags = {
  Client  = "internal"
  Product = "digilux-platform"
  Owner   = "platform-team"
}
