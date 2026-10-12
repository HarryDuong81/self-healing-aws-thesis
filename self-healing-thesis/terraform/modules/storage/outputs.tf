output "bucket_name" {
  description = "S3 bucket name (use in backend.hcl and orchestrator env vars)"
  value       = aws_s3_bucket.state.id
}

output "bucket_arn" {
  description = "ARN of the S3 bucket"
  value       = aws_s3_bucket.state.arn
}

output "dynamodb_table_name" {
  description = "DynamoDB lock table name (use in backend.hcl)"
  value       = aws_dynamodb_table.lock.name
}
