# terraform/modules/iam/outputs.tf

output "iam_instance_profile_name" {
  description = "EC2 instance profile name — passed to modules/monitoring and modules/compute"
  value       = aws_iam_instance_profile.ec2_ssm.name
}

output "iam_role_arn" {
  description = "IAM role ARN (for reference in other policies if needed)"
  value       = aws_iam_role.ec2_ssm.arn
}
