variable "project" {
  description = "Project name — used in all resource names and tags"
  type        = string
  default     = "self-healing-thesis"
}

variable "environment" {
  description = "Deployment environment label"
  type        = string
  default     = "dev"
}

variable "vpc_cidr" {
  description = "CIDR block for the VPC"
  type        = string
  default     = "10.0.0.0/16"
}

variable "public_subnet_cidr" {
  description = "CIDR for the public subnet (monitoring server)"
  type        = string
  default     = "10.0.1.0/24"
}

variable "private_subnet_cidr" {
  description = "CIDR for the private subnet (target instances)"
  type        = string
  default     = "10.0.2.0/24"
}

variable "availability_zone" {
  description = "Availability zone for public and private subnets"
  type        = string
  default     = "ap-southeast-1a"
}

variable "researcher_cidr" {
  description = "CIDR allowed inbound SSH/dashboard access to monitoring server"
  type        = string
  # Set to your actual IP in terraform.tfvars: e.g. "1.2.3.4/32"
  validation {
    condition     = can(cidrnetmask(var.researcher_cidr))
    error_message = "researcher_cidr must be a valid CIDR block (e.g. 1.2.3.4/32)."
  }
}
