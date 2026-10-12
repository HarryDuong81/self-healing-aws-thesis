"""
tests/unit/test_false_positive_rate.py — Unit tests for
analysis/05_false_positive_rate.py's trial-window construction and
alert-classification logic (RQ4, Thesis §3.7.3).

Covers:
  - A trial window's end comes from the matching MTTR record's latest-
    reached phase timestamp when one exists.
  - A trial with no matching MTTR record falls back to a fault-class-aware
    conservative window length.
  - An alert matching the injecting trial's (fault_type, target) inside its
    window is a true positive.
  - An alert inside a window but NOT matching (fault_type, target) is a
    false positive — even if it fired for a genuine, unrelated condition
    (§3.7.3's deliberately-conservative upper-bound framing).
  - An alert outside every trial window is excluded from both the
    numerator and the denominator.
  - A suppressed alert (dedup window, §3.5.2) is still counted in the
    denominator if it falls inside an active trial window.

Thesis reference: §3.7.3 (false-positive-rate secondary metric)
"""
from __future__ import annotations

import sys
from pathlib import Path

_ANALYSIS = Path(__file__).resolve().parent.parent.parent / "analysis"
sys.path.insert(0, str(_ANALYSIS))

import importlib
fpr = importlib.import_module("05_false_positive_rate")


def _correlation(trial_id, t0_ms, fault_type, target):
    return {"trial_id": trial_id, "t0_epoch_ms": t0_ms, "fault_type": fault_type, "target": target}


def _mttr(trial_id, **phase_ms):
    record = {"trial_id": trial_id}
    record.update(phase_ms)
    return record


def _alert(received_ms, fault_type, target, suppressed=False):
    return {
        "received_ms": received_ms, "fault_type": fault_type,
        "target": target, "suppressed": suppressed,
    }


class TestBuildTrialWindows:

    def test_window_end_uses_latest_mttr_phase_timestamp(self):
        correlation = [_correlation("t-1", 1_000_000, "cpu_overload", "target-1")]
        mttr = [_mttr("t-1", t_fault_injected_ms=1_000_000, t_validated_ms=1_050_000)]

        windows = fpr.build_trial_windows(correlation, mttr)

        assert len(windows) == 1
        assert windows[0]["t0_ms"] == 1_000_000
        assert windows[0]["end_ms"] == 1_050_000

    def test_no_matching_mttr_record_falls_back_to_conservative_window(self):
        correlation = [_correlation("t-2", 1_000_000, "instance_failure", "target-2")]

        windows = fpr.build_trial_windows(correlation, [])

        assert len(windows) == 1
        # instance_failure fallback = 900s (§7.4 convention)
        assert windows[0]["end_ms"] == 1_000_000 + 900 * 1000

    def test_non_instance_failure_fallback_uses_default_window(self):
        correlation = [_correlation("t-3", 1_000_000, "cpu_overload", "target-1")]

        windows = fpr.build_trial_windows(correlation, [])

        assert windows[0]["end_ms"] == 1_000_000 + fpr._DEFAULT_FALLBACK_WINDOW_SECONDS * 1000

    def test_missing_t0_is_skipped(self):
        correlation = [{"trial_id": "t-4", "fault_type": "cpu_overload", "target": "target-1"}]

        windows = fpr.build_trial_windows(correlation, [])

        assert windows == []


class TestClassifyAlerts:

    def test_matching_alert_inside_window_is_true_positive(self):
        windows = [{"trial_id": "t-1", "t0_ms": 1_000_000, "end_ms": 1_050_000,
                    "fault_type": "cpu_overload", "target": "target-1"}]
        alerts = [_alert(1_010_000, "cpu_overload", "target-1")]

        result = fpr.classify_alerts(alerts, windows)

        assert result["n_true_positive"] == 1
        assert result["n_false_positive"] == 0
        assert result["n_alerts_in_denominator"] == 1

    def test_mismatched_fault_type_inside_window_is_false_positive(self):
        windows = [{"trial_id": "t-1", "t0_ms": 1_000_000, "end_ms": 1_050_000,
                    "fault_type": "cpu_overload", "target": "target-1"}]
        alerts = [_alert(1_010_000, "memory_exhaustion", "target-1")]

        result = fpr.classify_alerts(alerts, windows)

        assert result["n_false_positive"] == 1
        assert result["n_true_positive"] == 0
        assert result["n_alerts_in_denominator"] == 1

    def test_mismatched_target_inside_window_is_false_positive(self):
        windows = [{"trial_id": "t-1", "t0_ms": 1_000_000, "end_ms": 1_050_000,
                    "fault_type": "cpu_overload", "target": "target-1"}]
        alerts = [_alert(1_010_000, "cpu_overload", "target-2")]

        result = fpr.classify_alerts(alerts, windows)

        assert result["n_false_positive"] == 1

    def test_alert_outside_every_window_is_excluded(self):
        windows = [{"trial_id": "t-1", "t0_ms": 1_000_000, "end_ms": 1_050_000,
                    "fault_type": "cpu_overload", "target": "target-1"}]
        alerts = [_alert(2_000_000, "cpu_overload", "target-1")]

        result = fpr.classify_alerts(alerts, windows)

        assert result["n_alerts_excluded"] == 1
        assert result["n_alerts_in_denominator"] == 0
        assert result["n_true_positive"] == 0
        assert result["n_false_positive"] == 0

    def test_suppressed_alert_inside_window_still_counts_in_denominator(self):
        windows = [{"trial_id": "t-1", "t0_ms": 1_000_000, "end_ms": 1_050_000,
                    "fault_type": "cpu_overload", "target": "target-1"}]
        alerts = [_alert(1_010_000, "memory_exhaustion", "target-1", suppressed=True)]

        result = fpr.classify_alerts(alerts, windows)

        assert result["n_alerts_in_denominator"] == 1
        assert result["n_false_positive"] == 1

    def test_false_positive_rate_computed_correctly(self):
        windows = [{"trial_id": "t-1", "t0_ms": 1_000_000, "end_ms": 1_050_000,
                    "fault_type": "cpu_overload", "target": "target-1"}]
        alerts = [
            _alert(1_010_000, "cpu_overload", "target-1"),      # TP
            _alert(1_020_000, "memory_exhaustion", "target-1"), # FP
            _alert(1_030_000, "cpu_overload", "target-1"),      # TP
            _alert(1_040_000, "disk_saturation", "target-1"),   # FP
        ]

        result = fpr.classify_alerts(alerts, windows)

        assert result["n_alerts_in_denominator"] == 4
        assert result["n_false_positive"] == 2
        assert result["false_positive_rate"] == 0.5

    def test_empty_denominator_reports_zero_rate_not_a_crash(self):
        result = fpr.classify_alerts([], [])
        assert result["false_positive_rate"] == 0.0
        assert result["n_alerts_in_denominator"] == 0
