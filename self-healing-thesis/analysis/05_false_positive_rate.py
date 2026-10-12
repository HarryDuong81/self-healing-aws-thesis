#!/usr/bin/env python3
"""
analysis/05_false_positive_rate.py — RQ4 false-positive-rate analysis
(Thesis §3.7.3 "Secondary Metrics").

Methodology (verbatim from §3.7.3, condensed):
  Population: the orchestrator's OWN alert-receipt log
              (results/alert_log.jsonl, orchestrator/alert_log.py), not
              Prometheus's alert history — Alertmanager's inhibition rules
              (§3.5.1) suppress alerts that fire but are never delivered to
              the orchestrator, and the metric is defined over the alerts
              the classifier actually had to act upon.
  Numerator:  alerts received during an active trial window that do NOT
              match BOTH the injected fault class and the injected target.
  Denominator: all alerts received during an active trial window,
              INCLUDING ones suppressed by the priority-arbitration dedup
              window (§3.5.2) — those were still received.
  Exclusion:  alerts received outside any active trial window are excluded
              entirely from both numerator and denominator.
  Deliberately conservative: an alert that fires correctly in response to
  a genuine incidental condition unrelated to the injected fault is still
  counted as a false positive, because the classifier cannot distinguish
  it from a spurious firing — the reported rate is an upper bound on the
  classifier's true false-positive rate.

Data sources:
  results/alert_log.jsonl    — every alert the orchestrator received
                                (orchestrator/alert_log.py)
  results/correlation.jsonl  — the fault-injection log: (trial_id, t0,
                                fault_type, target) per trial
                                (experiments/correlation/correlate.py)
  results/mttr_log*.jsonl    — per-trial outcome records, used only to
                                find each trial's empirical END timestamp
                                (the latest non-null t_*_ms field) so a
                                trial's active window is
                                [t0, last_known_phase_timestamp] rather
                                than an arbitrary constant. A trial with no
                                matching MTTR record (fault never detected
                                at all) falls back to
                                t0 + _FALLBACK_WINDOW_SECONDS[fault_type].

Output: results/05_false_positive_rate.json

Usage:
    python analysis/05_false_positive_rate.py \\
        --results-dir results/ \\
        --out results/05_false_positive_rate.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("05_false_positive_rate")

# Mirrors run_one.py's own instance_failure-vs-other timeout asymmetry
# — reused here only as the fallback trial-window length
# for a trial whose fault was never detected at all (no MTTR record to
# read an empirical end timestamp from), so this case doesn't need its
# own separately-tuned constant.
_SLO_SECONDS = 300
_FALLBACK_WINDOW_SECONDS: dict[str, int] = {
    "instance_failure": 900,
}
_DEFAULT_FALLBACK_WINDOW_SECONDS = _SLO_SECONDS + 120

# Deployment-session fix (2026-09-22): confirmed live, the first genuine run
# of this script against real data (correlation.jsonl + alert_log.jsonl both
# transferred from the host for the first time) reported a false-positive
# rate of 1.0 (111/111) -- every single alert, with zero exceptions. That
# uniformity is itself the signature of a systematic comparison bug, not
# real classifier behaviour, and it was: correlation.jsonl's own `target`
# field is the harness's short CLI-argument form ("target-1", written
# verbatim from run_one.py's/inject_once.py's --target), while
# alert_log.jsonl's `target` field is Alertmanager's own label, sourced from
# the EC2 instance's full Name tag ("self-healing-thesis-experiment-
# target-1"). classify_alerts() compared these two conventions directly and
# they could never match, regardless of whether the alert was genuinely
# correct. Normalising at this boundary (not touching either source module,
# both of which are internally consistent and correct for their own layer)
# reclassified 64 of the original 111 "false positives" as true positives;
# the remaining 47 are not explained by this bug and need their own
# investigation. Same normalisation pattern already used in
# h1_official_wilcoxon_test.py's TARGET_PREFIX handling for the identical
# naming mismatch on the S2-v2 side.
_TARGET_PREFIX = "self-healing-thesis-experiment-"


def _normalise_target(short_target: str) -> str:
    """correlation.jsonl's short form -> alert_log.jsonl's full EC2-Name-tag
    form, so both sides of the comparison in classify_alerts() speak the
    same convention. A target that already carries the prefix (defensive;
    not currently produced by any writer) passes through unchanged."""
    if not short_target or short_target.startswith(_TARGET_PREFIX):
        return short_target
    return _TARGET_PREFIX + short_target


# mttr_log.jsonl phase timestamp fields, latest-phase-first, used to find
# each trial's empirical window end (the latest phase this trial reached).
_PHASE_TIMESTAMP_FIELDS_LATEST_FIRST = [
    "t_validated_ms",
    "t_observability_restored_ms",
    "t_remediated_ms",
    "t_classified_ms",
    "t_detected_ms",
    "t_fault_injected_ms",
]


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning("Skipping malformed JSON line in %s", path)
    return records


def _trial_end_ms(mttr_record: dict) -> Optional[int]:
    """Latest non-null phase timestamp this trial actually reached.

    Deployment-session correction (2026-09-22): `if value:` treated the
    schema's -1 "not applicable" sentinel (used throughout this codebase --
    e.g. S3's T_MTTR_seconds=-1 for capacity-only rows) as truthy, so a
    trial that never reached a later phase (e.g. circuit_breaker_open,
    which aborts after classification but before remediation, leaving
    t_remediated_ms/t_observability_restored_ms/t_validated_ms all -1)
    incorrectly returned that -1 immediately as "the latest reached phase",
    instead of falling through to check the earlier fields that actually
    hold real values (t_classified_ms, in that case). Confirmed live: this
    was masked in build_trial_windows() by its own `end_ms < t0` fallback
    check (-1 is always < a real epoch-ms t0), so no window was ever
    literally malformed -- but the fallback window it substituted (420s)
    is wider than the trial's genuine, real end (t_classified_ms, ~50-90s
    after t0 for the 5 known circuit_breaker_open trials, CLAUDE.md
    §15.8's H1 addendum), which let at least one genuinely out-of-window
    alert be counted as a false positive that a correct window would have
    excluded entirely. Fixed to skip the -1 sentinel and fall through to
    the next, earlier field."""
    for field in _PHASE_TIMESTAMP_FIELDS_LATEST_FIRST:
        value = mttr_record.get(field)
        if value and value != -1:
            return int(value)
    return None


def build_trial_windows(
    correlation_records: list[dict],
    mttr_records: list[dict],
) -> list[dict]:
    """
    One window per injected fault: {t0_ms, end_ms, fault_type, target,
    trial_id}. end_ms comes from the matching MTTR record's last-reached
    phase timestamp when one exists; otherwise a conservative fallback
    window length (fault-class-aware, §7.4 convention).
    """
    mttr_by_trial_id = {r["trial_id"]: r for r in mttr_records if r.get("trial_id")}

    windows = []
    for corr in correlation_records:
        t0 = corr.get("t0_epoch_ms")
        fault_type = corr.get("fault_type", "")
        target = _normalise_target(corr.get("target", ""))
        trial_id = corr.get("trial_id", "")
        if t0 is None:
            continue

        mttr_record = mttr_by_trial_id.get(trial_id)
        end_ms = _trial_end_ms(mttr_record) if mttr_record else None
        if end_ms is None or end_ms < t0:
            fallback_s = _FALLBACK_WINDOW_SECONDS.get(
                fault_type, _DEFAULT_FALLBACK_WINDOW_SECONDS
            )
            end_ms = t0 + fallback_s * 1000

        windows.append({
            "trial_id":   trial_id,
            "t0_ms":      t0,
            "end_ms":     end_ms,
            "fault_type": fault_type,
            "target":     target,
        })
    return windows


def classify_alerts(
    alert_records: list[dict],
    trial_windows: list[dict],
) -> dict:
    """
    Classify every alert receipt as: outside any trial window (excluded),
    a true positive (matches the injecting trial's fault_type + target),
    or a false positive (falls inside a window but doesn't match).

    An alert landing inside more than one trial window (only possible if
    windows overlap, which sequential trial execution + inter-trial
    cooldown should prevent in a well-formed campaign) is matched against
    every window it falls inside; it counts as a true positive if it
    matches ANY of them, and is flagged in "ambiguous_alerts" for manual
    review either way.
    """
    included = excluded = true_positive = false_positive = 0
    false_positive_details: list[dict] = []
    ambiguous_alerts: list[dict] = []

    for alert in alert_records:
        received_ms = alert.get("received_ms")
        if received_ms is None:
            excluded += 1
            continue

        matching_windows = [
            w for w in trial_windows
            if w["t0_ms"] <= received_ms <= w["end_ms"]
        ]
        if not matching_windows:
            excluded += 1
            continue

        included += 1
        if len(matching_windows) > 1:
            ambiguous_alerts.append({
                "alert": alert,
                "matching_trial_ids": [w["trial_id"] for w in matching_windows],
            })

        is_true_positive = any(
            w["fault_type"] == alert.get("fault_type")
            and w["target"] == alert.get("target")
            for w in matching_windows
        )
        if is_true_positive:
            true_positive += 1
        else:
            false_positive += 1
            false_positive_details.append({
                "received_iso":     alert.get("received_iso"),
                "target":           alert.get("target"),
                "fault_type":       alert.get("fault_type"),
                "suppressed":       alert.get("suppressed"),
                "matching_trial_ids": [w["trial_id"] for w in matching_windows],
            })

    denominator = included  # alerts received during an active trial window
    rate = (false_positive / denominator) if denominator else 0.0

    return {
        "false_positive_rate":     round(rate, 4),
        "n_alerts_total":          len(alert_records),
        "n_alerts_excluded":       excluded,
        "n_alerts_in_denominator": denominator,
        "n_true_positive":         true_positive,
        "n_false_positive":        false_positive,
        "n_ambiguous_window_matches": len(ambiguous_alerts),
        "false_positive_details":  false_positive_details,
        "ambiguous_alerts":        ambiguous_alerts,
        "note": (
            "Deliberately conservative upper bound (Thesis §3.7.3): an alert "
            "correctly firing for a genuine, incidental condition unrelated "
            "to the injected fault is still counted as a false positive, "
            "since the classifier cannot distinguish it from a spurious "
            "firing."
        ),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="RQ4 false-positive-rate analysis (Thesis §3.7.3)."
    )
    parser.add_argument("--results-dir", default="results",
                        help="Directory containing alert_log.jsonl, "
                             "correlation.jsonl, mttr_log*.jsonl (default: results/)")
    parser.add_argument("--out", default="results/05_false_positive_rate.json",
                        help="Output file path (default: results/05_false_positive_rate.json)")
    args = parser.parse_args(argv)

    results_dir = Path(args.results_dir)
    alert_records = _load_jsonl(results_dir / "alert_log.jsonl")
    correlation_records = _load_jsonl(results_dir / "correlation.jsonl")

    mttr_records: list[dict] = []
    for mttr_path in sorted(results_dir.glob("mttr_log*.jsonl")):
        mttr_records.extend(_load_jsonl(mttr_path))

    logger.info(
        "Loaded %d alert receipts, %d injection records, %d MTTR records",
        len(alert_records), len(correlation_records), len(mttr_records),
    )

    if not alert_records:
        logger.warning("No alert receipts found — nothing to analyse.")
    if not correlation_records:
        logger.warning("No injection records found — every alert will be excluded (no active trial windows).")

    trial_windows = build_trial_windows(correlation_records, mttr_records)
    result = classify_alerts(alert_records, trial_windows)
    result["n_trial_windows"] = len(trial_windows)

    output = json.dumps(result, indent=2)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(output)
    logger.info(
        "False-positive rate: %.4f (%d/%d) — written to %s",
        result["false_positive_rate"], result["n_false_positive"],
        result["n_alerts_in_denominator"], args.out,
    )
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
