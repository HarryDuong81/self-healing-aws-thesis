"""
tests/unit/test_mttr_logger.py — Unit tests for orchestrator/mttr_logger.py

Covers:
  - MTTRLogger initialisation from a minimal alert payload
  - Phase recording methods set correct timestamps
  - total_seconds() returns sum of phases
  - Phase-sum invariant: |decomposed_sum - total_mttr| < _PHASE_SUM_TOLERANCE
  - save() persists JSONL record with all required fields
  - Invariant violation (synthetic bug) raises RuntimeError

Thesis reference: §3.7.2 Decomposed MTTR formula (Eq. 3.1)
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import patch
import os
import tempfile

import pytest

_ORCHESTRATOR = Path(__file__).resolve().parent.parent.parent / "orchestrator"
sys.path.insert(0, str(_ORCHESTRATOR))

# Point results dir to a temp directory for tests
import mttr_logger as ml


@pytest.fixture(autouse=True)
def _temp_results(tmp_path, monkeypatch):
    """Redirect JSONL output to a temporary directory."""
    monkeypatch.setattr(ml, "JSONL_PATH", tmp_path / "mttr_log.jsonl")
    monkeypatch.setattr(ml, "_DEFAULT_RESULTS_DIR", tmp_path)
    yield tmp_path


def _minimal_alert(fault_type: str = "cpu_overload", injection_ts: float = None) -> dict:
    annotations = {}
    if injection_ts is not None:
        # mttr_logger.py::_resolve_trial_correlation() only takes the
        # explicit-annotation path when BOTH trial_id and
        # injection_timestamp are present together (by design — an
        # injection_timestamp alone has nothing to key the record on).
        # injection_ts=None leaves annotations empty, correctly exercising
        # the correlation-file-lookup fallback path instead.
        annotations["trial_id"] = "t-test-fixture"
        annotations["injection_timestamp"] = str(injection_ts)
    return {
        "labels":      {"fault_type": fault_type, "instance": "10.0.2.1:9100", "node_name": "target-1"},
        "annotations": annotations,
    }


class TestMTTRLoggerInit:

    def test_trial_id_starts_with_t(self):
        logger = ml.MTTRLogger(_minimal_alert())
        assert logger.trial_id.startswith("t-")

    def test_fault_type_from_alert_labels(self):
        logger = ml.MTTRLogger(_minimal_alert("service_crash"))
        assert logger.fault_type == "service_crash"

    def test_target_stored(self):
        logger = ml.MTTRLogger(_minimal_alert(), target="target-2")
        assert logger.target == "target-2"

    def test_injection_timestamp_parsed(self):
        ts = time.time()
        logger = ml.MTTRLogger(_minimal_alert(injection_ts=ts))
        assert logger._t_fault_injected_ms == int(ts * 1000)

    def test_missing_injection_timestamp_is_none(self):
        logger = ml.MTTRLogger(_minimal_alert())
        assert logger._t_fault_injected_ms is None


class TestPhaseRecording:

    def test_record_detection_sets_mono(self):
        logger = ml.MTTRLogger(_minimal_alert())
        logger.record_detection()
        assert logger._mono_detect is not None
        assert logger._t_detected_ms is not None

    def test_record_detection_sets_fallback_injection_ts(self):
        logger = ml.MTTRLogger(_minimal_alert())   # no injection_ts
        logger.record_detection()
        # When no injection_ts is provided, it falls back to detection time
        assert logger._t_fault_injected_ms == logger._t_detected_ms

    def test_record_classification_sets_mono(self):
        logger = ml.MTTRLogger(_minimal_alert())
        logger.record_detection()
        logger.record_classification("ansible", "kill_runaway_process.yml")
        assert logger._mono_classify is not None
        assert logger.actuator == "ansible"
        assert logger.playbook == "kill_runaway_process.yml"

    def test_record_remediation_sets_mono(self):
        logger = ml.MTTRLogger(_minimal_alert())
        logger.record_detection()
        logger.record_classification("ansible", "kill_runaway_process.yml")
        logger.record_remediation(retry_count=1)
        assert logger._mono_remediate is not None
        assert logger._retry_count == 1

    def test_record_validation_sets_mono(self):
        logger = ml.MTTRLogger(_minimal_alert())
        logger.record_detection()
        logger.record_classification("ansible", "kill_runaway_process.yml")
        logger.record_remediation()
        logger.record_validation()
        assert logger._mono_validate is not None
        assert logger._t_validated_ms is not None


class TestTotalSeconds:

    def test_total_seconds_negative_before_recording(self):
        logger = ml.MTTRLogger(_minimal_alert())
        assert logger.total_seconds() == -1.0

    def test_total_seconds_positive_after_full_recording(self):
        logger = ml.MTTRLogger(_minimal_alert())
        logger.record_detection()
        time.sleep(0.01)
        logger.record_classification("ansible", "kill_runaway_process.yml")
        time.sleep(0.01)
        logger.record_remediation()
        time.sleep(0.01)
        logger.record_validation()
        assert logger.total_seconds() > 0

    def test_total_seconds_monotonically_increases(self):
        logger = ml.MTTRLogger(_minimal_alert())
        logger.record_detection()
        time.sleep(0.05)
        logger.record_classification("ansible", "kill_runaway_process.yml")
        time.sleep(0.05)
        logger.record_remediation()
        time.sleep(0.05)
        logger.record_validation()
        assert logger.total_seconds() >= 0.15


class TestPhaseInvariant:

    def _record_all_phases(self, logger, sleep_s: float = 0.01) -> ml.MTTRLogger:
        logger.record_detection()
        time.sleep(sleep_s)
        logger.record_classification("ansible", "kill_runaway_process.yml")
        time.sleep(sleep_s)
        logger.record_remediation()
        time.sleep(sleep_s)
        logger.record_validation()
        return logger

    def test_phase_sum_invariant_holds(self):
        """T_detect + T_classify + T_remediate + T_validate == total_seconds() ± 1ms"""
        # Provide injection_ts so T_detect is 0 (inject == detect)
        ts = time.time()
        logger = ml.MTTRLogger(_minimal_alert(injection_ts=ts))
        time.sleep(0.001)   # slight delay before record_detection so t_detected > t_injected
        self._record_all_phases(logger)

        t_mttr   = logger.total_seconds()
        t_detect_wc = (logger._t_detected_ms - logger._t_fault_injected_ms) / 1000.0
        t_classify  = logger._phase_seconds(logger._mono_detect,    logger._mono_classify)
        t_remediate = logger._phase_seconds(logger._mono_classify,  logger._mono_remediate)
        t_validate  = logger._phase_seconds(logger._mono_remediate, logger._mono_validate)

        decomposed_sum = t_detect_wc + t_classify + t_remediate + t_validate
        assert abs(decomposed_sum - t_mttr) < ml._PHASE_SUM_TOLERANCE, (
            f"Phase-sum invariant violated: sum={decomposed_sum:.6f} total={t_mttr:.6f}"
        )

    def test_save_success_writes_jsonl(self, tmp_path):
        ts = time.time()
        logger = ml.MTTRLogger(_minimal_alert(injection_ts=ts), target="target-1")
        time.sleep(0.001)
        self._record_all_phases(logger)

        logger.save(recovery_status="success")

        jsonl = (tmp_path / "mttr_log.jsonl")
        assert jsonl.exists()
        record = json.loads(jsonl.read_text().strip())
        assert record["fault_type"]   == "cpu_overload"
        assert record["target"]        == "target-1"
        assert record["recovery_status"] == "success"
        assert record["T_MTTR_seconds"]  > 0

    def test_save_includes_all_schema_fields(self, tmp_path):
        """Ch4 Table 4.8 — every documented field must be present on every record."""
        ts = time.time()
        logger = ml.MTTRLogger(_minimal_alert(injection_ts=ts))
        time.sleep(0.001)
        self._record_all_phases(logger)
        logger.save(recovery_status="success")

        record = json.loads((tmp_path / "mttr_log.jsonl").read_text().strip())
        for field in ml.RECORD_FIELDNAMES:
            assert field in record, f"Missing required field: {field}"

    def test_phase_sum_invariant_violation_raises(self):
        """
        A genuine T_MTTR vs decomposed_sum mismatch must raise RuntimeError.

        Exercises _assert_phase_sum_invariant() directly rather than trying
        to "corrupt" a logger's monotonic checkpoint: the four phases are a
        telescoping sum of consecutive checkpoint deltas, so their exact
        (pre-rounding) total is mathematically guaranteed to equal
        total_seconds()'s span no matter which intermediate checkpoint is
        perturbed — confirmed live: shifting _mono_validate shifts T_MTTR
        and T_validate identically, so the invariant never actually trips,
        regardless of tolerance. Only rounding accumulation (see
        _PHASE_SUM_TOLERANCE's comment) or a genuine phase-computation bug
        can produce a real mismatch, so this test supplies one directly.
        """
        with pytest.raises(RuntimeError, match="VIOLATED"):
            ml._assert_phase_sum_invariant(
                T_MTTR=100.0, decomposed_sum=50.0, trial_id="t-test-violation",
            )


# ── Record schema validation (structural, beyond the phase-sum invariant) ─────

class TestRecordSchema:
    """
    The phase-sum invariant catches arithmetic that does not reconcile; these
    cover records that reconcile but are semantically wrong and would otherwise
    be indistinguishable from real measurements at analysis time.
    """

    def _valid(self) -> dict:
        return {
            "trial_id": "t-1", "fault_type": "cpu_overload",
        "block": 1,
            "recovery_status": "success", "escalation_reason": "",
            "retry_count": 0, "T_MTTR_seconds": 42.0,
            "t_observability_restored_ms": -1,
            "T_observability_restore_seconds": -1.0,
        }

    def test_valid_record_passes(self):
        ml._assert_record_schema(self._valid())

    def test_success_with_escalation_reason_rejected(self):
        rec = self._valid(); rec["escalation_reason"] = "retry_exhausted"
        with pytest.raises(RuntimeError, match="escalation_reason is set"):
            ml._assert_record_schema(rec)

    def test_failure_without_escalation_reason_rejected(self):
        rec = self._valid(); rec["recovery_status"] = "timeout"
        with pytest.raises(RuntimeError, match="carries no escalation_reason"):
            ml._assert_record_schema(rec)

    def test_negative_retry_count_rejected(self):
        rec = self._valid(); rec["retry_count"] = -1
        with pytest.raises(RuntimeError, match="negative"):
            ml._assert_record_schema(rec)

    def test_observability_value_on_ansible_class_rejected(self):
        # The restore phase belongs to instance_failure alone; a real value on
        # any other class means a checkpoint leaked across fault classes.
        rec = self._valid(); rec["T_observability_restore_seconds"] = 12.5
        with pytest.raises(RuntimeError, match="observability-restore value"):
            ml._assert_record_schema(rec)

    def test_instance_failure_may_carry_a_real_restore_value(self):
        rec = self._valid()
        rec.update(fault_type="instance_failure",
                   t_observability_restored_ms=1_700_000_000_000,
                   T_observability_restore_seconds=210.4)
        ml._assert_record_schema(rec)
