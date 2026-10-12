#!/usr/bin/env python3
"""
analysis/05_sensitivity.py — RQ4 false-positive-rate sensitivity analysis
(Thesis §3.7.3), companion to 05_false_positive_rate.py.

Answers: if one or more specific, already-identified alert-receipt entries
are known to be an artefact of a since-fixed infrastructure defect (e.g. the
Alertmanager group_interval/repeat_interval redelivery defect, CLAUDE.md
§15.6) rather than a genuine classifier false positive, how does the
reported rate change if those entries are excluded from BOTH numerator and
denominator (they would never have been delivered at all under the fixed
configuration, not merely reclassified)?

This is NOT a replacement for 05_false_positive_rate.py's own official
result — report both together. Entries are excluded by exact match on
`matching_trial_ids`, not by manual row inspection, so the exclusion is
reproducible and auditable.

Usage:
    python analysis/05_sensitivity.py \\
        --false-positive-rate-result results/05_false_positive_rate.json \\
        --exclude-trial-id t-2026-09-16-4e5eaa5e \\
        --out results/05_sensitivity.json
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
logger = logging.getLogger("05_sensitivity")


def run_sensitivity(official: dict, exclude_trial_ids: set[str]) -> dict:
    details = official.get("false_positive_details", [])

    excluded = [
        d for d in details
        if set(d.get("matching_trial_ids", [])) & exclude_trial_ids
    ]
    kept = [
        d for d in details
        if not (set(d.get("matching_trial_ids", [])) & exclude_trial_ids)
    ]

    n_fp_adjusted = official["n_false_positive"] - len(excluded)
    n_denom_adjusted = official["n_alerts_in_denominator"] - len(excluded)
    rate_adjusted = round(n_fp_adjusted / n_denom_adjusted, 4) if n_denom_adjusted else None

    return {
        "note": (
            "Exploratory sensitivity analysis, NOT a replacement for "
            "05_false_positive_rate.py's official result — report both together. "
            "Excludes only entries whose matching_trial_ids intersect the "
            "supplied --exclude-trial-id set, on the basis that those specific "
            "trial windows are confirmed instances of a since-fixed infrastructure "
            "defect (not a general reclassification of any alert that merely "
            "resembles one)."
        ),
        "excluded_trial_ids": sorted(exclude_trial_ids),
        "n_entries_excluded": len(excluded),
        "excluded_entries": excluded,
        "official": {
            "false_positive_rate": official["false_positive_rate"],
            "n_false_positive": official["n_false_positive"],
            "n_alerts_in_denominator": official["n_alerts_in_denominator"],
        },
        "sensitivity_adjusted": {
            "false_positive_rate": rate_adjusted,
            "n_false_positive": n_fp_adjusted,
            "n_alerts_in_denominator": n_denom_adjusted,
        },
        "n_false_positive_remaining_unexcluded": len(kept),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="RQ4 false-positive-rate sensitivity analysis (Thesis §3.7.3) — "
                    "see module docstring."
    )
    parser.add_argument("--false-positive-rate-result", required=True,
                        help="Path to 05_false_positive_rate.py's own JSON output "
                             "(must have been run with false_positive_details intact).")
    parser.add_argument("--exclude-trial-id", action="append", required=True,
                        help="trial_id(s) whose matching false-positive alert-log "
                             "entries are confirmed instances of a since-fixed "
                             "infrastructure defect, to exclude from both numerator "
                             "and denominator. Repeatable.")
    parser.add_argument("--out", default="results/05_sensitivity.json",
                        help="Output file path (default: results/05_sensitivity.json)")
    args = parser.parse_args(argv)

    official = json.loads(Path(args.false_positive_rate_result).read_text())
    if "false_positive_details" not in official:
        logger.error(
            "%s has no false_positive_details — re-run 05_false_positive_rate.py "
            "(it always includes this field; do not hand-edit the output)",
            args.false_positive_rate_result,
        )
        return 2

    logger.info("Loaded official result: rate=%.4f (%d/%d)",
                official["false_positive_rate"], official["n_false_positive"],
                official["n_alerts_in_denominator"])

    result = run_sensitivity(official, set(args.exclude_trial_id))

    logger.info("Excluded %d entries matching %s",
                result["n_entries_excluded"], args.exclude_trial_id)
    logger.info("Sensitivity-adjusted rate: %s (%s/%s)",
                result["sensitivity_adjusted"]["false_positive_rate"],
                result["sensitivity_adjusted"]["n_false_positive"],
                result["sensitivity_adjusted"]["n_alerts_in_denominator"])

    output = json.dumps(result, indent=2)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(output)
    logger.info("Results written to %s", args.out)
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
