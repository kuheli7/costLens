locals {
  account_id  = data.aws_caller_identity.current.account_id
  bucket_name = "${var.project}-${local.account_id}-${var.aws_region}" # no account ID in code: it is derived here
  common_env = {
    COSTLENS_BUCKET       = aws_s3_bucket.data.bucket
    COSTLENS_CUR_PREFIX   = "cur"
    COSTLENS_STATE_KEY    = "state/costlens.sqlite"
    COSTLENS_SLACK_SECRET = aws_secretsmanager_secret.slack.name
    COSTLENS_LLM_SECRET   = aws_secretsmanager_secret.llm.name
  }
}

# --------------------------------------------------------------------------- storage

resource "aws_s3_bucket" "data" {
  bucket = local.bucket_name
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    id     = "expire-old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

# Lets AWS deliver Cost and Usage Report / Data Exports files into the "cur/" prefix.
# The principals and conditions follow the AWS documentation for Data Exports delivery
# and have not been exercised against a live account yet.
data "aws_iam_policy_document" "billing_delivery" {
  statement {
    sid    = "BillingDeliveryRead"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["billingreports.amazonaws.com", "bcm-data-exports.amazonaws.com"]
    }
    actions   = ["s3:GetBucketAcl", "s3:GetBucketPolicy"]
    resources = [aws_s3_bucket.data.arn]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }

  statement {
    sid    = "BillingDeliveryWrite"
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["billingreports.amazonaws.com", "bcm-data-exports.amazonaws.com"]
    }
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.data.arn}/cur/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }

  statement {
    sid     = "DenyInsecureTransport"
    effect  = "Deny"
    actions = ["s3:*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    resources = [aws_s3_bucket.data.arn, "${aws_s3_bucket.data.arn}/*"]
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "data" {
  bucket = aws_s3_bucket.data.id
  policy = data.aws_iam_policy_document.billing_delivery.json

  depends_on = [aws_s3_bucket_public_access_block.data]
}

# --------------------------------------------------------------------------- secrets
# Containers only. Put the values in by hand (aws secretsmanager put-secret-value) so they
# never appear in Terraform state or code.

resource "aws_secretsmanager_secret" "slack" {
  name                    = "${var.project}/slack"
  description             = "JSON: {\"bot_token\": \"xoxb-...\", \"signing_secret\": \"...\"}"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "llm" {
  name                    = "${var.project}/llm"
  description             = "JSON: {\"api_key\": \"...\"} for the Claude API (only used when enable_ai is true)"
  recovery_window_in_days = 7
}

# --------------------------------------------------------------------------- IAM (read-only)

data "aws_iam_policy_document" "assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.project}-lambda"
  assume_role_policy = data.aws_iam_policy_document.assume.json
}

data "aws_iam_policy_document" "lambda" {
  # Read-only on the account. There is no write permission outside the tool's own bucket.
  statement {
    sid = "ReadBilling"
    actions = [
      "ce:Get*",
      "ce:Describe*",
      "ce:List*",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "ReadCloudTrailAndMetrics"
    actions   = ["cloudtrail:LookupEvents", "cloudwatch:GetMetricData", "cloudwatch:GetMetricStatistics", "cloudwatch:ListMetrics"]
    resources = ["*"]
  }

  statement {
    sid = "DescribeResources"
    actions = [
      "ec2:Describe*",
      "autoscaling:Describe*",
      "elasticloadbalancing:Describe*",
      "rds:Describe*",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "OwnBucketObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.data.arn}/normalized/*", "${aws_s3_bucket.data.arn}/raw/*", "${aws_s3_bucket.data.arn}/state/*"]
  }

  statement {
    sid       = "ReadCur"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.data.arn}/cur/*"]
  }

  statement {
    sid       = "ListOwnBucket"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.data.arn]
  }

  statement {
    sid       = "ReadOwnSecrets"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.slack.arn, aws_secretsmanager_secret.llm.arn]
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${var.project}-read-only"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}

resource "aws_iam_role_policy_attachment" "logs" {
  role       = aws_iam_role.lambda.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# --------------------------------------------------------------------------- Lambdas

resource "aws_lambda_function" "ingest" {
  function_name    = "${var.project}-ingest"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "costlens.handlers.lambda_ingest"
  filename         = var.lambda_package
  source_code_hash = filebase64sha256(var.lambda_package)
  timeout          = 300
  memory_size      = 1024

  environment {
    variables = local.common_env
  }

  depends_on = [aws_cloudwatch_log_group.ingest]
}

resource "aws_lambda_function" "digest" {
  function_name    = "${var.project}-digest"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "costlens.handlers.lambda_digest"
  filename         = var.lambda_package
  source_code_hash = filebase64sha256(var.lambda_package)
  timeout          = 300
  memory_size      = 1024

  environment {
    variables = merge(local.common_env, {
      COSTLENS_SLACK_CHANNEL = var.slack_channel
      COSTLENS_AI            = var.enable_ai ? "on" : "off"
      COSTLENS_TOKEN_BUDGET  = tostring(var.token_budget)
      COSTLENS_WASTE         = var.enable_waste_rules ? "on" : "off"
    })
  }

  depends_on = [aws_cloudwatch_log_group.digest]
}

resource "aws_lambda_function" "interactions" {
  function_name    = "${var.project}-slack-interactions"
  role             = aws_iam_role.lambda.arn
  runtime          = "python3.12"
  handler          = "costlens.handlers.lambda_interactions"
  filename         = var.lambda_package
  source_code_hash = filebase64sha256(var.lambda_package)
  timeout          = 10
  memory_size      = 512

  environment {
    variables = local.common_env
  }

  depends_on = [aws_cloudwatch_log_group.interactions]
}

# The URL is public by necessity (Slack calls it); the handler rejects any request whose
# Slack signature is missing, wrong or stale.
resource "aws_lambda_function_url" "interactions" {
  function_name      = aws_lambda_function.interactions.function_name
  authorization_type = "NONE"
}

resource "aws_cloudwatch_log_group" "ingest" {
  name              = "/aws/lambda/${var.project}-ingest"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "digest" {
  name              = "/aws/lambda/${var.project}-digest"
  retention_in_days = var.log_retention_days
}

resource "aws_cloudwatch_log_group" "interactions" {
  name              = "/aws/lambda/${var.project}-slack-interactions"
  retention_in_days = var.log_retention_days
}

# --------------------------------------------------------------------------- schedules

resource "aws_cloudwatch_event_rule" "ingest" {
  name                = "${var.project}-ingest-daily"
  schedule_expression = var.ingest_schedule
}

resource "aws_cloudwatch_event_target" "ingest" {
  rule = aws_cloudwatch_event_rule.ingest.name
  arn  = aws_lambda_function.ingest.arn
}

resource "aws_lambda_permission" "ingest" {
  statement_id  = "AllowEventBridgeIngest"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ingest.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.ingest.arn
}

resource "aws_cloudwatch_event_rule" "digest" {
  name                = "${var.project}-digest-daily"
  schedule_expression = var.digest_schedule
}

resource "aws_cloudwatch_event_target" "digest" {
  rule = aws_cloudwatch_event_rule.digest.name
  arn  = aws_lambda_function.digest.arn
}

resource "aws_lambda_permission" "digest" {
  statement_id  = "AllowEventBridgeDigest"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.digest.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.digest.arn
}

# --------------------------------------------------------------------------- budget alarm

resource "aws_budgets_budget" "monthly" {
  count        = var.budget_alert_email == "" ? 0 : 1
  name         = "${var.project}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.budget_alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}

# --------------------------------------------------------------------------- CUR 2.0 export (optional)

resource "aws_bcmdataexports_export" "cur" {
  count = var.create_cur_export ? 1 : 0

  export {
    name = "${var.project}-cur"

    data_query {
      # Only the columns CostLens reads. Column names are UNVERIFIED against a live export.
      query_statement = <<-SQL
        SELECT
          bill_billing_period_start_date,
          line_item_usage_start_date,
          line_item_usage_end_date,
          line_item_line_item_type,
          line_item_product_code,
          line_item_usage_type,
          line_item_resource_id,
          line_item_usage_amount,
          line_item_unblended_cost,
          product_region_code,
          tags
        FROM COST_AND_USAGE_REPORT
      SQL
      table_configurations = {
        COST_AND_USAGE_REPORT = {
          TIME_GRANULARITY                      = "HOURLY"
          INCLUDE_RESOURCES                     = "TRUE"
          INCLUDE_SPLIT_COST_ALLOCATION_DATA    = "FALSE"
          INCLUDE_MANUAL_DISCOUNT_COMPATIBILITY = "FALSE"
        }
      }
    }

    destination_configurations {
      s3_destination {
        s3_bucket = aws_s3_bucket.data.bucket
        s3_prefix = "cur"
        s3_region = var.aws_region

        s3_output_configurations {
          overwrite   = "OVERWRITE_REPORT"
          format      = "PARQUET"
          compression = "PARQUET"
          output_type = "CUSTOM"
        }
      }
    }

    refresh_cadence {
      frequency = "SYNCHRONOUS"
    }
  }

  depends_on = [aws_s3_bucket_policy.data]
}
