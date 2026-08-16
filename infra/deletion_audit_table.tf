# This file has been superseded by the modular structure introduced in v1.0.
# Resources have been moved to:
#   dynamodb.tf      — aws_dynamodb_table.deletion_audit
#   s3.tf            — aws_s3_bucket.archive (+ versioning, encryption, lifecycle)
#   iam.tf           — all IAM roles and policies
#   eventbridge.tf   — aws_scheduler_schedule.daily_sweep
#   lambda.tf        — aws_lambda_function.phase1 + phase2
#   api_gateway.tf   — REST API, routes, authorizer, deployment
#   cloudwatch.tf    — log groups, metric filters, alarms
#   outputs.tf       — all outputs
