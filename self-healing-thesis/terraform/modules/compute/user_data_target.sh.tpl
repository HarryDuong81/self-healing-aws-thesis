#!/usr/bin/env bash
# user_data_target.sh.tpl — Bootstrap script for target EC2 instances
# Rendered by Terraform templatefile(); placeholders: project, environment, index
set -euo pipefail
exec > >(tee /var/log/user-data.log | logger -t user-data) 2>&1

PROJECT="${project}"
ENVIRONMENT="${environment}"
INSTANCE_INDEX="${index}"

echo "=== Self-Healing Thesis — Target Instance $INSTANCE_INDEX Bootstrap ==="
echo "  Project     : $PROJECT"
echo "  Environment : $ENVIRONMENT"
echo "  Index       : $INSTANCE_INDEX"
echo "  Started     : $(date -u +%FT%TZ)"

# ── System update (retry/backoff around a flaky regional mirror — confirmed
# live: ap-southeast-1.ec2.archive.ubuntu.com intermittently returns 503
# Service Unavailable across a wide swath of packages, and under
# set -euo pipefail a single such failure previously aborted this script
# outright before it reached any of the actual target software below) ────────
apt_retry() {
  local max=5 n=0
  until apt-get "$@"; do
    n=$((n + 1))
    if [ "$n" -ge "$max" ]; then
      echo "apt-get $* failed after $max attempts" >&2
      return 1
    fi
    echo "apt-get $* failed (attempt $n/$max) - retrying in 15s..." >&2
    sleep 15
  done
}

apt_retry update -q
apt_retry upgrade -y -q
apt_retry install -y -q \
    ca-certificates curl gnupg lsb-release unzip jq \
    iproute2 iptables \
    stress-ng \
    nginx

# ── SSM Agent ────────────────────────────────────────────────────────────────
# Ubuntu 26.04 ships SSM Agent via snap; ensure it is installed and running.
if ! snap list amazon-ssm-agent 2>/dev/null | grep -q amazon; then
    snap install amazon-ssm-agent --classic || true
fi
snap start amazon-ssm-agent || true
snap set amazon-ssm-agent active=true || true

# Fallback: deb package
if ! systemctl is-active --quiet snap.amazon-ssm-agent.amazon-ssm-agent 2>/dev/null; then
    curl -fsSL \
      "https://s3.ap-southeast-1.amazonaws.com/amazon-ssm-ap-southeast-1/latest/debian_amd64/amazon-ssm-agent.deb" \
      -o /tmp/ssm.deb
    dpkg -i /tmp/ssm.deb || true
    systemctl enable amazon-ssm-agent
    systemctl start amazon-ssm-agent
    rm /tmp/ssm.deb
fi

# ── NGINX + /health endpoint ──────────────────────────────────────────────────
# Enable JSON access log format for Logstash grok
mkdir -p /var/log/nginx-json

cat > /etc/nginx/conf.d/health.conf << 'NGINX'
log_format json_combined escape=json
  '{"time":"$time_iso8601",'
  '"remote_addr":"$remote_addr",'
  '"method":"$request_method",'
  '"uri":"$request_uri",'
  '"status":$status,'
  '"bytes_sent":$bytes_sent,'
  '"request_time":$request_time,'
  '"upstream_response_time":"$upstream_response_time",'
  '"http_user_agent":"$http_user_agent"}';

server {
    listen 80 default_server;
    server_name _;

    access_log /var/log/nginx-json/access.log json_combined;
    error_log  /var/log/nginx/error.log;

    location /healthz {
        access_log off;
        add_header Content-Type application/json;
        return 200 '{"status":"ok","instance":"$hostname"}';
    }

    location /nginx_status {
        stub_status;
        allow 10.0.0.0/8;
        deny all;
    }

    location / {
        return 200 '<html><body><h1>Self-Healing Target $hostname</h1></body></html>';
        add_header Content-Type text/html;
    }
}
NGINX

# Remove default site
rm -f /etc/nginx/sites-enabled/default

nginx -t
systemctl enable nginx
systemctl restart nginx

# ── Node Exporter v1.7.0 ──────────────────────────────────────────────────────
NODE_EXPORTER_VERSION="1.7.0"
NODE_EXPORTER_URL="https://github.com/prometheus/node_exporter/releases/download/v$${NODE_EXPORTER_VERSION}/node_exporter-$${NODE_EXPORTER_VERSION}.linux-amd64.tar.gz"

useradd --system --no-create-home --shell /bin/false node_exporter 2>/dev/null || true

curl -fsSL "$NODE_EXPORTER_URL" -o /tmp/node_exporter.tar.gz
tar -xzf /tmp/node_exporter.tar.gz -C /tmp/
mv "/tmp/node_exporter-$${NODE_EXPORTER_VERSION}.linux-amd64/node_exporter" /usr/local/bin/
chmod +x /usr/local/bin/node_exporter
rm -rf /tmp/node_exporter.tar.gz "/tmp/node_exporter-$${NODE_EXPORTER_VERSION}.linux-amd64"

mkdir -p /var/lib/node_exporter/textfile

cat > /etc/systemd/system/node_exporter.service << 'UNIT'
[Unit]
Description=Prometheus Node Exporter
After=network.target

[Service]
Type=simple
User=node_exporter
Group=node_exporter
ExecStart=/usr/local/bin/node_exporter \
    --web.listen-address=:9100 \
    --collector.systemd \
    --collector.processes \
    --collector.textfile.directory=/var/lib/node_exporter/textfile
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable node_exporter
systemctl start node_exporter

# ── Configuration-drift detection (canonical_hash / running_hash) ────────────
# Thesis Table 3.3 / §3.4.1: the configuration_drift alert depends on two
# custom gauge series not emitted by Node Exporter natively. Published here
# by a one-minute systemd timer through the textfile-collector mechanism.
#
# Hashes conf.d/healthz.conf — the file experiments/inject/scenarios.yml's
# configuration_drift scenario actually modifies (an invalid directive
# appended via SSM). Using any other file (e.g. the package-default
# /etc/nginx/nginx.conf, which nothing here or in the injector ever
# touches) would mean the injected fault could never trip this alert.
#
# healthz.conf is NOT created earlier in this script — it is normally
# deployed later by ansible/bootstrap.yml's target_nodes play ("NGINX |
# Write /healthz endpoint config"), which runs only after terraform apply
# completes and SSM connectivity is confirmed. Snapshotting it here at
# first boot, before that file exists, previously failed with "cp: cannot
# stat ... No such file or directory" — and under `set -euo pipefail`
# (line 4), that aborted the rest of THIS script outright, silently
# skipping the drift-check timer below AND the CloudWatch Agent install
# further down (confirmed live: every target instance's user-data.log
# ends at this exact cp failure, config-drift-check.timer was never
# installed, and CloudWatch Agent was never installed either). Create the
# same content ansible/bootstrap.yml deploys so the snapshot has something
# valid to hash; Ansible's later write of identical content is then just
# an idempotent no-op, not a second source of truth.
cat > /etc/nginx/conf.d/healthz.conf << 'NGINX2'
server {
    listen 80;
    location /healthz {
        access_log off;
        return 200 "ok\n";
        add_header Content-Type text/plain;
    }
    location /nginx_status {
        stub_status;
        access_log off;
        allow 10.0.0.0/8;
        deny all;
    }
}
NGINX2
nginx -t
systemctl reload nginx

cp /etc/nginx/conf.d/healthz.conf /etc/nginx/conf.d/healthz.conf.canonical

cat > /usr/local/bin/config_drift_check.sh << 'DRIFTSCRIPT'
#!/usr/bin/env bash
# Publishes canonical_hash / running_hash gauges (Thesis Table 3.3) via the
# Node Exporter textfile collector. CRC32 (cksum) gives a small integer
# metric value; Prometheus gauges are numeric, not string-valued.
set -euo pipefail

CANONICAL_CONF="/etc/nginx/conf.d/healthz.conf.canonical"
RUNNING_CONF="/etc/nginx/conf.d/healthz.conf"
OUT="/var/lib/node_exporter/textfile/config_drift.prom"
TMP="$${OUT}.$$"

canonical_hash=$(cksum "$CANONICAL_CONF" 2>/dev/null | awk '{print $1}')
running_hash=$(cksum "$RUNNING_CONF" 2>/dev/null | awk '{print $1}')

cat > "$TMP" <<EOF
# HELP canonical_hash CRC32 of the boot-time canonical healthz.conf snapshot.
# TYPE canonical_hash gauge
canonical_hash $${canonical_hash:-0}
# HELP running_hash CRC32 of the currently running healthz.conf.
# TYPE running_hash gauge
running_hash $${running_hash:-0}
EOF
mv "$TMP" "$OUT"
DRIFTSCRIPT
chmod +x /usr/local/bin/config_drift_check.sh

cat > /etc/systemd/system/config-drift-check.service << 'UNIT'
[Unit]
Description=Publish canonical_hash/running_hash gauges for configuration_drift detection

[Service]
Type=oneshot
ExecStart=/usr/local/bin/config_drift_check.sh
UNIT

cat > /etc/systemd/system/config-drift-check.timer << 'UNIT'
[Unit]
Description=Run config-drift-check every minute (Thesis §3.4.1)

[Timer]
OnBootSec=30s
OnUnitActiveSec=60s

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now config-drift-check.timer
/usr/local/bin/config_drift_check.sh   # publish an initial reading immediately

# ── CloudWatch Agent ─────────────────────────────────────────────────────────
curl -fsSL "https://s3.amazonaws.com/amazoncloudwatch-agent/ubuntu/amd64/latest/amazon-cloudwatch-agent.deb" \
    -o /tmp/cwa.deb
dpkg -i /tmp/cwa.deb || apt_retry install -fy
rm /tmp/cwa.deb

# Metrics config: publishes mem_used_percent to the CWAgent namespace with
# an AutoScalingGroupName append-dimension only (NOT InstanceId — see
# below). EC2 has no native memory-utilisation metric, so the S3-baseline
# ASG's memory_exhaustion CloudWatch alarm (terraform/modules/compute/
# ../asg_baseline/main.tf) depends on this config existing — without it the agent binary is
# installed but publishes nothing, and that alarm sits permanently in
# INSUFFICIENT_DATA.
#
# InstanceId was deliberately removed from append_dimensions — confirmed
# live: CloudWatch alarms require an EXACT dimension-set match against the
# published metric, not a subset match. With both AutoScalingGroupName and
# InstanceId appended, the metric only ever existed under the combined
# 2-dimension set; asg_memory_exhaustion's alarm (which filters on
# AutoScalingGroupName alone, since InstanceId isn't stable across ASG
# instance replacement) could never match it and never fired, even though
# the agent was genuinely running and collecting correctly. AWS/EC2's own
# native CPUUtilization metric avoids this because AWS auto-publishes a
# separate AutoScalingGroupName-only rollup for ASG members; CWAgent custom
# metrics don't get that for free, so the append-dimension set must match
# exactly what the alarm expects.
# On non-ASG instances (target-1/2/3) the AutoScalingGroupName pseudo-
# dimension fails to resolve and is dropped, so the metric publishes with
# no dimensions at all there — harmless, since nothing consumes CWAgent's
# mem_used_percent for those instances (Prometheus/node_exporter is the
# actual source for their memory dashboards/alerts), so this config is
# still safe to share unconditionally between the S1/S2 targets and the
# ASG launch template's copy of this same script.
mkdir -p /opt/aws/amazon-cloudwatch-agent/etc
cat > /opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json << 'CWACONFIG'
{
  "metrics": {
    "namespace": "CWAgent",
    "append_dimensions": {
      "AutoScalingGroupName": "$${aws:AutoScalingGroupName}"
    },
    "metrics_collected": {
      "mem": {
        "measurement": ["mem_used_percent"],
        "metrics_collection_interval": 30
      }
    }
  }
}
CWACONFIG

/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl \
    -a fetch-config -m ec2 -s \
    -c file:/opt/aws/amazon-cloudwatch-agent/etc/amazon-cloudwatch-agent.json

# ── Placeholder dirs for Filebeat + OTel (Ansible installs these) ─────────────
mkdir -p /etc/filebeat /etc/otel-collector /opt/otel-collector

echo "=== Target $INSTANCE_INDEX bootstrap complete at $(date -u +%FT%TZ) ==="
