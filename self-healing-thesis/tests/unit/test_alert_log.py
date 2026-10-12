"""
tests/unit/test_alert_log.py — Unit tests for orchestrator/alert_log.py
(RQ4 alert-receipt logging, Thesis §3.7.3).

Thesis reference: §3.7.3 (false-positive-rate secondary metric)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ORCHESTRATOR = Path(__file__).resolve().parent.parent.parent / "orchestrator"
sys.path.insert(0, str(_ORCHESTRATOR))

import alert_log


def test_record_alert_receipt_writes_expected_fields(tmp_path, monkeypatch):
    log_path = tmp_path / "alert_log.jsonl"
    monkeypatch.setattr(alert_log, "JSONL_PATH", log_path)

    alert_log.record_alert_receipt(
        target="target-2", fault_type="cpu_overload", instance="10.0.2.2:9100",
        suppressed=False, received_ms=1_700_000_000_000,
    )

    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1

    record = json.loads(lines[0])
    assert record["target"] == "target-2"
    assert record["fault_type"] == "cpu_overload"
    assert record["instance"] == "10.0.2.2:9100"
    assert record["suppressed"] is False
    assert record["received_ms"] == 1_700_000_000_000
    assert record["received_iso"].startswith("2023-11-14")


def test_record_alert_receipt_appends_multiple_records(tmp_path, monkeypatch):
    log_path = tmp_path / "alert_log.jsonl"
    monkeypatch.setattr(alert_log, "JSONL_PATH", log_path)

    alert_log.record_alert_receipt(
        target="target-1", fault_type="service_crash", instance="10.0.2.1:9100",
        suppressed=False, received_ms=1_700_000_000_000,
    )
    alert_log.record_alert_receipt(
        target="target-1", fault_type="cpu_overload", instance="10.0.2.1:9100",
        suppressed=True, received_ms=1_700_000_005_000,
    )

    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 2
    first, second = (json.loads(ln) for ln in lines)
    assert first["suppressed"] is False
    assert second["suppressed"] is True


def test_record_alert_receipt_defaults_received_ms_to_now(tmp_path, monkeypatch):
    log_path = tmp_path / "alert_log.jsonl"
    monkeypatch.setattr(alert_log, "JSONL_PATH", log_path)

    alert_log.record_alert_receipt(
        target="target-3", fault_type="disk_saturation", instance="10.0.2.3:9100",
        suppressed=False,
    )

    record = json.loads(log_path.read_text().strip())
    assert isinstance(record["received_ms"], int)
    assert record["received_ms"] > 0
