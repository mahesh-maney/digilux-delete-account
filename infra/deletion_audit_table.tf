# ── DynamoDB: deletion_audit ───────────────────────────────────────────────────
# Tracks account deletion status across Phase 1 and Phase 2.
# PK: userId (String)
# SK: requestedAt (String, ISO-8601 UTC timestamp)

resource "aws_dynamodb_table" "deletion_audit" {
  name         = "digilux_honeywell_deletion_audit"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "userId"
  range_key    = "requestedAt"

  attribute {
    name = "userId"
    type = "S"
  }

  attribute {
    name = "requestedAt"
    type = "S"
  }

  # Retain the table if Terraform destroys this resource — audit trail must never be lost
  lifecycle {
    prevent_destroy = true
  }

  tags = {
    Project     = "digilux-honeywell"
    Feature     = "delete-account"
    ManagedBy   = "terraform"
  }
}

# ── IAM: grant Phase 1 and Phase 2 Lambdas access ─────────────────────────────

data "aws_iam_policy_document" "deletion_audit_access" {
  statement {
    sid    = "DeletionAuditReadWrite"
    effect = "Allow"
    actions = [
      "dynamodb:PutItem",
      "dynamodb:UpdateItem",
      "dynamodb:GetItem",
      "dynamodb:Query",
    ]
    resources = [
      aws_dynamodb_table.deletion_audit.arn,
    ]
  }
}

resource "aws_iam_policy" "deletion_audit_access" {
  name        = "digilux-honeywell-deletion-audit-access"
  description = "Grants Phase 1 and Phase 2 delete-account Lambdas read/write on deletion_audit"
  policy      = data.aws_iam_policy_document.deletion_audit_access.json
}

# Attach to Phase 1 Lambda role
resource "aws_iam_role_policy_attachment" "phase1_deletion_audit" {
  role       = aws_iam_role.phase1_delete_account_role.name   # replace with your Phase 1 role resource
  policy_arn = aws_iam_policy.deletion_audit_access.arn
}

# Attach to Phase 2 Lambda role
resource "aws_iam_role_policy_attachment" "phase2_deletion_audit" {
  role       = aws_iam_role.phase2_archive_worker_role.name   # replace with your Phase 2 role resource
  policy_arn = aws_iam_policy.deletion_audit_access.arn
}


# ── S3: archive bucket (new) ───────────────────────────────────────────────────
# Stores archived user data before hard-delete in Phase 2.

resource "aws_s3_bucket" "honeywell_archive" {
  bucket = "digilux-honeywell-archive"

  lifecycle {
    prevent_destroy = true
  }

  tags = {
    Project   = "digilux-honeywell"
    Feature   = "delete-account"
    ManagedBy = "terraform"
  }
}

resource "aws_s3_bucket_versioning" "honeywell_archive" {
  bucket = aws_s3_bucket.honeywell_archive.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "honeywell_archive" {
  bucket = aws_s3_bucket.honeywell_archive.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Block all public access
resource "aws_s3_bucket_public_access_block" "honeywell_archive" {
  bucket                  = aws_s3_bucket.honeywell_archive.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}


# ── EventBridge Scheduler: daily INACTIVE sweep ────────────────────────────────

resource "aws_scheduler_schedule" "deletion_sweep" {
  name       = "digilux-honeywell-deletion-archive-sweep"
  group_name = "default"

  flexible_time_window {
    mode = "OFF"
  }

  # Runs daily at 02:00 UTC
  schedule_expression = "cron(0 2 * * ? *)"

  target {
    arn      = aws_lambda_function.phase2_archive_worker.arn   # replace with your Phase 2 Lambda resource
    role_arn = aws_iam_role.eventbridge_scheduler_role.arn     # replace with your EventBridge role resource

    input = jsonencode({
      source = "aws.scheduler"
    })
  }
}
