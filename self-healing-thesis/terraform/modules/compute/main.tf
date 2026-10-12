# =============================================================================
# terraform/modules/compute/main.tf
# Target EC2 Instances — 3 × t3.micro (Ubuntu 26.04 LTS)
#
# ── Academic justification for for_each (MANDATORY) ──────────────────────────
# The compute module MUST use for_each, MUST NOT use count.
#
# With count, Terraform assigns numeric indices [0], [1], [2].  If target-1 is
# destroyed (instance_failure fault), Terraform re-indexes the remaining
# instances: what was [1] becomes [0], what was [2] becomes [1]. Any state
# lookup keyed on that index would now address the WRONG instance —
# destroying a healthy node and breaking the deterministic MAPE-K experiment.
#
# With for_each = toset(["target-1","target-2","target-3"]) each instance has
# a permanent, immutable string key that never shifts regardless of how many
# siblings are terminated and recreated:
#
#   module.compute.aws_instance.targets["target-1"]   ← always target-1
#   module.compute.aws_instance.targets["target-2"]   ← always target-2
#   module.compute.aws_instance.targets["target-3"]   ← always target-3
#
# The Flask orchestrator therefore does NOT use -target at all (Ch3 §3.3.2,
# §3.6.2; Ch4 §4.2.1, §4.2.2). It runs a workspace-scope
# `terraform apply` against the entire targets/ workspace; the for_each key
# stability above is what lets that apply reconcile exactly one missing
# instance without disturbing its healthy siblings — the -target flag is not
# the mechanism that provides selectivity here, the stable for_each address is.
#
# ── IAM ───────────────────────────────────────────────────────────────────────
# The shared IAM role / instance profile is defined in modules/iam/ and passed
# in via var.iam_instance_profile_name.  This avoids the circular dependency
# that would arise if compute defined IAM and monitoring also consumed it.
# Dependency chain: storage → networking → iam → monitoring → compute.
#
# ── Network isolation ─────────────────────────────────────────────────────────
# All target instances sit in the private subnet (10.0.2.0/24).
# associate_public_ip_address = false — no public IP, no exposure.
# Port 22 is NOT in the target security group — SSH is structurally impossible.
# Ansible remediation is delivered exclusively via SSM VPC Interface Endpoints.
#
# ── CloudWatch alarms ─────────────────────────────────────────────────────────
# Per-instance alarms are created here because they depend on instance IDs
# that are only available after for_each resolves.  They serve as secondary
# detection cross-validation (primary: Prometheus → Alertmanager → orchestrator).
# =============================================================================

locals {
  name_prefix = "${var.project}-${var.environment}"

  # Canonical target keys — IMMUTABLE for the duration of the experiment campaign.
  # Changing this set destroys and recreates all affected instances, resets their
  # SSM registration, and breaks any in-flight experiment trials.
  target_names = toset(["target-1", "target-2", "target-3"])
}

# ── Ubuntu 26.04 LTS AMI (Canonical account 099720109477) ────────────────────
# Pinned by image-id, matching terraform/bootstrap/main.tf's data source —
# a name-pattern filter here previously broke live when Canonical rotated
# the AMI mid-campaign (documented rationale for why
# bootstrap uses image-id pinning instead of a name-pattern lookup).
data "aws_ami" "ubuntu" {
  owners = ["099720109477"]

  filter {
    name   = "image-id"
    values = ["ami-0532913178263be11"]
  }
}

# ── Target EC2 instances ──────────────────────────────────────────────────────
# for_each key  : "target-1" | "target-2" | "target-3"
# Terraform addr: module.compute.aws_instance.targets["target-N"]
# Remediation   : workspace-scope `terraform apply` on the targets/ workspace
# (no -target flag — Ch3 §3.6.2)
resource "aws_instance" "targets" {
  for_each = local.target_names

  ami           = data.aws_ami.ubuntu.id
  instance_type = var.target_instance_type

  # Private subnet only — no internet-facing exposure.
  subnet_id                   = var.private_subnet_id
  private_ip                  = var.target_private_ips[each.key]
  associate_public_ip_address = false # explicit: no public IP under any circumstance
  vpc_security_group_ids      = [var.sg_targets_id]
  iam_instance_profile        = var.iam_instance_profile_name

  # Deliberate reversal of the "no key_name" no-SSH invariant — see
  # terraform/bootstrap/main.tf's target_ssh key-pair comment for the
  # confirmed AWS Session Manager defect this works around. Fault
  # detection/injection still goes through SSM RunCommand unaffected; only
  # Ansible's own remediation dispatch connection changes to SSH.
  key_name = var.key_pair_name != "" ? var.key_pair_name : null

  # IMDSv2 required — SSM Agent 3.x mandates token-based metadata access.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.target_root_volume_gb
    delete_on_termination = true
    encrypted             = true

    tags = {
      Name        = "${local.name_prefix}-${each.key}-root"
      Project     = var.project
      Environment = var.environment
    }
  }

  # Minimal bootstrap: SSM Agent verification, NGINX, Node Exporter.
  # All heavier software (Filebeat, OTel collector) is installed by Ansible
  # after apply so that Terraform remains idempotent.
  user_data = base64encode(templatefile(
    "${path.module}/user_data_target.sh.tpl",
    {
      project     = var.project
      environment = var.environment
      target_name = each.key
      index       = split("-", each.key)[1] # "target-2" → "2"
    }
  ))

  # Lifecycle:
  # • create_before_destroy = false — for instance_failure remediation we want
  #   Terraform to launch a clean replacement *after* detecting the terminated
  #   instance, not attempt an in-place swap that could cause resource conflicts.
  # • prevent_destroy = false — the orchestrator must be able to reprovision.
  # • ignore_changes = [user_data] — bootstrap runs once; subsequent applies
  #   must not force instance replacement due to user_data content changes.
  lifecycle {
    create_before_destroy = false
    prevent_destroy       = false
    ignore_changes        = [user_data]
  }

  tags = {
    Name        = "${local.name_prefix}-${each.key}"
    Project     = var.project
    Environment = var.environment
    Role        = "target"
    TargetName  = each.key
    Index       = split("-", each.key)[1]
  }
}

# ── CloudWatch Alarm: CPU overload (fault class 1) ────────────────────────────
# Secondary detection — cross-validates the Prometheus CPUHighAlert rule.
# Primary path: Prometheus scrapes node_exporter → Alertmanager → /alert webhook.
# Secondary path: CloudWatch metric → SNS → operator email + MTTR audit trail.
resource "aws_cloudwatch_metric_alarm" "cpu_high" {
  for_each = local.target_names

  alarm_name        = "${local.name_prefix}-cpu-high-${each.key}"
  alarm_description = <<-DESC
    CPU utilisation > ${var.cpu_alarm_threshold}% on ${each.key}.
    Cross-validates Prometheus CPUHighAlert (fault_type=cpu_overload).
    Remediation: ansible-playbook remediation/kill_runaway_process.yml --limit ${each.key}
  DESC

  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "CPUUtilization"
  namespace           = "AWS/EC2"
  period              = 60
  statistic           = "Average"
  threshold           = var.cpu_alarm_threshold
  treat_missing_data  = "notBreaching"

  dimensions = {
    InstanceId = aws_instance.targets[each.key].id
  }

  alarm_actions = [var.sns_topic_arn]
  ok_actions    = [var.sns_topic_arn]

  tags = {
    Project    = var.project
    FaultClass = "cpu_overload"
    Target     = each.key
    Actuator   = "ansible"
  }
}

# ── CloudWatch Alarm: Instance failure (fault class 6) ───────────────────────
# Secondary detection — cross-validates the Prometheus InstanceDown rule.
# StatusCheckFailed fires within 60 s of termination (before node_exporter
# scrape target disappears, which takes 30 s of silence + for:30s window).
# treat_missing_data = "breaching" ensures the alarm fires even when the
# instance is gone and no data is being sent.
resource "aws_cloudwatch_metric_alarm" "status_check_failed" {
  for_each = local.target_names

  alarm_name        = "${local.name_prefix}-status-check-${each.key}"
  alarm_description = <<-DESC
    EC2 StatusCheckFailed on ${each.key}.
    Cross-validates Prometheus InstanceDown (fault_type=instance_failure).
    Remediation: workspace-scope `terraform apply` on the targets/ workspace
    reconciles the missing ${each.key} instance from the for_each map
    (no -target flag).
  DESC

  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "StatusCheckFailed"
  namespace           = "AWS/EC2"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "breaching" # alarm fires when instance is gone

  dimensions = {
    InstanceId = aws_instance.targets[each.key].id
  }

  alarm_actions = [var.sns_topic_arn]
  ok_actions    = [var.sns_topic_arn]

  tags = {
    Project    = var.project
    FaultClass = "instance_failure"
    Target     = each.key
    Actuator   = "terraform"
  }
}
