# =============================================================================
# modules/iam — IAM roles, instance profiles, SSM permissions
#
# Thesis Ch4 §4.2.1 Table 4.3: separate IAM module so that both the
# monitoring server and the target EC2 instances receive the same instance
# profile without creating a circular dependency between modules/monitoring
# and modules/compute.
#
# Dependency chain: storage → network → iam → {monitoring, compute}
#
# Resources created:
#   aws_iam_role.ec2_ssm                       — EC2 trust policy
#   aws_iam_role_policy_attachment.ssm_core    — AmazonSSMManagedInstanceCore
#   aws_iam_role_policy_attachment.cw_agent    — CloudWatchAgentServerPolicy
#   aws_iam_role_policy.orchestrator           — custom: S3, EC2, SSM, TF state
#   aws_iam_instance_profile.ec2_ssm           — profile shared by all instances
# =============================================================================

locals {
  name_prefix = "${var.project}-${var.environment}"
}

# ── EC2 trust role ─────────────────────────────────────────────────────────────
resource "aws_iam_role" "ec2_ssm" {
  name = "${local.name_prefix}-ec2-ssm-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = { Name = "${local.name_prefix}-ec2-ssm-role" }
}

# ── AWS-managed policies ───────────────────────────────────────────────────────
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.ec2_ssm.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy_attachment" "cw_agent" {
  role       = aws_iam_role.ec2_ssm.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

# ── Custom orchestrator policy ─────────────────────────────────────────────────
# Grants: S3 experiment results, EC2 describe/reprovision, SSM dispatch,
#         Terraform state (S3 + DynamoDB), IAM read for Terraform plan.
resource "aws_iam_role_policy" "orchestrator" {
  name = "${local.name_prefix}-orchestrator-permissions"
  role = aws_iam_role.ec2_ssm.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3ExperimentsAndSSM"
        Effect = "Allow"
        Action = [
          "s3:PutObject", "s3:GetObject", "s3:ListBucket", "s3:DeleteObject",
        ]
        Resource = [
          "arn:aws:s3:::${var.data_bucket_name}",
          "arn:aws:s3:::${var.data_bucket_name}/*",
        ]
      },
      {
        # EC2 permissions for Terraform remediation (instance_failure only).
        # Workspace-scope `terraform apply` on the targets/ workspace — no
        # -target flag (Ch3 §3.6.2, §4.2.1, §4.2.2).
        Sid    = "EC2Remediation"
        Effect = "Allow"
        Action = [
          "ec2:DescribeInstances",
          "ec2:DescribeInstanceStatus",
          "ec2:DescribeInstanceAttribute",
          "ec2:DescribeInstanceTypes",
          "ec2:DescribeInstanceCreditSpecifications",
          "ec2:DescribeImages",
          "ec2:DescribeSubnets",
          "ec2:DescribeSecurityGroups",
          "ec2:DescribeIamInstanceProfileAssociations",
          "ec2:DescribeVpcs",
          "ec2:DescribeVpcEndpoints",
          "ec2:DescribeTags",
          "ec2:RunInstances",
          "ec2:TerminateInstances",
          "ec2:StopInstances",
          "ec2:StartInstances",
          "ec2:ModifyInstanceAttribute",
          "ec2:CreateTags",
        ]
        Resource = "*"
      },
      {
        # SSM RunCommand permissions — used by experiments/inject/inject_once.py
        # to inject faults via ssm:SendCommand directly.
        Sid    = "SSMOrchestrator"
        Effect = "Allow"
        Action = [
          "ssm:SendCommand",
          "ssm:GetCommandInvocation",
          "ssm:ListCommandInvocations",
          "ssm:DescribeInstanceInformation",
          "ssm:ListAssociations",
        ]
        Resource = "*"
      },
      {
        # SSM Session Manager permissions — used by the orchestrator's own
        # runtime `ansible-playbook ... --connection community.aws.aws_ssm`
        # dispatch (roles/self_healing → execute.py), a completely different
        # SSM API surface from RunCommand above. Was missing entirely.
        # Confirmed live: every real remediation dispatch failed with
        # "AccessDeniedException ... not authorized to perform:
        # ssm:StartSession on resource: .../document/SSM-SessionManagerRunShell"
        # — retry-exhausted and escalated on every attempt.
        Sid    = "SSMSessionManager"
        Effect = "Allow"
        Action = [
          "ssm:StartSession",
          "ssm:TerminateSession",
          "ssm:ResumeSession",
        ]
        Resource = "*"
      },
      {
        # SSM Parameter Store — the target SSH private key is distributed to
        # the monitoring host (Ansible control node) as a SecureString rather
        # than bundled into a deployment artefact.
        Sid    = "SSMParameterTargetKey"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:GetParameters",
        ]
        Resource = "arn:aws:ssm:${var.aws_region}:*:parameter/${var.project}/${var.environment}/target-ssh-private-key"
      },
      {
        # Required alongside ssm:GetParameter, not instead of it. With
        # WithDecryption=true, Parameter Store calls KMS on the caller's
        # behalf; without kms:Decrypt the request fails at that step with
        # AccessDeniedException. (Ciphertext is only ever returned when
        # GetParameter is called WITHOUT --with-decryption, which this
        # artefact never does.)
        #
        # The AWS-managed alias/aws/ssm key has no stable ARN to name, so
        # scope by service instead: this allows decryption only when the call
        # arrives through SSM in this region, not for arbitrary KMS use.
        Sid      = "KMSDecryptSSMParameters"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:ViaService" = "ssm.${var.aws_region}.amazonaws.com"
          }
        }
      },
      {
        # Terraform state — orchestrator runs terraform apply on monitoring server.
        # Two distinct state files (Ch4 §4.2.2): bootstrap/terraform.tfstate
        # (read-only in practice, but ListBucket needs the prefix) and
        # targets/terraform.tfstate (read-write; this is the workspace the
        # orchestrator actually applies against at remediation time).
        Sid    = "TerraformState"
        Effect = "Allow"
        Action = [
          "s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket",
        ]
        Resource = [
          "arn:aws:s3:::${var.s3_bucket_name}",
          "arn:aws:s3:::${var.s3_bucket_name}/bootstrap/terraform.tfstate",
          "arn:aws:s3:::${var.s3_bucket_name}/targets/terraform.tfstate",
        ]
      },
      {
        # DynamoDB state lock (Ch3 §3.7.1 Table 3.5, Ch4 §4.2.2). Table name
        # MUST match modules/storage's aws_dynamodb_table.lock name exactly:
        # "${local.name_prefix}-tflock" (singular, no hyphen before "tflock").
        Sid    = "TerraformLock"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:DescribeTable",
        ]
        Resource = "arn:aws:dynamodb:${var.aws_region}:*:table/${var.project}-${var.environment}-tflock"
      },
      {
        Sid      = "IAMRead"
        Effect   = "Allow"
        Action   = ["iam:GetInstanceProfile", "iam:PassRole", "iam:GetRole"]
        Resource = "*"
      },
      {
        # Required by experiments/trials/run_s3_trial.py, which runs on the
        # monitoring host under this same instance profile and resolves the
        # baseline ASG and its CloudWatch alarms through the AWS API.
        # Confirmed live: DescribeAutoScalingGroups denied with AccessDenied
        # for this role before these actions were granted.
        #
        # The earlier rationale for this grant — that a targets/ apply
        # refreshes the ASG too, so S1 remediation itself would break while
        # the baseline was enabled — no longer applies. The baseline moved to
        # its own root and state (terraform/baseline/), so the orchestrator's
        # apply neither reads nor refreshes it. The grant is retained solely
        # for the S3 harness above; it is not needed by S1 remediation.
        Sid    = "AutoScalingBaseline"
        Effect = "Allow"
        Action = [
          "autoscaling:DescribeAutoScalingGroups",
          "autoscaling:DescribeAutoScalingInstances",
          "autoscaling:DescribeLaunchConfigurations",
          "autoscaling:DescribeTags",
          "autoscaling:DescribeScalingActivities",
        ]
        Resource = "*"
      },
      {
        # run_s3_trial.py's own scale-back-down after a cpu_overload/
        # memory_exhaustion trial adds a second instance -- confirmed live:
        # missing entirely, so every rep after the first failed with
        # AccessDenied and left the extra instance running, corrupting
        # every subsequent rep's measurement (they detected the already-
        # scaled-out ASG instead of a fresh one). Scoped to just the S3
        # baseline ASG by name -- the UUID segment of its ARN is wildcarded
        # because it is assigned by AWS at creation and would break this
        # policy across a future terraform destroy/apply of that ASG.
        Sid      = "AutoScalingBaselineScaleDown"
        Effect   = "Allow"
        Action   = ["autoscaling:TerminateInstanceInAutoScalingGroup"]
        Resource = "arn:aws:autoscaling:${var.aws_region}:*:autoScalingGroup:*:autoScalingGroupName/${local.name_prefix}-asg-baseline"
      },
      {
        # Human escalation (orchestrator/app.py _escalate).
        # Constructed directly rather than referencing module.monitoring's
        # aws_sns_topic.alerts.arn output — modules/iam is upstream of
        # modules/monitoring in the dependency chain (storage → network →
        # iam → {monitoring, compute}), so referencing it here would be
        # circular. Name matches modules/monitoring/main.tf's
        # aws_sns_topic.alerts exactly: "${local.name_prefix}-alerts".
        # Confirmed live: every escalation attempt failed with
        # "AuthorizationError ... not authorized to perform: SNS:Publish"
        # because this permission never existed at all.
        Sid      = "SNSEscalation"
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = "arn:aws:sns:${var.aws_region}:*:${local.name_prefix}-alerts"
      },
      {
        # modules/compute's per-target aws_cloudwatch_metric_alarm.cpu_high /
        # status_check_failed resources live in the same targets/ workspace
        # the orchestrator applies at remediation time — every terraform
        # plan/apply refreshes all resources in the workspace, not just
        # -replace-targeted ones, so read+write on these alarms is required
        # even though remediation itself never touches them directly.
        Sid    = "CloudWatchAlarms"
        Effect = "Allow"
        Action = [
          "cloudwatch:DescribeAlarms",
          "cloudwatch:PutMetricAlarm",
          "cloudwatch:DeleteAlarms",
          "cloudwatch:ListTagsForResource",
          "cloudwatch:TagResource",
          "cloudwatch:UntagResource",
          # Read-only metric-data access — used by experiments/trials/
          # run_s3_trial.py and ad-hoc diagnostics to inspect what the S3
          # baseline's alarms are actually seeing (e.g. confirming whether
          # mem_used_percent genuinely never crossed threshold vs. a config
          # gap). Confirmed live: GetMetricStatistics denied with
          # AccessDenied for this role, blocking that diagnosis entirely.
          "cloudwatch:GetMetricStatistics",
          "cloudwatch:GetMetricData",
          "cloudwatch:ListMetrics",
        ]
        Resource = "*"
      },
    ]
  })
}

# ── Instance profile ───────────────────────────────────────────────────────────
# Shared by the monitoring server and all three target EC2 instances.
resource "aws_iam_instance_profile" "ec2_ssm" {
  name = "${local.name_prefix}-ec2-ssm-profile"
  role = aws_iam_role.ec2_ssm.name
}
