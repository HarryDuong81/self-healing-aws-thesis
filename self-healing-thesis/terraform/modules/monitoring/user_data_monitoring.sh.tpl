#!/usr/bin/env bash
# user_data_monitoring.sh.tpl — Bootstrap for monitoring/orchestrator server
# Rendered by Terraform templatefile(); variables: project, environment, aws_region
# =============================================================================
set -euo pipefail

# ── System baseline ───────────────────────────────────────────────────────────
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq

# ── Install base tools ────────────────────────────────────────────────────────
apt-get install -y -qq \
  curl wget git unzip jq htop \
  python3 python3-pip python3-venv \
  awscli \
  ansible \
  nginx

# ── Install Terraform ─────────────────────────────────────────────────────────
# Pinned to match Thesis Ch.4 Table 4.2 / terraform/*/backend.tf's
# `required_version = "~> 1.14"` — confirmed live: this had drifted to 1.7.5,
# an older version predating the pin bump, causing every root's `terraform
# init` to fail on this host with "Unsupported Terraform Core version".
TERRAFORM_VERSION="1.14.9"
curl -sLo /tmp/terraform.zip \
  "https://releases.hashicorp.com/terraform/$${TERRAFORM_VERSION}/terraform_$${TERRAFORM_VERSION}_linux_amd64.zip"
unzip -q /tmp/terraform.zip -d /usr/local/bin/
rm /tmp/terraform.zip
terraform version

# ── Install Docker + Compose ──────────────────────────────────────────────────
curl -fsSL https://get.docker.com | sh
systemctl enable docker
systemctl start docker
# Compose v2 plugin
mkdir -p /usr/local/lib/docker/cli-plugins
curl -sL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# ── Create orchestrator user + directory ──────────────────────────────────────
useradd -m -s /bin/bash orchestrator || true
usermod -aG docker orchestrator
usermod -aG sudo orchestrator

mkdir -p /opt/orchestrator/{results,logs,playbooks}
chown -R orchestrator:orchestrator /opt/orchestrator

# ── Set environment metadata ──────────────────────────────────────────────────
cat > /etc/environment << 'EOF'
PROJECT="${project}"
ENVIRONMENT="${environment}"
AWS_DEFAULT_REGION="${aws_region}"
ORCHESTRATOR_BASE_DIR="/opt/orchestrator"
EOF

# ── Tag instance for SSM inventory ───────────────────────────────────────────
INSTANCE_ID=$(curl -s --max-time 5 \
  -H "X-aws-ec2-metadata-token: $(curl -s -X PUT -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' \
    http://169.254.169.254/latest/api/token)" \
  http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null || echo "unknown")

aws ec2 create-tags \
  --region "${aws_region}" \
  --resources "$${INSTANCE_ID}" \
  --tags Key=NodeName,Value=monitoring Key=Role,Value=monitoring \
  2>/dev/null || true

echo "[user-data] Monitoring server bootstrap complete. Instance: $${INSTANCE_ID}"
