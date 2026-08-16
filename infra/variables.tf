# ── Environment ────────────────────────────────────────────────────────────────

variable "env" {
  description = "Deployment environment identifier, e.g. digilux | honeywell | staging"
  type        = string
}

variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "ap-south-1"
}

# ── Cognito ─────────────────────────────────────────────────────────────────────

variable "cognito_user_pool_id" {
  description = "Cognito User Pool ID for this environment (e.g. ap-south-1_KJpJMEzyM)"
  type        = string
}

# ── Naming ──────────────────────────────────────────────────────────────────────

variable "table_prefix" {
  description = "Prefix shared by all DynamoDB table names (e.g. digilux_honeywell)"
  type        = string
  default     = "digilux_honeywell"
}

variable "bucket_prefix" {
  description = "Prefix shared by all S3 bucket names (e.g. digilux-honeywell)"
  type        = string
  default     = "digilux-honeywell"
}

# ── Lambda ──────────────────────────────────────────────────────────────────────

variable "lambda_runtime" {
  description = "Lambda Python runtime"
  type        = string
  default     = "python3.12"
}

variable "phase1_timeout" {
  description = "Phase 1 Lambda timeout in seconds (API Gateway hard limit is 29 s)"
  type        = number
  default     = 29
}

variable "phase2_timeout" {
  description = "Phase 2 Lambda timeout in seconds (archive + delete pipeline)"
  type        = number
  default     = 300
}

variable "lambda_memory_mb" {
  description = "Memory allocated to both Lambda functions in MB"
  type        = number
  default     = 512
}

# ── S3 archive ──────────────────────────────────────────────────────────────────

variable "archive_expiry_days" {
  description = "Days after which archived objects expire from the archive bucket. Set 0 to never expire."
  type        = number
  default     = 2555 # ~7 years
}

# ── Admin endpoint ──────────────────────────────────────────────────────────────

variable "admin_api_key_value" {
  description = "Value for the admin force-archive API key. Leave empty to let AWS auto-generate one."
  type        = string
  default     = ""
  sensitive   = true
}

# ── Alerting ────────────────────────────────────────────────────────────────────

variable "alert_email" {
  description = "Email address for CloudWatch alarm notifications. Leave empty to skip alarm creation."
  type        = string
  default     = ""
}

# ── Tags ────────────────────────────────────────────────────────────────────────

variable "tags" {
  description = "Additional tags to merge onto every resource"
  type        = map(string)
  default     = {}
}
