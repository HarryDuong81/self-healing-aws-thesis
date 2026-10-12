variable "project" {
  description = "Project name"
  type        = string
  default     = "self-healing-thesis"
}

variable "environment" {
  description = "Deployment environment label"
  type        = string
  default     = "dev"
}

variable "bucket_name" {
  description = "Globally unique S3 bucket name for Terraform state + SSM payloads"
  type        = string
}

variable "noncurrent_version_expiry_days" {
  description = "Days after which non-current object versions are deleted"
  type        = number
  default     = 90
}
