# =============================================================================
# terraform/targets/main.tf — Target EC2 Instances Workspace
#
# This workspace is the ONLY Terraform workspace on which automated
# remediation operates at runtime (Thesis §5.9). It manages
# exactly three t3.micro target instances and nothing else.
#
# Design invariants:
#   1. for_each (not count) — preserves stable resource addresses across
#      removals.  With count, removing target-2 would re-index target-3
#      to index 1, silently recycling its state.  With for_each, each
#      instance address is module.compute.aws_instance.targets["target-2"],
#      so a targeted destroy/apply is unambiguous (Thesis §3.6.2).
#
#   2. No -target at runtime — scope is enforced structurally by the
# workspace boundary (Thesis §5.9). The orchestrator
#      invokes `terraform apply` against this entire workspace; the
#      for_each key matching the faulted instance name provides selectivity.
#
#   3. SSM-only access — no inbound port 22.  The aws_ssm Ansible connection
#      plugin and SSM VPC endpoints (provisioned in bootstrap) provide the
# management channel (Thesis §5.8).
#
#   4. IMDSv2 mandatory — http_tokens = "required" mitigates SSRF-based
#      metadata exfiltration.
#
#   5. State isolation — this workspace's tfstate is at key
#      "targets/terraform.tfstate"; bootstrap state is at
#      "bootstrap/terraform.tfstate".  Destroying targets never deletes
#      shared VPC, IAM, or S3/DynamoDB resources.
#
# Remote-state references:
#   Networking outputs (VPC ID, private subnet, security group IDs) and
#   the IAM instance profile are consumed from the bootstrap state via
#   `data.terraform_remote_state.bootstrap`.  This eliminates duplication
#   and guarantees consistency without cross-module variable threading.
#
# Toolchain pin (Thesis Table 4.1): Terraform v1.14.9 / aws provider v5.x
# =============================================================================

# ── Provider configuration ────────────────────────────────────────────────────

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Workspace   = "targets"
    }
  }
}

# ── Bootstrap remote state ────────────────────────────────────────────────────

data "terraform_remote_state" "bootstrap" {
  backend = "s3"

  config = {
    bucket  = var.bootstrap_state_bucket
    key     = var.bootstrap_state_key
    region  = var.aws_region
    encrypt = true
  }
}

# ── Local derived values ───────────────────────────────────────────────────────

locals {
  name_prefix = "${var.project}-${var.environment}"

  # Bootstrap remote state shortcuts
  private_subnet_id         = data.terraform_remote_state.bootstrap.outputs.private_subnet_id
  sg_targets_id             = data.terraform_remote_state.bootstrap.outputs.sg_targets_id
  iam_instance_profile_name = data.terraform_remote_state.bootstrap.outputs.iam_instance_profile_name
  sns_topic_arn             = data.terraform_remote_state.bootstrap.outputs.sns_topic_arn
  state_bucket_name         = data.terraform_remote_state.bootstrap.outputs.state_bucket_name
  targets_ssh_key_pair_name = data.terraform_remote_state.bootstrap.outputs.targets_ssh_key_pair_name
}

# ── Target EC2 instances ──────────────────────────────────────────────────────
#
# All target-instance provisioning lives in the reusable
# modules/compute module (Thesis Ch4 §4.2.1 Table 4.3) so that:
#   - Node Exporter version, health-endpoint path, and CloudWatch Agent
# installation stay pinned in exactly one place .
#   - for_each preserves stable resource addresses:
#       module.compute.aws_instance.targets["target-1"], ["target-2"], ["target-3"]
#   - The orchestrator dispatches remediation as a `terraform apply`
#     (no -target flag) scoped to this targets root configuration;
#     the Plan layer's drift gate compares for_each keys to confine the
#     remediation to the faulted target (Thesis §3.5.4, §3.6.2).

module "compute" {
  source = "../modules/compute"

  project     = var.project
  environment = var.environment
  aws_region  = var.aws_region

  private_subnet_id     = local.private_subnet_id
  sg_targets_id         = local.sg_targets_id
  key_pair_name         = local.targets_ssh_key_pair_name
  target_instance_type  = var.instance_type
  target_root_volume_gb = 20

  iam_instance_profile_name = local.iam_instance_profile_name
  s3_bucket_name            = local.state_bucket_name
  sns_topic_arn             = local.sns_topic_arn
}

# ── Outputs ───────────────────────────────────────────────────────────────────

output "target_instance_ids" {
  description = "Map of target name → EC2 instance ID"
  value       = module.compute.target_instance_ids
}

output "target_private_ips" {
  description = "Map of target name → private IP (used by health_checker.py)"
  value       = module.compute.target_private_ips
}

output "ubuntu_ami_id" {
  description = "Ubuntu 26.04 LTS AMI ID used for all target instances"
  value       = module.compute.ubuntu_ami_id
}
