# =============================================================================
# terraform/bootstrap/variables.tf
#
# Variables for the shared-infrastructure workspace.
# Required values without defaults must be supplied via terraform.tfvars or
# the -var flag.  See the root README.md §Bootstrapping for an example
# terraform.tfvars.
# =============================================================================

# ── Identity ──────────────────────────────────────────────────────────────────

variable "project" {
  description = "Project label — used in all resource names and tags"
  type        = string
  default     = "self-healing-thesis"
}

variable "environment" {
  description = "Deployment environment label (experiment / dev / staging)"
  type        = string
  default     = "experiment"
}

variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "ap-southeast-1"
}

# ── Networking ────────────────────────────────────────────────────────────────

variable "availability_zone" {
  description = "Single AZ used for all subnets and EC2 instances (thesis §3.3.1 — one AZ)"
  type        = string
  default     = "ap-southeast-1a"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "public_subnet_cidr" {
  description = "CIDR for the public subnet (monitoring/orchestrator server)"
  type        = string
  default     = "10.0.1.0/24"
}

variable "private_subnet_cidr" {
  description = "CIDR for the private subnet (three target EC2 instances)"
  type        = string
  default     = "10.0.2.0/24"
}

variable "researcher_cidr" {
  description = "CIDR allowed inbound SSH + dashboard access to the monitoring server (e.g. 1.2.3.4/32)"
  type        = string

  validation {
    condition     = can(cidrnetmask(var.researcher_cidr))
    error_message = "researcher_cidr must be a valid CIDR block (e.g. 1.2.3.4/32)."
  }
}

# ── Storage ───────────────────────────────────────────────────────────────────

variable "state_bucket_name" {
  description = "Globally unique S3 bucket name for Terraform state and SSM payloads"
  type        = string
  # Must be globally unique.  Example: "self-healing-thesis-tfstate-<account-id>"
}

variable "data_bucket_name" {
  description = "S3 bucket for experiment results mirrored by the orchestrator (its-thesis-data)"
  type        = string
  default     = "its-thesis-data"
}

variable "noncurrent_version_expiry_days" {
  description = "Days after which non-current S3 object versions are expired"
  type        = number
  default     = 90
}

# ── Monitoring server ─────────────────────────────────────────────────────────

variable "key_pair_name" {
  description = "EC2 key pair name for researcher SSH access to the monitoring server (leave empty to disable)"
  type        = string
  default     = ""
}

variable "sns_email" {
  description = "Email address for SNS escalation notifications (circuit breaker trips, retry exhaustion)"
  type        = string
}
