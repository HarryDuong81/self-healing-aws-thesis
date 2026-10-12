# =============================================================================
# terraform/bootstrap/backend.tf
#
# BOOTSTRAP SEQUENCE — two-phase initialisation (chicken-and-egg):
#
#   Phase 1 — local backend (first run; S3 bucket does not exist yet):
#     terraform init
#     terraform apply   # creates S3 bucket + DynamoDB table
#
#   Phase 2 — migrate to S3 backend (run once after Phase 1):
#     terraform init -migrate-state
#     # Terraform prompts: "Do you want to copy existing state?" → yes
#
#   Subsequent runs — normal S3-backed workflow:
#     terraform plan / apply / destroy
#
# State key: bootstrap/terraform.tfstate
# The targets workspace reads this state via data.terraform_remote_state.bootstrap.
# =============================================================================

terraform {
  required_version = ">= 1.14.9"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
  }

  # ── Phase 2 backend (comment out for Phase 1 cold-start) ─────────────────
  # After `terraform apply` has created the S3 bucket and DynamoDB table,
  # uncomment this block and run `terraform init -migrate-state`.
  #
  # State locking: DynamoDB (not Terraform's native S3 lockfile), per Ch3
  # §3.3.2 / §3.7.1 Table 3.5 and Ch4 §4.2.2 — the Plan layer's stale-lock
  # precondition (orchestrator/plan.py) queries this table directly.
  backend "s3" {
    # Injected at init time via -backend-config flags:
    #   terraform init \
    #     -backend-config="bucket=<BUCKET>" \
    #     -backend-config="region=ap-southeast-1" \
    #     -backend-config="dynamodb_table=<TABLE>"
    key     = "bootstrap/terraform.tfstate"
    encrypt = true
  }
}
