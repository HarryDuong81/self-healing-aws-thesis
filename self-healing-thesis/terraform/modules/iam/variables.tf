# terraform/modules/iam/variables.tf

variable "project" {
  description = "Project name (used in resource naming)"
  type        = string
  default     = "self-healing-thesis"
}

variable "environment" {
  description = "Deployment environment label (experiment / dev / prod)"
  type        = string
  default     = "experiment"
}

variable "aws_region" {
  description = "AWS region (used in DynamoDB ARN in IAM policy)"
  type        = string
  default     = "ap-southeast-1"
}

variable "s3_bucket_name" {
  description = "S3 bucket name used in IAM policy (Terraform state)"
  type        = string
}

# Was missing entirely — the S3ExperimentsAndSSM statement below reused
# s3_bucket_name (the tfstate bucket), so the orchestrator's IAM role had
# zero grants on the actual SSM/data bucket. Confirmed live: the
# community.aws.aws_ssm connection plugin's HeadBucket call against
# its-thesis-data-<account-id> returned 403 Forbidden during the first real
# end-to-end remediation dispatch, crash-looping every fault-injection
# trial at the exact same step.
variable "data_bucket_name" {
  description = "S3 bucket name for experiment results and SSM command payload transfer"
  type        = string
}
