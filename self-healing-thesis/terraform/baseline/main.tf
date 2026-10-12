# =============================================================================
# terraform/baseline/main.tf — S3 (AWS Auto Scaling) Baseline Workspace
#
# This root owns the S3 system under test and nothing else. It exists as a
# separate root — rather than a flag inside terraform/targets — because
# ownership, not a variable, is what keeps the baseline safe.
#
# The hazard this replaces: the ASG used to live in modules/compute behind
# `count = var.enable_asg_baseline ? 1 : 0`, with the variable defaulting to
# false. Every apply against terraform/targets that omitted the flag
# therefore planned a destroy of the entire baseline. The orchestrator's own
# automatic instance_failure remediation is exactly such an apply — it passes
# only -var=project and -var=environment — so an S1 instance_failure trial
# scheduled before S3's portion of the campaign would have silently taken the
# S3 arm down mid-run. Because the two roots now write different state keys,
# terraform/targets does not carry these resources at all and cannot destroy
# them under any variable value, from any copy of the repository, run by
# either a human or the orchestrator.
#
# Enabling the baseline is `terraform apply` in this directory.
# Disabling it is `terraform destroy` in this directory.
# There is no flag, and asg.auto.tfvars is no longer required anywhere.
#
# State isolation:
#   bootstrap/terraform.tfstate  — VPC, IAM, monitoring host, S3, DynamoDB
#   targets/terraform.tfstate    — the three S1/S2 target instances
#   baseline/terraform.tfstate   — this workspace (the S3 ASG)
#
# Cost note: the baseline runs one t3.micro continuously while it exists.
# Destroy it when the S3 trial window closes.
#
# Toolchain pin (Thesis Table 4.2): Terraform v1.14.9 / aws provider v5.x
# =============================================================================

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Workspace   = "baseline"
    }
  }
}

# ── Bootstrap remote state ────────────────────────────────────────────────────
# Read-only. The baseline consumes shared networking, IAM and SNS exactly as
# the targets root does, so both systems under test sit on identical
# infrastructure and the S1-vs-S3 comparison is not confounded by it.

data "terraform_remote_state" "bootstrap" {
  backend = "s3"

  config = {
    bucket  = var.bootstrap_state_bucket
    key     = var.bootstrap_state_key
    region  = var.aws_region
    encrypt = true
  }
}

locals {
  private_subnet_id         = data.terraform_remote_state.bootstrap.outputs.private_subnet_id
  sg_targets_id             = data.terraform_remote_state.bootstrap.outputs.sg_targets_id
  iam_instance_profile_name = data.terraform_remote_state.bootstrap.outputs.iam_instance_profile_name
  sns_topic_arn             = data.terraform_remote_state.bootstrap.outputs.sns_topic_arn
  ubuntu_ami_id             = data.terraform_remote_state.bootstrap.outputs.ubuntu_ami_id
}

# ── S3 baseline Auto Scaling Group ────────────────────────────────────────────

module "asg_baseline" {
  source = "../modules/asg_baseline"

  project     = var.project
  environment = var.environment
  aws_region  = var.aws_region

  ubuntu_ami_id             = local.ubuntu_ami_id
  private_subnet_id         = local.private_subnet_id
  sg_targets_id             = local.sg_targets_id
  iam_instance_profile_name = local.iam_instance_profile_name
  sns_topic_arn             = local.sns_topic_arn

  target_instance_type  = var.instance_type
  target_root_volume_gb = 20
}

# ── Outputs ───────────────────────────────────────────────────────────────────

output "asg_baseline_name" {
  description = "Name of the S3 baseline Auto Scaling Group"
  value       = module.asg_baseline.asg_baseline_name
}

output "asg_cpu_alarm_name" {
  description = "CloudWatch alarm name for the S3 cpu_overload arm"
  value       = module.asg_baseline.asg_cpu_alarm_name
}

output "asg_memory_alarm_name" {
  description = "CloudWatch alarm name for the S3 memory_exhaustion arm"
  value       = module.asg_baseline.asg_memory_alarm_name
}
