output "bucket" {
  description = "Bucket that receives the CUR export and holds normalised data and state."
  value       = aws_s3_bucket.data.bucket
}

output "cur_delivery_prefix" {
  description = "Point the Data Exports (CUR 2.0) S3 destination at this bucket and prefix."
  value       = "s3://${aws_s3_bucket.data.bucket}/cur"
}

output "slack_interactions_url" {
  description = "Set this as the Interactivity Request URL in the Slack app."
  value       = aws_lambda_function_url.interactions.function_url
}

output "slack_secret_name" {
  value = aws_secretsmanager_secret.slack.name
}

output "llm_secret_name" {
  value = aws_secretsmanager_secret.llm.name
}
