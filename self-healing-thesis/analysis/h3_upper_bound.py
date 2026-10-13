#!/usr/bin/env python3
"""
analysis/h3_upper_bound.py — Reproduce the §5.3.3 H3 calculation.

H3 (Thesis §5.3.3, Round-11 C11-3):
    The 95th-percentile of the OBSERVED T_MTTR for S1 is ≤ 300 s
    (the RQ1 SLO upper bound).

Key invariant :
    t_clear is committed only on the 5-second health-checker poll observation.
    Worst-case lag is 20 s (15 s scrape + 5 s poll).
    The raw observed T_MTTR is therefore an UPPER BOUND on the true T_MTTR.
    Do NOT subtract the worst-case lag from the percentile — the H3 argument
    is monotonicity, not estimation.

Usage:
    python analysis/h3_upper_bound.py \\
        --campaign s3://its-thesis-data/campaign/ \\
        --system S1

    # Local JSONL:
    python analysis/h3_upper_bound.py \\
        --campaign results/mttr_log.jsonl \\
        --system S1

Output JSON:
    {
      "system":                    "S1",
      "n_trials":                  60,
      "p95_T_MTTR_obs_seconds":    <value>,
      "slo_threshold_seconds":     300,
      "h3_pass":                   true|false,
      "upper_bound_interpretation": "raw_obs_ge_true_pointwise",
      "polling_jitter_max_seconds": 20,
      "conformance": {
        "n_conforming":            <value>,
        "n_trials":                60,
        "proportion_conforming":   <value>,
        "ci_method":               "clopper_pearson_exact",
        "ci_confidence_level":     0.95,
        "ci_lower":                <value>,
        "ci_upper":                <value>
      }
    }

The `conformance` block is a secondary statistic reported alongside the
primary percentile check, not a substitute for it: the proportion of
trials with T_MTTR_obs <= slo, with an exact (Clopper-Pearson) 95%
binomial confidence interval — the standard "exact" method for a binomial
proportion CI, as opposed to a normal-approximation/Wilson interval.
Computed via scipy.stats.binomtest(...).proportion_ci(method="exact"),
already a project dependency (02_wilcoxon.py).

Thesis reference: §5.3.3, §3.1.3, Round-11 C11-3
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
logger = logging.getLogger("h3_upper_bound")

_SLO_SECONDS          = 300
_POLLING_JITTER_MAX_S = 20 # 15 s scrape + 5 s poll 


def _load_records(campaign_path: str) -> list[dict]:
    """Load MTTR records from a local JSONL file or an S3 prefix."""
    if campaign_path.startswith("s3://"):
        return _load_from_s3(campaign_path)
    p = Path(campaign_path)
    if p.is_dir():
        p = p / "mttr_log.jsonl"
    records = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _load_from_s3(s3_prefix: str) -> list[dict]:
    import boto3
    parts  = s3_prefix.lstrip("s3://").split("/", 1)
    bucket = parts[0]
    prefix = parts[1].rstrip("/") if len(parts) > 1 else ""
    key    = f"{prefix}/mttr_log.jsonl" if prefix else "mttr_log.jsonl"
    region = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")
    s3     = boto3.client("s3", region_name=region)
    content = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
    records = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def _percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile: the value at rank k = ceil(p/100 * n).

    This is the textbook definition, so the 95th percentile of n = 58 values is
    the 56th smallest (ceil(55.1) = 56).  An earlier version used
    int(round(p / 100 * n)) - 1, which Python rounds half to even and which is
    not the nearest-rank rule: it returned the 55th smallest for n = 58.  The
    product is rounded to nine decimals before ceil() so that floating-point
    error cannot push an exact integer rank (e.g. 0.95 * 20) up by one.
    """
    if not values:
        return float("nan")
    sorted_v = sorted(values)
    n        = len(sorted_v)
    k        = math.ceil(round(p / 100 * n, 9))
    return sorted_v[min(max(k, 1), n) - 1]


def compute_conformance(mttr_values: list[float], slo: int) -> dict:
    """
    Proportion of trials with T_MTTR_obs <= slo, with an exact (Clopper-
    Pearson) 95% binomial confidence interval — reported alongside the
    percentile check, not instead of it. A trial's T_MTTR_obs is itself an
    upper bound on its true T_MTTR (see module docstring); this function
    does not add or remove that property, it just summarises the same
    per-trial pass/fail outcomes as a proportion with an interval instead
    of (only) a percentile.
    """
    from scipy import stats

    n_trials = len(mttr_values)
    n_conforming = sum(1 for v in mttr_values if v <= slo)
    proportion = n_conforming / n_trials if n_trials else float("nan")

    result = stats.binomtest(n_conforming, n_trials, p=0.5)
    ci = result.proportion_ci(confidence_level=0.95, method="exact")

    return {
        "n_conforming": n_conforming,
        "n_trials": n_trials,
        "proportion_conforming": round(proportion, 4),
        "ci_method": "clopper_pearson_exact",
        "ci_confidence_level": 0.95,
        "ci_lower": round(ci.low, 4),
        "ci_upper": round(ci.high, 4),
    }


def compute_h3(
    records: list[dict],
    system: str,
    slo: int = _SLO_SECONDS,
    exclude_fault_types: tuple[str, ...] = (),
) -> dict:
    """
    Compute the H3 upper-bound test for a given system.

    Filters to successful trials for the specified system, extracts T_MTTR_seconds,
    computes the 95th percentile, and evaluates against the SLO.

    exclude_fault_types: an explicit, reported exclusion (not a silent drop —
    the returned dict always records what was excluded), for a supplementary
    breakdown such as "H3 excluding instance_failure" — the aggregate
    95th-percentile figure over all six fault classes is the primary,
    reportable metric and this parameter never replaces it, only supplements
    it alongside a clearly-labelled fault_scope field.

    Returns a result dict whose fields match exactly what Thesis §5.3.3 promises.
    """
    system_records = [
        r for r in records
        if r.get("system_under_test") == system
        and r.get("recovery_status")  == "success"
        and r.get("T_MTTR_seconds", -1) > 0
        and r.get("fault_type") not in exclude_fault_types
    ]

    if not system_records:
        logger.warning("No successful %s records found in dataset.", system)
        return {
            "system":                     system,
            "n_trials":                   0,
            "p95_T_MTTR_obs_seconds":     None,
            "slo_threshold_seconds":      slo,
            "h3_pass":                    False,
            "upper_bound_interpretation": "raw_obs_ge_true_pointwise",
            "polling_jitter_max_seconds": _POLLING_JITTER_MAX_S,
            "conformance":                None,
            "note": "No successful records found — cannot evaluate H3.",
        }

    mttr_values = [r["T_MTTR_seconds"] for r in system_records]
    p95         = _percentile(mttr_values, 95)
    h3_pass     = p95 <= slo

    result = {
        "system":                     system,
        "fault_scope":                "all six canonical fault classes" if not exclude_fault_types
                                       else f"excludes {', '.join(exclude_fault_types)}",
        "excluded_fault_types":       list(exclude_fault_types),
        "n_trials":                   len(system_records),
        "p95_T_MTTR_obs_seconds":     round(p95, 3),
        "slo_threshold_seconds":      slo,
        "h3_pass":                    h3_pass,
        # ── Upper-bound fields (Thesis §5.3.3 promise) ────────────────────────
        # The raw observed T_MTTR is an UPPER BOUND on the true T_MTTR because
        # t_clear is committed at the 5-second poll tick, not the instant the
        # alert clears. Hence raw_T_MTTR_obs >= true_T_MTTR pointwise.
        # The H3 argument: if the upper bound ≤ SLO, then the true value also ≤ SLO.
        # Do NOT subtract polling_jitter_max_seconds from p95.
        "upper_bound_interpretation": "raw_obs_ge_true_pointwise",
        "polling_jitter_max_seconds": _POLLING_JITTER_MAX_S,
        # ── Descriptive statistics ─────────────────────────────────────────────
        "descriptive": {
            "min":   round(min(mttr_values), 3),
            "p50":   round(_percentile(mttr_values, 50), 3),
            "p95":   round(p95, 3),
            "max":   round(max(mttr_values), 3),
            "mean":  round(sum(mttr_values) / len(mttr_values), 3),
        },
        # ── Conformance proportion + exact binomial CI (secondary statistic,
        #    reported alongside the p95 upper-bound check, not instead of it)
        "conformance": compute_conformance(mttr_values, slo),
    }

    return result


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute the H3 upper-bound T_MTTR test (Thesis §5.3.3).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--campaign", required=True,
        help="Path to mttr_log.jsonl (local), directory containing it, or s3://bucket/prefix",
    )
    parser.add_argument(
        "--system", default="S1",
        choices=["S1", "S2", "S3"],
        help="System under test label (default: S1)",
    )
    parser.add_argument(
        "--slo", type=int, default=_SLO_SECONDS,
        help=f"SLO threshold in seconds (default: {_SLO_SECONDS})",
    )
    parser.add_argument(
        "--exclude-fault-type", action="append", default=[],
        help="Fault type(s) to exclude from this computation (e.g. "
             "instance_failure), for a supplementary breakdown alongside "
             "the primary all-six-classes figure. Repeatable.",
    )
    parser.add_argument(
        "--out", default=None,
        help="Write result JSON to this file path (default: stdout only)",
    )
    args = parser.parse_args(argv)

    records = _load_records(args.campaign)
    logger.info("Loaded %d MTTR records from %s", len(records), args.campaign)

    result = compute_h3(
        records, system=args.system, slo=args.slo,
        exclude_fault_types=tuple(args.exclude_fault_type),
    )

    output = json.dumps(result, indent=2)
    print(output)

    if args.out:
        Path(args.out).write_text(output)
        logger.info("Result written to %s", args.out)

    verdict = "PASS" if result["h3_pass"] else "FAIL"
    logger.info(
        "H3 %s | system=%s p95=%.1fs SLO=%ds n=%d",
        verdict, args.system,
        result.get("p95_T_MTTR_obs_seconds") or -1,
        args.slo, result["n_trials"],
    )
    conf = result.get("conformance")
    if conf:
        logger.info(
            "Conformance | %d/%d (%.1f%%) trials <= %ds SLO, exact 95%% CI [%.4f, %.4f]",
            conf["n_conforming"], conf["n_trials"],
            100 * conf["proportion_conforming"],
            args.slo, conf["ci_lower"], conf["ci_upper"],
        )

    return 0 if result["h3_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
