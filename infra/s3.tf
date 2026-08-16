# ── Archive bucket ─────────────────────────────────────────────────────────────
# Stores all archived user data before hard-delete in Phase 2.
# Versioned + AES256 encrypted + fully private.

resource "aws_s3_bucket" "archive" {
  bucket = local.archive_bucket

  lifecycle {
    prevent_destroy = true
  }

  tags = {
    Name = local.archive_bucket
  }
}

resource "aws_s3_bucket_versioning" "archive" {
  bucket = aws_s3_bucket.archive.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "archive" {
  bucket = aws_s3_bucket.archive.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "archive" {
  bucket = aws_s3_bucket.archive.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Lifecycle: expire archives after var.archive_expiry_days (default ~7 years)
resource "aws_s3_bucket_lifecycle_configuration" "archive" {
  count  = var.archive_expiry_days > 0 ? 1 : 0
  bucket = aws_s3_bucket.archive.id

  rule {
    id     = "expire-old-archives"
    status = "Enabled"

    filter {
      prefix = "archive/"
    }

    expiration {
      days = var.archive_expiry_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}
