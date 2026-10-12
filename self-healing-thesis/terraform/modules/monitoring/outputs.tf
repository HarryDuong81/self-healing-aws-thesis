# terraform/modules/monitoring/outputs.tf

output "monitoring_instance_id" {
  description = "EC2 instance ID of the monitoring/orchestrator server"
  value       = aws_instance.monitoring.id
}

output "monitoring_public_ip" {
  description = "Elastic IP (public) address of the monitoring/orchestrator server"
  value       = aws_eip.monitoring.public_ip
}

output "monitoring_private_ip" {
  description = "Private IP address of the monitoring/orchestrator server"
  value       = aws_instance.monitoring.private_ip
}

output "sns_topic_arn" {
  description = "ARN of the SNS alert topic (email escalation for retry exhaustion, circuit breaker)"
  value       = aws_sns_topic.alerts.arn
}

output "monitoring_cpu_alarm_name" {
  description = "CloudWatch alarm name for the monitoring server CPU"
  value       = aws_cloudwatch_metric_alarm.monitoring_cpu.alarm_name
}
