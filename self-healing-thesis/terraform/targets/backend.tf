# =============================================================================
# terraform/targets/backend.tf
#
# Remote S3 backend for the targets workspace.
#
# The S3 bucket and DynamoDB lock table are provisioned by the bootstrap
# workspace (terraform/bootstrap/).  Run bootstrap first:
#   cd terraform/bootstrap && terraform apply -auto-approve
#
# Then initialise this workspace supplying the bucket name as a backend
# config override (avoids hard-coding the generated bucket name here):
#   terraform init \
#     -backend-config="bucket=<bootstrap_output_bucket_name>" \
#     -backend-config="dynamodb_table=<bootstrap_output_dynamodb_table_name>" \
#     -backend-config="region=ap-southeast-1"
#
# The state key isolates the targets workspace from the bootstrap state
# so that `terraform destroy` in targets never deletes shared networking.
#
# State locking: DynamoDB (Ch3 §3.3.2 / §3.7.1 Table 3.5, Ch4 §4.2.2). The
# orchestrator's Plan layer (plan.py) queries this same table directly as a
# stale-lock precondition before dispatching Terraform remediation.
# =============================================================================

terraform {
  required_version = "~> 1.14"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    # Populated at init time via -backend-config flags
    key     = "targets/terraform.tfstate"
    encrypt = true
    # bucket, dynamodb_table, region supplied as -backend-config at init
  }
}
