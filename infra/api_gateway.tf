# ── REST API ───────────────────────────────────────────────────────────────────

resource "aws_api_gateway_rest_api" "main" {
  name        = local.api_name
  description = "Digilux Honeywell Account Deletion API (${var.env})"

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}


# ── Cognito Authorizer (used by DELETE /account) ───────────────────────────────

resource "aws_api_gateway_authorizer" "cognito" {
  name            = "${local.name_prefix}-cognito-authorizer"
  rest_api_id     = aws_api_gateway_rest_api.main.id
  type            = "COGNITO_USER_POOLS"
  identity_source = "method.request.header.Authorization"
  provider_arns   = [local.user_pool_arn]
}


# ── /account ────────────────────────────────────────────────────────────────────

resource "aws_api_gateway_resource" "account" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_rest_api.main.root_resource_id
  path_part   = "account"
}

# DELETE /account — user-facing, protected by Cognito Authorizer
resource "aws_api_gateway_method" "delete_account" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.account.id
  http_method   = "DELETE"
  authorization = "COGNITO_USER_POOLS"
  authorizer_id = aws_api_gateway_authorizer.cognito.id
}

resource "aws_api_gateway_integration" "delete_account" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.account.id
  http_method             = aws_api_gateway_method.delete_account.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.phase1.invoke_arn
}

# OPTIONS /account — CORS preflight
resource "aws_api_gateway_method" "options_account" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.account.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "options_account" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  resource_id = aws_api_gateway_resource.account.id
  http_method = aws_api_gateway_method.options_account.http_method
  type        = "MOCK"
  request_templates = {
    "application/json" = "{\"statusCode\": 200}"
  }
}

resource "aws_api_gateway_method_response" "options_account_200" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  resource_id = aws_api_gateway_resource.account.id
  http_method = aws_api_gateway_method.options_account.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Origin"  = true
  }
}

resource "aws_api_gateway_integration_response" "options_account_200" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  resource_id = aws_api_gateway_resource.account.id
  http_method = aws_api_gateway_method.options_account.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = "'Authorization,Content-Type'"
    "method.response.header.Access-Control-Allow-Methods" = "'DELETE,OPTIONS'"
    "method.response.header.Access-Control-Allow-Origin"  = "'*'"
  }
  depends_on = [aws_api_gateway_integration.options_account]
}


# ── /admin/archive ──────────────────────────────────────────────────────────────

resource "aws_api_gateway_resource" "admin" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_rest_api.main.root_resource_id
  path_part   = "admin"
}

resource "aws_api_gateway_resource" "admin_archive" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  parent_id   = aws_api_gateway_resource.admin.id
  path_part   = "archive"
}

# POST /admin/archive — admin-only, protected by API key
resource "aws_api_gateway_method" "post_admin_archive" {
  rest_api_id      = aws_api_gateway_rest_api.main.id
  resource_id      = aws_api_gateway_resource.admin_archive.id
  http_method      = "POST"
  authorization    = "NONE"
  api_key_required = true
}

resource "aws_api_gateway_integration" "post_admin_archive" {
  rest_api_id             = aws_api_gateway_rest_api.main.id
  resource_id             = aws_api_gateway_resource.admin_archive.id
  http_method             = aws_api_gateway_method.post_admin_archive.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.phase2.invoke_arn
}

# OPTIONS /admin/archive — CORS preflight
resource "aws_api_gateway_method" "options_admin_archive" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  resource_id   = aws_api_gateway_resource.admin_archive.id
  http_method   = "OPTIONS"
  authorization = "NONE"
}

resource "aws_api_gateway_integration" "options_admin_archive" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  resource_id = aws_api_gateway_resource.admin_archive.id
  http_method = aws_api_gateway_method.options_admin_archive.http_method
  type        = "MOCK"
  request_templates = {
    "application/json" = "{\"statusCode\": 200}"
  }
}

resource "aws_api_gateway_method_response" "options_admin_archive_200" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  resource_id = aws_api_gateway_resource.admin_archive.id
  http_method = aws_api_gateway_method.options_admin_archive.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
    "method.response.header.Access-Control-Allow-Origin"  = true
  }
}

resource "aws_api_gateway_integration_response" "options_admin_archive_200" {
  rest_api_id = aws_api_gateway_rest_api.main.id
  resource_id = aws_api_gateway_resource.admin_archive.id
  http_method = aws_api_gateway_method.options_admin_archive.http_method
  status_code = "200"
  response_parameters = {
    "method.response.header.Access-Control-Allow-Headers" = "'Authorization,Content-Type,X-Api-Key'"
    "method.response.header.Access-Control-Allow-Methods" = "'POST,OPTIONS'"
    "method.response.header.Access-Control-Allow-Origin"  = "'*'"
  }
  depends_on = [aws_api_gateway_integration.options_admin_archive]
}


# ── Lambda invoke permissions ───────────────────────────────────────────────────

resource "aws_lambda_permission" "phase1_apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.phase1.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.main.execution_arn}/*/*"
}

resource "aws_lambda_permission" "phase2_apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.phase2.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.main.execution_arn}/*/*"
}


# ── Deployment & Stage ──────────────────────────────────────────────────────────
# Redeploy any time a method or integration changes.

resource "aws_api_gateway_deployment" "main" {
  rest_api_id = aws_api_gateway_rest_api.main.id

  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_method.delete_account,
      aws_api_gateway_method.post_admin_archive,
      aws_api_gateway_integration.delete_account,
      aws_api_gateway_integration.post_admin_archive,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_api_gateway_integration.delete_account,
    aws_api_gateway_integration.post_admin_archive,
    aws_api_gateway_integration.options_account,
    aws_api_gateway_integration.options_admin_archive,
  ]
}

resource "aws_api_gateway_stage" "main" {
  rest_api_id   = aws_api_gateway_rest_api.main.id
  deployment_id = aws_api_gateway_deployment.main.id
  stage_name    = var.env

  tags = {
    Name = "${local.api_name}-${var.env}"
  }
}


# ── Admin API Key ───────────────────────────────────────────────────────────────

resource "aws_api_gateway_api_key" "admin" {
  name    = "${local.name_prefix}-admin-key"
  enabled = true
  # If var.admin_api_key_value is empty, AWS generates the value automatically
  value = var.admin_api_key_value != "" ? var.admin_api_key_value : null
}

resource "aws_api_gateway_usage_plan" "admin" {
  name        = "${local.name_prefix}-admin-plan"
  description = "Rate-limited plan for admin force-archive endpoint"

  api_stages {
    api_id = aws_api_gateway_rest_api.main.id
    stage  = aws_api_gateway_stage.main.stage_name
  }

  throttle_settings {
    rate_limit  = 10
    burst_limit = 5
  }
}

resource "aws_api_gateway_usage_plan_key" "admin" {
  key_id        = aws_api_gateway_api_key.admin.id
  key_type      = "API_KEY"
  usage_plan_id = aws_api_gateway_usage_plan.admin.id
}
