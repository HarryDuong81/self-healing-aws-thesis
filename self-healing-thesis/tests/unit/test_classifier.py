"""
tests/unit/test_classifier.py — Unit tests for orchestrator/classifier.py

Covers:
  - classify_and_route() returns correct (actuator, action) for all six fault classes
  - Unknown fault_type raises ValueError
  - Missing fault_type label raises ValueError
  - get_retry_policy() returns correct policy for each fault class
  - get_validation_window() returns correct window
  - ANSIBLE_PLAYBOOKS dict is populated for the five Ansible fault classes
  - instance_failure maps to terraform actuator with empty action string

Thesis reference: §3.5.2, Table 3.4 (routing table), §4.4.2
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Insert orchestrator/ into sys.path so imports resolve without package install
_ORCHESTRATOR = Path(__file__).resolve().parent.parent.parent / "orchestrator"
sys.path.insert(0, str(_ORCHESTRATOR))

import classifier


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _alert(fault_type: str) -> dict:
    """Minimal Alertmanager alert payload for a given fault_type."""
    return {"labels": {"fault_type": fault_type, "instance": "10.0.2.1:9100"}}


# ── classify_and_route() ──────────────────────────────────────────────────────

class TestClassifyAndRoute:

    def test_cpu_overload_routes_to_ansible(self):
        actuator, action = classifier.classify_and_route(_alert("cpu_overload"))
        assert actuator == "ansible"
        assert action   == "kill_runaway_process.yml"

    def test_memory_exhaustion_routes_to_ansible(self):
        actuator, action = classifier.classify_and_route(_alert("memory_exhaustion"))
        assert actuator == "ansible"
        assert action   == "memory_cleanup.yml"

    def test_disk_saturation_routes_to_ansible(self):
        actuator, action = classifier.classify_and_route(_alert("disk_saturation"))
        assert actuator == "ansible"
        assert action   == "disk_cleanup.yml"

    def test_service_crash_routes_to_ansible(self):
        actuator, action = classifier.classify_and_route(_alert("service_crash"))
        assert actuator == "ansible"
        assert action   == "service_crash.yml"

    def test_configuration_drift_routes_to_ansible(self):
        actuator, action = classifier.classify_and_route(_alert("configuration_drift"))
        assert actuator == "ansible"
        assert action   == "config_reapply.yml"

    def test_instance_failure_routes_to_terraform(self):
        actuator, action = classifier.classify_and_route(_alert("instance_failure"))
        assert actuator == "terraform"
        assert action   == ""   # empty string — no playbook on Terraform path

    def test_unknown_fault_type_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown fault_type"):
            classifier.classify_and_route(_alert("nonexistent_fault"))

    def test_missing_fault_type_raises_value_error(self):
        with pytest.raises(ValueError, match="missing required label"):
            classifier.classify_and_route({"labels": {}})

    def test_all_six_fault_classes_covered(self):
        fault_classes = [
            "cpu_overload", "memory_exhaustion", "disk_saturation",
            "service_crash", "configuration_drift", "instance_failure",
        ]
        for fc in fault_classes:
            actuator, action = classifier.classify_and_route(_alert(fc))
            assert actuator in ("ansible", "terraform"), f"unexpected actuator for {fc}"


# ── get_retry_policy() ────────────────────────────────────────────────────────

class TestRetryPolicy:

    def test_ansible_faults_have_max_retries_2(self):
        for fc in ("cpu_overload", "memory_exhaustion", "disk_saturation", "service_crash"):
            policy = classifier.get_retry_policy(fc)
            assert policy["max_retries"] == 2, f"{fc} should have max_retries=2"

    def test_configuration_drift_has_max_retries_1(self):
        policy = classifier.get_retry_policy("configuration_drift")
        assert policy["max_retries"] == 1

    def test_instance_failure_has_max_retries_0(self):
        # Thesis §3.5.3: do NOT retry on the Terraform path
        policy = classifier.get_retry_policy("instance_failure")
        assert policy["max_retries"] == 0

    def test_instance_failure_backoff_is_empty(self):
        policy = classifier.get_retry_policy("instance_failure")
        assert policy["backoff_seconds"] == []

    def test_cpu_overload_backoff_is_geometric(self):
        policy = classifier.get_retry_policy("cpu_overload")
        assert policy["backoff_seconds"] == [1, 4]


# ── get_validation_window() ───────────────────────────────────────────────────

class TestValidationWindow:

    def test_per_class_validation_windows(self):
        # Per Thesis Ch.3 Table 3.4 and Ch.4 Table 4.6 — canonical windows.
        # Four Ansible fast-recovery classes get 30 s (their pilot medians
        # are 2 to 7 s); disk_saturation gets 90 s (pilot median 20 s); and
        # instance_failure gets 180 s (EC2 recreation + SSM registration).
        expected = {
            "cpu_overload":        30,
            "memory_exhaustion":   30,
            "disk_saturation":     90,
            "service_crash":       30,
            "configuration_drift": 30,
            "instance_failure":    180,
        }
        for fc, want in expected.items():
            got = classifier.get_validation_window(fc)
            assert got == want, f"{fc}: expected {want}s, got {got}s"

    def test_unknown_fault_type_returns_default(self):
        # Default fallback matches Ch.4 Table 4.6 policy for unknown types
        # (safe upper bound while investigating).
        assert classifier.get_validation_window("nonexistent") == 90


# ── ANSIBLE_PLAYBOOKS dict ────────────────────────────────────────────────────

class TestAnsiblePlaybooks:

    def test_five_ansible_playbooks_registered(self):
        assert len(classifier.ANSIBLE_PLAYBOOKS) == 5

    def test_instance_failure_not_in_ansible_playbooks(self):
        assert "instance_failure" not in classifier.ANSIBLE_PLAYBOOKS

    def test_playbook_paths_end_with_yml(self):
        for fc, path in classifier.ANSIBLE_PLAYBOOKS.items():
            assert path.endswith(".yml"), f"{fc} playbook path should end with .yml"


# ── get_label() ───────────────────────────────────────────────────────────────

class TestGetLabel:

    def test_returns_human_readable_label(self):
        assert "CPU" in classifier.get_label("cpu_overload")

    def test_unknown_fault_returns_fault_type_itself(self):
        assert classifier.get_label("unknown_fault") == "unknown_fault"


# ── Closed vocabulary invariant ───────────────────────────────────────────────

def test_routing_yaml_covers_exactly_six_fault_classes():
    """Thesis §6: the classifier must reject any fault_type outside the six."""
    assert set(classifier._FAULT_CLASSES.keys()) == {
        "cpu_overload",
        "memory_exhaustion",
        "disk_saturation",
        "service_crash",
        "configuration_drift",
        "instance_failure",
    }


class TestObservabilityRestorePolicy:
    """
    The restore step is declared per fault class in routing.yaml rather than
    tested for by name in app.py; these pin the declaration.
    """

    def test_only_instance_failure_requires_restore(self):
        assert classifier.needs_observability_restore("instance_failure") is True

    @pytest.mark.parametrize("fault", [
        "cpu_overload", "memory_exhaustion", "disk_saturation",
        "service_crash", "configuration_drift",
    ])
    def test_ansible_classes_do_not_require_restore(self, fault):
        assert classifier.needs_observability_restore(fault) is False

    def test_every_fault_class_declares_the_flag(self):
        # A class added without the flag would silently default to False and
        # skip a restore it may need; require the declaration to be explicit.
        for fault, cfg in classifier._ROUTING["fault_classes"].items():
            assert "observability_restore" in cfg, (
                f"{fault} does not declare observability_restore in routing.yaml"
            )
