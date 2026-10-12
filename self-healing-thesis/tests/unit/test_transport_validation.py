"""
tests/unit/test_transport_validation.py — Unit tests for
orchestrator/plan.py::transport_validation (Plan-layer gate 3 of 4).

Covers (Thesis Ch4 Table 4.9 "test_transport_validation.py"):
  - SSM describe_instance_information PingStatus check.
  - PingStatus == "Online" -> proceed (True).
  - PingStatus in {"Offline", "ConnectionLost"} -> abort (False).
  - Absent inventory record (instance not registered with SSM) -> abort (False).
  - No instance_id supplied -> best-effort skip -> proceed (True).
  - boto3 client exception -> abort (False), not raised.

Thesis reference: §3.5.3 (four pre-Execute safety gates, gate 3); Ch4 §4.4.3;
 edge K6.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ORCHESTRATOR = Path(__file__).resolve().parent.parent.parent / "orchestrator"
sys.path.insert(0, str(_ORCHESTRATOR))

import plan


class TestTransportValidation:

    def test_missing_instance_id_fails_closed(self):
        # An unevaluable gate is a failed gate: without ec2_instance_id the SSM
        # readiness check cannot run, so the remediation must abort rather than
        # dispatch past an unchecked precondition.  Regression guard for the
        # former fail-open behaviour, which silently bypassed gate 3 on every
        # cpu_overload trial.
        assert plan.transport_validation("target-1", instance_id=None) is False

    def test_online_ping_status_proceeds(self):
        mock_ssm = MagicMock()
        mock_ssm.describe_instance_information.return_value = {
            "InstanceInformationList": [{"PingStatus": "Online"}]
        }
        with patch.object(plan.boto3, "client", return_value=mock_ssm):
            result = plan.transport_validation("target-1", instance_id="i-0abc123")
        assert result is True

    @pytest.mark.parametrize("ping_status", ["Offline", "ConnectionLost", "Unknown"])
    def test_non_online_ping_status_aborts(self, ping_status):
        mock_ssm = MagicMock()
        mock_ssm.describe_instance_information.return_value = {
            "InstanceInformationList": [{"PingStatus": ping_status}]
        }
        with patch.object(plan.boto3, "client", return_value=mock_ssm):
            result = plan.transport_validation("target-1", instance_id="i-0abc123")
        assert result is False

    def test_absent_inventory_record_aborts(self):
        """Instance not yet registered with SSM (e.g. still booting) — abort."""
        mock_ssm = MagicMock()
        mock_ssm.describe_instance_information.return_value = {
            "InstanceInformationList": []
        }
        with patch.object(plan.boto3, "client", return_value=mock_ssm):
            result = plan.transport_validation("target-1", instance_id="i-0abc123")
        assert result is False

    def test_boto3_exception_aborts_without_raising(self):
        with patch.object(plan.boto3, "client", side_effect=RuntimeError("network error")):
            result = plan.transport_validation("target-1", instance_id="i-0abc123")
        assert result is False

    def test_filters_by_correct_instance_id(self):
        mock_ssm = MagicMock()
        mock_ssm.describe_instance_information.return_value = {
            "InstanceInformationList": [{"PingStatus": "Online"}]
        }
        with patch.object(plan.boto3, "client", return_value=mock_ssm):
            plan.transport_validation("target-1", instance_id="i-0abc123")
        call_kwargs = mock_ssm.describe_instance_information.call_args.kwargs
        assert call_kwargs["Filters"] == [{"Key": "InstanceIds", "Values": ["i-0abc123"]}]
