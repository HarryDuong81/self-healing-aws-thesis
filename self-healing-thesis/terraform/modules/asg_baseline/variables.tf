# terraform/modules/asg_baseline/variables.tf
#
# Every input here is either a naming/sizing knob or a value produced by the
# bootstrap workspace. This module reads nothing from modules/compute and
# holds no reference to aws_instance.targets — that independence is what
# allows the baseline to live in its own Terraform root and state.

variable "project" {
  description = "Project slug — used as a prefix in all resource names. Must match the targets root, or run_s3_trial.py's ASG-name resolution will not find the group."
  type        = string
  default     = "self-healing-thesis"
}

variable "environment" {
  description = "Workspace environment label. Must match the targets root, for the same reason as project."
  type        = string
  default     = "experiment"
}

variable "aws_region" {
  description = "AWS region (must match the bootstrap workspace)"
  type        = string
  default     = "ap-southeast-1"
}

variable "ubuntu_ami_id" {
  description = "Ubuntu 26.04 LTS AMI ID, taken from the bootstrap remote state so the baseline instance and the S1/S2 targets are never built from different images"
  type        = string
}

variable "private_subnet_id" {
  description = "Private subnet the ASG launches into (bootstrap output)"
  type        = string
}

variable "sg_targets_id" {
  description = "Security group applied to baseline instances — the same sg-targets the S1/S2 targets use (bootstrap output)"
  type        = string
}

variable "iam_instance_profile_name" {
  description = "Instance profile granting SSM and CloudWatch Agent permissions (bootstrap output)"
  type        = string
}

variable "sns_topic_arn" {
  description = "SNS topic for alarm notifications (bootstrap output)"
  type        = string
}

variable "target_instance_type" {
  description = "EC2 instance type for the baseline instance — must match the S1/S2 targets for the comparison to be like-for-like"
  type        = string
  default     = "t3.micro"
}

variable "target_root_volume_gb" {
  description = "Root EBS volume size, matching the S1/S2 targets"
  type        = number
  default     = 20
}

variable "cpu_alarm_threshold" {
  description = "CPUUtilization percentage above which the scale-out policy fires (S3 cpu_overload arm)"
  type        = number
  default     = 90
}

variable "asg_mem_alarm_threshold" {
  description = "CWAgent mem_used_percent above which the scale-out policy fires (S3 memory_exhaustion arm). See the calibration comment in main.tf before changing this."
  type        = number
  default     = 75
}
