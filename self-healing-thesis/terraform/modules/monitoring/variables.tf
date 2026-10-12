# terraform/modules/monitoring/variables.tf
# =============================================================================
# Variables for the monitoring module (Monitoring Stack Server + SNS).
# IAM resources have been moved to modules/iam/ (workflow v3 §1.1).
# =============================================================================

variable "project" {
  description = "Project name"
  type        = string
  default     = "self-healing-thesis"
}

variable "environment" {
  description = "Deployment environment label"
  type        = string
  default     = "experiment"
}

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "ap-southeast-1"
}

# ── Network ───────────────────────────────────────────────────────────────────

variable "public_subnet_id" {
  description = "Public subnet ID for the monitoring/orchestrator server"
  type        = string
}

variable "sg_orchestrator_id" {
  description = "Security group ID for the monitoring/orchestrator server"
  type        = string
}

# ── IAM (received from modules/iam) ──────────────────────────────────────────

variable "iam_instance_profile_name" {
  description = "EC2 instance profile name from modules/iam — attached to the monitoring server"
  type        = string
}

# ── Monitoring Server Compute ─────────────────────────────────────────────────

variable "monitoring_instance_type" {
  description = "EC2 instance type for the monitoring/orchestrator server"
  type        = string
  default     = "t3.medium"

  validation {
    condition     = contains(["t3.small", "t3.medium", "t3.large"], var.monitoring_instance_type)
    error_message = "monitoring_instance_type must be t3.small, t3.medium, or t3.large."
  }
}

variable "monitoring_root_volume_gb" {
  description = "Root EBS volume size for the monitoring server (GiB)"
  type        = number
  default     = 30
}

variable "key_pair_name" {
  description = "EC2 key pair name for researcher SSH access to the monitoring server. Leave empty to disable key-based SSH."
  type        = string
  default     = ""
}

variable "s3_bucket_name" {
  description = "S3 bucket for experiment results and SSM payloads"
  type        = string
}

# ── Alerting ──────────────────────────────────────────────────────────────────

variable "sns_email" {
  description = "Email address for SNS escalation notifications (circuit breaker, retry exhaustion)"
  type        = string
}
