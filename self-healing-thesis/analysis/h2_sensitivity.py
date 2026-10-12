#!/usr/bin/env python3
"""
analysis/h2_sensitivity.py — RQ2 H2 sensitivity analysis (Thesis §5.3.2).

Answers a narrower question than 02_wilcoxon.py::run_mann_whitney_h2(): given
a clean-baseline estimate for some phase of S1's instance_failure T_MTTR
(e.g. T_observability_restore, if a known infrastructure defect inflated it
for some or all of the official trials — see CLAUDE.md §15.8), does the
correction change H2's statistical conclusion? This is deliberately NOT a
replacement for the official H2 result in 02_wilcoxon.py's own output — it
answers "would the conclusion survive a plausible correction," not "what is
the corrected official number." Both should be reported together, never one
without the other.

Excludes trial_ids passed via --exclude-trial-id from EITHER arm before
computing anything, so a known non-campaign artefact (e.g. an isolated pilot
trial mixed into results/mttr_log_s3.jsonl) doesn't have to be hand-edited
out of the source file first.

Usage:
    python analysis/h2_sensitivity.py \\
        --campaign results/ \\
        --s3-campaign results/mttr_log_s3.jsonl \\
        --phase T_observability_restore_seconds \\
        --clean-value 309.07 \\
        --exclude-trial-id s3-2026-09-11-instance_failure-1789115511 \\
        --out results/h2_sensitivity.json

    # Or derive --clean-value from specific known-clean trial_ids instead of
    # supplying it directly:
    python analysis/h2_sensitivity.py \\
        --campaign results/ \\
        --s3-campaign results/mttr_log_s3.jsonl \\
        --phase T_observability_restore_seconds \\
        --clean-trial-id rq3-2026-09-22-dd49839e \\
        --clean-trial-id rq3-2026-09-22-a0ef472e \\
        --out results/h2_sensitivity.json
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from pathlib import Path
from typing import Optional

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("h2_sensitivity")

_ALPHA = 0.05
_ALPHA_ADJUSTED = _ALPHA / 2


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _load_campaign(campaign_path: str) -> list[dict]:
    p = Path(campaign_path)
    if p.is_dir():
        records: list[dict] = []
        for f in sorted(p.glob("mttr_log*.jsonl")):
            records.extend(_load_jsonl(f))
        return records
    return _load_jsonl(p)


def _instance_failure_success(
    records: list[dict], system: str, exclude_trial_ids: set[str]
) -> list[dict]:
    return [
        r for r in records
        if r.get("system_under_test") == system
        and r.get("fault_type") == "instance_failure"
        and r.get("recovery_status") == "success"
        and r.get("trial_id") not in exclude_trial_ids
    ]


def rank_biserial_effect_size(x: list[float], y: list[float], stat: float) -> float:
    return round(1 - (2 * stat) / (len(x) * len(y)), 6)


def run_sensitivity(
    s1_records: list[dict],
    s3_records: list[dict],
    phase_field: str,
    clean_value: float,
    exclude_trial_ids: set[str],
) -> dict:
    from scipy import stats

    s1 = _instance_failure_success(s1_records, "S1", exclude_trial_ids)
    s3 = _instance_failure_success(s3_records, "S3", exclude_trial_ids)

    if not s1 or not s3:
        raise RuntimeError(
            f"Not enough data to run the sensitivity analysis: n_S1={len(s1)}, n_S3={len(s3)}"
        )

    x_orig = [r["T_MTTR_seconds"] for r in s1]
    x_adj = [
        r["T_MTTR_seconds"] - r.get(phase_field, 0.0) + clean_value
        for r in s1
    ]
    y = [r["T_MTTR_seconds"] for r in s3]

    def _summarise(x: list[float]) -> dict:
        stat, p = stats.mannwhitneyu(x, y, alternative="two-sided")
        return {
            "n1": len(x), "n2": len(y),
            "median_x": round(statistics.median(x), 2),
            "median_y": round(statistics.median(y), 2),
            "statistic": round(float(stat), 6),
            "p_value": round(float(p), 6),
            "effect_size_rank_biserial": rank_biserial_effect_size(x, y, float(stat)),
            "significant_at_alpha_adjusted": bool(p < _ALPHA_ADJUSTED),
            "complete_separation": bool(stat == len(x) * len(y)),
        }

    return {
        "hypothesis": "H2_sensitivity",
        "description": (
            f"Sensitivity analysis: S1 instance_failure T_MTTR with '{phase_field}' "
            f"replaced by a clean-baseline estimate ({clean_value}), vs S3, unchanged."
        ),
        "note": (
            "NOT a replacement for 02_wilcoxon.py's official H2 result — report "
            "both together. This answers whether a known, quantified measurement "
            "artefact could plausibly change H2's conclusion, not what the "
            "corrected official number is."
        ),
        "phase_field_adjusted": phase_field,
        "clean_value_used": clean_value,
        "excluded_trial_ids": sorted(exclude_trial_ids),
        "original": _summarise(x_orig),
        "sensitivity_adjusted": _summarise(x_adj),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="RQ2 H2 sensitivity analysis (Thesis §5.3.2) — see module docstring."
    )
    parser.add_argument("--campaign", required=True,
                        help="S1 mttr_log.jsonl path or directory (globs mttr_log*.jsonl)")
    parser.add_argument("--s3-campaign", required=True,
                        help="S3 mttr_log_s3.jsonl path")
    parser.add_argument("--phase", default="T_observability_restore_seconds",
                        help="MTTR record field to replace with the clean-baseline "
                             "estimate (default: T_observability_restore_seconds)")
    parser.add_argument("--clean-value", type=float, default=None,
                        help="Clean-baseline value to substitute for --phase. "
                             "Mutually exclusive with --clean-trial-id.")
    parser.add_argument("--clean-trial-id", action="append", default=[],
                        help="S1 trial_id(s) confirmed to be measured under clean "
                             "conditions; --clean-value is derived as their mean "
                             "--phase value if --clean-value is not given directly. "
                             "Repeatable.")
    parser.add_argument("--exclude-trial-id", action="append", default=[],
                        help="trial_id(s) to exclude from either arm before analysis "
                             "(e.g. a known non-campaign pilot trial). Repeatable.")
    parser.add_argument("--out", default="results/h2_sensitivity.json",
                        help="Output file path (default: results/h2_sensitivity.json)")
    args = parser.parse_args(argv)

    s1_records = _load_campaign(args.campaign)
    s3_records = _load_campaign(args.s3_campaign)
    logger.info("Loaded %d S1 records, %d S3 records", len(s1_records), len(s3_records))

    exclude_ids = set(args.exclude_trial_id)

    clean_value = args.clean_value
    if clean_value is None:
        if not args.clean_trial_id:
            logger.error("Must supply either --clean-value or one or more --clean-trial-id")
            return 2
        clean_pool = [
            r for r in s1_records
            if r.get("trial_id") in set(args.clean_trial_id) and args.phase in r
        ]
        if not clean_pool:
            logger.error("None of the supplied --clean-trial-id values were found with field %s", args.phase)
            return 2
        clean_value = statistics.mean(r[args.phase] for r in clean_pool)
        logger.info("Derived clean_value=%.3f from %d trial(s): %s",
                    clean_value, len(clean_pool), args.clean_trial_id)

    result = run_sensitivity(s1_records, s3_records, args.phase, clean_value, exclude_ids)

    output = json.dumps(result, indent=2)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(output)
    logger.info("Results written to %s", args.out)
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
