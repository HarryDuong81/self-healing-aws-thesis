# =============================================================================
# terraform/targets/variables.tf
# =============================================================================

variable "aws_region" {
  description = "AWS region for all resources (must match bootstrap workspace)"
  type        = string
  default     = "ap-southeast-1"
}

variable "project" {
  description = "Project slug — used as a prefix in all resource names"
  type        = string
  default     = "self-healing-thesis" # must match terraform/bootstrap's default
}

variable "environment" {
  description = "Workspace environment label"
  type        = string
  default     = "experiment"
}

variable "instance_type" {
  description = "EC2 instance type for target nodes"
  type        = string
  default     = "t3.micro"
}

# The bootstrap workspace outputs the Ubuntu 26.04 LTS AMI ID resolved at
# plan time.  Pass it here to avoid a second data source lookup that could
# return a different AMI after a new release lands mid-campaign.
variable "ubuntu_ami_id" {
  description = "Ubuntu 26.04 LTS AMI ID (copied from bootstrap output, ap-southeast-1)"
  type        = string
  # Set in terraform.tfvars or via -var flag after reading bootstrap output.
  # Example: ami-0532913178263be11 (Ubuntu 26.04 LTS resolute hvm:ebs-ssd, ap-southeast-1)
}

variable "bootstrap_state_bucket" {
  description = "S3 bucket where bootstrap workspace state is stored"
  type        = string
  # Passed at init time; also exposed as a variable so modules can reference it
  # for SSM payload and results uploads without a second bucket.
}

variable "bootstrap_state_key" {
  description = "S3 key for the bootstrap workspace state file"
  type        = string
  default     = "bootstrap/terraform.tfstate"
}

variable "availability_zone" {
  description = "AZ for all instances (single-AZ design per Thesis §3.3.1)"
  type        = string
  default     = "ap-southeast-1a"
}

variable "key_pair_name" {
  description = "EC2 key pair name (optional — targets use SSM; key pair is emergency-only)"
  type        = string
  default     = ""
}

# NOTE: the S3 (AWS Auto Scaling) baseline is NOT configured from this root.
# It has its own root and state at terraform/baseline/. The former
# enable_asg_baseline flag was removed deliberately: with the ASG in this
# workspace's state, any apply that omitted the flag — including the
# orchestrator's own automatic instance_failure remediation, which passes
# only -var=project and -var=environment — planned a destroy of the whole
# baseline. Do not reintroduce it here.
