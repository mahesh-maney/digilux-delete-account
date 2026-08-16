# ── deletion_audit ─────────────────────────────────────────────────────────────
# This is the ONLY DynamoDB table created by this module.
# All other tables (user_data, device_data, …) already exist — they are
# referenced via locals.table[*] for IAM policy construction only.
#
# PK: userId      (String)
# SK: requestedAt (String, ISO-8601 UTC)

resource "aws_dynamodb_table" "deletion_audit" {
  name         = local.table.deletion_audit
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

  point_in_time_recovery {
    enabled = true
  }

  # Audit trail must never be accidentally destroyed by a terraform destroy
  lifecycle {
    prevent_destroy = true
  }

  tags = {
    Name = local.table.deletion_audit
  }
}
