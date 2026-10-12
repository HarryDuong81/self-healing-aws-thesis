# =============================================================================
# module/storage — S3 Bucket (Terraform state + SSM) + DynamoDB Lock Table
# =============================================================================
# BOOTSTRAP NOTE:
#   These resources must exist BEFORE the S3 backend is configured.
#   Create them first with a local backend, then migrate state.
#   See terraform/global/backend.tf for step-by-step instructions.
# =============================================================================

locals {
  name_prefix = "${var.project}-${var.environment}"
}

# ── S3 Bucket ─────────────────────────────────────────────────────────────────
resource "aws_s3_bucket" "state" {
  bucket = var.bucket_name

  # DELIBERATE FULL-TEARDOWN CHANGE (session note): prevent_destroy removed
  # and force_destroy added so `terraform destroy` can actually remove this
  # bucket in one pass, including every historical object version (state
  # files, any S3-mirrored results) — versioning being enabled means a
  # destroy would otherwise fail with BucketNotEmpty even with
  # prevent_destroy gone. Both changes are intentional for a genuine "tear
  # down everything" pass, not a permanent relaxation — restore
  # prevent_destroy = true (and drop force_destroy) if this backend is
  # ever recreated for continued use.
  force_destroy = true

  tags = { Name = var.bucket_name, Purpose = "terraform-state-ssm-payloads" }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket = aws_s3_bucket.state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "expire-noncurrent-versions"
    status = "Enabled"

    filter {} # required by AWS provider ≥ 4.x; empty = apply to all objects

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_expiry_days
    }
  }
}

# ── DynamoDB Lock Table ───────────────────────────────────────────────────────
resource "aws_dynamodb_table" "lock" {
  name         = "${local.name_prefix}-tflock"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  ttl {
    attribute_name = "Expiry"
    enabled        = true
  }

  # DELIBERATE FULL-TEARDOWN CHANGE (session note): prevent_destroy removed
  # so this table can be destroyed alongside everything else. Restore
  # prevent_destroy = true if this backend is ever recreated for continued
  # use.

  tags = { Name = "${local.name_prefix}-tflock", Purpose = "terraform-state-lock" }
}
