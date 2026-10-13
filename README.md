# Automated Self-Healing Cloud Infrastructure on AWS EC2

Software artefact and dataset for the MSc thesis *Automated Self-Healing Cloud Infrastructure: An Empirical Study of Observability-Triggered Terraform and Ansible Remediation on AWS EC2* (Duong Tat Hung, Darmstadt University of Applied Sciences, in cooperation with the Vietnamese-German University).

The artefact is a closed-loop pipeline (MAPE-K) that detects six fault classes on AWS EC2 from Prometheus metrics and remediates five of them with Ansible and one with Terraform. The thesis evaluates it against a manual SRE baseline and an AWS Auto Scaling baseline.

## What is in this repository

| Path | Content |
|---|---|
| `self-healing-thesis/orchestrator/` | Flask orchestrator: classifier, four safety gates, actuator dispatch, validation, MTTR logger |
| `self-healing-thesis/ansible/` | Remediation playbooks, roles for the monitoring stack and the telemetry agents, inventory |
| `self-healing-thesis/terraform/` | Infrastructure as code: shared infrastructure, the three targets, the Auto Scaling baseline |
| `self-healing-thesis/observability/` | Prometheus rules, Alertmanager, Grafana, Logstash, OpenTelemetry Collector configuration |
| `self-healing-thesis/experiments/` | Fault injection and trial harness |
| `self-healing-thesis/analysis/` | Statistical analysis scripts used for Chapter 5 |
| `self-healing-thesis/tests/` | Unit and integration tests (the unit suite has 300 tests in 24 modules) |
| `.github/workflows/` | Continuous integration (lint and unit tests) and manually triggered deploy workflows |

The experimental dataset is attached to the release `dataset-2026-10-08` as `Final_Thesis_Dataset_2026-10-08_v10.zip`. It contains the raw trial records, the analysis outputs, and a `MANIFEST.json` with SHA-256 hashes of inputs, scripts and outputs. `reproduce_campaign2.py` in that archive regenerates the reported results and checks them against the stored ones.

## Provenance

This repository is a snapshot of the development repository at commit `13186e22` (1 October 2026 campaign code plus later orchestrator changes), published without the development history. The development history contained local logs and machine-specific files that are not part of the artefact. The S2 calibration code described in Chapter 5 is kept on the branch `s2-calibration` (tag `s2-calibration-2026-10-11`); `main` is the code state described for the primary campaign.

## What is deliberately not included

Terraform state, SSH keys, `terraform.tfvars` files, AWS credentials, the thesis manuscript and the monitoring-host logs. Anyone reproducing the lab must supply their own AWS account and values (`terraform/README.md`). In this public copy the AWS account ID in two default settings (`bootstrap_state_bucket` and an unused S3 prefix in `trial_config.yml`) is replaced by the placeholder `<ACCOUNT_ID>`; set it for your own account.

## Running the checks

```bash
cd self-healing-thesis
python -m pytest tests/unit -q
ruff check orchestrator/
```

Deploying the lab creates billable AWS resources (one `t3.medium` monitoring host, three `t3.micro` targets, a NAT gateway). Toolchain versions are pinned in `.toolchain` and in Chapter 4, Table 4.2 of the thesis.

## Licence and citation

Licence: MIT for the code and for the dataset archive (see `LICENSE`).

Archived on Zenodo:

- Software (this repository): [10.5281/zenodo.23319490](https://doi.org/10.5281/zenodo.23319490) (concept DOI, always the latest version); the current version, v1.0.1, is [10.5281/zenodo.23332350](https://doi.org/10.5281/zenodo.23332350). v1.0.0 ([10.5281/zenodo.23319491](https://doi.org/10.5281/zenodo.23319491)) uses the earlier percentile rule (see the v1.0.1 release notes).
- Dataset (`Final_Thesis_Dataset_2026-10-08_v11.zip`, `S2_calibration_2026-10-11_public.zip`, `SHA256SUMS.txt`): [10.5281/zenodo.23319631](https://doi.org/10.5281/zenodo.23319631) (concept DOI, always the latest version); the current version (v2) is [10.5281/zenodo.23331860](https://doi.org/10.5281/zenodo.23331860). Version v1 ([10.5281/zenodo.23319632](https://doi.org/10.5281/zenodo.23319632)) holds the v10 zip, whose aggregate P95 used a rounding rule that gave 548.88 s instead of the nearest-rank value 563.98 s.

Thesis reference: *to be added when available.*
