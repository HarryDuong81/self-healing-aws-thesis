# Observability-Driven Self-Healing Infrastructure

**MSc Thesis Artefact — Darmstadt University of Applied Sciences / Vietnamese-German University**
Author: Duong Tat Hung · Supervisor: Prof. Dr.-Ing. Michael von Rüden

> An empirical evaluation of Terraform and Ansible remediation strategies on AWS EC2.
> The pipeline implements a closed-loop MAPE-K control loop that detects six canonical
> fault classes and remediates them within a 300-second SLO target.

---

## Table of Contents

1. [Technical Architecture Overview](#1-technical-architecture-overview)
2. [Toolchain and Pinned Versions](#2-toolchain-and-pinned-versions)
3. [Repository Layout](#3-repository-layout)
4. [Prerequisites and Local Pre-flight Check](#4-prerequisites-and-local-pre-flight-check)
5. [Bootstrapping Guide](#5-bootstrapping-guide)
6. [Observability Stack and Orchestrator Deployment](#6-observability-stack-and-orchestrator-deployment)
7. [Remediation Operational Detail](#7-remediation-operational-detail)
8. [Running the Experimental Campaign](#8-running-the-experimental-campaign)
9. [Testing Runbook](#9-testing-runbook)
10. [Tear Down](#10-tear-down)
11. [Architecture Invariants](#11-architecture-invariants)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Technical Architecture Overview

### 1.1 Spatial Topology

The lab occupies a single Availability Zone in `ap-southeast-1`. Targets accept no
inbound connections from anywhere except the monitoring server. The monitoring
server's `sg-orchestrator` security group exposes ports 22, 3000, 5601, 9090, and
16686 directly to `researcher_cidr` (a deliberate direct-port-exposure design, not a
reverse proxy) — see [`terraform/README.md`](terraform/README.md) §3 for the full
rationale.

```
ap-southeast-1a
├── VPC 10.0.0.0/16
│   ├── Public Subnet 10.0.1.0/24
│   │   ├── NAT Gateway (EIP)           ← targets egress via NAT
│   │   └── Monitoring Server (t3.medium, EIP)
│   │       ├── Prometheus     :9090
│   │       ├── Alertmanager   :9093
│   │       ├── Grafana        :3000
│   │       ├── Elasticsearch  :9200
│   │       ├── Kibana         :5601
│   │       ├── Logstash       :5044   ← Filebeat ingest
│   │       ├── Jaeger UI      :16686
│   │       ├── OTel Collector :4317   ← OTLP gRPC
│   │       ├── cAdvisor       :8080   ← RQ3 container metrics
│   │       └── Orchestrator   :9094   ← systemd service, Alertmanager webhook
│   └── Private Subnet 10.0.2.0/24
│       ├── target-1 (t3.micro)  ← Node Exporter :9100, NGINX :80
│       ├── target-2 (t3.micro)
│       └── target-3 (t3.micro)
│
└── SSM VPC Interface Endpoints
    ├── com.amazonaws.ap-southeast-1.ssm
    ├── com.amazonaws.ap-southeast-1.ssmmessages
    └── com.amazonaws.ap-southeast-1.ec2messages
```

**Transport, precisely:** fault injection (`SendCommand`), the Plan layer's liveness
gate (`PingStatus`), and manual operator access all go over AWS SSM through the three
VPC Interface Endpoints above — no inbound port is required on targets for any of
these. **Ansible remediation dispatch is different**: it runs over direct SSH (port
22) from the monitoring server to each target's `sshd`, using a Terraform-generated
key pair. Port 22 on `sg-targets` is scoped exclusively to `sg-orchestrator` — never
to `researcher_cidr` or `0.0.0.0/0`. This is a deliberate reversal of an earlier,
SSM-only design, adopted after a reproducible defect was isolated in the
`community.aws.aws_ssm` connection plugin under automated, multi-step orchestrator
execution (the plugin's session reliably began remote execution but never delivered
the completion message back over the WebSocket). See §3.2.2 of the architecture
chapter for the full account.

### 1.2 Temporal Data Path (Alert Flow)

The following sequence traces a `cpu_overload` event from injection to MTTR record:

```
t0  Fault injected       stress-ng spawns CPU load on target-2
    (scrape interval: 15 s)
t1  Metric collected     Prometheus scrapes node_exporter on target-2
    (rule evaluation: 15 s)
t2  Alert fires          CPUOverload rule: idle_cpu < 15% for 30 s -> FIRING
    (group_wait: 5 s + route match)
t3  Webhook delivered    Alertmanager POSTs to orchestrator:9094/alert
                          -> MTTRLogger.record_detection()  [T_detect = t3 - t0]
    (classify_and_route)
t4  Routing decided      fault_type=cpu_overload -> ansible / kill_runaway_process.yml
                          -> MTTRLogger.record_classification()  [T_classify = t4 - t3]
    (Plan: CB + lease + transport)
t5  Ansible dispatched   ansible-playbook --limit target-2  (SSH, port 22, connection
                          type set by ansible/inventory/group_vars/target_nodes/vars.yml)
                          -> SIGKILL rogue process, sweep siblings, verify load drop
                          -> MTTRLogger.record_remediation()  [T_remediate = t5 - t4]
    (HealthChecker polls the Prometheus ALERTS series every 5 s — never the
     target directly; the Execute pillar does not read sensor data)
t6  Alert clears         Prometheus evaluates CPUOverload -> RESOLVED
                          -> HealthChecker observes an empty ALERTS query result
                          -> MTTRLogger.record_validation()  [T_validate = t6 - t5]

    MTTR record written   JSON Lines record only (no CSV), S3 mirror when the
                          harness is invoked with --out
                          Phase-sum invariant asserted: |sum(Ti) - T_MTTR| < 1 ms
```

`instance_failure` trials splice a fifth phase, `T_observability_restore`, between
`T_remediate` and `T_validate` — the Execute layer restores Filebeat and the OTel
Agent on the recreated instance, and the Knowledge layer's `ObservabilityChecker`
independently confirms both are genuinely forwarding data before the trial can be
recorded as successful. See §7.1 and §11.

**Decomposed MTTR formula:**

```
T_MTTR = T_detect + T_classify + T_remediate + T_validate = t_clear - t0

# instance_failure only:
T_MTTR = T_detect + T_classify + T_remediate + T_observability_restore + T_validate
```

### 1.3 Three-Pillar Observability

| Pillar | Technology | Role in Pipeline |
|--------|-----------|-----------------|
| **Metrics** | Prometheus + Node Exporter | **Detection** — alert rules trigger remediation |
| **Logs** | Filebeat -> Logstash -> Elasticsearch | **Diagnostic** — post-trial explainability only |
| **Traces** | OTel SDK -> OTel Collector -> Jaeger | **Diagnostic** — per-span MAPE-K phase timing |

Log and trace signals are **not** wired into the runtime alert path. On
`instance_failure` trials only, the Filebeat/OTel Agent forwarding check performed by
`ObservabilityChecker` gates trial success — but that check still reads its own
dedicated verification signal (Elasticsearch document arrival, OTel self-telemetry
counters), not the log/trace *content* as a detection trigger. Routing log- or
trace-derived signals into the runtime alert path more broadly would require a
thesis update.

---

## 2. Toolchain and Pinned Versions

These versions are load-bearing for reproducibility. Changing them between campaign
trials invalidates the dataset and requires recompiling the thesis. Run
`make verify-pins` to diff the installed toolchain against `.toolchain`.

| Component | Version | Notes |
|-----------|---------|-------|
| Terraform | **1.14.9** | `~> 1.14` in `required_version` |
| AWS Terraform Provider | **~5.x** | Lock file committed |
| ansible-core | **2.17.x** | `pip install "ansible-core>=2.17,<2.18"` |
| community.aws collection | **9.x** | `ansible-galaxy collection install "community.aws:>=9.0.0,<10.0.0"` |
| Python (orchestrator, monitoring host) | Native AMI interpreter (Ubuntu 26.04 LTS ships Python 3.14.4) | The orchestrator venv is built directly against this interpreter — see `ansible/roles/self_healing/tasks/main.yml`. `.toolchain`'s `python=3.11` entry and `orchestrator/Dockerfile`'s `python:3.12-slim` base image are both stale; the Dockerfile is not part of the live deployment path (the orchestrator runs as a systemd service on the monitoring host's native Python, not in a container) |
| Docker Engine | **29.8.1** | Installed via the `get.docker.com` convenience script during Ansible bootstrap; this is the version on the monitoring host during the primary campaign. `.toolchain` checks `29.8`; the script itself enforces no exact pin |
| Docker Compose plugin | **5.5.1** | As reported by `docker compose version` on the monitoring host during the primary campaign; `.toolchain` checks `5.5`. `ansible/bootstrap.yml` separately downloads a standalone `docker-compose` v2.24.0 binary and links it as the CLI plugin, which differs from the version the host reported |
| AWS CLI | **2.31.35** | Ubuntu `awscli` package (installed by `ansible/roles/common` and the monitoring host `user_data`); not machine-checked |
| Prometheus | **3.11.3** | `prom/prometheus:v3.11.3` |
| Alertmanager | **0.32.1** | `prom/alertmanager:v0.32.1` |
| Grafana | **13.0** | `grafana/grafana:13.0` — `13.0.0` was never published to Docker Hub; confirmed live against the registry API |
| Elasticsearch / Logstash / Kibana | **9.4.1** | `docker.elastic.co/{elasticsearch,logstash,kibana}/...:9.4.1` |
| Jaeger | **2.18.0** | `jaegertracing/jaeger:2.18.0` |
| OTel Collector | **0.152.0** | `otel/opentelemetry-collector-contrib:0.152.0` |
| cAdvisor | **0.49.1** | `gcr.io/cadvisor/cadvisor:v0.49.1` |
| Node Exporter (targets) | **1.7.0** | systemd unit, installed by `user_data_target.sh.tpl` |
| AMI | Ubuntu 26.04 LTS (resolute) | `ami-0532913178263be11`, `ap-southeast-1` — pinned by `image-id` filter |

---

## 3. Repository Layout

```
self-healing-thesis/
├── terraform/
│   ├── bootstrap/               Shared infra workspace — applied once
│   │   ├── main.tf              Module wiring: storage -> networking -> iam -> monitoring
│   │   ├── backend.tf           S3 backend + DynamoDB locking (two-phase bootstrap)
│   │   └── variables.tf
│   ├── targets/                 Remediation-scope workspace — the ONLY workspace
│   │   │                        on which automated remediation operates
│   │   ├── main.tf              Reads bootstrap outputs; invokes module "compute"
│   │   ├── backend.tf           S3 backend + DynamoDB locking
│   │   └── variables.tf
│   └── modules/
│       ├── networking/          VPC, subnets, NAT, 3 SGs, SSM VPC endpoints; standalone
│       │                        aws_security_group_rule resources only (no inline
│       │                        ingress blocks — the two patterns fight each other)
│       ├── iam/                 Shared instance profile (SSM + CW + S3 + EC2 + DynamoDB)
│       ├── storage/              S3 state bucket + S3 data bucket + DynamoDB lock table
│       ├── monitoring/          Monitoring server EC2 + EIP + SNS topic
│       ├── compute/             Target instances (for_each) + user_data template
│       └── asg_baseline/        S3-baseline ASG, alarms, scaling policies
├── ansible/
│   ├── ansible.cfg               Header comment describes the superseded SSM-only
│   │                              model — group_vars/ (below) governs actual behaviour
│   ├── inventory/
│   │   ├── aws_ec2.yml           Dynamic inventory (aws_ec2 plugin) — no static hosts file
│   │   └── group_vars/
│   │       ├── monitoring_host/vars.yml   ansible_connection: community.aws.aws_ssm
│   │       └── target_nodes/vars.yml      ansible_connection: ssh — this file, not
│   │                                       ansible.cfg, governs remediation transport
│   ├── bootstrap.yml             System deps + telemetry-agent bootstrap (2 plays):
│   │                              monitoring_host (Docker/Compose/Terraform/venv) and
│   │                              target_nodes (Node Exporter, NGINX, Filebeat, OTel
│   │                              Agent). Does NOT start the observability stack or
│   │                              deploy the orchestrator — see playbooks/deploy.yml.
│   ├── site.yml                  Master playbook — imports all 6 fault-class playbooks
│   │                              by tag; manual/integration-test entry point, not the
│   │                              runtime dispatch path
│   ├── playbooks/
│   │   ├── kill_runaway_process.yml   cpu_overload
│   │   ├── memory_cleanup.yml         memory_exhaustion
│   │   ├── disk_cleanup.yml           disk_saturation
│   │   ├── service_crash.yml          service_crash
│   │   ├── config_reapply.yml         configuration_drift
│   │   ├── diagnostic_agents.yml      instance_failure only — restores Filebeat + OTel
│   │   │                              Agent on the recreated instance
│   │   └── deploy.yml                 Full stack deploy: common -> monitoring_stack
│   │                                  (starts Docker Compose) -> self_healing (deploys
│   │                                  + starts the orchestrator) -> target_node (legacy)
│   └── roles/
│       ├── common/               Baseline packages on all hosts
│       ├── apt_lock_guard/       Waits out cloud-init + apt/dpkg locks
│       ├── filebeat/             Diagnostic-path agent — shared by bootstrap.yml and
│       │                        diagnostic_agents.yml
│       ├── otel_collector/       Same sharing pattern as roles/filebeat
│       ├── monitoring_stack/     Docker Compose observability stack — the role that
│       │                        actually issues `docker compose up`
│       ├── self_healing/         Flask orchestrator (systemd service + orchestrator.env)
│       └── target_node/          Legacy — superseded by roles/filebeat + roles/otel_collector;
│                                  not confirmed dead, still wired into deploy.yml
├── orchestrator/
│   ├── app.py                    Flask entry point (/alert, /metrics, /healthz)
│   ├── classifier.py             YAML-backed fault classifier + priority matrix
│   ├── plan.py                   CircuitBreaker, LeaseTracker, transport/drift validation,
│   │                              DynamoDB stale-lock precondition
│   ├── execute.py                Ansible (SSH) + Terraform actuator mechanism;
│   │                              restore_diagnostic_agents() for instance_failure
│   ├── mttr_logger.py            JSON Lines record + phase-sum invariant + trial correlation
│   ├── health_checker.py         Stage E3 probe + canonical Prometheus-ALERTS poll
│   ├── observability_checker.py  Diagnostic-agent forwarding verification (instance_failure)
│   ├── alert_log.py              Alert-receipt log — RQ4 false-positive-rate population
│   ├── routing.yaml              Declarative fault-to-actuator mapping
│   └── Dockerfile                Not part of the live deployment path — see §2
├── observability/
│   ├── docker-compose.yml        Eight services + cAdvisor
│   ├── prometheus/
│   │   └── rules/fault_classes.yml
│   ├── alertmanager/
│   ├── grafana/
│   ├── kibana/                   Best-effort — frequently OOM-crashes at its current
│   │                             container memory limit; not a blocker for any RQ
│   ├── logstash/
│   └── otelcol/
│       └── config.yaml
├── experiments/
│   ├── inject/                   inject_once.py + scenarios.yml (dev/CI smoke-test path)
│   ├── trials/
│   │   ├── run_campaign.py       Automated 60-trial S1 campaign — see §8 (S3 run separately, run_s3_trial.py)
│   │   ├── run_one.py            Single-trial harness, --system S1|S3 only
│   │   ├── run_manual_trial.py   S2 (manual SRE) guided procedure — see §8
│   │   ├── run_s3_trial.py       S3 (AWS Auto Scaling) trial runner
│   │   ├── trial_config.yml, wait_steady_state.py, capture_host_baseline.py
│   └── correlation/              correlate.py — harness/orchestrator trial-ID correlation
├── analysis/                     Plain Python scripts, NOT Jupyter notebooks
│   ├── 01_descriptive.py
│   ├── 02_wilcoxon.py
│   ├── h3_upper_bound.py         Not "03_"-prefixed on disk
│   ├── 04_overhead.py
│   └── 05_false_positive_rate.py RQ4 — needs alert_log.jsonl, correlation.jsonl, mttr_log*.jsonl
├── results/
└── tests/
    ├── unit/                     See §9.1 for the current module list
    └── integration/              8 scenarios — see §9.2
```

---

## 4. Prerequisites and Local Pre-flight Check

### 4.1 Required tools

```bash
# Terraform 1.14.9 (use tfenv)
tfenv install 1.14.9 && tfenv use 1.14.9
terraform version   # Terraform v1.14.9

# ansible-core 2.17.x + community.aws 9.x
pip install --upgrade "ansible-core>=2.17,<2.18"
ansible-galaxy collection install "community.aws:>=9.0.0,<10.0.0"
ansible --version   # ansible [core 2.17.x]

# Docker Engine (any current version — no exact pin enforced locally)
docker --version

# Python for local harness/analysis scripts — any Python 3.11+ works locally;
# only the orchestrator's own venv on the monitoring host is pinned to that
# host's native interpreter (see §2)
pip install -r requirements.txt

# ruff linter
pip install ruff
```

### 4.2 AWS credential configuration

Required IAM permissions: EC2, SSM, S3, DynamoDB, IAM, VPC, SNS, CloudWatch, Auto
Scaling (all Full Access is the simplest starting point for a lab environment).

```bash
aws configure --profile self-healing
# Default region: ap-southeast-1

export AWS_PROFILE=self-healing
aws sts get-caller-identity   # verify
```

### 4.3 Pre-flight check (`make pre-flight`)

All checks must pass before deployment or campaign execution:

```bash
make pre-flight
```

Equivalent to:

```bash
# Terraform format + validate
terraform -chdir=terraform/bootstrap fmt -check -recursive
terraform -chdir=terraform/bootstrap validate
terraform -chdir=terraform/targets   fmt -check -recursive
terraform -chdir=terraform/targets   validate

# Ansible lint + syntax check
ansible-lint ansible/
ansible-playbook ansible/playbooks/*.yml --syntax-check

# Orchestrator: lint + unit tests
ruff check orchestrator/
pytest tests/unit/ -v

# Compose validation
docker compose -f observability/docker-compose.yml config -q
```

---

## 5. Bootstrapping Guide

> Full command-by-command detail, including the two-phase S3+DynamoDB bootstrap
> rationale and troubleshooting, lives in [`terraform/README.md`](terraform/README.md).
> This section is the condensed path.

### 5.1 Bootstrap workspace (run once — shared infrastructure)

```bash
cd terraform/bootstrap
nano terraform.tfvars   # researcher_cidr, state_bucket_name, data_bucket_name, sns_email

# Phase 1 — comment out the backend "s3" {} block in backend.tf first
terraform init
terraform plan
terraform apply -auto-approve

# Phase 2 — uncomment the backend "s3" {} block, then migrate state
terraform init -migrate-state \
  -backend-config="bucket=$(terraform output -raw state_bucket_name)" \
  -backend-config="region=ap-southeast-1" \
  -backend-config="dynamodb_table=$(terraform output -raw dynamodb_table_name)"
# Type 'yes' to copy state to S3

# Capture required outputs
BUCKET_NAME=$(terraform output -raw state_bucket_name)
TABLE_NAME=$(terraform output -raw dynamodb_table_name)
MONITORING_IP=$(terraform output -raw monitoring_host_ip)
MONITORING_ID=$(terraform output -raw monitoring_instance_id)
```

### 5.2 Deploy the targets workspace

```bash
cd ../targets
nano terraform.tfvars
# ubuntu_ami_id          = "ami-0532913178263be11"   (from bootstrap output ubuntu_ami_id)
# bootstrap_state_bucket = "${BUCKET_NAME}"

terraform init \
  -backend-config="bucket=${BUCKET_NAME}" \
  -backend-config="dynamodb_table=${TABLE_NAME}" \
  -backend-config="region=ap-southeast-1"

terraform plan
terraform apply -auto-approve

terraform output -json target_private_ips
# { "target-1": "10.0.2.10", "target-2": "10.0.2.11", "target-3": "10.0.2.12" }
```

### 5.3 No manual inventory step needed

`ansible/inventory/aws_ec2.yml` is a **dynamic inventory** (the `aws_ec2` plugin,
filtered by the `Project`/`Workspace`/`NodeName` tags Terraform applies) — there is
no static hosts file to generate or edit.

```bash
cd ../../ansible
ansible-inventory -i inventory/aws_ec2.yml --graph   # verify grouping
```

### 5.4 Verify SSM registration (allow a few minutes after apply)

```bash
aws ssm describe-instance-information \
  --region ap-southeast-1 \
  --query 'InstanceInformationList[*].[InstanceId,PingStatus,ComputerName]' \
  --output table
# All four managed instances (monitoring + 3 targets) must show PingStatus = Online
```

---

## 6. Observability Stack and Orchestrator Deployment

Deployment requires **two** Ansible entry points, in order — they cover
non-overlapping responsibilities:

```bash
cd ansible

# 1. System dependencies + telemetry agents
ansible-playbook bootstrap.yml -i inventory/aws_ec2.yml --limit monitoring_host
ansible-playbook bootstrap.yml -i inventory/aws_ec2.yml --limit target-1,target-2,target-3

# 2. Start the observability stack and deploy the orchestrator
ansible-playbook playbooks/deploy.yml -i inventory/aws_ec2.yml --tags monitoring,orchestrator
```

`bootstrap.yml` installs Docker/Compose/Terraform/the base Python-Ansible venv on the
monitoring host and Node Exporter/NGINX/Filebeat/OTel Agent on the targets, but it
does **not** start the Docker Compose stack and does **not** deploy the orchestrator
itself. `playbooks/deploy.yml --tags monitoring` (the `monitoring_stack` role) is
what actually issues `docker compose up`; `--tags orchestrator` (the `self_healing`
role) deploys the Flask orchestrator's venv, `orchestrator.env`, and systemd service.
Export `DYNAMODB_LOCK_TABLE` and `SNS_TOPIC_ARN` before the second command — see
`ansible/inventory/group_vars/monitoring_host/vars.yml` for the exact lookup pattern.

**Verify:**

```bash
curl -sf "http://${MONITORING_IP}:9094/healthz" | jq .
# { "status": "ok", ... }

curl -sf "http://${MONITORING_IP}:9090/api/v1/alerts" | \
  jq '.data.alerts[] | {labels: .labels, state: .state}'
# Confirm steady state — all alert rules must be inactive
```

**Manual re-deployment / debugging only** — connect via SSM Session Manager (no SSH
required for this) and drive Docker Compose directly:

```bash
aws ssm start-session --target ${MONITORING_ID} --region ap-southeast-1
docker compose -f /opt/monitoring/docker-compose.yml ps
docker compose -f /opt/monitoring/docker-compose.yml logs -f
journalctl -u orchestrator -f
```

---

## 7. Remediation Operational Detail

### 7.1 Alert-to-actuator call chain

```
Node Exporter (target-2:9100)
  |  /metrics scrape (15 s interval)
  v
Prometheus — fault_classes.yml rule evaluation
  CPUOverload: 100 - idle_cpu > 85 for 30s -> labels: {fault_type: cpu_overload}
  |  alert fires -> Alertmanager
  v
Alertmanager (9093) — route matches fault_type label
  |  POST http://<docker-bridge-gateway>:9094/alert
  v
app.py /alert handler
  |  priority-resolution: weight[cpu_overload] = 60
  |  dedup check: (target-2, cpu_overload) not in suppressor (15 s TTL)
  |  every alert logged to results/alert_log.jsonl, matching or suppressed (RQ4)
  v
plan.py — four preconditions (canonical order):
  1. CircuitBreaker.check("target-2", "cpu_overload")  -> "closed" (1/3 alerts)
  2. LeaseTracker.acquire("target-2")                  -> True (TTL 600 s)
  3. transport_validation("target-2", instance_id)     -> SSM PingStatus=Online
  4. drift_validation() — SKIPPED (Ansible path; Terraform path only)
  v
execute.py dispatch(actuator="ansible", action="kill_runaway_process.yml")
  |  ansible-playbook kill_runaway_process.yml --limit target-2
  |    -> SSH (port 22, sg-orchestrator -> sg-targets) -> target-2's sshd
  |    -> ps -eo pid,pcpu,comm --sort=-pcpu
  |    -> SIGKILL rogue PID + sibling sweep (pgrep -x comm)
  |    -> until: load1/nproc*100 < 80% (retry with 1s, 4s backoff)
  |    -> curl localhost/healthz -> HTTP 200
  |  rc=0
  v
health_checker.py HealthChecker.wait_until_healthy(timeout=30s)
  |  poll Prometheus ALERTS{fault_type="cpu_overload",node_name="target-2",
  |    alertstate="firing"} every 5 s -> empty result = alert cleared
  |  (never probes target-2 directly — the Execute pillar does not read
  |   sensor data; validation is delegated to the Monitor -> Analyse channel)
  v
mttr_logger.py MTTRLogger.save(recovery_status="success")
  |  assert |T_detect + T_classify + T_remediate + T_validate - T_MTTR| < 1 ms
  |  append JSON Lines record -> results/mttr_log.jsonl
  v
  LeaseTracker.release("target-2")
```

`instance_failure` inserts an additional Execute→Knowledge handoff between
`T_remediate` and `T_validate`: `execute.py::restore_diagnostic_agents()` (SSH,
act-only) runs `diagnostic_agents.yml` against the recreated instance, then
`observability_checker.py::ObservabilityChecker` (read-only) independently confirms
Filebeat and the OTel Agent are genuinely forwarding data before `T_validate` begins.
A failure at either step escalates with `escalation_reason=diagnostic_bootstrap_failed`
without ever reaching `HealthChecker`.

### 7.2 Six canonical fault classes

| `fault_type` | Alert threshold | `for` | Severity | Actuator | Max retries |
|---|---|---|---|---|---|
| `cpu_overload` | `idle_cpu < 15%` | 30 s | high | Ansible | 2 |
| `memory_exhaustion` | `MemAvailable/MemTotal < 0.10` | 30 s | high | Ansible | 2 |
| `disk_saturation` | `fs_free < 10%` | 60 s | high | Ansible | 2 |
| `service_crash` | `node_systemd_unit_state{name="nginx.service", state="active"} == 0` | 30 s | critical | Ansible | 2 |
| `configuration_drift` | `canonical_hash != running_hash` | 30 s | medium | Ansible | 1 |
| `instance_failure` | `up{job="node_exporter"} == 0` | 60 s | critical | Terraform | **0** |

### 7.3 Geometric backoff retry schedule

For the four resource/service Ansible fault classes (max_retries = 2):

| Attempt | Backoff before | Source in routing.yaml |
|---------|---------------|----------------------|
| 1 (initial) | — | — |
| 2 (retry 1) | 1 s | `backoff_seconds[0]` |
| 3 (retry 2) | 4 s | `backoff_seconds[1]` |

`configuration_drift` (max_retries = 1): `backoff_seconds = [1]`. `instance_failure`
(max_retries = 0): no retries — a failed `terraform apply` may leave the state
backend partially locked, and retrying without manual inspection risks
double-provisioning or state corruption.

### 7.4 Priority-resolution matrix (15-second deduplication window)

```
Scalar severity weights:
  instance_failure    100   (EC2 gone — highest impact)
  service_crash        80   (NGINX down — customer-facing)
  cpu_overload         60   (shared weight)
  memory_exhaustion    60
  disk_saturation      40
  configuration_drift  20   (eventual consistency acceptable)
```

When multiple alerts fire within 15 seconds for the same `(target, fault_type)`,
the highest-weight alert is actioned; siblings are suppressed for 15 s but still
logged to `results/alert_log.jsonl` (they count toward the RQ4 denominator).

---

## 8. Running the Experimental Campaign

### 8.1 Campaign design

| System | Label | Description | How it's run | Trials |
|--------|-------|-------------|--------------|--------|
| S1 | Self-healing pipeline | MAPE-K automated remediation | `run_campaign.py` (automated) | 60 (10×6 fault classes) |
| S3 | AWS Auto Scaling | ASG step-scaling / instance replacement | `run_s3_trial.py`, one trial at a time | 30 (10×3 — `cpu_overload`, `memory_exhaustion`, `instance_failure` only), run separately |
| S2 | Manual SRE response | Human operator follows runbook | `run_manual_trial.py`, one trial at a time | 60 (10×6), run separately |

`run_campaign.py` schedules **60 trials, S1 only.** Neither S2 nor S3 is dispatched
by this harness, for the same underlying reason: an earlier revision did build a
30-row S3 schedule and dispatch it through the same code path as S1, but
`run_one.py`'s `--system` flag never actually selected different behaviour — every
"S3" trial that produced was silently recorded as an ordinary S1 trial (the
orchestrator always stamps `system_under_test` from its own `SYSTEM_UNDER_TEST` env
var, which defaults to `"S1"`), and the failure only surfaced later, at analysis
time. S3 is now run exclusively via `run_s3_trial.py`, the same way S2 is run via
`run_manual_trial.py` — deliberately, by a human, once its prerequisite
infrastructure is confirmed up.

S2 refuses to start unless `orchestrator.service` is stopped first (so no automated
remediation can race the human responder), prints the manual fix command for the
operator to run in a separate SSM session, and polls the same canonical Prometheus
`ALERTS` series for `t_clear`. Reproducing the full 150-trial thesis dataset means
running the 60-trial automated S1 campaign, the 30 S3 trials, *and* 60 manual S2
trials (10 per fault class) — three separate invocations, not one.

S3 requires the S3-baseline ASG, which lives in its own Terraform root and state
(`terraform/baseline/`). It is not created by any apply against `terraform/targets`
and cannot be destroyed by one either — that separation is what stops an automated
`instance_failure` remediation from taking the comparison system down mid-campaign.
Run it from the workstation; the monitoring host has no copy of this root.

```bash
cd terraform/baseline
terraform init -backend-config=../bootstrap/outputs.backend.tfvars
terraform apply -auto-approve
# ... run S3 trials, e.g.: python experiments/trials/run_s3_trial.py --fault instance_failure ...
terraform destroy -auto-approve    # closes the S3 window; the baseline keeps
                                   # incurring cost for as long as it exists
```

### 8.2 Run the automated S1 campaign

```bash
# Confirm steady state first
python experiments/trials/wait_steady_state.py --window 60

# Dry run (prints the schedule, injects nothing)
python experiments/trials/run_campaign.py \
  --config experiments/trials/trial_config.yml --dry-run

# Live run
python experiments/trials/run_campaign.py \
  --config experiments/trials/trial_config.yml \
  --out s3://its-thesis-data/campaign/
```

`--start-from N` resumes an interrupted campaign at trial N.

### 8.3 Run the manual S2 trials

```bash
python experiments/trials/run_manual_trial.py --fault cpu_overload --target target-1
# repeat for the remaining fault classes/targets, 10 per fault class
```

Records are written to `results/mttr_log_s2.jsonl`, a separate file from S1/S3 —
only the aggregate `T_MTTR_seconds` is treated as directly comparable to S1; the
sub-phase fields are human-response approximations, not instrumented measurements.

### 8.4 Inject a single fault for development testing

```bash
python experiments/inject/inject_once.py \
  --target target-2 --fault cpu_overload --duration 120

# Tail orchestrator logs
journalctl -u orchestrator -f
```

### 8.5 Campaign integrity guards

Do **not** between trials of a single campaign run:

- Modify `routing.yaml`, `fault_classes.yml`, alert thresholds, or backoff schedule
- Restart any observability container during an active trial window
- Use `-target` in any Terraform invocation
- Subtract polling-lag from the observed 95th-percentile T_MTTR

---

## 9. Testing Runbook

### 9.1 Unit tests

```bash
pytest tests/unit/ -v --cov=orchestrator --cov-report=term-missing
```

| Module | What is tested |
|--------|---------------|
| `test_classifier.py` | All 6 fault types resolve to correct actuator and playbook |
| `test_plan.py` | `CircuitBreaker` Closed->Open at θ=3, Half-Open after Tcool; `LeaseTracker` acquire/release/TTL |
| `test_mttr_logger.py` | Phase-sum invariant; JSONL schema completeness (four- and five-phase) |
| `test_retry.py` | Routing-table backoff schedule per fault class |
| `test_drift_validation.py` | `terraform plan -refresh-only -json` exit-code handling; DynamoDB stale-lock precondition |
| `test_health_checker.py` | Canonical Prometheus-ALERTS poll; Stage E3 HTTP probe |
| `test_priority_arbitration.py` | Severity-weight sort order; 15 s suppressor-cache TTL |
| `test_transport_validation.py` | SSM `PingStatus` check — Online/Offline/absent-record/exception paths |
| `test_alert_log.py` | Alert-receipt record shape (RQ4 population) |
| `test_false_positive_rate.py` | Trial-window construction and alert classification (RQ4) |

### 9.2 Integration tests — 300-second SLO acceptance gate

Eight scenarios exercise the full MAPE-K pipeline against a staging environment
(a separate deployment of the same `terraform/bootstrap` + `terraform/targets`
workspaces under a different `environment` value):

```bash
INTEGRATION_TESTS=1 pytest tests/integration/ -v --timeout=600
```

| # | fault_type | Injection method | Acceptance criterion |
|---|---|---|---|
| 1 | cpu_overload | `stress-ng --cpu 2 --timeout 120s` | `recovery_status=success`; T_MTTR < 300 s |
| 2 | memory_exhaustion | `stress-ng --vm 1 --vm-bytes 90%` | T_MTTR < 300 s |
| 3 | disk_saturation | `fallocate` fills the root volume | T_MTTR < 300 s |
| 4 | service_crash | `systemctl stop nginx` | T_MTTR < 300 s |
| 5 | configuration_drift | Invalid directive appended to `conf.d/healthz.conf` | T_MTTR < 300 s |
| 6 | instance_failure | `aws ec2 terminate-instances` | T_MTTR < 250 s (180 s validation window) |
| 7 | retry_exhaustion | Real fault against a deliberately-broken staging target (`RETRY_EXHAUSTION_TARGET` env var); skips cleanly if unset | `escalation_reason == "retry_exhausted"` |
| 8 | transport_validation | Synthetic webhook post with a non-existent `instance_id` (deterministic, no staging fixture needed) | `escalation_reason == "transport_unreachable"` |

**Acceptance criterion (scenarios 1–6):**

```python
assert rec["T_MTTR_seconds"] < 300, (
    f"SLO violation: {rec['fault_type']} "
    f"T_MTTR={rec['T_MTTR_seconds']:.1f}s >= 300s"
)
```

### 9.3 H3 upper-bound calculation

```bash
python analysis/h3_upper_bound.py \
  --campaign s3://its-thesis-data/campaign/ \
  --system S1
```

Reports the 95th-percentile of raw `T_MTTR_obs` for S1. This is an **upper bound**
on the true T_MTTR, not an estimate — do not subtract the worst-case polling lag
(20 s) from this figure; doing so invalidates the monotonicity argument required
for the H3 hypothesis test.

---

## 10. Tear Down

Destroy resources in reverse dependency order:

```bash
# 1. Target instances (and the S3-baseline ASG, if still enabled)
cd terraform/targets
terraform destroy -auto-approve

# 2. Observability stack (if tearing down without also destroying the monitoring
#    host — otherwise this is implicit in step 3)
ssh ubuntu@"$MONITORING_IP" "docker compose -f /opt/monitoring/docker-compose.yml down -v"

# 3. Shared infrastructure (DELETES the S3 state/data buckets and DynamoDB table —
#    mirror results first)
aws s3 sync s3://its-thesis-data/campaign/ ./results/campaign-backup/
cd ../bootstrap
terraform destroy -auto-approve
```

The S3 state bucket and DynamoDB lock table have `prevent_destroy = true` by
default; see [`terraform/README.md`](terraform/README.md) §12 for how to remove
them deliberately once results are safely mirrored elsewhere.

---

## 11. Architecture Invariants

| Invariant | Enforced by |
|-----------|-------------|
| `\|sum(Ti) - T_MTTR\| < 1 ms` (4-phase, or 5-phase for `instance_failure`) | `mttr_logger._assert_phase_sum_invariant()` — fatal error, record not written |
| Policy in YAML, mechanism in code | `routing.yaml` + separate `plan.py` / `execute.py` |
| Logs/traces -> diagnostic only, not runtime alert (except the dedicated `instance_failure` forwarding check) | Alertmanager routes on `fault_type` label only |
| T_MTTR is an upper bound (no polling-lag correction) | `health_checker.py` 5-s poll; H3 argument is monotonicity |
| Four Plan gates in canonical order: CB -> Lease -> Transport -> Drift | `app.py::_run_mape_k()` |
| Stateless NGINX workload assumption | `config_reapply.yml` externalises config |
| cAdvisor excluded from RQ3 aggregate | `analysis/04_overhead.py` filters `container_name!="cadvisor"` |
| SSH remediation dispatch scoped to `sg-orchestrator` only, never `researcher_cidr` or `0.0.0.0/0` | `sg-targets`' port-22 ingress rule; **supersedes** the earlier "no port 22 on targets" design |
| No `-target` in Terraform apply | `execute.py` — workspace boundary is scope, not a per-invocation flag |
| Drift scope is parsed from `terraform plan -refresh-only -json`, not human-readable plan text | `plan.py::drift_validation()` — the JSON format carries a documented cross-version compatibility guarantee, the text format does not |

---

## 12. Troubleshooting

### SSM PingStatus shows `ConnectionLost`

Verify all three SSM VPC endpoints are `available`:

```bash
aws ec2 describe-vpc-endpoints \
  --filters "Name=vpc-id,Values=$(cd terraform/bootstrap && terraform output -raw vpc_id)" \
  --query 'VpcEndpoints[*].[ServiceName,State]' \
  --output table
```

### Ansible remediation dispatch fails with an SSH error

Confirm `sg-targets` still has its port-22 ingress rule scoped to `sg-orchestrator`,
and that `terraform/bootstrap/target_ssh_key.pem` was synced to the monitoring host
with `0600` permissions by the `self_healing` role. This is a different failure
surface from SSM connectivity — fault injection can succeed over SSM while
remediation dispatch fails over SSH, or vice versa.

### Alertmanager webhook delivered but orchestrator logs `Empty or non-JSON body`

Alertmanager runs in Docker; the orchestrator runs as a host-native systemd service.
The webhook URL must reference the Docker bridge gateway IP (commonly
`172.17.0.1:9094`), not a container-network hostname:

```yaml
# observability/alertmanager/alertmanager.yml (rendered from alertmanager.yml.j2)
receivers:
  - name: orchestrator
    webhook_configs:
      - url: "http://172.17.0.1:9094/alert"
```

### `FATAL — Decomposed-MTTR phase-sum invariant VIOLATED`

A `RuntimeError` from `_assert_phase_sum_invariant` means the monotonic-clock
timestamps were recorded out of order or a phase was skipped. Find the `trial_id`
in the error message and confirm that `record_detection()`, `record_classification()`,
`record_remediation()`, `record_observability_restore()` (instance_failure only),
and `record_validation()` are all called in the correct sequence for that fault
class. The error intentionally prevents writing a corrupt trial record.

### Circuit breaker stuck in Open state

Auto-resets after Tcool = 30 minutes from the last alert. For immediate manual
reset during development:

```python
from orchestrator.plan import CircuitBreaker
cb = CircuitBreaker()
cb.reset("target-2", "cpu_overload")
```

---

*Toolchain pins are load-bearing for reproducibility.
Verify all pins before every campaign run with `make verify-pins`.*
