# =============================================================================
# modules/monitoring — Monitoring Stack Server + SNS + CloudWatch alarms
#
# Workflow v3 §1.1:
#   modules/monitoring/ → Monitoring Stack Server (t3.medium) + EIP + SNS alarms
#   modules/iam/        → IAM role / instance profile (separate module)
#
# Dependency chain: storage → network → iam → monitoring → compute
#
# The monitoring server (t3.medium) runs the full observability stack:
#   Prometheus, Alertmanager, Grafana, Elasticsearch, Logstash, Kibana,
#   Jaeger, OpenTelemetry Collector, Flask Orchestrator — all via Docker Compose.
#
# The iam_instance_profile_name is received from modules/iam (not created here).
# =============================================================================

locals {
  name_prefix = "${var.project}-${var.environment}"
}

# ── Ubuntu 26.04 LTS AMI ──────────────────────────────────────────────────────
data "aws_ami" "ubuntu" {
  owners = ["099720109477"] # Canonical

  # Pin the exact AMI ID used in the thesis campaign (Ubuntu 26.04 LTS resolute,
  # ap-southeast-1, published 2026-06-04).  Using image-id avoids a name-pattern
  # lookup that fails when Canonical changes the path prefix between releases.
  filter {
    name   = "image-id"
    values = ["ami-0532913178263be11"]
  }
}

# ── Monitoring / Orchestrator Server ──────────────────────────────────────────
# t3.medium: runs Prometheus, Alertmanager, Grafana, Elasticsearch, Logstash,
# Kibana, Jaeger (all via Docker Compose) + Flask orchestrator (gunicorn).
#
# Public subnet + EIP: researcher SSH access + Grafana/Kibana dashboards.
# SSM access is primary; key_pair_name is optional.
resource "aws_instance" "monitoring" {
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = var.monitoring_instance_type
  subnet_id              = var.public_subnet_id
  vpc_security_group_ids = [var.sg_orchestrator_id]
  iam_instance_profile   = var.iam_instance_profile_name
  key_name               = var.key_pair_name != "" ? var.key_pair_name : null

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required" # IMDSv2 — required for SSM
    http_put_response_hop_limit = 1
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.monitoring_root_volume_gb
    delete_on_termination = true
    encrypted             = true
    tags = {
      Name = "${local.name_prefix}-monitoring-root"
    }
  }

  user_data = base64encode(templatefile(
    "${path.module}/user_data_monitoring.sh.tpl",
    {
      project     = var.project
      environment = var.environment
      aws_region  = var.aws_region
    }
  ))

  tags = {
    Name = "${local.name_prefix}-monitoring"
    Role = "monitoring"
  }

  # Prevent accidental destruction of the orchestrator during experiments.
  lifecycle {
    ignore_changes = [user_data]
  }
}

# ── Elastic IP (static public address for monitoring server) ──────────────────
resource "aws_eip" "monitoring" {
  instance = aws_instance.monitoring.id
  domain   = "vpc"

  tags = {
    Name = "${local.name_prefix}-monitoring-eip"
  }
}

# ── SNS Topic: email escalation ───────────────────────────────────────────────
resource "aws_sns_topic" "alerts" {
  name = "${local.name_prefix}-alerts"
  tags = { Name = "${local.name_prefix}-alerts" }
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.sns_email
}

# ── CloudWatch Alarm: monitoring server CPU ───────────────────────────────────
# Alerts if the orchestrator server becomes overloaded.
# Does NOT trigger automated remediation — requires manual intervention.
resource "aws_cloudwatch_metric_alarm" "monitoring_cpu" {
  alarm_name          = "${local.name_prefix}-monitoring-cpu-high"
  alarm_description   = "Monitoring server CPU > 85% — orchestrator may be overloaded (manual intervention required)"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 3
  metric_name         = "CPUUtilization"
  namespace           = "AWS/EC2"
  period              = 60
  statistic           = "Average"
  threshold           = 85
  treat_missing_data  = "notBreaching"

  dimensions = {
    InstanceId = aws_instance.monitoring.id
  }

  alarm_actions = [aws_sns_topic.alerts.arn]
  tags          = { Role = "monitoring-server" }
}
