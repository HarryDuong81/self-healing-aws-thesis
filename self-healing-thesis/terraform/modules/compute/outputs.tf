# terraform/modules/compute/outputs.tf
# for_each produces maps keyed by "target-1" | "target-2" | "target-3"

output "target_instance_ids" {
  description = "Map of target name → EC2 instance ID"
  value       = { for k, v in aws_instance.targets : k => v.id }
}

output "target_private_ips" {
  description = "Map of target name → private IP address"
  value       = { for k, v in aws_instance.targets : k => v.private_ip }
}

output "target_instance_types" {
  description = "EC2 instance type applied to all targets"
  value       = var.target_instance_type
}

output "ubuntu_ami_id" {
  description = "Ubuntu 26.04 LTS AMI ID resolved at deployment time"
  value       = data.aws_ami.ubuntu.id
}
