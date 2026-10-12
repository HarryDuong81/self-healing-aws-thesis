"""
tests/unit/test_health_checker.py — Unit tests for
orchestrator/health_checker.py (two-stage Validate-phase verification).

Covers (Thesis Ch4 Table 4.9 "test_health_checker.py"):
  - Canonical t_clear check: polls Prometheus ALERTS{fault_type=...,
    node_name=..., alertstate="firing"} every 5s; empty result -> True
    (alert cleared); non-empty result until timeout -> False.
  - The canonical check NEVER probes the target directly (Ch3 §3.2.1 — the
    Execute pillar never reads sensor data directly).
  - Stage E3 sanity probe (is_healthy / e3_sanity_check) is a fast HTTP
    /healthz probe, independent of the canonical check, used only for
    retry-loop feedback.

Thesis reference: §3.7.2 (two-stage verification), Ch4 §4.6.2 / Appendix 4.A.9
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ORCHESTRATOR = Path(__file__).resolve().parent.parent.parent / "orchestrator"
sys.path.insert(0, str(_ORCHESTRATOR))

import health_checker as hc_module
from health_checker import HealthChecker


def _mock_response(result: list):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"data": {"result": result}}
    return resp


class TestCanonicalValidation:
    """wait_until_healthy — the canonical t_clear signal."""

    def test_alert_cleared_immediately_returns_true(self):
        checker = HealthChecker(target="target-1", fault_type="cpu_overload")
        with patch.object(hc_module.time, "sleep"), \
             patch.object(hc_module.requests, "get", return_value=_mock_response([])):
            assert checker.wait_until_healthy(timeout=30) is True

    def test_query_uses_fault_type_and_node_name_labels(self):
        checker = HealthChecker(target="target-2", fault_type="memory_exhaustion")
        with patch.object(hc_module.time, "sleep"), \
             patch.object(hc_module.requests, "get", return_value=_mock_response([])) as mock_get:
            checker.wait_until_healthy(timeout=30)
        params = mock_get.call_args.kwargs["params"]
        assert 'fault_type="memory_exhaustion"' in params["query"]
        assert 'node_name="target-2"' in params["query"]
        assert 'alertstate="firing"' in params["query"]

    def test_still_firing_until_timeout_returns_false(self):
        checker = HealthChecker(target="target-1", fault_type="cpu_overload")
        still_firing = _mock_response([{"metric": {"fault_type": "cpu_overload"}}])
        with patch.object(hc_module.time, "monotonic", side_effect=[0, 1, 2, 3, 31]), \
             patch.object(hc_module.time, "sleep"), \
             patch.object(hc_module.requests, "get", return_value=still_firing):
            assert checker.wait_until_healthy(timeout=30) is False

    def test_clears_after_transient_query_failures(self):
        """Prometheus query errors are logged and retried, not fatal."""
        checker = HealthChecker(target="target-1", fault_type="cpu_overload")
        with patch.object(hc_module.time, "sleep"), \
             patch.object(
                 hc_module.requests, "get",
                 side_effect=[
                     hc_module.requests.RequestException("connection reset"),
                     _mock_response([]),
                 ],
             ):
            assert checker.wait_until_healthy(timeout=30) is True

    def test_polls_at_5_second_interval(self):
        assert HealthChecker._POLL_INTERVAL_SECONDS == 5


class TestStageE3Probe:
    """is_healthy / e3_sanity_check — fast in-process retry-loop feedback only."""

    def test_is_healthy_true_on_http_200(self):
        checker = HealthChecker(target="10.0.2.5", fault_type="service_crash")
        mock_resp = MagicMock(status_code=200)
        with patch.object(hc_module.requests, "get", return_value=mock_resp):
            assert checker.is_healthy() is True

    def test_is_healthy_false_on_non_200(self):
        checker = HealthChecker(target="10.0.2.5", fault_type="service_crash")
        mock_resp = MagicMock(status_code=503)
        with patch.object(hc_module.requests, "get", return_value=mock_resp):
            assert checker.is_healthy() is False

    def test_is_healthy_false_on_request_exception(self):
        checker = HealthChecker(target="10.0.2.5", fault_type="service_crash")
        with patch.object(hc_module.requests, "get", side_effect=hc_module.requests.RequestException()):
            assert checker.is_healthy() is False

    def test_e3_sanity_check_fails_fast_without_ssm_call_when_http_down(self):
        checker = HealthChecker(target="10.0.2.5", fault_type="service_crash")
        with patch.object(checker, "is_healthy", return_value=False), \
             patch.object(hc_module.boto3, "client") as mock_boto:
            assert checker.e3_sanity_check(instance_id="i-0abc") is False
        mock_boto.assert_not_called()

    def test_e3_sanity_check_true_when_http_ok_and_no_instance_id(self):
        checker = HealthChecker(target="10.0.2.5", fault_type="service_crash")
        with patch.object(checker, "is_healthy", return_value=True):
            assert checker.e3_sanity_check() is True

    def test_e3_url_uses_healthz_path_by_default(self):
        checker = HealthChecker(target="10.0.2.5", fault_type="service_crash")
        assert checker.e3_url == "http://10.0.2.5:80/healthz"
