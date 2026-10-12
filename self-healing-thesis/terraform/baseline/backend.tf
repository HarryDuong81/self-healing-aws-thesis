# =============================================================================
# terraform/baseline/backend.tf
#
# Remote S3 backend for the S3-baseline workspace.
#
# The bucket and DynamoDB lock table are provisioned by terraform/bootstrap.
# Initialise this workspace the same way as targets/, reusing the backend
# config file bootstrap emits:
#
#   cd terraform/baseline
#   terraform init -backend-config=../bootstrap/outputs.backend.tfvars
#
# The distinct state key is the whole safety mechanism: because these
# resources live under "baseline/terraform.tfstate" and not
# "targets/terraform.tfstate", no apply against the targets root — human or
# orchestrator, from any copy of the repository — can plan a change to them.
#
# State locking uses the same DynamoDB table as the other two workspaces, so
# a baseline apply and a runtime remediation apply cannot interleave.
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
    key     = "baseline/terraform.tfstate"
    encrypt = true
    # bucket, dynamodb_table, region supplied as -backend-config at init
  }
}
