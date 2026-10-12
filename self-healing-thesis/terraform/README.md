# Terraform Deployment Guide

**Self-Healing Infrastructure — Master's Thesis**
*Observability-Driven Self-Healing Infrastructure on AWS EC2 · Duong Tat Hung · Darmstadt UAS*

This guide covers every step to provision, operate, and destroy the AWS infrastructure
defined under `terraform/bootstrap/` and `terraform/targets/` using Terraform.

> **Scope:** This README covers Terraform only.
> For the full project overview (experiments, Ansible, result analysis) see the
> [root README](../README.md).

---

## Table of Contents

1. [What Terraform Provisions](#1-what-terraform-provisions)
2. [Directory Structure](#2-directory-structure)
3. [Architecture Overview](#3-architecture-overview)
4. [Prerequisites](#4-prerequisites)
5. [Pre-Flight Checklist](#5-pre-flight-checklist)
   - [PF-1 — AWS Credentials & IAM](#pf-1--aws-credentials--iam)
   - [PF-2 — EC2 Key Pair](#pf-2--ec2-key-pair)
   - [PF-3 — Your Public IP](#pf-3--your-public-ip)
6. [Bootstrap the Shared Infrastructure](#6-bootstrap-the-shared-infrastructure)
7. [Deploy the Targets Workspace](#7-deploy-the-targets-workspace)
8. [Post-Apply Verification](#8-post-apply-verification)
9. [Day-2 Operations](#9-day-2-operations)
10. [Variables Reference](#10-variables-reference)
11. [Outputs Reference](#11-outputs-reference)
12. [Destroy](#12-destroy)
13. [Troubleshooting](#13-troubleshooting)

---

## 1. What Terraform Provisions

The infrastructure is split across **two independent workspaces** with separate state
files, matching the runtime-remediation safety boundary the thesis defends (Ch3
§3.3.2, §3.6.2): the orchestrator only ever runs `terraform apply` inside
`terraform/targets/`, so shared infrastructure can never be touched by an automated
remediation, even by accident.

| Workspace | State key | Applied | Contains |
|-----------|-----------|---------|----------|
| **`terraform/bootstrap/`** | `bootstrap/terraform.tfstate` | Once, at campaign start | VPC, subnets, IGW, NAT Gateway, 3 SSM VPC Interface Endpoints, security groups, shared IAM role/instance profile, S3 state bucket, S3 data bucket, DynamoDB lock table, Monitoring Stack Server (t3.medium) + EIP, SNS topic, the Terraform-generated SSH key pair used for Ansible remediation dispatch |
| **`terraform/targets/`** | `targets/terraform.tfstate` | Once at start; re-applied automatically by the orchestrator on `instance_failure` remediation | 3 target EC2 instances (t3.micro, `for_each`). Holds no S3-baseline resources |
| **`terraform/baseline/`** | `baseline/terraform.tfstate` | Manually, to open and close an S3 trial window; never touched by the orchestrator | The S3-baseline Auto Scaling Group, its launch template, scaling policies and CloudWatch alarms |

Both workspaces are composed from five reusable modules under `terraform/modules/`:

| Module | Used by | Key Resources |
|--------|---------|--------------|
| **storage** | bootstrap | S3 state bucket (versioned, AES-256), S3 data bucket, DynamoDB lock table |
| **networking** | bootstrap | VPC, public subnet (monitoring), private subnet (targets, `10.0.2.0/24`), IGW, NAT GW, 3 SSM VPC Interface Endpoints, 3 security groups (`sg-orchestrator`, `sg-targets`, `sg-ssm-endpoints`) — all `sg-orchestrator` ingress rules are standalone `aws_security_group_rule` resources; never mixed with an inline `ingress {}` block on the same security group, which would fight the standalone rules on every subsequent apply |
| **iam** | bootstrap | Shared EC2 IAM role (SSM + CloudWatch + S3 + EC2 remediation + DynamoDB lock + Auto Scaling describe), instance profile used by both the monitoring server and all target instances |
| **monitoring** | bootstrap | Monitoring server EC2 t3.medium + EIP, SNS topic + email subscription |
| **compute** | targets | 3 target EC2 instances (`for_each = toset(["target-1","target-2","target-3"])`, private subnet, no public IP) + the optional S3-baseline ASG |

> **Why a separate `iam` module?** The monitoring server and all target instances share
> the same instance profile. Keeping IAM in a dedicated module avoids a circular dependency
> between `monitoring` and `compute`.

**What Terraform does NOT install:**
Software on the EC2 instances (Docker, Prometheus, NGINX, Node Exporter, Flask orchestrator)
is installed by Ansible after `apply`. Each instance's `user_data` script only performs the
minimal **detection-path** bootstrap (SSM Agent, NGINX with `/healthz`, Node Exporter v1.7.0,
CloudWatch Agent, the `canonical_hash`/`running_hash` drift-detection timer) — the
**diagnostic-path** agents (Filebeat, OTel Collector agent) are installed afterward by the
Ansible bootstrap play, keeping `terraform apply` idempotent.

**Ansible-first remediation:**
Five of six fault classes (`cpu_overload`, `memory_exhaustion`, `disk_saturation`,
`service_crash`, `configuration_drift`) are remediated by Ansible playbooks dispatched
over **direct SSH** (port 22, `sg-orchestrator` → `sg-targets`, using the Terraform-generated
key pair) — **no Terraform involved**. Terraform is reserved exclusively for
`instance_failure`, the only case where the EC2 resource itself no longer exists.
Remediation is a **workspace-scope** `terraform apply` on the entire `targets/` workspace —
**the `-target` flag is never used** (Ch3 §3.6.2, §4.2.1/§4.2.2). Scope is enforced
structurally: the `targets/` workspace declares only the three target instances and their
immediate dependencies, so an apply cannot reach shared bootstrap infrastructure even by
accident.

> **A note on SSM's actual scope.** AWS Systems Manager is still load-bearing in this
> architecture, but not as the Ansible remediation transport: it carries fault injection
> (`SendCommand`), the Plan layer's liveness gate (`PingStatus`), and manual/emergency
> operator access (`Session Manager`). Ansible remediation dispatch moved to SSH after a
> reproducible defect was isolated in the `community.aws.aws_ssm` connection plugin under
> automated, multi-step execution — see the root README §1.1 for the full account.

---

## 2. Directory Structure

```
terraform/
├── modules/
│   ├── storage/                  Terraform state backend + data bucket
│   │   ├── main.tf               S3 state bucket + S3 data bucket + DynamoDB lock table
│   │   ├── variables.tf
│   │   └── outputs.tf            → bucket_name, dynamodb_table_name
│   │
│   ├── networking/                VPC topology
│   │   ├── main.tf                VPC, subnets, IGW, NAT GW, 3 SGs, SSM VPC endpoints;
│   │   │                          sg-targets' port-22 ingress rule, scoped to
│   │   │                          sg-orchestrator only, is what carries Ansible
│   │   │                          remediation dispatch
│   │   ├── variables.tf
│   │   └── outputs.tf             → vpc_id, subnet IDs, security group IDs
│   │
│   ├── iam/                       Shared IAM role & instance profile
│   │   ├── main.tf                EC2-SSM role, SSM/S3/EC2/CloudWatch/DynamoDB/
│   │   │                          AutoScaling policy, instance profile
│   │   ├── variables.tf
│   │   └── outputs.tf             → iam_instance_profile_name, iam_role_arn
│   │
│   ├── monitoring/                Monitoring server + alerting
│   │   ├── main.tf                t3.medium EC2 + EIP + SNS topic
│   │   ├── variables.tf
│   │   └── outputs.tf             → monitoring_public_ip, sns_topic_arn
│   │
│   └── compute/                   Target EC2 instances + S3-baseline ASG
│       ├── main.tf                3×t3.micro, for_each = toset(["target-1","target-2","target-3"])
│       ├── variables.tf
│       ├── outputs.tf             → target_instance_ids (map), target_private_ips (map),
│       │                          asg_baseline_name
│       └── user_data_target.sh.tpl  Bootstrap: SSM Agent, NGINX /healthz, Node Exporter
│                                    v1.7.0, CloudWatch Agent, config-drift-check.timer
│
├── bootstrap/                     ← Shared infra workspace. Applied once.
│   ├── backend.tf                 S3 backend, DynamoDB locking (two-phase bootstrap — §6)
│   ├── main.tf                    Module wiring: storage → networking → iam → monitoring;
│   │                              also generates the target_ssh key pair used for
│   │                              Ansible remediation dispatch
│   ├── variables.tf
│   ├── outputs.tf                 → consumed by targets/ via terraform_remote_state
│   └── terraform.tfvars           Concrete values — edit before applying (not committed)
│
└── targets/                       ← Remediation-scope workspace. The ONLY workspace
    │                              on which automated remediation operates.
    ├── backend.tf                 S3 backend, DynamoDB locking
    ├── main.tf                    Reads bootstrap outputs via terraform_remote_state;
    │                              invokes module "compute"
    ├── variables.tf
    └── terraform.tfvars           Concrete values — edit before applying (not committed)
```

---

## 3. Architecture Overview

```
                          ┌──────────────────────────────────┐
  Researcher              │   AWS ap-southeast-1              │
  (dashboards, SSH,       │                                   │
   SSM)                   │  ┌─────────────────────────┐      │
         │                │  │  Public Subnet           │      │
         │ ports 22,      │  │  10.0.1.0/24             │      │
         │ 3000, 5601,    │  │                          │      │
         │ 9090, 16686    │  │  ┌───────────────────┐   │      │
         └──────────────► │  │  │ Monitoring Server  │   │      │
                          │  │  │ t3.medium + EIP    │   │      │
                          │  │  │                    │   │      │
                          │  │  │ Docker: Prometheus │   │      │
                          │  │  │  Alertmanager      │   │      │
                          │  │  │  Grafana / ELK     │   │      │
                          │  │  │  Jaeger / OTel Col │   │      │
                          │  │  │                    │   │      │
                          │  │  │ systemd: Flask     │   │      │
                          │  │  │  Orchestrator      │   │      │
                          │  │  │  :9094/alert       │   │      │
                          │  │  │  (Ansible control  │   │      │
                          │  │  │   node)             │   │      │
                          │  │  └───────────────────┘   │      │
                          │  └─────────────────────────┘      │
                          │       │ SSH :22           │        │
                          │       │ (remediation)      │        │
                          │       │                    │ SSM    │
                          │       │                    │ (inject,│
                          │  ┌────▼─────────────────┐  │ liveness)│
                          │  │  Private Subnet          │      │
                          │  │  10.0.2.0/24             │      │
                          │  │                          │      │
                          │  │  ┌────────┐ ┌────────┐   │      │
                          │  │  │target-1│ │target-2│   │      │
                          │  │  │t3.micro│ │t3.micro│   │      │
                          │  │  └────────┘ └────────┘   │      │
                          │  │         ┌────────┐        │      │
                          │  │         │target-3│        │      │
                          │  │         │t3.micro│        │      │
                          │  │         └────────┘        │      │
                          │  │  SSM VPC Endpoints ◄────  │      │
                          │  └─────────────────────────┘      │
                          └──────────────────────────────────┘
```

**Key design decisions:**

| Decision | Rationale |
|----------|-----------|
| Two Terraform workspaces (`bootstrap`, `targets`) | Runtime remediation applies only `targets/`; shared VPC/IAM/storage can never be touched by an automated apply |
| 3 fixed targets (`for_each`, not ASG) for S1/S2 | Thesis requires deterministic, repeatable fault injection for S1/S2; the S3 (ASG) baseline is a separate, gated resource set that stays inert during S1/S2 trials |
| `for_each = toset([...])` not `count` | Stable string keys (`targets["target-1"]`) prevent Terraform re-indexing all instances when one is terminated and recreated (Thesis Ch4 §4.2.1) |
| No `-target` at runtime | Scope is enforced structurally by the `targets/` workspace boundary, not by per-invocation flags |
| Separate `iam` module | Monitoring server and compute targets share one instance profile; extracting IAM breaks the monitoring↔compute circular dependency |
| SSH for Ansible remediation dispatch, scoped to `sg-orchestrator` only | Port 22 on `sg-targets` accepts ingress from `sg-orchestrator` alone — never `researcher_cidr` or `0.0.0.0/0` — adopted after a reproducible `community.aws.aws_ssm` connection-plugin defect under automated execution |
| SSM retained for fault injection and liveness gating | `SendCommand`/`PingStatus` continue over the 3 SSM VPC Interface Endpoints; this is a separate API surface from Ansible's own transport |
| DynamoDB state locking | Both workspaces use DynamoDB (not Terraform's native S3 lockfile) — the orchestrator's Plan layer queries the lock table directly as a stale-lock precondition (Ch4 §4.2.2) |
| Direct dashboard port exposure | `sg-orchestrator` exposes ports 22/3000/5601/9090/16686 directly to `researcher_cidr` (not behind a reverse proxy) |

---

## 4. Prerequisites

| Tool | Pinned Version | Verify | Install |
|------|----------------|--------|---------|
| **Terraform** | 1.14.9 | `terraform version` | https://developer.hashicorp.com/terraform/install |
| **AWS CLI** | v2 (no exact patch version pinned in this repo) | `aws --version` | https://aws.amazon.com/cli/ |
| **Git** | any | `git --version` | https://git-scm.com |

See the root [README](../README.md) §2 for the full pinned toolchain (Ansible, Python,
Docker, and the observability stack image tags). Those tools run on the monitoring
server, installed by Ansible after `terraform apply` — not required on your local
workstation, except Terraform and the AWS CLI themselves.

---

## 5. Pre-Flight Checklist

Complete all items **before** running `terraform init`.

---

### PF-1 — AWS Credentials & IAM

```bash
aws configure
#   AWS Access Key ID     : <your key>
#   AWS Secret Access Key : <your secret>
#   Default region name   : ap-southeast-1
#   Default output format : json

aws sts get-caller-identity
```

Expected:
```json
{
    "UserId": "AIDAXXXXXXXXXXXXXXXXX",
    "Account": "123456789012",
    "Arn": "arn:aws:iam::123456789012:user/your-username"
}
```

The IAM user/role needs the following managed policies (or equivalent custom policies):

```
AmazonEC2FullAccess         AmazonVPCFullAccess        AmazonS3FullAccess
AmazonDynamoDBFullAccess    CloudWatchFullAccess        AmazonSNSFullAccess
IAMFullAccess               AmazonSSMFullAccess         AutoScalingFullAccess
```

---

### PF-2 — EC2 Key Pair

Two distinct key-pair concerns exist, and they're easy to conflate:

1. **The target SSH key pair is generated automatically by Terraform** —
   `tls_private_key.target_ssh` in `terraform/bootstrap/main.tf` — and requires no
   operator action. It is what `execute.py` uses to dispatch Ansible remediation
   playbooks over SSH to the targets; the private key is synced to the monitoring
   host by the `self_healing` role. You do not create or manage this key pair.
2. **`key_pair_name` (the `terraform.tfvars` variable below) is optional** and
   controls only whether *you* can SSH into the **monitoring server** directly for
   manual/emergency access. `sg-orchestrator` has port 22 open to `researcher_cidr`
   regardless, but without a key pair attached you'd need to fall back to SSM
   Session Manager for that specific host. Leaving `key_pair_name = ""` is safe and
   is the default.

```bash
aws ec2 create-key-pair \
    --key-name self-healing-key \
    --region ap-southeast-1 \
    --query "KeyMaterial" \
    --output text > ~/.ssh/self-healing-key.pem

chmod 400 ~/.ssh/self-healing-key.pem
```

Set `key_pair_name = "self-healing-key"` in both `terraform/bootstrap/terraform.tfvars` and
`terraform/targets/terraform.tfvars` (leave `""` to disable) if you want this optional
monitoring-server-only access.

---

### PF-3 — Your Public IP

`sg-orchestrator` restricts inbound access on ports 22, 3000, 5601, 9090, 16686 to
`researcher_cidr` only.

```bash
curl -4 -sf https://checkip.amazonaws.com
# Example: 203.0.113.42
```

Append `/32` → **`203.0.113.42/32`**. Use this as `researcher_cidr` in
`terraform/bootstrap/terraform.tfvars`.

> If your IP changes between sessions, re-apply with the updated CIDR — scope the
> apply to just the 5 affected ingress rules rather than a bare `apply`, to avoid
> the `module.monitoring` depends_on quirk described in §13:
> ```bash
> cd terraform/bootstrap
> NEW_IP=$(curl -4 -sf https://checkip.amazonaws.com)
> terraform apply -var="researcher_cidr=${NEW_IP}/32" \
>   -target=module.networking.aws_security_group_rule.orchestrator_ingress_ssh \
>   -target=module.networking.aws_security_group_rule.orchestrator_ingress_grafana \
>   -target=module.networking.aws_security_group_rule.orchestrator_ingress_prometheus \
>   -target=module.networking.aws_security_group_rule.orchestrator_ingress_kibana \
>   -target=module.networking.aws_security_group_rule.orchestrator_ingress_jaeger
> ```

---

## 6. Bootstrap the Shared Infrastructure

The S3 state bucket and DynamoDB lock table are themselves managed by the **storage module**
inside `terraform/bootstrap/` — a chicken-and-egg problem for first-time setup, resolved with
a two-phase bootstrap.

```bash
cd terraform/bootstrap
```

**Fill in `terraform.tfvars`** (required fields have no default):

```hcl
project     = "self-healing-thesis"
environment = "experiment"
aws_region  = "ap-southeast-1"

researcher_cidr   = "203.0.113.42/32"                          # from PF-3
state_bucket_name = "self-healing-tfstate-<your-account-id>"   # globally unique
data_bucket_name  = "its-thesis-data-<your-account-id>"        # globally unique
sns_email         = "you@example.com"

key_pair_name = ""   # or "self-healing-key" from PF-2 — monitoring-server SSH only
```

**Phase 1 — apply with a local backend** (the S3 bucket doesn't exist yet):

```bash
# Comment out the backend "s3" {} block in backend.tf first
terraform init
terraform plan
terraform apply -auto-approve
```

This creates the S3 state bucket, S3 data bucket, DynamoDB lock table, VPC, subnets, NAT
Gateway, 3 SSM VPC endpoints, IAM role/instance profile, the monitoring server, the SNS
topic, and the target SSH key pair used later for Ansible remediation dispatch. Takes
~10–15 minutes (the NAT Gateway is the slowest single resource).

Check your email and confirm the SNS subscription.

**Phase 2 — migrate state to S3:**

```bash
# Uncomment the backend "s3" {} block in backend.tf
terraform init -migrate-state \
  -backend-config="bucket=$(terraform output -raw state_bucket_name)" \
  -backend-config="region=ap-southeast-1" \
  -backend-config="dynamodb_table=$(terraform output -raw dynamodb_table_name)"
# Type "yes" when prompted to copy state to S3
```

**Verify:**

```bash
terraform state list
terraform output monitoring_host_ip
```

---

## 7. Deploy the Targets Workspace

```bash
cd ../targets
```

**Fill in `terraform.tfvars`**:

```hcl
project     = "self-healing-thesis"
environment = "experiment"
aws_region  = "ap-southeast-1"

instance_type     = "t3.micro"
availability_zone = "ap-southeast-1a"
key_pair_name     = ""

# Copy from bootstrap outputs:
ubuntu_ami_id          = "ami-0532913178263be11"   # terraform -chdir=../bootstrap output -raw ubuntu_ami_id
bootstrap_state_bucket = "..."                      # terraform -chdir=../bootstrap output -raw state_bucket_name

```

**Init and apply:**

```bash
BUCKET=$(terraform -chdir=../bootstrap output -raw state_bucket_name)
TABLE=$(terraform -chdir=../bootstrap output -raw dynamodb_table_name)

terraform init \
  -backend-config="bucket=$BUCKET" \
  -backend-config="dynamodb_table=$TABLE" \
  -backend-config="region=ap-southeast-1"

terraform plan
terraform apply -auto-approve
```

Creates the three target instances (~8–12 minutes for `user_data` to complete SSM
registration).

**Read outputs and run the Ansible bootstrap play:**

```bash
terraform output -json target_instance_ids
terraform output -json target_private_ips

cd ../../ansible
ansible-playbook bootstrap.yml -i inventory/aws_ec2.yml --limit monitoring_host
ansible-playbook bootstrap.yml -i inventory/aws_ec2.yml --limit target-1,target-2,target-3
```

`bootstrap.yml` installs system dependencies and telemetry agents only — it does not
start the observability stack or deploy the orchestrator. See the root README §6 for
the second Ansible entry point (`playbooks/deploy.yml`) that does.

`ansible/inventory/aws_ec2.yml` is a **dynamic inventory** (the `aws_ec2` plugin, filtered by
the `Project`/`Role` tags Terraform applies) — there is no static inventory file to hand-edit.

---

## 8. Post-Apply Verification

Wait a few minutes after `apply` for `user_data` to complete on all instances, then run these
checks.

---

### Check 1 — SSM Connectivity to All Targets

```bash
aws ssm describe-instance-information \
    --filters "Key=tag:Role,Values=target" \
    --query "InstanceInformationList[*].{ID:InstanceId,IP:IPAddress,Ping:PingStatus}" \
    --output table \
    --region ap-southeast-1
```

✅ Expected: all three instances show `PingStatus = Online`.

❌ If `ConnectionLost` or missing, verify the SSM VPC endpoints are healthy:
```bash
aws ec2 describe-vpc-endpoints \
    --filters "Name=vpc-id,Values=$(terraform -chdir=terraform/bootstrap output -raw vpc_id)" \
    --query "VpcEndpoints[*].{Service:ServiceName,State:State}" \
    --output table \
    --region ap-southeast-1
```

---

### Check 2 — Monitoring Server Reachable

```bash
MONITORING_IP=$(terraform -chdir=terraform/bootstrap output -raw monitoring_host_ip)

curl -sf "http://$MONITORING_IP:9094/healthz"
# Expected: {"status":"ok",...}   (only meaningful after the orchestrator role has
# been deployed — see the root README §6; the healthz endpoint doesn't exist until then)
```

If you configured a key pair (PF-2):
```bash
ssh -i ~/.ssh/self-healing-key.pem -o StrictHostKeyChecking=no ubuntu@"$MONITORING_IP" \
    "echo 'SSH OK' && uptime"
```

❌ If `Connection refused`, confirm `researcher_cidr` in `terraform.tfvars` matches your
current IP and re-apply if needed (see PF-3).

---

### Check 3 — Target `user_data` Bootstrap Complete

```bash
INSTANCE_ID=$(terraform -chdir=terraform/targets output -json target_instance_ids \
    | python3 -c "import sys,json; print(list(json.load(sys.stdin).values())[0])")

aws ssm send-command \
    --instance-ids "$INSTANCE_ID" \
    --document-name "AWS-RunShellScript" \
    --parameters 'commands=["tail -5 /var/log/user-data.log", "systemctl is-active nginx node_exporter config-drift-check.timer"]' \
    --region ap-southeast-1 \
    --query "Command.CommandId" --output text
```

Then, after a few seconds:
```bash
aws ssm get-command-invocation \
    --command-id <CommandId> \
    --instance-id "$INSTANCE_ID" \
    --region ap-southeast-1 \
    --query "{Status:Status,Output:StandardOutputContent}"
```

✅ Expected: `nginx`, `node_exporter`, and `config-drift-check.timer` all show `active`.

---

### Check 4 — SNS Email Subscription Confirmed

```bash
aws sns list-subscriptions-by-topic \
    --topic-arn "$(terraform -chdir=terraform/bootstrap output -raw sns_topic_arn)" \
    --region ap-southeast-1 \
    --query "Subscriptions[0].SubscriptionArn"
```

✅ Expected: a full ARN string (not `"PendingConfirmation"`).

---

### Check 5 — Prometheus Scraping All Targets (post-Ansible)

```bash
curl -s "http://$MONITORING_IP:9090/api/v1/targets" \
    | python3 -m json.tool | grep -A3 '"job":"node_exporter"'
```

✅ Expected: `"health": "up"` for all three targets.

---

### Check 6 — All Six Alert Rules Loaded (post-Ansible)

```bash
curl -s "http://$MONITORING_IP:9090/api/v1/rules" | python3 -c "
import sys, json
rules = json.load(sys.stdin)
for g in rules['data']['groups']:
    for r in g['rules']:
        ft = r.get('labels', {}).get('fault_type', '')
        print(f'  {r[\"name\"]:25s} -> fault_type={ft}')
"
```

✅ Expected rules — 6 fault classes, Ansible-first routing:
```
  CPUOverload           -> fault_type=cpu_overload         (Ansible, SSH: kill_runaway_process.yml)
  MemoryExhaustion      -> fault_type=memory_exhaustion    (Ansible, SSH: memory_cleanup.yml)
  DiskSaturation        -> fault_type=disk_saturation      (Ansible, SSH: disk_cleanup.yml)
  ServiceCrash          -> fault_type=service_crash        (Ansible, SSH: service_crash.yml)
  ConfigurationDrift    -> fault_type=configuration_drift  (Ansible, SSH: config_reapply.yml)
  InstanceFailure       -> fault_type=instance_failure     (Terraform: workspace-scope apply, no -target)
```

---

### Check 7 — Orchestrator Webhook Responding

```bash
curl -s -X POST "http://$MONITORING_IP:9094/alert" \
    -H "Content-Type: application/json" \
    -d '{
      "alerts": [{
        "status": "firing",
        "labels": {
          "alertname": "ServiceCrash",
          "fault_type": "service_crash",
          "severity":   "critical",
          "instance":   "10.0.2.10:9100",
          "node_name":  "target-1"
        },
        "annotations": {}
      }]
    }'
```

✅ Expected: `{"processed": 1, "status": "received", "suppressed": 0}`

---

## 9. Day-2 Operations

### `instance_failure` Remediation — Re-Provision a Terminated Instance

After a target is terminated (e.g. via `python experiments/inject/inject_once.py --target
target-1 --fault instance_failure`), the orchestrator (S1) runs a workspace-scope `terraform
apply` on the `targets/` workspace automatically — **no `-target` flag** (Ch3 §3.6.2,
§4.2.1/§4.2.2) — and then automatically restores and forwarding-verifies the diagnostic-path
agents on the recreated instance itself (`execute.py::restore_diagnostic_agents()` +
`observability_checker.py::ObservabilityChecker`); no manual Ansible re-run is needed for
that step any more. For manual S2 remediation or debugging:

```bash
cd terraform/targets

terraform plan
# Expected: "Plan: 1 to add" for the terminated target

terraform apply -auto-approve

aws ssm describe-instance-information \
    --filters "Key=tag:Role,Values=target" \
    --query "InstanceInformationList[*].{ID:InstanceId,Ping:PingStatus}" \
    --output table --region ap-southeast-1

# Only if diagnostic agents genuinely need a manual re-run (e.g. debugging a
# diagnostic_bootstrap_failed escalation outside the automated S1 path):
cd ../../ansible
ansible-playbook playbooks/diagnostic_agents.yml -i inventory/aws_ec2.yml --limit target-1
```

> **Why does a workspace-scope apply still address just one instance?** The `for_each` key
> (`"target-1"`) is stable — it never shifts when a sibling is destroyed/recreated. Since only
> one instance is actually missing from the declared set, Terraform's own plan naturally
> proposes adding exactly that one instance; no `-target` flag is needed for selectivity.

---

### Run an S3-Baseline (Auto Scaling) Trial

```bash
cd terraform/baseline
terraform init -backend-config=../bootstrap/outputs.backend.tfvars
terraform apply -auto-approve
# ... run S3 trials (see root README §8) ...
terraform destroy -auto-approve
```

> The baseline has its own root and its own state key, so enabling and disabling it are
> ordinary `apply` and `destroy` operations in `terraform/baseline`. There is no variable
> to re-pass and no flag to forget. This replaces an earlier `enable_asg_baseline` toggle
> inside `terraform/targets`: with the ASG in that root's state, any apply omitting the
> flag — including the orchestrator's own automatic remediation, which passes only
> `-var=project` and `-var=environment` — planned a destroy of the whole baseline.

---

### Force-Replace the Monitoring Server

```bash
cd terraform/bootstrap
terraform apply -replace=module.monitoring.aws_instance.monitoring
# The same EIP is re-associated — all service URLs remain unchanged
```

---

### Update `researcher_cidr` After an IP Change

See PF-3 above for the `-target`-scoped apply pattern — a bare `terraform apply` in this
workspace risks triggering a spurious full replacement of the monitoring instance (§13).

---

## 10. Variables Reference

### Bootstrap workspace (`terraform/bootstrap/variables.tf`)

| Variable | Type | Default | Required | Description |
|----------|------|---------|----------|-------------|
| `project` | string | `"self-healing-thesis"` | No | Project label used in all resource names and tags |
| `environment` | string | `"experiment"` | No | Deployment environment label |
| `aws_region` | string | `"ap-southeast-1"` | No | AWS region |
| `availability_zone` | string | `"ap-southeast-1a"` | No | Single AZ for the whole lab (Thesis §3.3.1) |
| `vpc_cidr` | string | `"10.0.0.0/16"` | No | VPC CIDR block |
| `public_subnet_cidr` | string | `"10.0.1.0/24"` | No | Monitoring server subnet |
| `private_subnet_cidr` | string | `"10.0.2.0/24"` | No | Target instances subnet |
| `researcher_cidr` | string | — | **Yes** | Your public IP/32 for dashboard + SSH access |
| `state_bucket_name` | string | — | **Yes** | Globally unique S3 bucket for Terraform state + SSM payloads |
| `data_bucket_name` | string | `"its-thesis-data"` | No | S3 bucket for experiment results (should be made globally unique) |
| `noncurrent_version_expiry_days` | number | `90` | No | S3 lifecycle for non-current object versions |
| `key_pair_name` | string | `""` | No | EC2 key pair for **monitoring-server** SSH only (leave empty to disable) — unrelated to the Terraform-generated target SSH key pair used for remediation dispatch |
| `sns_email` | string | — | **Yes** | Email for SNS escalation notifications |

### Targets workspace (`terraform/targets/variables.tf`)

| Variable | Type | Default | Required | Description |
|----------|------|---------|----------|-------------|
| `aws_region` | string | `"ap-southeast-1"` | No | Must match the bootstrap workspace |
| `project` | string | `"self-healing-thesis"` | No | Must match the bootstrap workspace |
| `environment` | string | `"experiment"` | No | Must match the bootstrap workspace |
| `instance_type` | string | `"t3.micro"` | No | EC2 instance type for target nodes |
| `ubuntu_ami_id` | string | — | **Yes** | Copy from `terraform -chdir=../bootstrap output -raw ubuntu_ami_id` |
| `bootstrap_state_bucket` | string | — | **Yes** | Copy from `terraform -chdir=../bootstrap output -raw state_bucket_name` |
| `bootstrap_state_key` | string | `"bootstrap/terraform.tfstate"` | No | S3 key of the bootstrap state file |
| `availability_zone` | string | `"ap-southeast-1a"` | No | Must match the bootstrap workspace |
| `key_pair_name` | string | `""` | No | Optional; unrelated to the Ansible remediation SSH key, which Terraform generates independently |

> **No `target_count` variable.** Targets use `for_each = toset(["target-1","target-2","target-3"])`.
> These three keys are fixed for the thesis campaign and must not change during a run —
> renaming a key destroys and recreates that instance, resetting its SSM registration.

---

## 11. Outputs Reference

### Bootstrap workspace

| Output | Example | Description |
|--------|---------|-------------|
| `monitoring_host_ip` | `54.100.200.30` | Elastic IP — stable across reboots; use for dashboards and optional manual SSH |
| `monitoring_private_ip` | `10.0.1.20` | Private IP of the monitoring server |
| `monitoring_instance_id` | `i-0abc000001` | Instance ID of the monitoring server |
| `state_bucket_name` | `self-healing-tfstate-...` | S3 bucket for Terraform state + SSM payloads |
| `data_bucket_name` | `its-thesis-data-...` | S3 bucket for experiment results |
| `dynamodb_table_name` | `self-healing-thesis-experiment-tflock` | DynamoDB lock table |
| `vpc_id` | `vpc-0abc...` | VPC ID |
| `public_subnet_id` / `private_subnet_id` | `subnet-0abc...` | Subnet IDs, consumed by `targets/` via remote state |
| `sg_orchestrator_id` / `sg_targets_id` | `sg-0abc...` | Security group IDs |
| `iam_role_arn` / `iam_instance_profile_name` | — | Shared IAM identity, consumed by `targets/` |
| `sns_topic_arn` | `arn:aws:sns:...` | SNS topic for escalation notifications |
| `ubuntu_ami_id` | `ami-0532913178263be11` | Pinned Ubuntu 26.04 LTS AMI — pass to the targets workspace |
| `targets_ssh_key_pair_name` | `self-healing-thesis-experiment-target-ssh` | The Terraform-generated key pair used for Ansible remediation dispatch over SSH |

### Targets workspace

| Output | Example | Description |
|--------|---------|-------------|
| `target_instance_ids` | `{"target-1":"i-0abc000002",...}` | Instance IDs keyed by `for_each` label |
| `target_private_ips` | `{"target-1":"10.0.2.10",...}` | Private IPs keyed by label — used by Ansible dynamic inventory and `health_checker.py` |
| `ubuntu_ami_id` | `ami-0532913178263be11` | AMI actually used for the target instances |
| `asg_baseline_name` | `self-healing-thesis-experiment-asg-baseline` | Name of the S3 baseline ASG. Output of the `terraform/baseline` root, not of `targets` |

---

## 12. Destroy

Destroy the targets workspace first, then bootstrap. NAT Gateways and Elastic IPs are the
highest-cost idle resources.

```bash
cd terraform/targets
terraform plan -destroy
terraform destroy -auto-approve

cd ../bootstrap
terraform plan -destroy
terraform destroy -auto-approve
```

> ⚠️ The S3 state bucket and DynamoDB lock table have `prevent_destroy = true` by default.
> Terraform refuses to destroy them automatically — this protects the campaign dataset. To
> remove them deliberately after confirming all results are mirrored elsewhere, temporarily
> remove `prevent_destroy` (and add `force_destroy = true` on the S3 bucket resources, since
> versioning means Terraform's own state writes during the destroy otherwise race the
> emptying step) in `terraform/modules/storage/main.tf` and `terraform/bootstrap/main.tf`,
> then re-plan and destroy. If the bucket still reports `BucketNotEmpty` afterward (Terraform
> writing its own state file into the very bucket being deleted is a known race), empty it
> directly:

```bash
BUCKET=$(terraform output -raw state_bucket_name)   # run before destroying bootstrap
TABLE=$(terraform output -raw dynamodb_table_name)

for BKT in "$BUCKET"; do
  aws s3api list-object-versions --bucket "$BKT" --query 'Versions[].[Key,VersionId]' --output text | \
    while read -r key vid; do aws s3api delete-object --bucket "$BKT" --key "$key" --version-id "$vid"; done
  aws s3api list-object-versions --bucket "$BKT" --query 'DeleteMarkers[].[Key,VersionId]' --output text | \
    while read -r key vid; do aws s3api delete-object --bucket "$BKT" --key "$key" --version-id "$vid"; done
  aws s3api delete-bucket --bucket "$BKT"
done

aws dynamodb delete-table --table-name "$TABLE" --region ap-southeast-1
```

**Verify no billable resources remain:**

```bash
aws ec2 describe-instances \
    --filters "Name=tag:Project,Values=self-healing-thesis" "Name=instance-state-name,Values=running" \
    --query "Reservations[*].Instances[*].[InstanceId,InstanceType]" \
    --output table --region ap-southeast-1
# Expected: empty

aws ec2 describe-nat-gateways \
    --filter "Name=tag:Project,Values=self-healing-thesis" "Name=state,Values=available,pending" \
    --query "NatGateways[*].NatGatewayId" --region ap-southeast-1
# Expected: []

aws ec2 describe-vpcs --filters "Name=tag:Project,Values=self-healing-thesis" \
    --query "Vpcs[].VpcId" --region ap-southeast-1
# Expected: [] — if a VPC survives here with no matching Terraform state, it may be an
# untracked duplicate from an earlier apply against a disconnected backend; investigate
# before deleting it manually
```

---

## 13. Troubleshooting

### `Error: Backend initialization required`

**Cause:** The S3 bucket doesn't exist yet, or `terraform init` hasn't been run after a fresh
clone, or the `backend "s3" {}` block is uncommented during Phase 1 of the bootstrap.

**Fix:** See [§6 — Bootstrap the Shared Infrastructure](#6-bootstrap-the-shared-infrastructure).

---

### `Error: error calling sts:GetCallerIdentity`

**Cause:** AWS credentials missing, expired, or wrong region.

**Fix:** `aws configure` → verify with `aws sts get-caller-identity`.

---

### `Error: Error acquiring the state lock`

**Cause:** A previous `terraform apply` was interrupted without releasing the DynamoDB lock,
or the orchestrator's own remediation apply is in flight.

**Fix:**
```bash
terraform force-unlock <LOCK_ID>
```
If the Plan layer's stale-lock precondition (`plan.py`) is aborting trials with
`escalation_reason=out_of_scope_drift`, check whether a lock is genuinely held before
force-unlocking.

---

### `Error: researcher_cidr must be a valid CIDR block`

**Fix:** Set `researcher_cidr = "1.2.3.4/32"` (with the `/32` suffix) in
`terraform/bootstrap/terraform.tfvars`.

---

### A bare `terraform apply` in `bootstrap/` wants to replace the monitoring instance

**Cause:** `module.monitoring` declares `depends_on = [module.networking, module.iam]` at
the module level. Any pending change in either of those modules — even an unrelated
security-group-rule tweak — causes Terraform to defer `data.aws_ami.ubuntu`'s read to
apply-time, which in turn plans a full, spurious replacement of the monitoring EC2
instance.

**Fix:** Always scope `bootstrap/` applies to the specific resource(s) actually changing
with `-target=...` (see PF-3's IP-update example) rather than a bare `apply`, while this
dependency structure is in place.

---

### Ansible remediation dispatch fails with an SSH error, but SSM connectivity checks pass

**Cause:** Fault injection and liveness gating use SSM; Ansible remediation dispatch uses
SSH. These are two independent transports that can fail independently of each other.

**Fix:** Confirm `sg-targets`' port-22 ingress rule is present and scoped to
`sg-orchestrator`, and that `terraform/bootstrap/target_ssh_key.pem` was synced to
`/opt/orchestrator/.ssh/target_ssh_key.pem` on the monitoring host with `0600`
permissions by the `self_healing` Ansible role.

---

### SSM Agent Shows `ConnectionLost` on Target Instances

**Cause:** One of:
1. The target hasn't finished `user_data` yet (~2–3 min after launch)
2. SSM VPC Interface Endpoints are not `available`
3. NAT Gateway route missing in the private route table

**Fix:**
```bash
aws ec2 describe-vpc-endpoints \
    --filters "Name=vpc-id,Values=$(terraform -chdir=terraform/bootstrap output -raw vpc_id)" \
    --query "VpcEndpoints[*].{Svc:ServiceName,State:State}" \
    --output table --region ap-southeast-1
# All three must show State = available:
#   com.amazonaws.ap-southeast-1.ssm
#   com.amazonaws.ap-southeast-1.ssmmessages
#   com.amazonaws.ap-southeast-1.ec2messages
```

---

### NAT Gateway Takes Several Minutes to Provision

**Cause:** NAT Gateways are AWS-managed and genuinely take 3–5 minutes. This is expected —
Terraform prints `Still creating...` periodically while it waits.

---

### `Error: creating Security Group: ... Character sets beyond ASCII are not supported`

**Cause:** A `description` field on an `aws_security_group` or `aws_security_group_rule`
resource contains a non-ASCII character (e.g. an em dash `—`). AWS's `GroupDescription` field
is ASCII-only.

**Fix:** Replace any em dash or smart quote in the offending description with a plain hyphen
`-` or straight quote.
