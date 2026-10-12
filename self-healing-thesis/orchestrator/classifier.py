#!/usr/bin/env python3
"""
classifier.py — Rule-based fault classifier (MAPE-K Analyse phase).

Loads the routing table from routing.yaml (the ConcernKnowledge artefact) and
maps Prometheus alert labels to (actuator, action) tuples.

Routing table design (Thesis §3.5.2, §4.4.2):
  "The policy lives in a YAML file rather than in application code so that it
   is auditable as a property of the configuration rather than asserted by the
   implementation, and any future change to the routing is visible in version
   control as a configuration diff."

  cpu_overload        → ansible   kill_runaway_process.yml  (max_retries=2)
  memory_exhaustion   → ansible   memory_cleanup.yml        (max_retries=2)
  disk_saturation     → ansible   disk_cleanup.yml          (max_retries=2)
  service_crash       → ansible   service_crash.yml         (max_retries=2)
  configuration_drift → ansible   config_reapply.yml        (max_retries=1)
  instance_failure    → terraform (targeted apply)           (max_retries=0)
"""

import os
from pathlib import Path
import yaml

# ── Load routing table from routing.yaml ─────────────────────────────────────
_ROUTING_FILE = Path(
    os.environ.get("ROUTING_YAML", str(Path(__file__).parent / "routing.yaml"))
)


def _load_routing() -> dict:
    """Load and return the parsed routing.yaml content."""
    with open(_ROUTING_FILE, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


_ROUTING = _load_routing()
_FAULT_CLASSES: dict[str, dict] = _ROUTING["fault_classes"]
_CIRCUIT_BREAKER_CFG: dict = _ROUTING.get("circuit_breaker", {})

# ── Playbook path for each Ansible fault class ────────────────────────────────
# Relative to ANSIBLE_DIR (set in orchestrator.env.j2).
ANSIBLE_PLAYBOOKS: dict[str, str] = {
    fc: cfg["playbook"]
    for fc, cfg in _FAULT_CLASSES.items()
    if cfg.get("actuator") == "ansible"
}

# ── Human-readable labels (for reports and Prometheus metric labels) ──────────
SCENARIO_LABELS: dict[str, str] = {
    "cpu_overload":         "CPU Overload",
    "memory_exhaustion":    "Memory Exhaustion",
    "disk_saturation":      "Disk Saturation",
    "service_crash":        "Service Crash",
    "configuration_drift":  "Configuration Drift",
    "instance_failure":     "Instance Failure",
}

# ── Validation window (seconds, indexed from routing.yaml) ───────────────────
VALIDATION_WINDOW: dict[str, int] = {
    fc: cfg["validation_window"] for fc, cfg in _FAULT_CLASSES.items()
}


def classify_and_route(alert: dict) -> tuple[str, str]:
    """
    Return (actuator, action) for the given Alertmanager alert payload.

    actuator = "ansible"   → ansible-playbook <playbooks_dir>/<action>.yml
    actuator = "terraform" → terraform apply -auto-approve (targets/ workspace)

    Raises ValueError for unknown or missing fault_type labels to make
    misconfigurations immediately visible rather than silently dropped.
    """
    fault_type = alert.get("labels", {}).get("fault_type")
    if not fault_type:
        raise ValueError(
            "Alert payload missing required label 'fault_type'. "
            "Check Prometheus alert rule labels in alert_rules.yml."
        )
    if fault_type not in _FAULT_CLASSES:
        raise ValueError(
            f"Unknown fault_type '{fault_type}'. "
            f"Registered types: {list(_FAULT_CLASSES)}"
        )
    cfg = _FAULT_CLASSES[fault_type]
    actuator = cfg["actuator"]
    action = cfg.get("playbook", "")   # empty string on the Terraform path (instance_failure)
    return actuator, action


def get_retry_policy(fault_type: str) -> dict:
    """Return the retry policy dict for a fault_type (from routing.yaml)."""
    cfg = _FAULT_CLASSES.get(fault_type, {})
    return {
        "max_retries":     cfg.get("max_retries", 2),
        "backoff_seconds": cfg.get("backoff_seconds", [1, 4]),
    }


def get_validation_window(fault_type: str) -> int:
    """Return the post-remediation validation window in seconds."""
    return VALIDATION_WINDOW.get(fault_type, 90)


def needs_observability_restore(fault_type: str) -> bool:
    """
    Whether this fault class runs the Execute-layer diagnostic-agent restore
    and therefore contributes a T_observability_restore phase.

    True for instance_failure alone: replacing the instance destroys the
    target-local Filebeat and OTel agents, which user_data does not reinstall.
    Declared in routing.yaml so the policy is not a name test in app.py.
    """
    return bool(_ROUTING["fault_classes"][fault_type].get("observability_restore", False))


def get_label(fault_type: str) -> str:
    """Return the human-readable label for a fault_type."""
    return SCENARIO_LABELS.get(fault_type, fault_type)


def get_circuit_breaker_config() -> dict:
    """Return the circuit-breaker configuration from routing.yaml."""
    return _CIRCUIT_BREAKER_CFG
