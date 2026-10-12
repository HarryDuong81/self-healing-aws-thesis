# terraform/modules/asg_baseline/outputs.tf

output "asg_baseline_name" {
  description = "Name of the S3 baseline Auto Scaling Group. run_s3_trial.py resolves the same name independently via boto3 (_DEFAULT_ASG_NAME) rather than reading this output, so the two must stay in agreement."
  value       = aws_autoscaling_group.baseline.name
}

output "asg_launch_template_id" {
  description = "Launch template backing the baseline ASG"
  value       = aws_launch_template.asg_baseline.id
}

output "asg_cpu_alarm_name" {
  description = "CloudWatch alarm name for the S3 cpu_overload arm (run_s3_trial.py polls this alarm by name)"
  value       = aws_cloudwatch_metric_alarm.asg_cpu_overload.alarm_name
}

output "asg_memory_alarm_name" {
  description = "CloudWatch alarm name for the S3 memory_exhaustion arm"
  value       = aws_cloudwatch_metric_alarm.asg_memory_exhaustion.alarm_name
}
