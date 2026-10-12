#!/usr/bin/env python3
"""
alert_log.py — Persists a record of every alert the orchestrator receives
(RQ4, Thesis §3.7.3 "Secondary Metrics" — false-positive rate).

False-positive rate is defined over the ORCHESTRATOR's own receipt log, not
Prometheus's alert history, because Alertmanager's inhibition rules
(§3.5.1) suppress certain alerts that fire but are never delivered here —
the metric is defined over the alerts the classifier actually had to act
upon. Every alert in a webhook's firing list is logged here exactly once,
including ones the priority-arbitration dedup window (§3.5.2) suppresses —
those were still received, and are counted in the false-positive-rate
denominator per the thesis's operationalisation.

This module does not decide what is or isn't a false positive — that
cross-reference against the fault-injection log (results/correlation.jsonl)
happens post-campaign in analysis/05_false_positive_rate.py, exactly like
every other analysis/*.py script operates on already-collected JSONL files
rather than computing anything at runtime.

Written to a local JSONL file only, matching every other results/*.jsonl
convention in this codebase (mttr_logger.py, correlate.py) — no S3 mirror
here; the campaign harness/analysis scripts mirror results/ as a whole
(AD-010).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

_DEFAULT_RESULTS_DIR = Path(os.environ.get("RESULTS_DIR", "")) or (
    Path(__file__).resolve().parent.parent / "results"
)

JSONL_PATH = _DEFAULT_RESULTS_DIR / "alert_log.jsonl"

RECORD_FIELDNAMES = [
    "received_ms", "received_iso", "target", "fault_type", "instance",
    "suppressed",
]


def record_alert_receipt(
    target: str,
    fault_type: str,
    instance: str,
    suppressed: bool,
    received_ms: int | None = None,
    ec2_instance_id: str | None = None,
) -> None:
    """
    Append one receipt record for an alert the /alert webhook just saw.

    Called once per alert in the firing list — for both the alert that
    gets dispatched to _run_mape_k() and any siblings suppressed by the
    dedup window — since both count as "received" for RQ4 (§3.7.3).
    """
    if received_ms is None:
        received_ms = int(time.time() * 1000)

    record = {
        "received_ms":  received_ms,
        "received_iso": datetime.fromtimestamp(received_ms / 1000, tz=timezone.utc).isoformat(),
        "target":       target,
        "fault_type":   fault_type,
        "instance":     instance,
        "suppressed":   suppressed,
        # Durable evidence that the firing rule preserved the label the Plan
        # layer's SSM readiness gate depends on.  Null here means gate 3 could
        # not be evaluated for that alert and failed closed.
        "ec2_instance_id": ec2_instance_id,
    }

    JSONL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with JSONL_PATH.open("a") as fh:
        fh.write(json.dumps(record) + "\n")
