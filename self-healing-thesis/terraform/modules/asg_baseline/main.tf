# =============================================================================
# terraform/modules/asg_baseline/main.tf — S3 baseline: AWS Auto Scaling Group
#
# Thesis Ch3 §3.1.2 Table 3.2 / Ch4 §4.2.1 Table 4.3: the S3 system under test
# is AWS Auto Scaling responding to CloudWatch alarms — the "native cloud"
# baseline against which the self-healing pipeline (S1) is compared. S3 is
# restricted to the infrastructure-recoverable fault subset (cpu_overload,
# memory_exhaustion, instance_failure) because an ASG cannot detect or
# remediate configuration-class faults.
#
# OWNERSHIP BOUNDARY — read this before adding anything here.
#
# These resources previously lived in modules/compute alongside
# aws_instance.targets, gated behind a var.enable_asg_baseline flag whose
# default was false. That arrangement was a latent hazard: any `terraform
# apply` against the targets/ root that did not carry the flag — including
# the orchestrator's own automatic instance_failure remediation, which passes
# only -var=project and -var=environment — evaluated the gate as false and
# planned a destroy of the whole baseline. The flag has been removed rather
# than defended: this module is instantiated from its own root
# (terraform/baseline/) writing its own state key, so terraform/targets no
# longer carries these resources in state and cannot destroy them under any
# variable value. Presence in the baseline root IS the enable switch.
#
# Consequently there is no `count` gate on any resource below. Do not
# reintroduce one — the whole point of this boundary is that enabling and
# disabling the baseline is `terraform apply` / `terraform destroy` in this
# root, an explicit act in an explicit directory, not a variable that some
# other root's apply can flip by omission.
#
# Mechanism: a single-instance ASG (min=1, max=2, desired=1) with EC2 health
# checks provides the instance_failure remediation natively (a terminated
# instance fails its EC2 status check and the ASG launches a replacement).
# For cpu_overload / memory_exhaustion, CloudWatch alarms drive a step-scaling
# policy that temporarily raises desired capacity to 2 (forcing a fresh
# instance to launch) and a companion low-threshold alarm scales back to 1
# once the metric recovers — AWS Auto Scaling's native remediation is adding
# capacity, not process-level repair, which is exactly the baseline limitation
# the thesis's comparative evaluation (RQ2, H2) is designed to surface.
# =============================================================================

locals {
  name_prefix = "${var.project}-${var.environment}"
}

resource "aws_launch_template" "asg_baseline" {
  name_prefix   = "${local.name_prefix}-asg-baseline-"
  image_id      = var.ubuntu_ami_id
  instance_type = var.target_instance_type

  iam_instance_profile {
    name = var.iam_instance_profile_name
  }

  network_interfaces {
    associate_public_ip_address = false
    security_groups             = [var.sg_targets_id]
    subnet_id                   = var.private_subnet_id
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  # EC2 basic monitoring (the default) only publishes CPUUtilization every
  # 5 minutes — the asg_cpu_overload/asg_memory_exhaustion alarms below
  # can't get 2 evaluation_periods of real data inside a 120-180s fault
  # injection window without this. Confirmed live: with basic monitoring,
  # the CPU alarm never fired even after a full 600s poll timeout despite
  # stress-ng genuinely holding CPU >90% for its whole 120s run — no
  # qualifying datapoint ever landed, and treat_missing_data=notBreaching
  # meant the missing data was silently treated as fine.
  monitoring {
    enabled = true
  }

  block_device_mappings {
    device_name = "/dev/sda1"
    ebs {
      volume_type           = "gp3"
      volume_size           = var.target_root_volume_gb
      delete_on_termination = true
      encrypted             = true
    }
  }

  # Deliberately the SAME bootstrap script the S1/S2 targets use, referenced
  # across module boundaries rather than copied: SSM Agent, NGINX /healthz,
  # Node Exporter, CloudWatch Agent (Thesis Ch3 §3.3.2 detection-path
  # bootstrap). A duplicate would be free to drift, and the CloudWatch Agent
  # append_dimensions block inside it is load-bearing for the
  # asg_memory_exhaustion alarm's exact dimension-set match — a divergence
  # there would silently break S3's memory arm rather than fail loudly.
  # Both modules ship together in terraform/modules/, so the relative path
  # resolves identically on the workstation and on the monitoring host.
  user_data = base64encode(templatefile(
    "${path.module}/../compute/user_data_target.sh.tpl",
    {
      project     = var.project
      environment = var.environment
      target_name = "asg-baseline"
      index       = "S3"
    }
  ))

  tag_specifications {
    resource_type = "instance"
    tags = {
      Name            = "${local.name_prefix}-asg-baseline"
      Project         = var.project
      Environment     = var.environment
      Role            = "s3-baseline"
      SystemUnderTest = "S3"
    }
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_autoscaling_group" "baseline" {
  name                = "${local.name_prefix}-asg-baseline"
  min_size            = 1
  max_size            = 2 # transient +1 during step-scaling remediation
  desired_capacity    = 1
  vpc_zone_identifier = [var.private_subnet_id]

  health_check_type         = "EC2"
  health_check_grace_period = 180 # matches instance_failure validation_window (Table 4.6)

  launch_template {
    id      = aws_launch_template.asg_baseline.id
    version = "$Latest"
  }

  tag {
    key                 = "Name"
    value               = "${local.name_prefix}-asg-baseline"
    propagate_at_launch = true
  }
  tag {
    key                 = "Role"
    value               = "s3-baseline"
    propagate_at_launch = true
  }

  # OPTIONAL DEFENCE-IN-DEPTH, deliberately left off by default.
  #
  # Uncommenting this makes `terraform destroy` in terraform/baseline/ fail
  # until the block is removed again, so tearing the baseline down costs a
  # code edit every time. Opening and closing an S3 window is a routine
  # operation in this lab, not a rare one, so the friction was judged more
  # likely to cause the baseline to be left running (and billing) than to
  # prevent a genuine accident — the root separation above already makes any
  # destroy an explicit act in an explicit directory. Enable it if the
  # baseline is ever left standing across a long campaign where an accidental
  # `terraform destroy` in the wrong terminal is the realistic risk.
  #
  # lifecycle {
  #   prevent_destroy = true
  # }
}

# ── Scaling policies (CloudWatch-alarm-driven remediation) ──────────────────

# CONFIRMED LIVE: scale_out_on_pressure was originally StepScaling with a
# single [0, +inf) step. Step Scaling does not act purely on the alarm's
# ALARM/OK state -- when invoked it re-fetches the metric's own aggregated
# value over the alarm's full evaluation window and picks a step based on
# that value versus the alarm threshold. With the cpu_overload alarm's
# evaluation_periods=2/datapoints_to_alarm=1 ("M of N", see that alarm's own
# comment), the alarm can enter ALARM off a single hot period while the
# other period in the same window is cool -- and the *averaged* value Step
# Scaling computes for step selection can fall back below the threshold even
# though the alarm itself is correctly ALARM. Confirmed live twice: alarm
# fired correctly (e.g. one period at 99.55%), but the scale-out action
# failed with "Auto Scaling Policy not executed because it does not specify
# any action for a metric value of: 66.39" -- 10 consecutive real campaign
# reps silently no-opped this way before being caught. Simple Scaling acts
# directly on the alarm's ALARM/OK transition with a fixed adjustment and
# never re-evaluates the metric, so it isn't exposed to this interaction.
# scale_in_on_recovery is unaffected and deliberately left as StepScaling:
# its interval requires metric <= threshold, which the OK condition (0 of 2
# periods breaching) always satisfies regardless of M-of-N averaging.
resource "aws_autoscaling_policy" "scale_out_on_pressure" {
  name                   = "${local.name_prefix}-asg-scale-out"
  autoscaling_group_name = aws_autoscaling_group.baseline.name
  policy_type            = "SimpleScaling"
  adjustment_type        = "ChangeInCapacity"
  scaling_adjustment     = 1
  cooldown               = 60
}

resource "aws_autoscaling_policy" "scale_in_on_recovery" {
  name                   = "${local.name_prefix}-asg-scale-in"
  autoscaling_group_name = aws_autoscaling_group.baseline.name
  policy_type            = "StepScaling"
  adjustment_type        = "ExactCapacity"

  step_adjustment {
    scaling_adjustment          = 1
    metric_interval_upper_bound = 0
  }
}

# ── CloudWatch alarms driving the scaling policies ───────────────────────────

resource "aws_cloudwatch_metric_alarm" "asg_cpu_overload" {
  alarm_name        = "${local.name_prefix}-asg-cpu-overload"
  alarm_description = "S3 baseline: CPUUtilization > ${var.cpu_alarm_threshold}% — Thesis Table 3.2 fault_type=cpu_overload"

  comparison_operator = "GreaterThanThreshold"
  # evaluation_periods=2 with datapoints_to_alarm=1 ("M of N"), not a
  # plain evaluation_periods=1 (deployment-session finding, revised): a
  # 120s stress-ng injection (experiments/inject/scenarios.yml's
  # cpu_overload default_duration) does not reliably land on 2
  # CONSECUTIVE full 60s CloudWatch buckets both above threshold, because
  # the injection's start time is not aligned to a minute boundary.
  # Confirmed live: a real trial injected at 12:59:48 produced
  # 70.7% / 99.99% / 25.3% across three consecutive minute buckets — only
  # ONE bucket ever cleanly captured the full fault window, so a plain
  # evaluation_periods=2 (requiring BOTH of the last 2 periods to breach)
  # failed 4 times in a row across a real campaign batch.
  #
  # A plain evaluation_periods=1 was tried first and confirmed live to
  # overcorrect: it made the alarm sensitive to a single noisy datapoint,
  # and a genuine second breach ~1 minute after a fault's own duration
  # expired (residual CPU activity, not a new fault) re-triggered
  # scale-out immediately after scale-in completed, flapping the ASG
  # between 1 and 2 instances. The M-of-N form below evaluates the same
  # 2-minute window as the original evaluation_periods=2 (so a single
  # transient blip outside the fault window doesn't retrigger it) but
  # only requires 1 of those 2 periods to breach (so the alignment
  # problem above is still solved) rather than requiring both.
  evaluation_periods  = 2
  datapoints_to_alarm = 1
  metric_name         = "CPUUtilization"
  namespace           = "AWS/EC2"
  # 60, not 30 — AWS/EC2 CPUUtilization is a native metric published at
  # 60s (detailed monitoring, enabled on the launch template above) or
  # 300s (basic) granularity, never 30s. A period that doesn't match any
  # granularity EC2 actually publishes at simply never gets real
  # datapoints — confirmed live, this combined with the missing detailed-
  # monitoring config above to make the alarm never fire at all.
  period             = 60
  statistic          = "Average"
  threshold          = var.cpu_alarm_threshold
  treat_missing_data = "notBreaching"

  dimensions = {
    AutoScalingGroupName = aws_autoscaling_group.baseline.name
  }

  alarm_actions = [aws_autoscaling_policy.scale_out_on_pressure.arn, var.sns_topic_arn]
  ok_actions    = [aws_autoscaling_policy.scale_in_on_recovery.arn]

  tags = { Project = var.project, FaultClass = "cpu_overload", SystemUnderTest = "S3" }
}

# NOTE: EC2 does not natively publish a memory-utilisation metric. This alarm
# depends on the CloudWatch Agent publishing "mem_used_percent" in the CWAgent
# namespace with an AutoScalingGroupName-only append-dimension (see
# ../compute/user_data_target.sh.tpl — InstanceId was deliberately removed
# from that config; see its own comment for why an exact dimension-set match
# matters). period = 30 here is correct and intentional: unlike AWS/EC2's
# native metrics, CWAgent publishes at whatever metrics_collection_interval
# the agent config sets, which that template pins to 30.
# Confirmed live: this pipeline genuinely works end-to-end (verified via
# aws cloudwatch get-metric-statistics showing real mem_used_percent data).
#
# CALIBRATED THRESHOLD (deployment-session decision, undocumented in the
# thesis as of this revision): mem_used_percent, as computed by the
# CloudWatch Agent's own memory-availability heuristic, consistently
# plateaus around 77-80% for this fault injection on this instance
# class/AMI — never reaching a 90% threshold, across repeated trials. This
# is a structural measurement-definition gap, not injection-script
# randomness: experiments/inject/scenarios.yml's memory_exhaustion sizing
# targets 6% available (94% used) based on the KERNEL's own /proc/meminfo
# MemAvailable figure, but CWAgent's underlying Go runtime library computes
# "available" memory differently (more page cache counted as
# reclaimable/available than the kernel's own heuristic credits), so the
# same actual memory pressure reads as a meaningfully lower mem_used_percent
# than the equivalent Prometheus/node_exporter-based reading S1's own
# memory_exhaustion alert uses (confirmed live: the identical injection
# script reliably clears a >90%-used equivalent via node_exporter's
# MemAvailable on target-1/2/3, but tops out around 77-80% via CWAgent on
# this ASG instance — a live get-metric-statistics pull during one such
# trial showed a peak of 77.93%, sustained above 77% for four consecutive
# 30s periods before dropping back to an ~24% idle baseline).
#
# The threshold below is deliberately calibrated to 75% — comfortably
# below the observed peak with a small margin, and far above the ~24-25%
# idle baseline, so it fires on genuine injected pressure without
# false-triggering at rest — rather than left at a 90% value this metric
# structurally cannot reach on this instance class. This changes what the
# S3 memory_exhaustion alarm represents (CWAgent-relative pressure, not a
# literal ">90% used" reading), but does not affect S1 at all: S1's
# memory_exhaustion detection is a completely separate mechanism
# (Prometheus/node_exporter, a different threshold, no shared code path
# other than the injection script itself). It also does not affect RQ2:
# S3's memory_exhaustion arm is already explicitly non-Wilcoxon-eligible
# supplementary material (§5.19) regardless of what threshold fires it.
resource "aws_cloudwatch_metric_alarm" "asg_memory_exhaustion" {
  alarm_name        = "${local.name_prefix}-asg-memory-exhaustion"
  alarm_description = "S3 baseline: mem_used_percent > ${var.asg_mem_alarm_threshold}% — Thesis Table 3.2 fault_type=memory_exhaustion"

  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "mem_used_percent"
  namespace           = "CWAgent"
  period              = 30
  statistic           = "Average"
  threshold           = var.asg_mem_alarm_threshold
  treat_missing_data  = "notBreaching"

  dimensions = {
    AutoScalingGroupName = aws_autoscaling_group.baseline.name
  }

  alarm_actions = [aws_autoscaling_policy.scale_out_on_pressure.arn, var.sns_topic_arn]
  ok_actions    = [aws_autoscaling_policy.scale_in_on_recovery.arn]

  tags = { Project = var.project, FaultClass = "memory_exhaustion", SystemUnderTest = "S3" }
}

# instance_failure remediation requires no CloudWatch alarm: the ASG's own
# EC2 health check detects the terminated instance's failed status check and
# launches a replacement automatically (Thesis Table 3.2, "EC2 Auto Scaling
# Group" remediation column).
