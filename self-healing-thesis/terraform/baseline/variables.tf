# terraform/baseline/variables.tf
#
# project and environment MUST match the targets root. They compose
# local.name_prefix, which composes the ASG's name, which
# experiments/trials/run_s3_trial.py resolves independently through boto3
# (_DEFAULT_ASG_NAME = "self-healing-thesis-experiment-asg-baseline").
# Changing either value here without changing that constant makes the S3
# harness fail with "ASG not found".

variable "aws_region" {
  description = "AWS region for all resources (must match the bootstrap workspace)"
  type        = string
  default     = "ap-southeast-1"
}

variable "project" {
  description = "Project slug — used as a prefix in all resource names"
  type        = string
  default     = "self-healing-thesis"
}

variable "environment" {
  description = "Workspace environment label"
  type        = string
  default     = "experiment"
}

variable "instance_type" {
  description = "EC2 instance type for the baseline instance — keep identical to the targets root's instance_type"
  type        = string
  default     = "t3.micro"
}

variable "bootstrap_state_bucket" {
  description = "S3 bucket where bootstrap workspace state is stored"
  type        = string
}

variable "bootstrap_state_key" {
  description = "S3 key for the bootstrap workspace state file"
  type        = string
  default     = "bootstrap/terraform.tfstate"
}
