"""
tests/unit/test_final_disk_pct.py — Unit tests for the final_disk_pct
actuator-report field.

Why this field exists: disk_cleanup.yml treats the filesystem as safe below
80% utilisation, the DiskSaturation alert clears only below roughly 90%, and
the playbook reports execution failure only above 95%. A run finishing between
90% and 95% therefore exits rc = 0 while the alert is still firing, and the
trial terminates as validation_timeout rather than retry_exhausted. The final
utilisation is the evidence distinguishing those outcomes, and Prometheus
retains it for only 30 days — so it is persisted in the authoritative trial
record instead of being reconstructed from the time series later.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import execute
import mttr_logger


# ── Marker parsing (Execute layer) ───────────────────────────────────────────

def test_marker_is_parsed_from_playbook_stdout():
    out = 'ok: [target-2] => {\n  "msg": "SELF_HEALING_FINAL_DISK_PCT=92"\n}'
    assert execute._parse_actuator_report(out) == {"final_disk_pct": 92}


def test_absent_marker_is_not_an_error():
    """The other four Ansible playbooks emit no marker; that is normal."""
    assert execute._parse_actuator_report("PLAY RECAP\nok=7 changed=3 failed=0") == {}


def test_out_of_range_value_is_rejected():
    out = "SELF_HEALING_FINAL_DISK_PCT=999"
    assert execute._parse_actuator_report(out) == {}


def test_zero_percent_is_a_real_value_not_a_falsy_drop():
    """0 is a legitimate reading and must survive the truthiness boundary."""
    assert execute._parse_actuator_report("SELF_HEALING_FINAL_DISK_PCT=0") == {
        "final_disk_pct": 0
    }


# ── Record construction (Knowledge layer) ────────────────────────────────────

def _logger_for(fault_type: str, tmp_path: Path) -> mttr_logger.MTTRLogger:
    os.environ["RESULTS_DIR"] = str(tmp_path)
    mttr_logger.JSONL_PATH = tmp_path / "mttr_log.jsonl"
    alert = {"labels": {"fault_type": fault_type, "node_name": "target-2"}}
    return mttr_logger.MTTRLogger(alert, target="target-2")


def _saved_record(log: mttr_logger.MTTRLogger, tmp_path: Path) -> dict:
    log.save(recovery_status="timeout", escalation_reason="validation_timeout")
    line = next(
        p for p in tmp_path.glob("mttr_log*.jsonl")
    ).read_text().strip().splitlines()[-1]
    return json.loads(line)


def test_disk_saturation_record_carries_the_field(tmp_path):
    log = _logger_for("disk_saturation", tmp_path)
    log.record_actuator_report({"final_disk_pct": 92})
    assert _saved_record(log, tmp_path)["final_disk_pct"] == 92


def test_field_is_absent_when_the_playbook_reported_nothing(tmp_path):
    """Absent, not zero — a missing reading must not read as 0% utilisation."""
    log = _logger_for("disk_saturation", tmp_path)
    assert "final_disk_pct" not in _saved_record(log, tmp_path)


def test_other_fault_classes_never_carry_the_field(tmp_path):
    """Scoped to disk_saturation even if an actuator reported a value."""
    log = _logger_for("cpu_overload", tmp_path)
    log.record_actuator_report({"final_disk_pct": 92})
    assert "final_disk_pct" not in _saved_record(log, tmp_path)


def test_schema_rejects_the_field_on_the_wrong_fault_class():
    with pytest.raises(RuntimeError, match="scoped to disk_saturation"):
        mttr_logger._assert_record_schema({
            "fault_type": "cpu_overload",
            "block": 1,
            "recovery_status": "success",
            "T_MTTR_seconds": 20.0,
            "retry_count": 0,
            "t_observability_restored_ms": -1,
            "T_observability_restore_seconds": -1.0,
            "final_disk_pct": 92,
        })


def test_schema_rejects_a_non_percentage_value():
    with pytest.raises(RuntimeError, match="not an integer percentage"):
        mttr_logger._assert_record_schema({
            "fault_type": "disk_saturation",
            "block": 1,
            "recovery_status": "success",
            "T_MTTR_seconds": 20.0,
            "retry_count": 0,
            "t_observability_restored_ms": -1,
            "T_observability_restore_seconds": -1.0,
            "final_disk_pct": -1,
        })
