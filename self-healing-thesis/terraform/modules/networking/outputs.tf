output "vpc_id" {
  description = "VPC ID"
  value       = aws_vpc.main.id
}

output "public_subnet_id" {
  description = "Public subnet ID (monitoring server)"
  value       = aws_subnet.public.id
}

output "private_subnet_id" {
  description = "Private subnet ID (target instances)"
  value       = aws_subnet.private.id
}

output "public_subnet_cidr" {
  description = "CIDR block of the public subnet"
  value       = aws_subnet.public.cidr_block
}

output "private_subnet_cidr" {
  description = "CIDR block of the private subnet"
  value       = aws_subnet.private.cidr_block
}

output "sg_orchestrator_id" {
  description = "Security group ID for the monitoring/orchestrator server"
  value       = aws_security_group.orchestrator.id
}

output "sg_targets_id" {
  description = "Security group ID for target instances"
  value       = aws_security_group.targets.id
}

output "nat_gateway_ip" {
  description = "Public IP of the NAT Gateway"
  value       = aws_eip.nat.public_ip
}

output "ssm_endpoint_ids" {
  description = "Map of SSM VPC endpoint IDs (ssm, ssmmessages, ec2messages)"
  value       = { for k, v in aws_vpc_endpoint.ssm : k => v.id }
}
