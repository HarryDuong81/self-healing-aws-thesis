"""
tests/unit/test_priority_arbitration.py — Unit tests for the priority-
resolution matrix and suppressor cache (app._sort_by_priority,
app._check_and_register_suppression).

Covers (Thesis Ch4 Table 4.9 "test_priority_arbitration.py", §3.5.1/§3.5.2):
  - Multi-alert payload sorts descending by severity weight w
    (instance_failure=100, service_crash=80, cpu_overload=60,
    memory_exhaustion=60, disk_saturation=40, configuration_drift=20).
  - The highest-weight alert is scheduled first.
  - Lower-weight siblings on the same (target, fault_type) key are placed in
    the suppressor cache with a 15 s TTL.

Thesis reference: §3.5.1 (Alertmanager routing), §3.5.2 (priority matrix P)
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_ORCHESTRATOR = Path(__file__).resolve().parent.parent.parent / "orchestrator"
sys.path.insert(0, str(_ORCHESTRATOR))

import app as app_module


def _alert(fault_type: str, target: str = "target-1") -> dict:
    return {"labels": {"fault_type": fault_type, "node_name": target}}


class TestSortByPriority:

    def test_instance_failure_sorts_first(self):
        alerts = [_alert("configuration_drift"), _alert("instance_failure"), _alert("cpu_overload")]
        sorted_alerts = app_module._sort_by_priority(alerts)
        assert sorted_alerts[0]["labels"]["fault_type"] == "instance_failure"

    def test_full_descending_order(self):
        alerts = [
            _alert("configuration_drift"),  # 20
            _alert("disk_saturation"),      # 40
            _alert("memory_exhaustion"),    # 60
            _alert("cpu_overload"),         # 60
            _alert("service_crash"),        # 80
            _alert("instance_failure"),     # 100
        ]
        sorted_types = [a["labels"]["fault_type"] for a in app_module._sort_by_priority(alerts)]
        assert sorted_types[0] == "instance_failure"
        assert sorted_types[1] == "service_crash"
        assert sorted_types[-1] == "configuration_drift"

    def test_unknown_fault_type_sorts_last(self):
        alerts = [_alert("cpu_overload"), {"labels": {"fault_type": "totally_unknown"}}]
        sorted_alerts = app_module._sort_by_priority(alerts)
        assert sorted_alerts[-1]["labels"]["fault_type"] == "totally_unknown"

    def test_stable_for_equal_weights(self):
        """cpu_overload and memory_exhaustion both weight 60 — order among
        equal-weight alerts should be preserved (Python sort is stable)."""
        alerts = [_alert("cpu_overload"), _alert("memory_exhaustion")]
        sorted_alerts = app_module._sort_by_priority(alerts)
        assert [a["labels"]["fault_type"] for a in sorted_alerts] == [
            "cpu_overload", "memory_exhaustion",
        ]


class TestSuppressorCache:

    @pytest.fixture(autouse=True)
    def _clear_suppressor(self):
        app_module._suppressor.clear()
        yield
        app_module._suppressor.clear()

    def test_first_alert_is_admitted(self):
        suppressed = app_module._check_and_register_suppression("target-1", "cpu_overload", now=100.0)
        assert suppressed is False

    def test_second_alert_same_key_within_window_is_suppressed(self):
        app_module._check_and_register_suppression("target-1", "cpu_overload", now=100.0)
        suppressed = app_module._check_and_register_suppression("target-1", "cpu_overload", now=105.0)
        assert suppressed is True

    def test_different_fault_type_same_target_not_suppressed(self):
        app_module._check_and_register_suppression("target-1", "cpu_overload", now=100.0)
        suppressed = app_module._check_and_register_suppression("target-1", "memory_exhaustion", now=100.0)
        assert suppressed is False

    def test_different_target_same_fault_type_not_suppressed(self):
        app_module._check_and_register_suppression("target-1", "cpu_overload", now=100.0)
        suppressed = app_module._check_and_register_suppression("target-2", "cpu_overload", now=100.0)
        assert suppressed is False

    def test_suppression_expires_after_15s_ttl(self):
        app_module._check_and_register_suppression("target-1", "cpu_overload", now=100.0)
        # 15.1s later — dedup window (15.0s) has elapsed
        suppressed = app_module._check_and_register_suppression("target-1", "cpu_overload", now=115.1)
        assert suppressed is False

    def test_admitted_alert_opens_a_new_15s_window(self):
        app_module._check_and_register_suppression("target-1", "cpu_overload", now=100.0)
        key = ("target-1", "cpu_overload")
        assert app_module._suppressor[key] == pytest.approx(100.0 + app_module._DEDUP_WINDOW_SECONDS)
