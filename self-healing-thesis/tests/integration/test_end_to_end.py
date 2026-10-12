"""
tests/integration/test_end_to_end.py — End-to-end integration tests.

Exercises all eleven scenarios required by (Ch4 Table 4.11 six
fault classes, extended per Round-19 M3 with retry-exhaustion and
transport-validation as distinct escalation scenarios):
  1. cpu_overload          → Ansible → kill_runaway_process.yml
  2. memory_exhaustion     → Ansible → memory_cleanup.yml
  3. disk_saturation       → Ansible → disk_cleanup.yml
  4. service_crash         → Ansible → service_crash.yml
  5. configuration_drift   → Ansible → config_reapply.yml
  6. instance_failure      → Terraform → targets workspace apply
  7. retry_exhaustion      → Ansible (forced fail) → SNS escalation
                              (escalation_reason == "retry_exhausted")
  8. transport_validation  → SSM unreachable target → SNS escalation
  9. diagnostic restore    → forced failure → diagnostic_bootstrap_failed
 10. drift check           → inconclusive → drift_check_error
 11. plan-scope guard      → out-of-scope plan → unsafe_plan_scope

Acceptance criterion (the five configuration classes):
    assert rec["T_MTTR_seconds"] < 300        # RQ1 SLO
instance_failure is held to a successful five-phase recovery instead: its
Terraform recreation plus diagnostic-agent restore routinely exceeds 300 s
(observed 453-797 s over the ten official trials), which is the finding that
makes the aggregate H3 fail, not a defect of the scenario.

Prerequisites:
  - AWS credentials in environment
  - Monitoring server and target instances running (terraform apply completed)
  - Observability stack running (docker compose up -d)
  - pytest --integration flag or INTEGRATION_TESTS=1 env var

Run:
    INTEGRATION_TESTS=1 pytest tests/integration/ -v --timeout=600

Thesis reference: §8 Testing, Table 4.11
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import pytest
import requests

_ROOT = Path(__file__).resolve().parent.parent.parent

# ── Skip marker ───────────────────────────────────────────────────────────────
# Integration tests require live AWS infrastructure.
# They are skipped by default unless explicitly enabled.

def _integration_enabled() -> bool:
    return os.environ.get("INTEGRATION_TESTS", "0") in ("1", "true", "yes")


pytestmark = pytest.mark.skipif(
    not _integration_enabled(),
    reason="Integration tests disabled. Set INTEGRATION_TESTS=1 to enable.",
)

_PROMETHEUS_URL   = os.environ.get("PROMETHEUS_URL",   "http://localhost:9090")
_ORCHESTRATOR_URL = os.environ.get("ORCHESTRATOR_URL", "http://localhost:9094")
_REGION           = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")
_SLO_SECONDS      = 300
# How long to wait for a trial's MTTR record.  The five configuration classes
# get the SLO plus the 120 s margin run_one.py also allows; instance_failure
# adds Terraform recreation and the diagnostic-agent restore, so it gets the
# 900 s ceiling run_one.py uses for that class.
_INSTANCE_FAILURE_CEILING_SECONDS = 900
_MTTR_LOG         = _ROOT / "results" / "mttr_log.jsonl"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _inject(fault: str, target: str, duration: int = 60) -> str:
    """Call inject_once.py and return the run_id used as trial_id."""
    run_id = str(uuid.uuid4())
    script = _ROOT / "experiments" / "inject" / "inject_once.py"
    cmd = [
        sys.executable, str(script),
        "--fault",    fault,
        "--target",   target,
        "--duration", str(duration),
        "--region",   _REGION,
        "--run-id",   run_id,
    ]
    result = subprocess.run(cmd, timeout=duration + 120)
    assert result.returncode == 0, f"inject_once.py failed for fault={fault}"
    return run_id


def _ceiling_for(fault: str) -> int:
    """Seconds to wait for the MTTR record of a trial injecting `fault`."""
    if fault == "instance_failure":
        return _INSTANCE_FAILURE_CEILING_SECONDS
    return _SLO_SECONDS + 120


def _wait_for_record(trial_id: str, timeout: int = _SLO_SECONDS + 120) -> Optional[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _MTTR_LOG.exists():
            for line in _MTTR_LOG.read_text().splitlines():
                try:
                    rec = json.loads(line)
                    if rec.get("trial_id") == trial_id:
                        return rec
                except json.JSONDecodeError:
                    continue
        time.sleep(5)
    return None


def _steady_state(window: int = 30, timeout: int = 180) -> bool:
    from experiments.trials.wait_steady_state import wait_steady_state
    return wait_steady_state(window=window, timeout=timeout,
                             prometheus_url=_PROMETHEUS_URL)


def _post_synthetic_alert(
    *,
    fault_type: str,
    target: str,
    instance: str,
    trial_id: str,
    instance_id: Optional[str] = None,
) -> None:
    """
    POST a synthetic, Alertmanager-v2-shaped webhook payload directly to the
    live orchestrator's /alert route. Used for escalation scenarios (7, 8)
    that must be forced deterministically rather than via real Prometheus
    alert firing — the same route real Alertmanager delivery hits, so it
    exercises the full Analyse -> Plan -> Execute -> Knowledge path.
    """
    labels = {"fault_type": fault_type, "node_name": target, "instance": instance}
    if instance_id:
        labels["instance_id"] = instance_id
    payload = {
        "alerts": [{
            "status": "firing",
            "labels": labels,
            # Both trial_id and injection_timestamp are required together for
            # mttr_logger.py's priority-1 correlation resolution — a synthetic
            # post has no corresponding experiments/correlation record to fall
            # back on (priority 2), so this is the only way this test's
            # trial_id round-trips into the MTTR log for _wait_for_record().
            "annotations": {
                "trial_id": trial_id,
                "injection_timestamp": str(time.time()),
            },
        }]
    }
    resp = requests.post(f"{_ORCHESTRATOR_URL}/alert", json=payload, timeout=10)
    resp.raise_for_status()


# ── Integration scenarios ─────────────────────────────────────────────────────

class TestOrchestrator:

    def test_orchestrator_health(self):
        """Orchestrator /healthz must return HTTP 200."""
        resp = requests.get(f"{_ORCHESTRATOR_URL}/healthz", timeout=5)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_prometheus_reachable(self):
        resp = requests.get(f"{_PROMETHEUS_URL}/-/ready", timeout=5)
        assert resp.status_code == 200


@pytest.mark.timeout(600)
class TestSelfHealingScenarios:
    """Eleven integration scenarios — Thesis §8, Table 4.11."""

    def _run_scenario(self, fault: str, target: str, duration: int = 60) -> dict:
        assert _steady_state(), "Pre-trial steady state not achieved"
        trial_id = _inject(fault, target, duration)
        rec = _wait_for_record(trial_id, timeout=_ceiling_for(fault))
        assert rec is not None, f"MTTR record not found for trial_id={trial_id} fault={fault}"
        # A failed trial records T_MTTR = -1.0, which would pass a bare
        # "< SLO" check, so the outcome is asserted first.
        assert rec["recovery_status"] == "success", (
            f"trial ended {rec['recovery_status']!r} "
            f"(escalation_reason={rec.get('escalation_reason')!r})"
        )
        if fault != "instance_failure":
            assert rec["T_MTTR_seconds"] < _SLO_SECONDS, (
                f"SLO violation: T_MTTR={rec['T_MTTR_seconds']}s >= {_SLO_SECONDS}s"
            )
        return rec

    def test_1_cpu_overload(self):
        rec = self._run_scenario("cpu_overload", "target-1", duration=90)
        assert rec["actuator"] == "ansible"
        assert "kill_runaway_process" in rec["playbook"]

    def test_2_memory_exhaustion(self):
        rec = self._run_scenario("memory_exhaustion", "target-2", duration=90)
        assert rec["actuator"] == "ansible"
        assert "memory_cleanup" in rec["playbook"]

    def test_3_disk_saturation(self):
        rec = self._run_scenario("disk_saturation", "target-3", duration=90)
        assert rec["actuator"] == "ansible"
        assert "disk_cleanup" in rec["playbook"]

    def test_4_service_crash(self):
        rec = self._run_scenario("service_crash", "target-1", duration=0)
        assert rec["actuator"] == "ansible"
        assert "service_crash" in rec["playbook"]

    def test_5_configuration_drift(self):
        rec = self._run_scenario("configuration_drift", "target-2", duration=0)
        assert rec["actuator"] == "ansible"
        assert "config_reapply" in rec["playbook"]

    @pytest.mark.timeout(1200)   # steady state + recreation + restore + validation
    def test_6_instance_failure(self):
        rec = self._run_scenario("instance_failure", "target-3", duration=0)
        assert rec["actuator"] == "terraform"
        # The fifth phase ran: diagnostic agents restored and forwarding verified.
        assert rec["T_observability_restore_seconds"] > 0
        assert rec["T_MTTR_seconds"] > 0

    def test_7_retry_exhaustion_escalates_to_sns(self):
        """
        Force Ansible to fail repeatedly (max_retries times) by injecting a
        fault against a pre-arranged, deliberately broken staging target and
        confirming SNS escalation with escalation_reason == "retry_exhausted".

        Retry exhaustion cannot be synthesised purely by posting a webhook
        payload — the actuator must genuinely fail max_retries+1 times, which
        requires a real target whose remediation precondition is broken (for
        example: NGINX intentionally uninstalled, or the verify step's
        threshold impossible to satisfy). Rather than skip unconditionally
        (the prior state of this test), the fixture is opt-in via
        RETRY_EXHAUSTION_TARGET so this scenario runs whenever staging is
        provisioned with the broken target, and skips cleanly otherwise.
        """
        broken_target = os.environ.get("RETRY_EXHAUSTION_TARGET", "")
        if not broken_target:
            pytest.skip(
                "RETRY_EXHAUSTION_TARGET not set — provision a staging target "
                "with a deliberately-broken remediation precondition (e.g. "
                "NGINX binary removed) and set RETRY_EXHAUSTION_TARGET=<name> "
                "to enable this scenario."
            )

        assert _steady_state(), "Pre-trial steady state not achieved"
        trial_id = _inject("cpu_overload", broken_target, duration=90)
        rec = _wait_for_record(trial_id)
        assert rec is not None, f"MTTR record not found for trial_id={trial_id}"
        assert rec["recovery_status"] == "retry_exhausted"
        assert rec["escalation_reason"] == "retry_exhausted"
        # retry_count records RETRIES, not attempts: _execute_with_retry returns
        # (False, max_retries) on exhaustion, so the field holds 2 and the trial
        # made 3 attempts (retry_count + 1).  The former assertion of 3 conflated
        # the two and would have failed the first time this opt-in test ran.
        assert rec["retry_count"] == 2   # max_retries=2 -> 2 retries, 3 attempts

    def test_8_transport_validation_escalates_to_sns(self):
        """
        Force an SSM-unreachable target (one with no SSM inventory record)
        and confirm the Plan layer's transport-validation gate aborts with
        escalation_reason == "transport_unreachable" (Thesis §3.5.3 gate 3;
        Ch4 §4.4.3 edge K6).

        Deterministic and does not require any staging fixture: an
        instance_id that has never registered with SSM always yields an
        empty InstanceInformationList, per plan.transport_validation().
        """
        trial_id = str(uuid.uuid4())
        _post_synthetic_alert(
            fault_type="cpu_overload",
            target="target-unreachable-test",
            instance="10.0.2.253:9100",
            instance_id="i-0000000000nonexist",
            trial_id=trial_id,
        )
        rec = _wait_for_record(trial_id, timeout=60)
        assert rec is not None, f"MTTR record not found for trial_id={trial_id}"
        assert rec["recovery_status"] == "aborted_plan"
        assert rec["escalation_reason"] == "transport_unreachable"

    @pytest.mark.timeout(1200)
    def test_9_diagnostic_bootstrap_failure_escalates_to_sns(self):
        """
        Force the Execute layer's post-recreation diagnostic-agent restore to
        fail and confirm the trial ends with
        escalation_reason == "diagnostic_bootstrap_failed".

        This is the only escalation reason that can fire *after* a successful
        remediation, so it is the one path where recovery_status must not be
        "success" despite the actuator having completed.  Requires a staging
        fixture: DIAGNOSTIC_FAILURE_TARGET names a target whose
        diagnostic_agents.yml run is arranged to fail (for example, the
        Filebeat apt source made unreachable), so the restore step fails
        while the Terraform replace itself succeeds.
        """
        target = os.environ.get("DIAGNOSTIC_FAILURE_TARGET", "")
        if not target:
            pytest.skip(
                "DIAGNOSTIC_FAILURE_TARGET not set — provision a staging "
                "target whose diagnostic-agent restore is arranged to fail "
                "(e.g. Filebeat package source unreachable) to enable this "
                "scenario."
            )

        assert _steady_state(), "Pre-trial steady state not achieved"
        trial_id = _inject("instance_failure", target, duration=0)
        rec = _wait_for_record(trial_id, timeout=_INSTANCE_FAILURE_CEILING_SECONDS)
        assert rec is not None, f"MTTR record not found for trial_id={trial_id}"
        assert rec["recovery_status"] == "diagnostic_bootstrap_failed"
        assert rec["escalation_reason"] == "diagnostic_bootstrap_failed"
        # The restore never completed, so neither the observability-restore
        # checkpoint nor the validation checkpoint may carry a real value.
        assert rec["t_observability_restored_ms"] == -1
        assert rec["T_observability_restore_seconds"] == -1.0
        assert rec["T_validate_seconds"] == -1.0

    def test_10_drift_check_error_escalates_to_sns(self):
        """
        Force the Plan layer's refresh-only drift check to be inconclusive and
        confirm the trial aborts with escalation_reason == "drift_check_error"
        rather than "out_of_scope_drift".

        The distinction is the point of the scenario: a check that could not
        determine drift status must not be recorded as one that ran and found
        drift.  The fixture makes the check inconclusive without touching real
        state — TERRAFORM_UNAVAILABLE_DIR names a directory that is not an
        initialised Terraform root, so `terraform plan -refresh-only` exits
        with an error rather than a drift verdict.
        """
        if not os.environ.get("TERRAFORM_UNAVAILABLE_DIR", ""):
            pytest.skip(
                "TERRAFORM_UNAVAILABLE_DIR not set — point it at a directory "
                "that is not an initialised Terraform root and restart the "
                "orchestrator with TERRAFORM_TARGETS_DIR set to it to enable "
                "this scenario."
            )

        trial_id = str(uuid.uuid4())
        _post_synthetic_alert(
            fault_type="instance_failure",
            target="target-1",
            instance="10.0.2.11:9100",
            instance_id=os.environ.get("DRIFT_CHECK_INSTANCE_ID", "i-0000000000nonexist"),
            trial_id=trial_id,
        )
        rec = _wait_for_record(trial_id, timeout=180)
        assert rec is not None, f"MTTR record not found for trial_id={trial_id}"
        assert rec["recovery_status"] == "aborted_plan"
        assert rec["escalation_reason"] == "drift_check_error"
        assert rec["drift_detected"] is False

    def test_11_unsafe_plan_scope_escalates_without_applying(self):
        """
        Force the Terraform plan produced for an instance_failure remediation
        to fall outside the permitted scope, and confirm the trial aborts with
        escalation_reason == "unsafe_plan_scope" having applied nothing.

        This is the machine-checkable form of the invariant Chapter 3 states
        in prose: an instance_failure remediation may create exactly the one
        failed target and must never destroy unrelated infrastructure. The
        guard runs between plan and apply, so a violation leaves real
        infrastructure untouched — the assertions below check for the absence
        of a remediation timestamp, not merely for the escalation.

        Distinct from scenario 10. Gate 4 (drift_check_error) asks whether
        state still reflects reality, using `plan -refresh-only`. This guard
        asks what mutations a normal plan is about to perform. A fixture that
        breaks one does not exercise the other.

        The fixture: point the orchestrator at a targets workspace whose
        configuration differs from live state in some way beyond the single
        missing instance — the simplest being a workspace where a second
        resource has been added or removed — so the plan contains more than
        one mutating change while state and reality still agree.
        """
        if not os.environ.get("UNSAFE_PLAN_TARGETS_DIR", ""):
            pytest.skip(
                "UNSAFE_PLAN_TARGETS_DIR not set — point it at an initialised "
                "targets workspace whose configuration produces more than one "
                "mutating change, and restart the orchestrator with "
                "TERRAFORM_TARGETS_DIR set to it to enable this scenario."
            )

        trial_id = str(uuid.uuid4())
        _post_synthetic_alert(
            fault_type="instance_failure",
            target="target-1",
            instance="10.0.2.11:9100",
            instance_id=os.environ.get("UNSAFE_PLAN_INSTANCE_ID", "i-0000000000nonexist"),
            trial_id=trial_id,
        )
        rec = _wait_for_record(trial_id, timeout=180)
        assert rec is not None, f"MTTR record not found for trial_id={trial_id}"
        assert rec["recovery_status"] == "aborted_plan"
        assert rec["escalation_reason"] == "unsafe_plan_scope"
        # The guard aborts before apply, so remediation never completed and
        # neither it nor anything downstream may carry a real timestamp.
        assert rec["t_remediated_ms"] == -1
        assert rec["T_remediate_seconds"] == -1.0
        assert rec["T_validate_seconds"] == -1.0
