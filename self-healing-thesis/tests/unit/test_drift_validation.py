"""
tests/unit/test_drift_validation.py — Unit tests for
orchestrator/plan.py::drift_validation and dynamodb_lock_status (Plan-layer
gate 4 of 4, Terraform path only).

Covers (Thesis Ch4 Table 4.9 "test_drift_validation.py"):
  - `terraform plan -refresh-only -json` exit code 0 (no drift) -> proceed.
  - Exit code 2 (drift detected), scoped to the faulted instance -> proceed,
    drift_detected=True, reason="scoped_drift". Scope is parsed from the
    JSON "resource_drift" UI messages, not the human-readable plan text
    (deployment-session correction — the text format carries no
    cross-version compatibility guarantee).
  - Exit code 2, drift on a resource OTHER than the faulted instance
    (scope-violation) -> abort, reason="out_of_scope_drift".
  - Non-"resource_drift" JSON message types and malformed lines in the
    -json stream are ignored, not mistaken for drift.
  - Exit code 1 (terraform error) -> abort, reason="terraform_error".
  - subprocess timeout / terraform binary not found -> abort, each with its
    own distinct reason ("timeout" / "binary_not_found") — NOT reported as
    "out_of_scope_drift", since drift status was never actually determined.
 - DynamoDB stale-lock precondition (Ch4 §4.2.2): a held
    lock short-circuits drift_validation before terraform plan even runs,
    reason="lock_held".

Thesis reference: §3.5.4 (state drift validation); Ch4 §4.2.2
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ORCHESTRATOR = Path(__file__).resolve().parent.parent.parent / "orchestrator"
sys.path.insert(0, str(_ORCHESTRATOR))

import plan


def _mock_popen(returncode: int, stdout: str = "", stderr: str = ""):
    """Stand in for subprocess.Popen, matching the Popen + communicate()
    pattern drift_validation() now uses instead of subprocess.run(timeout=).
    Confirmed live 2026-10-01: subprocess.run's timeout handling SIGKILLs
    terraform unconditionally, which orphans its own DynamoDB state lock
    every time (SIGKILL cannot be intercepted, so Terraform's deferred
    lock-release code never runs) -- see plan.py's own comment at the call
    site for the full incident. Real Popen.returncode is None until the
    process exits; MagicMock lets a test fix it directly as the post-exit
    value, which is what the production code observes after communicate()
    returns."""
    mock_proc = MagicMock()
    mock_proc.communicate.return_value = (stdout, stderr)
    mock_proc.returncode = returncode
    mock_proc.pid = 12345
    return mock_proc


class TestDriftValidationNoLock:
    """All tests here assume no DynamoDB lock is held (default env config)."""

    @pytest.fixture(autouse=True)
    def _no_lock(self):
        with patch.object(plan, "dynamodb_lock_status", return_value=None):
            yield

    def test_no_drift_proceeds(self):
        with patch.object(plan.subprocess, "Popen", return_value=_mock_popen(0)):
            proceed, drift, reason = plan.drift_validation("target-2")
        assert proceed is True
        assert drift is False
        assert reason == ""

    def test_drift_scoped_to_faulted_instance_proceeds(self):
        # -json UI-message stream: a single "resource_drift" line whose
        # change.resource.addr matches the faulted target exactly.
        stdout = json.dumps({
            "type": "resource_drift",
            "change": {"resource": {"addr": 'module.compute.aws_instance.targets["target-2"]'}},
        }) + "\n"
        with patch.object(plan.subprocess, "Popen", return_value=_mock_popen(2, stdout=stdout)):
            proceed, drift, reason = plan.drift_validation("target-2")
        assert proceed is True
        assert drift is True
        assert reason == "scoped_drift"

    def test_drift_out_of_scope_aborts(self):
        # Drift on target-3 while target-2 is the faulted instance under
        # remediation — out of scope, must abort.
        stdout = json.dumps({
            "type": "resource_drift",
            "change": {"resource": {"addr": 'module.compute.aws_instance.targets["target-3"]'}},
        }) + "\n"
        with patch.object(plan.subprocess, "Popen", return_value=_mock_popen(2, stdout=stdout)):
            proceed, drift, reason = plan.drift_validation("target-2")
        assert proceed is False
        assert drift is True
        assert reason == "out_of_scope_drift"

    def test_drift_on_sibling_resource_of_faulted_target_is_in_scope(self):
        # Every resource keyed to the faulted target is expected to drift when
        # that target is terminated -- its CloudWatch alarms as well as the
        # instance.  Scope is decided by the for_each key, not resource type.
        stdout = "\n".join([
            json.dumps({"type": "resource_drift", "change": {"resource": {
                "addr": 'module.compute.aws_instance.targets["target-2"]',
                "resource_key": "target-2"}}}),
            json.dumps({"type": "resource_drift", "change": {"resource": {
                "addr": 'module.compute.aws_cloudwatch_metric_alarm.cpu_high["target-2"]',
                "resource_key": "target-2"}}}),
        ]) + "\n"
        with patch.object(plan.subprocess, "Popen", return_value=_mock_popen(2, stdout=stdout)):
            proceed, drift, reason = plan.drift_validation("target-2")
        assert proceed is True
        assert reason == "scoped_drift"

    def test_drift_on_unkeyed_shared_resource_aborts(self):
        # A drifting resource with no for_each key is shared infrastructure
        # within this root configuration and is never in scope.
        stdout = json.dumps({"type": "resource_drift", "change": {"resource": {
            "addr": "module.compute.aws_launch_template.asg_baseline"}}}) + "\n"
        with patch.object(plan.subprocess, "Popen", return_value=_mock_popen(2, stdout=stdout)):
            proceed, drift, reason = plan.drift_validation("target-2")
        assert proceed is False
        assert drift is True
        assert reason == "out_of_scope_drift"

    def test_module_qualified_sibling_drift_is_detected(self):
        # Regression guard: a literal "aws_instance.targets" prefix test does
        # not match Terraform's fully-qualified, module-scoped address, which
        # would silently render this gate inert in production.
        stdout = json.dumps({"type": "resource_drift", "change": {"resource": {
            "addr": 'module.compute.aws_instance.targets["target-3"]',
            "resource_key": "target-3"}}}) + "\n"
        with patch.object(plan.subprocess, "Popen", return_value=_mock_popen(2, stdout=stdout)):
            proceed, drift, reason = plan.drift_validation("target-2")
        assert proceed is False
        assert reason == "out_of_scope_drift"

    def test_count_indexed_resource_aborts(self):
        # A count-indexed resource is addressed as ...name[0] and carries an
        # integer resource_key rather than a string one.  An integer key can
        # never equal the faulted target's name, so it is out of scope either
        # way, but the payload shape is worth pinning against a regression in
        # the key comparison.
        #
        # The address below is historical: the S3-baseline ASG was once
        # count-gated inside modules/compute and could therefore appear in a
        # targets drift check.  It now lives in its own root and state
        # (terraform/baseline, AD-017) and can no longer appear here at all.
        # The case is retained because count-indexed resources may be
        # reintroduced to this root for other reasons, and the gate must keep
        # rejecting them.
        stdout = json.dumps({"type": "resource_drift", "change": {"resource": {
            "addr": "module.compute.aws_autoscaling_group.baseline[0]",
            "resource_key": 0}}}) + "\n"
        with patch.object(plan.subprocess, "Popen", return_value=_mock_popen(2, stdout=stdout)):
            proceed, drift, reason = plan.drift_validation("target-2")
        assert proceed is False
        assert reason == "out_of_scope_drift"

    def test_scoped_drift_mixed_with_baseline_drift_aborts(self):
        # Case D2: the faulted target drifted *and* so did some other,
        # unattributable resource.  The gate must reject the whole plan, not
        # proceed because the faulted target happens to appear in the set.
        # The second address is the historical baseline ASG (see the note on
        # test_count_indexed_resource_aborts); what it stands for here is any
        # co-occurring out-of-scope drift.
        stdout = "\n".join([
            json.dumps({"type": "resource_drift", "change": {"resource": {
                "addr": 'module.compute.aws_instance.targets["target-2"]',
                "resource_key": "target-2"}}}),
            json.dumps({"type": "resource_drift", "change": {"resource": {
                "addr": "module.compute.aws_autoscaling_group.baseline[0]",
                "resource_key": 0}}}),
        ]) + "\n"
        with patch.object(plan.subprocess, "Popen", return_value=_mock_popen(2, stdout=stdout)):
            proceed, drift, reason = plan.drift_validation("target-2")
        assert proceed is False
        assert reason == "out_of_scope_drift"

    def test_drift_json_ignores_non_drift_message_types(self):
        # -json emits many message types (log lines, refresh_start, etc.);
        # only "resource_drift" entries should be inspected for scope.
        stdout = "\n".join([
            json.dumps({"type": "refresh_start", "change": {}}),
            json.dumps({"type": "resource_drift",
                        "change": {"resource": {"addr": 'module.compute.aws_instance.targets["target-2"]'}}}),
            "not even json",
        ]) + "\n"
        with patch.object(plan.subprocess, "Popen", return_value=_mock_popen(2, stdout=stdout)):
            proceed, drift, reason = plan.drift_validation("target-2")
        assert proceed is True
        assert drift is True
        assert reason == "scoped_drift"

    def test_terraform_error_aborts(self):
        with patch.object(plan.subprocess, "Popen", return_value=_mock_popen(1, stderr="rate limited")):
            proceed, drift, reason = plan.drift_validation("target-2")
        assert proceed is False
        assert drift is False
        assert reason == "terraform_error"

    def test_subprocess_timeout_sends_sigterm_and_aborts(self):
        # First communicate() call (the initial 120s wait) times out; the
        # grace-period communicate(timeout=10) call after SIGTERM succeeds,
        # simulating the now-expected case where terraform honours SIGTERM
        # and releases its own lock cleanly. SIGKILL must NOT be sent here.
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="terraform", timeout=120),
            ("", ""),
        ]
        with patch.object(plan.subprocess, "Popen", return_value=mock_proc), \
             patch.object(plan.os, "getpgid", return_value=12345), \
             patch.object(plan.os, "killpg") as mock_killpg:
            proceed, drift, reason = plan.drift_validation("target-2")
        assert proceed is False
        assert drift is False
        assert reason == "timeout"
        mock_killpg.assert_called_once_with(12345, plan.signal.SIGTERM)

    def test_subprocess_timeout_escalates_to_sigkill_if_sigterm_ignored(self):
        # Both the initial wait and the post-SIGTERM grace period time out --
        # terraform ignored SIGTERM, so SIGKILL is the last-resort fallback.
        # This is the rare case this fix exists to minimise, not the norm.
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.communicate.side_effect = [
            subprocess.TimeoutExpired(cmd="terraform", timeout=120),
            subprocess.TimeoutExpired(cmd="terraform", timeout=10),
            ("", ""),
        ]
        with patch.object(plan.subprocess, "Popen", return_value=mock_proc), \
             patch.object(plan.os, "getpgid", return_value=12345), \
             patch.object(plan.os, "killpg") as mock_killpg:
            proceed, drift, reason = plan.drift_validation("target-2")
        assert proceed is False
        assert drift is False
        assert reason == "timeout"
        mock_killpg.assert_any_call(12345, plan.signal.SIGTERM)
        mock_killpg.assert_any_call(12345, plan.signal.SIGKILL)
        assert mock_killpg.call_count == 2

    def test_terraform_binary_missing_aborts(self):
        with patch.object(plan.subprocess, "Popen", side_effect=FileNotFoundError()):
            proceed, drift, reason = plan.drift_validation("target-2")
        assert proceed is False
        assert drift is False
        assert reason == "binary_not_found"


class TestDynamoDBLockStatus:

    def test_no_table_configured_skips_check(self):
        result = plan.dynamodb_lock_status(table_name="", bucket="")
        assert result is None

    def test_no_item_returns_none(self):
        mock_dynamodb = MagicMock()
        mock_dynamodb.get_item.return_value = {}
        with patch.object(plan.boto3, "client", return_value=mock_dynamodb):
            result = plan.dynamodb_lock_status(
                table_name="self-healing-thesis-experiment-tflock",
                bucket="self-healing-tfstate-123",
                key="targets/terraform.tfstate",
            )
        assert result is None

    def test_lock_held_returns_info_dict(self):
        mock_dynamodb = MagicMock()
        mock_dynamodb.get_item.return_value = {
            "Item": {"Info": {"S": '{"Operation":"OperationTypeApply","Who":"ci-runner"}'}}
        }
        with patch.object(plan.boto3, "client", return_value=mock_dynamodb):
            result = plan.dynamodb_lock_status(
                table_name="self-healing-thesis-experiment-tflock",
                bucket="self-healing-tfstate-123",
                key="targets/terraform.tfstate",
            )
        assert result is not None
        assert result["Operation"] == "OperationTypeApply"

    def test_dynamodb_error_returns_none_without_raising(self):
        with patch.object(plan.boto3, "client", side_effect=RuntimeError("network error")):
            result = plan.dynamodb_lock_status(
                table_name="self-healing-thesis-experiment-tflock",
                bucket="self-healing-tfstate-123",
            )
        assert result is None


class TestDriftValidationWithLockHeld:

    def test_held_lock_aborts_before_running_terraform_plan(self):
        with patch.object(plan, "dynamodb_lock_status", return_value={"Operation": "OperationTypeApply"}), \
             patch.object(plan.subprocess, "Popen") as mock_popen:
            proceed, drift, reason = plan.drift_validation("target-2")
        assert proceed is False
        assert drift is False
        assert reason == "lock_held"
        mock_popen.assert_not_called()   # short-circuited before terraform plan
