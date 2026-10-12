"""
tests/unit/test_retry.py — Unit tests for the retry-equipped actuator
dispatch (app._execute_with_retry) and the routing-table retry policy it
consumes (classifier.get_retry_policy).

Covers (Thesis Ch4 Table 4.9 "test_retry.py"):
  - Backoff schedule matches [1, 4] s (geometric, common ratio 4) for the
    four Ansible classes with max_retries=2.
  - configuration_drift: max_retries=1, backoff=[1].
  - instance_failure (Terraform path): max_retries=0, backoff=[] — no retry.
  - _execute_with_retry succeeds without retrying when the first attempt
    succeeds; retries up to max_retries and gives up if all attempts fail;
    sleeps the correct backoff schedule between attempts.

Thesis reference: §3.5.3, §4.4.3, §5.5, §6
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ORCHESTRATOR = Path(__file__).resolve().parent.parent.parent / "orchestrator"
sys.path.insert(0, str(_ORCHESTRATOR))

from classifier import get_retry_policy
import app as app_module


class TestRoutingTableRetryPolicy:
    """Backoff schedule and max_retries as declared in routing.yaml (Table 4.6)."""

    @pytest.mark.parametrize(
        "fault_type,expected_max_retries,expected_backoff",
        [
            ("cpu_overload",        2, [1, 4]),
            ("memory_exhaustion",   2, [1, 4]),
            ("disk_saturation",     2, [1, 4]),
            ("service_crash",       2, [1, 4]),
            ("configuration_drift", 1, [1]),
            ("instance_failure",    0, []),
        ],
    )
    def test_per_class_retry_policy(self, fault_type, expected_max_retries, expected_backoff):
        policy = get_retry_policy(fault_type)
        assert policy["max_retries"] == expected_max_retries
        assert policy["backoff_seconds"] == expected_backoff

    def test_no_backoff_schedule_ever_contains_16(self):
        """Thesis §6 — the historical 16s third interval must never return."""
        for fault_type in (
            "cpu_overload", "memory_exhaustion", "disk_saturation",
            "service_crash", "configuration_drift", "instance_failure",
        ):
            assert 16 not in get_retry_policy(fault_type)["backoff_seconds"]

    def test_instance_failure_has_zero_retries(self):
        """Terraform path: max_retries=0 — a failed apply is never retried automatically."""
        policy = get_retry_policy("instance_failure")
        assert policy["max_retries"] == 0
        assert policy["backoff_seconds"] == []


class TestExecuteWithRetry:
    """app._execute_with_retry — the actual retry-loop mechanism."""

    def _alert(self, fault_type="cpu_overload"):
        return {"labels": {"fault_type": fault_type, "node_name": "target-1"}}

    def test_succeeds_on_first_attempt_no_sleep(self):
        with patch.object(app_module, "dispatch") as mock_dispatch, \
             patch.object(app_module.time, "sleep") as mock_sleep:
            mock_dispatch.return_value = None  # no exception == success
            success, final_retry = app_module._execute_with_retry(
                "ansible", "kill_runaway_process.yml", self._alert(),
                fault_type="cpu_overload", target="target-1", trial_id="t-1",
                max_retries=2, backoff_secs=[1, 4],
            )
        assert success is True
        assert final_retry == 0
        mock_sleep.assert_not_called()
        mock_dispatch.assert_called_once()

    def test_succeeds_on_second_attempt_sleeps_1s(self):
        with patch.object(app_module, "dispatch") as mock_dispatch, \
             patch.object(app_module.time, "sleep") as mock_sleep:
            mock_dispatch.side_effect = [RuntimeError("transient"), None]
            success, final_retry = app_module._execute_with_retry(
                "ansible", "kill_runaway_process.yml", self._alert(),
                fault_type="cpu_overload", target="target-1", trial_id="t-1",
                max_retries=2, backoff_secs=[1, 4],
            )
        assert success is True
        assert final_retry == 1
        mock_sleep.assert_called_once_with(1)

    def test_succeeds_on_third_attempt_sleeps_1s_then_4s(self):
        with patch.object(app_module, "dispatch") as mock_dispatch, \
             patch.object(app_module.time, "sleep") as mock_sleep:
            mock_dispatch.side_effect = [
                RuntimeError("transient"), RuntimeError("transient"), None,
            ]
            success, final_retry = app_module._execute_with_retry(
                "ansible", "kill_runaway_process.yml", self._alert(),
                fault_type="cpu_overload", target="target-1", trial_id="t-1",
                max_retries=2, backoff_secs=[1, 4],
            )
        assert success is True
        assert final_retry == 2
        assert mock_sleep.call_args_list == [((1,),), ((4,),)]

    def test_gives_up_after_max_retries_exhausted(self):
        with patch.object(app_module, "dispatch") as mock_dispatch, \
             patch.object(app_module.time, "sleep"):
            mock_dispatch.side_effect = RuntimeError("permanent failure")
            success, final_retry = app_module._execute_with_retry(
                "ansible", "kill_runaway_process.yml", self._alert(),
                fault_type="cpu_overload", target="target-1", trial_id="t-1",
                max_retries=2, backoff_secs=[1, 4],
            )
        assert success is False
        assert final_retry == 2
        assert mock_dispatch.call_count == 3   # 1 initial + 2 retries

    def test_terraform_path_never_retries(self):
        """instance_failure: max_retries=0 — exactly one attempt, no sleep."""
        with patch.object(app_module, "dispatch") as mock_dispatch, \
             patch.object(app_module.time, "sleep") as mock_sleep:
            mock_dispatch.side_effect = RuntimeError("terraform apply failed")
            success, final_retry = app_module._execute_with_retry(
                "terraform", "", self._alert("instance_failure"),
                fault_type="instance_failure", target="target-1", trial_id="t-1",
                max_retries=0, backoff_secs=[],
            )
        assert success is False
        assert final_retry == 0
        mock_sleep.assert_not_called()
        mock_dispatch.assert_called_once()

    def test_configuration_drift_single_retry(self):
        """configuration_drift: max_retries=1, backoff=[1]."""
        with patch.object(app_module, "dispatch") as mock_dispatch, \
             patch.object(app_module.time, "sleep") as mock_sleep:
            mock_dispatch.side_effect = [RuntimeError("first fails"), None]
            success, final_retry = app_module._execute_with_retry(
                "ansible", "config_reapply.yml", self._alert("configuration_drift"),
                fault_type="configuration_drift", target="target-1", trial_id="t-1",
                max_retries=1, backoff_secs=[1],
            )
        assert success is True
        assert final_retry == 1
        mock_sleep.assert_called_once_with(1)


class TestRetryExhaustionRecord:
    """What _run_mape_k persists when every attempt fails (Table 4.9)."""

    def test_exhaustion_records_retries_not_attempts(self):
        """
        retry_count is the number of retries after the first attempt.  With
        max_retries = 2 the actuator runs three times, and the record must say
        2: _execute_with_retry returns (False, max_retries), and the handler
        previously wrote max_retries + 1, which is the attempt count.
        """
        saved: dict = {}
        mttr = MagicMock()
        mttr.save.side_effect = lambda **kw: saved.update(kw)
        mttr.record_remediation.side_effect = (
            lambda **kw: saved.__setitem__("remediation_retry_count", kw["retry_count"])
        )
        alert = {"labels": {"fault_type": "cpu_overload", "node_name": "target-1",
                            "ec2_instance_id": "i-0000000000000000a"}}
        policy = {"max_retries": 2, "backoff_seconds": [1, 4]}
        with patch.object(app_module, "MTTRLogger", return_value=mttr), \
             patch.object(app_module, "classify_and_route",
                          return_value=("ansible", "kill_runaway_process.yml")), \
             patch.object(app_module._circuit_breaker, "check", return_value="closed"), \
             patch.object(app_module._lease_tracker, "acquire", return_value=True), \
             patch.object(app_module._lease_tracker, "release"), \
             patch.object(app_module, "transport_validation", return_value=True), \
             patch.object(app_module, "get_retry_policy", return_value=policy), \
             patch.object(app_module, "_execute_with_retry", return_value=(False, 2)), \
             patch.object(app_module, "_escalate") as escalate:
            app_module._run_mape_k(alert, target="target-1",
                                   fault_type="cpu_overload", instance="10.0.2.10")
        assert saved["recovery_status"] == "retry_exhausted"
        assert saved["escalation_reason"] == "retry_exhausted"
        assert saved["retry_count"] == 2
        assert saved["remediation_retry_count"] == 2
        escalate.assert_called_once_with(alert, reason="retry_exhausted")
