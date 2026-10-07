variable "aws_region" {
  description = "Region for the tool's own resources. Nothing else in the code hard-codes a region."
  type        = string
  default     = "ap-south-1"
}

variable "aws_profile" {
  description = "Named AWS CLI profile to deploy with. Null uses the default credential chain."
  type        = string
  default     = null
}

variable "project" {
  description = "Name prefix for every resource."
  type        = string
  default     = "costlens"
}

variable "lambda_package" {
  description = "Path to the zip built by scripts/build_lambda.sh (or .ps1)."
  type        = string
  default     = "../build/costlens-lambda.zip"
}

variable "ingest_schedule" {
  description = "EventBridge cron (UTC) for the daily CUR/Cost Explorer ingest."
  type        = string
  default     = "cron(0 3 * * ? *)"
}

variable "digest_schedule" {
  description = "EventBridge cron (UTC) for the daily digest. The default is 09:00 IST."
  type        = string
  default     = "cron(30 3 * * ? *)"
}

variable "slack_channel" {
  description = "Slack channel ID for the digest. Empty disables posting."
  type        = string
  default     = ""
}

variable "enable_ai" {
  description = "Use Claude to write the digest sentences. Off means the plain template writer."
  type        = bool
  default     = false
}

variable "token_budget" {
  description = "Hard monthly-style token budget for AI text; the template writer takes over when spent."
  type        = number
  default     = 200000
}

variable "enable_waste_rules" {
  description = "Run the read-only waste rules (Describe* and CloudWatch reads)."
  type        = bool
  default     = true
}

variable "budget_limit_usd" {
  description = "Monthly AWS budget limit; the tool's sandbox should never come near it."
  type        = number
  default     = 10
}

variable "budget_alert_email" {
  description = "Where the budget alarm emails go. Empty skips the budget."
  type        = string
  default     = ""
}

variable "log_retention_days" {
  type    = number
  default = 30
}

variable "create_cur_export" {
  description = <<-EOT
    Create the CUR 2.0 (Data Exports) export in Terraform. Off by default: the column list below
    comes from the legacy CUR schema and has not been applied to a live account. If AWS rejects a
    column name, fix it in main.tf (the error names it) or create the export in the console.
  EOT
  type        = bool
  default     = false
}
