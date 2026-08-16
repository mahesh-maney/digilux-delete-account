# ── Phase 1 Lambda IAM ─────────────────────────────────────────────────────────

resource "aws_iam_role" "phase1" {
  name = "${local.name_prefix}-phase1-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "phase1_basic_exec" {
  role       = aws_iam_role.phase1.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "phase1" {
  name = "${local.name_prefix}-phase1-policy"
  role = aws_iam_role.phase1.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "CognitoRevoke"
        Effect = "Allow"
        Action = [
          "cognito-idp:AdminUserGlobalSignOut",
          "cognito-idp:AdminDeleteUser",
        ]
        Resource = local.user_pool_arn
      },
      {
        Sid    = "UserDataReadUpdate"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:UpdateItem",
        ]
        Resource = "${local.table_arn_prefix}/${local.table.user_data}"
      },
      {
        Sid    = "DeviceDataQueryUpdate"
        Effect = "Allow"
        Action = [
          "dynamodb:Query",
          "dynamodb:UpdateItem",
        ]
        Resource = [
          "${local.table_arn_prefix}/${local.table.device_data}",
          "${local.table_arn_prefix}/${local.table.device_data}/index/*",
        ]
      },
      {
        Sid    = "DeletionAuditWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
        ]
        Resource = aws_dynamodb_table.deletion_audit.arn
      },
    ]
  })
}


# ── Phase 2 Lambda IAM ─────────────────────────────────────────────────────────

resource "aws_iam_role" "phase2" {
  name = "${local.name_prefix}-phase2-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "phase2_basic_exec" {
  role       = aws_iam_role.phase2.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_iam_role_policy" "phase2" {
  name = "${local.name_prefix}-phase2-policy"
  role = aws_iam_role.phase2.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      # Read all 16 tables (resolve step)
      {
        Sid    = "DynamoDBReadAll"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:Query",
          "dynamodb:Scan",
        ]
        Resource = [
          for name in values(local.table) :
          "${local.table_arn_prefix}/${name}"
        ]
      },
      # Query GSIs on all tables
      {
        Sid    = "DynamoDBQueryGSI"
        Effect = "Allow"
        Action = ["dynamodb:Query"]
        Resource = [
          for name in values(local.table) :
          "${local.table_arn_prefix}/${name}/index/*"
        ]
      },
      # Delete from all tables except user_data (which is kept as audit trail)
      {
        Sid    = "DynamoDBDeleteAll"
        Effect = "Allow"
        Action = ["dynamodb:DeleteItem"]
        Resource = [
          for key, name in local.table :
          "${local.table_arn_prefix}/${name}"
          if key != "user_data"
        ]
      },
      # Update user_data (clear archivePending) and deletion_audit (update status)
      {
        Sid    = "DynamoDBUpdateAuditAndUserData"
        Effect = "Allow"
        Action = ["dynamodb:UpdateItem"]
        Resource = [
          "${local.table_arn_prefix}/${local.table.user_data}",
          aws_dynamodb_table.deletion_audit.arn,
        ]
      },
      # S3 archive bucket — full archive read/write
      {
        Sid    = "S3ArchiveReadWrite"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:GetObject",
          "s3:HeadObject",
          "s3:DeleteObject",
          "s3:DeleteObjects",
        ]
        Resource = "${aws_s3_bucket.archive.arn}/*"
      },
      {
        Sid      = "S3ArchiveList"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = aws_s3_bucket.archive.arn
      },
      # S3 metadata bucket — read + delete source objects + list
      {
        Sid    = "S3MetadataReadDelete"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:CopyObject",
          "s3:DeleteObject",
          "s3:DeleteObjects",
          "s3:ListBucket",
        ]
        Resource = [
          "arn:aws:s3:::${local.metadata_bucket}",
          "arn:aws:s3:::${local.metadata_bucket}/*",
        ]
      },
    ]
  })
}


# ── EventBridge Scheduler IAM ──────────────────────────────────────────────────

resource "aws_iam_role" "eventbridge_scheduler" {
  name = "${local.name_prefix}-scheduler-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy" "eventbridge_scheduler" {
  name = "${local.name_prefix}-scheduler-policy"
  role = aws_iam_role.eventbridge_scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.phase2.arn
    }]
  })
}
