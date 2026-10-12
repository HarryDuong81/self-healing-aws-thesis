# =============================================================================
# terraform/bootstrap/outputs.tf
#
# Values consumed by the targets workspace via:
#   data "terraform_remote_state" "bootstrap" { backend = "s3" ... }
#
# Reference in targets/main.tf:
#   data.terraform_remote_state.bootstrap.outputs.<name>
# =============================================================================

# ── Required by terraform/targets/main.tf ────────────────────────────────────

output "private_subnet_id" {
  description = "Private subnet ID for target EC2 instances"
  value       = module.networking.private_subnet_id
}

output "sg_targets_id" {
  description = "Security group ID for target instances (inbound SSH from sg_orchestrator only — see targets_ssh_key_pair_name)"
  value       = module.networking.sg_targets_id
}

output "targets_ssh_key_pair_name" {
  description = "EC2 key pair name attached to target instances for Ansible SSH connectivity (deliberate no-SSH invariant reversal — see terraform/bootstrap/main.tf)"
  value       = aws_key_pair.target_ssh.key_name
}

output "iam_instance_profile_name" {
  description = "EC2 instance profile name — attached to target instances"
  value       = module.iam.iam_instance_profile_name
}

# ── Monitoring server ─────────────────────────────────────────────────────────

output "monitoring_host_ip" {
  description = "Elastic IP (public) of the monitoring/orchestrator server"
  value       = module.monitoring.monitoring_public_ip
}

output "monitoring_private_ip" {
  description = "Private IP of the monitoring/orchestrator server"
  value       = module.monitoring.monitoring_private_ip
}

output "monitoring_instance_id" {
  description = "EC2 instance ID of the monitoring/orchestrator server"
  value       = module.monitoring.monitoring_instance_id
}

# ── Storage ───────────────────────────────────────────────────────────────────

output "state_bucket_name" {
  description = "S3 bucket name used for Terraform state and SSM payloads"
  value       = module.storage.bucket_name
}

output "data_bucket_name" {
  description = "S3 bucket name for experiment results (MTTR JSONL, CSV, notebooks)"
  value       = aws_s3_bucket.data.id
}

output "dynamodb_table_name" {
  description = "DynamoDB lock table name"
  value       = module.storage.dynamodb_table_name
}

# ── Network ───────────────────────────────────────────────────────────────────

output "vpc_id" {
  description = "VPC ID"
  value       = module.networking.vpc_id
}

output "public_subnet_id" {
  description = "Public subnet ID (monitoring server)"
  value       = module.networking.public_subnet_id
}

output "sg_orchestrator_id" {
  description = "Security group ID for the monitoring/orchestrator server"
  value       = module.networking.sg_orchestrator_id
}

# ── IAM ───────────────────────────────────────────────────────────────────────

output "iam_role_arn" {
  description = "IAM role ARN for the shared EC2 instance profile"
  value       = module.iam.iam_role_arn
}

# ── Alerting ──────────────────────────────────────────────────────────────────

output "sns_topic_arn" {
  description = "SNS topic ARN for escalation notifications"
  value       = module.monitoring.sns_topic_arn
}

# ── AMI ───────────────────────────────────────────────────────────────────────
# Pass this to the targets workspace so both workspaces pin the same AMI:
#   terraform -chdir=terraform/targets apply \
#     -var "ubuntu_ami_id=$(terraform -chdir=terraform/bootstrap output -raw ubuntu_ami_id)"

output "ubuntu_ami_id" {
  description = "Ubuntu 26.04 LTS AMI ID resolved at bootstrap plan time — pass to targets workspace"
  value       = data.aws_ami.ubuntu.id
}

# ── Backend config (for use with -backend-config flag) ───────────────────────

output "backend_bucket" {
  description = "S3 bucket for targets workspace backend (use with -backend-config=../bootstrap/outputs.backend.tfvars)"
  value       = module.storage.bucket_name
}

output "backend_dynamodb_table" {
  description = "DynamoDB lock table for targets workspace backend"
  value       = module.storage.dynamodb_table_name
}

# ── Target SSH key parameter ─────────────────────────────────────────────────
# Consumed by roles/self_healing (to fetch the key onto the monitoring host)
# and by deploy.yml's SSM script. Only the parameter NAME is exported — the
# value stays out of `terraform output` reach.

output "target_ssh_parameter_name" {
  description = "SSM Parameter Store name holding the target SSH private key"
  value       = aws_ssm_parameter.target_ssh_private_key.name
}
