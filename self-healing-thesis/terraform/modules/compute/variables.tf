variable "project" {
  description = "Project name"
  type        = string
  default     = "self-healing-thesis"
}

variable "environment" {
  description = "Deployment environment label (used in resource names and S3 state key)"
  type        = string
  default     = "experiment"
}

variable "aws_region" {
  description = "AWS region"
  type        = string
  default     = "ap-southeast-1"
}

# ── Target instance variables ─────────────────────────────────────────────────
# Targets use for_each with keys "target-1" | "target-2" | "target-3".
# Thesis Ch4 §4.2.1: MUST use for_each, MUST NOT use count (stable resource
# identity is a precondition of workspace-scope remediation).

variable "private_subnet_id" {
  description = "Private subnet ID for target instances"
  type        = string
}

# Static private IPs so instance_failure recreation (Terraform destroy +
# create against the same for_each key) preserves each target's address —
# without this, AWS auto-assigns a new IP from the subnet pool on every
# recreation (confirmed live: 10.0.2.70/89/43 became 10.0.2.55/35/236 after
# a prior replacement), which churns Prometheus's scrape config and the
# orchestrator's TARGET_IPS env var on every single instance_failure trial.
# .10/.11/.12 sit safely above AWS's reserved range in each subnet (.0
# network, .1 VPC router, .2 DNS, .3 reserved) and below the .255 broadcast.
variable "target_private_ips" {
  description = "Fixed private IP per target key, within the private subnet's CIDR — keeps each instance's address stable across instance_failure recreation"
  type        = map(string)
  default = {
    "target-1" = "10.0.2.10"
    "target-2" = "10.0.2.11"
    "target-3" = "10.0.2.12"
  }
}

variable "sg_targets_id" {
  description = "Security group ID for target instances"
  type        = string
}

variable "key_pair_name" {
  description = "EC2 key pair name attached to target instances for Ansible SSH connectivity (deliberate no-SSH invariant reversal — see terraform/bootstrap/main.tf)"
  type        = string
  default     = ""
}

variable "target_instance_type" {
  description = "EC2 instance type for target instances (t3.micro baseline)"
  type        = string
  default     = "t3.micro"

  validation {
    condition     = contains(["t3.micro", "t3.small", "t3.medium"], var.target_instance_type)
    error_message = "target_instance_type must be t3.micro, t3.small, or t3.medium."
  }
}

variable "target_root_volume_gb" {
  description = "Root EBS volume size for each target instance (GiB)"
  type        = number
  default     = 20
}

variable "sns_topic_arn" {
  description = "SNS topic ARN for CloudWatch alarm actions (cross-validation)"
  type        = string
}

variable "cpu_alarm_threshold" {
  description = "CPU % threshold for CloudWatch alarm (cross-validates CPUHighAlert)"
  type        = number
  default     = 90
}

variable "s3_bucket_name" {
  description = "S3 bucket name — used for SSM payloads, experiment results, and Terraform state"
  type        = string
}

variable "iam_instance_profile_name" {
  description = "IAM instance profile name shared by monitoring server and all targets (defined in modules/iam/)"
  type        = string
}
