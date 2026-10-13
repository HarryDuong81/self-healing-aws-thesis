#!/usr/bin/env python3
"""
analysis/01_descriptive.py — Descriptive statistics for the 150-trial campaign.

Produces per-system, per-fault-class summary statistics used in Thesis §5.1 and §5.2:
  - N trials, success rate
  - T_MTTR: mean, median, p95, min, max (seconds)
  - Phase decomposition: mean T_detect, T_classify, T_remediate, T_validate

Output: results/01_descriptive.json  (all fields the thesis prose promises)

Usage:
    python analysis/01_descriptive.py \\
        --campaign s3://its-thesis-data/campaign/ \\
        --out results/01_descriptive.json

    # Local:
    python analysis/01_descriptive.py \\
        --campaign results/mttr_log.jsonl
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
from pathlib import Path
from typing import Optional

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("01_descriptive")

_SYSTEMS    = ["S1", "S2", "S3"]
_FAULT_TYPES = [
    "cpu_overload",
    "memory_exhaustion",
    "disk_saturation",
    "service_crash",
    "configuration_drift",
    "instance_failure",
]
_PHASE_FIELDS = [
    "T_detect", "T_classify", "T_remediate",
    # instance_failure only; the sentinel filter below excludes the other
    # five fault classes from this phase's population automatically.
    "T_observability_restore",
    "T_validate",
]


# ── Data-admission predicates ────────────────────────────────────────────────
# The record schema uses three distinct semantic states, and they must not be
# collapsed into one another:
#
#   -1.0  phase not applicable to this fault class (e.g. the observability-
#         restore phase on any class other than instance_failure)
#    0.0  phase applies and was measured; the measured duration was zero
#   >0.0  phase applies and was measured; positive duration
#
# A phase duration of exactly 0.0 is a real observation, not a missing value.
# Phases are rounded to millisecond precision on capture, so a sub-millisecond
# phase — T_classify in particular, being an in-memory lookup against an
# already-loaded routing table — legitimately records 0.0.  Admitting phases
# with `> 0` would silently discard those observations and shrink n.
#
# T_MTTR_seconds is different: a completed trial cannot take zero seconds, and
# -1.0 is the schema's non-applicability sentinel there too (S3's capacity-only
# rows).  `> 0` is therefore the correct predicate for the total alone.

def _admits_phase(record: dict, key: str) -> bool:
    """True when a phase field holds a real measurement, including 0.0."""
    return record.get(key, -1) >= 0


def _admits_total(record: dict, key: str = "T_MTTR_seconds") -> bool:
    """True when a total-MTTR field holds a real, non-zero measurement."""
    return record.get(key, -1) > 0


# Phases that do not apply to every fault class.  "Not applicable" and "zero
# valid observations" are different findings and must not both surface as
# n = 0: the first is a property of the experimental design, the second would
# indicate that every trial of that class failed to record the phase.  Where a
# phase does not apply, the summary is replaced by an explicit marker.
_PHASE_APPLICABILITY = {
    "T_observability_restore": {"instance_failure"},
}

_NOT_APPLICABLE = {
    "applicable": False,
    "reason": "phase does not apply to this fault class (schema sentinel -1.0)",
}


def _phase_applies(phase: str, fault_type: str) -> bool:
    allowed = _PHASE_APPLICABILITY.get(phase)
    return True if allowed is None else fault_type in allowed


def _load_records(campaign_path: str) -> list[dict]:
    if campaign_path.startswith("s3://"):
        return _load_from_s3(campaign_path)
    p = Path(campaign_path)
    if p.is_dir():
        p = p / "mttr_log.jsonl"
    records = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def _load_from_s3(s3_prefix: str) -> list[dict]:
    import boto3
    parts  = s3_prefix.lstrip("s3://").split("/", 1)
    bucket = parts[0]
    prefix = parts[1].rstrip("/") if len(parts) > 1 else ""
    key    = f"{prefix}/mttr_log.jsonl" if prefix else "mttr_log.jsonl"
    s3     = boto3.client("s3", region_name=os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1"))
    content = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
    records = []
    for line in content.splitlines():
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def _mean(values: list[float]) -> Optional[float]:
    return round(sum(values) / len(values), 3) if values else None


def _percentile(values: list[float], p: float) -> Optional[float]:
    """Nearest-rank percentile, rank k = ceil(p/100 * n) (see h3_upper_bound.py)."""
    if not values:
        return None
    s = sorted(values)
    k = math.ceil(round(p / 100 * len(s), 9))
    return round(s[min(max(k, 1), len(s)) - 1], 3)


def _summary(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "mean": None, "median": None, "p95": None, "min": None, "max": None}
    return {
        "n":      len(values),
        "mean":   _mean(values),
        "median": _percentile(values, 50),
        "p95":    _percentile(values, 95),
        "min":    round(min(values), 3),
        "max":    round(max(values), 3),
    }


def compute_descriptive(records: list[dict]) -> dict:
    results: dict = {}

    for system in _SYSTEMS:
        sys_records = [r for r in records if r.get("system_under_test") == system]
        n_total     = len(sys_records)
        n_success   = sum(1 for r in sys_records if r.get("recovery_status") == "success")
        success_rec = [r for r in sys_records if r.get("recovery_status") == "success"]

        mttr_values = [r["T_MTTR_seconds"] for r in success_rec if _admits_total(r)]

        by_fault: dict = {}
        for fault in _FAULT_TYPES:
            fault_recs = [r for r in success_rec if r.get("fault_type") == fault]
            fault_mttr = [r["T_MTTR_seconds"] for r in fault_recs if _admits_total(r)]
            phase_stats: dict = {}
            for phase in _PHASE_FIELDS:
                if not _phase_applies(phase, fault):
                    phase_stats[phase] = dict(_NOT_APPLICABLE)
                    continue
                key     = f"{phase}_seconds"
                vals    = [r[key] for r in fault_recs if _admits_phase(r, key)]
                summary = _summary(vals)
                if phase in _PHASE_APPLICABILITY:
                    # Restricted phases carry the marker on both branches, so
                    # "applies, no observations" is self-describing rather than
                    # a bare n = 0 the reader has to interpret.
                    summary["applicable"] = True
                    if not vals and fault_recs:
                        # Third state: the phase applies to this class and the
                        # class has successful trials, yet nothing was recorded.
                        # That is a collection failure, not a design property —
                        # surface it rather than leaving it buried in the JSON.
                        logger.warning(
                            "%s: %s applies to %s and %d successful trial(s) exist, "
                            "but no observation was recorded — investigate before "
                            "reporting these results",
                            system, phase, fault, len(fault_recs),
                        )
                phase_stats[phase] = summary
            by_fault[fault] = {
                "n_trials":    len(fault_recs),
                "T_MTTR":      _summary(fault_mttr),
                "phases":      phase_stats,
            }

        phase_overall: dict = {}
        for phase in _PHASE_FIELDS:
            key  = f"{phase}_seconds"
            vals = [r[key] for r in success_rec if _admits_phase(r, key)]
            summary = _summary(vals)
            restricted = _PHASE_APPLICABILITY.get(phase)
            if restricted is not None:
                # Not pooled over all six classes — say so in the artefact
                # itself so the figure is not read as a whole-campaign mean.
                summary["population"] = sorted(restricted)
            phase_overall[phase] = summary

        results[system] = {
            "n_total":       n_total,
            "n_success":     n_success,
            "success_rate":  round(n_success / n_total, 4) if n_total else None,
            "T_MTTR_overall": _summary(mttr_values),
            "phases_overall": phase_overall,
            "by_fault_type": by_fault,
        }

    return results


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Descriptive statistics for the 150-trial self-healing campaign (Thesis §5.1)."
    )
    parser.add_argument("--campaign", required=True,
                        help="mttr_log.jsonl path, directory, or s3://bucket/prefix")
    parser.add_argument("--out", default="results/01_descriptive.json",
                        help="Output file path (default: results/01_descriptive.json)")
    args = parser.parse_args(argv)

    records = _load_records(args.campaign)
    logger.info("Loaded %d records", len(records))

    result = compute_descriptive(records)

    output = json.dumps(result, indent=2)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(output)
    logger.info("Descriptive statistics written to %s", args.out)
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
