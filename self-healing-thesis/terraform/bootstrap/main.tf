# =============================================================================
# terraform/bootstrap/main.tf — Shared Infrastructure Workspace
#
# Creates all long-lived resources shared across the experiment:
#   - S3 bucket: Terraform state + SSM payloads + experiment results
#   - DynamoDB table: Terraform state locking
#   - VPC + subnets + internet/NAT gateways + route tables
#   - Security groups (orchestrator, targets, SSM endpoints)
#   - SSM VPC Interface Endpoints (ssm, ssmmessages, ec2messages)
#   - IAM role + instance profile (shared by monitoring server and targets)
#   - Monitoring Stack Server (t3.medium) + Elastic IP
#   - SNS alert topic (escalation for retry exhaustion, circuit breaker)
#   - CloudWatch alarm for monitoring server CPU
#
# The targets workspace reads this state via:
#   data "terraform_remote_state" "bootstrap" { ... }
# and consumes: private_subnet_id, sg_targets_id, iam_instance_profile_name.
#
# Toolchain pin (Thesis Table 4.1): Terraform v1.14.9 / aws provider v5.x
# Module dependency order: storage → networking → iam → monitoring
# =============================================================================

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Workspace   = "bootstrap"
    }
  }
}

# ── Storage (must exist before S3 backend migration) ─────────────────────────

module "storage" {
  source = "../modules/storage"

  project     = var.project
  environment = var.environment
  bucket_name = var.state_bucket_name

  noncurrent_version_expiry_days = var.noncurrent_version_expiry_days
}

# ── Thesis experiment data bucket ─────────────────────────────────────────────
# Separate from the state bucket — holds MTTR JSONL, CSV, and notebook outputs.
# Orchestrator mirrors results here after each trial .

resource "aws_s3_bucket" "data" {
  bucket = var.data_bucket_name

  # DELIBERATE FULL-TEARDOWN CHANGE (session note): force_destroy added so
  # `terraform destroy` can remove this bucket in one pass despite
  # versioning being enabled (aws_s3_bucket_versioning.data) and the bucket
  # holding real campaign results — confirmed already backed up before this
  # change. Remove force_destroy if this bucket is ever recreated for
  # continued use, to avoid an accidental future data loss.
  force_destroy = true

  tags = { Name = var.data_bucket_name, Purpose = "thesis-experiment-results" }
}

resource "aws_s3_bucket_versioning" "data" {
  bucket = aws_s3_bucket.data.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "data" {
  bucket = aws_s3_bucket.data.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket = aws_s3_bucket.data.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# ── Networking ────────────────────────────────────────────────────────────────

module "networking" {
  source = "../modules/networking"

  project             = var.project
  environment         = var.environment
  vpc_cidr            = var.vpc_cidr
  public_subnet_cidr  = var.public_subnet_cidr
  private_subnet_cidr = var.private_subnet_cidr
  availability_zone   = var.availability_zone
  researcher_cidr     = var.researcher_cidr
}

# ── IAM ───────────────────────────────────────────────────────────────────────
# Shared instance profile used by both the monitoring server and target instances.

module "iam" {
  source = "../modules/iam"

  project          = var.project
  environment      = var.environment
  aws_region       = var.aws_region
  s3_bucket_name   = var.state_bucket_name
  data_bucket_name = var.data_bucket_name

  depends_on = [module.storage]
}

# ── Ubuntu 26.04 LTS AMI ─────────────────────────────────────────────────────
# Resolved once at bootstrap plan time; the resolved AMI ID is emitted as an
# output so the targets workspace can pin the same AMI without a second lookup
# (prevents a newly-released AMI from silently splitting the two workspaces
# during a long campaign).

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

# ── Monitoring Stack Server ───────────────────────────────────────────────────
# t3.medium in the public subnet; hosts all eight observability containers
# (Thesis Table 4.3) plus the Flask MAPE-K orchestrator.

module "monitoring" {
  source = "../modules/monitoring"

  project     = var.project
  environment = var.environment
  aws_region  = var.aws_region

  # Network
  public_subnet_id   = module.networking.public_subnet_id
  sg_orchestrator_id = module.networking.sg_orchestrator_id

  # IAM
  iam_instance_profile_name = module.iam.iam_instance_profile_name

  # Compute
  key_pair_name  = var.key_pair_name
  s3_bucket_name = var.state_bucket_name

  # Alerting
  sns_email = var.sns_email

  depends_on = [module.networking, module.iam]
}

# ── Target SSH Key Pair ─────────────────────────────────────────────────────
# Deliberate, explicit reversal of the thesis's "no SSH on targets" invariant
# , authorised as a workaround for a confirmed AWS Session
# Manager defect: the community.aws/amazon.aws aws_ssm connection plugin's
# interactive session reliably starts remote command execution (its own
# verbose trace shows "Begin marker received") but the completion message is
# never delivered back over the session WebSocket — confirmed via strace that
# the local session-manager-plugin process is alive and correctly polling
# epoll for the awaited data, ruling out a local hang. ssm:SendCommand
# (RunCommand) was independently confirmed 100% reliable for every equivalent
# operation during debugging, but cannot run Ansible playbooks directly; SSH
# restores that capability without abandoning Ansible as the actuator.
resource "tls_private_key" "target_ssh" {
  algorithm = "RSA"
  rsa_bits  = 4096
}

resource "aws_key_pair" "target_ssh" {
  key_name   = "${var.project}-${var.environment}-target-ssh"
  public_key = tls_private_key.target_ssh.public_key_openssh

  tags = { Name = "${var.project}-${var.environment}-target-ssh" }
}

# Written locally so the self_healing Ansible role can sync it to the
# monitoring host (control node), mirroring the existing pattern used for
# Terraform modules / experiment harness sync.
resource "local_sensitive_file" "target_ssh_private_key" {
  filename        = "${path.module}/target_ssh_key.pem"
  content         = tls_private_key.target_ssh.private_key_pem
  file_permission = "0600"
}

# ── Target SSH key — SSM Parameter Store (SecureString) ───────────────────────
# Primary distribution channel for the CD path: the GitHub runner never handles
# the private key, and the monitoring host fetches it directly with its own
# instance-role credentials. Encrypted with the AWS-managed key alias/aws/ssm;
# the instance role is granted kms:Decrypt scoped by kms:ViaService in
# terraform/modules/iam/main.tf.
#
# Standard tier: a 4096-bit RSA key in PKCS#1 PEM form is ~3.2 KB against the
# 4 KB Standard limit. Advanced is billed per parameter per month and buys
# nothing here; if the size were ever exceeded the apply fails loudly.
#
# The key is already stored in state by tls_private_key.target_ssh — that is
# inherent to the resource. This parameter does not change that; it exists so
# the key can reach the monitoring host without passing through a CI runner or
# a deployment artefact. Its value is deliberately NOT exported as a Terraform
# output (see outputs.tf) to keep it out of `terraform output` reach.
resource "aws_ssm_parameter" "target_ssh_private_key" {
  name        = "/${var.project}/${var.environment}/target-ssh-private-key"
  description = "PEM private key for Ansible SSH dispatch to target_nodes"
  type        = "SecureString"
  value       = tls_private_key.target_ssh.private_key_pem
  tier        = "Standard"

  tags = { Name = "${var.project}-${var.environment}-target-ssh-key" }
}

# ── Backend config export ─────────────────────────────────────────────────────
# Written to outputs.backend.tfvars so the targets workspace can consume it:
#   terraform init -backend-config=../bootstrap/outputs.backend.tfvars

resource "local_file" "backend_tfvars" {
  filename = "${path.module}/outputs.backend.tfvars"
  content  = <<-EOT
    bucket         = "${module.storage.bucket_name}"
    region         = "${var.aws_region}"
    dynamodb_table = "${module.storage.dynamodb_table_name}"
    encrypt        = true
  EOT

  depends_on = [module.storage]
}
