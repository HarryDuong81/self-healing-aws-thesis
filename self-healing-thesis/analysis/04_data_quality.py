#!/usr/bin/env python3
"""
analysis/04_data_quality.py — RQ3 data-quality checks, companion to
04_overhead.py.

Deployment-session addition (2026-09-22): after finding real, analogous
data-quality gaps in H1 (a schedule/circuit-breaker interaction leaving 5
units with no successful S1 trial) and RQ4 (a -1-sentinel bug silently
widening some trial windows), RQ3's own results/overhead_samples.jsonl
population was checked for the same class of issue before trusting it.
Confirmed clean at the time (see CLAUDE.md §15.8's RQ3 data-quality
addendum) -- this script makes that check reusable and reproducible
instead of a one-off manual query, so it can be re-run against any future
RQ3 sample set (a fresh campaign, a re-run after further fixes, etc.)
without re-deriving the checks from scratch.

Checks performed:
  1. Null/None cpu_cores or rss_mib values -- 04_overhead.py's own
     aggregation already skips a null cpu_cores value correctly (`if cpu is
     None: continue`), but a null value still means that sample carried no
     usable data (e.g. overhead_sampler.py's own negative-CPU-delta
     rejection) and is worth surfacing explicitly rather than silently
     absorbed into a smaller effective sample size.
  2. Per-container, per-phase trial coverage -- every container should have
     at least one sample in both the "steady" and "active" phase for every
     distinct trial_id present in the file. A container missing from a
     trial's steady or active phase (e.g. because it was mid-restart
     during every poll tick of that phase) would silently understate that
     container's contribution to the trial without ever raising an error.

This is a read-only diagnostic, not a statistical test -- it has no
official/sensitivity-adjusted pair of results the way h2_sensitivity.py
and 05_sensitivity.py do. It either finds gaps (worth investigating
before trusting 04_overhead.py's output) or confirms there are none.

Usage:
    python analysis/04_data_quality.py \\
        --samples results/overhead_samples.jsonl \\
        --out results/04_data_quality.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Optional

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("04_data_quality")

_PHASES = ("steady", "active")


def _load_samples(path: str) -> list[dict]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def check_null_values(samples: list[dict]) -> dict:
    null_cpu = [r for r in samples if r.get("cpu_cores") is None]
    null_mem = [r for r in samples if r.get("rss_mib") is None]
    return {
        "n_samples_total": len(samples),
        "n_null_cpu_cores": len(null_cpu),
        "n_null_rss_mib": len(null_mem),
        "null_cpu_examples": null_cpu[:5],
        "null_mem_examples": null_mem[:5],
    }


def check_container_coverage(samples: list[dict]) -> dict:
    by_container_phase: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in samples:
        by_container_phase[(r["container"], r["phase"])].add(r["trial_id"])

    all_trial_ids = sorted({r["trial_id"] for r in samples})
    containers = sorted({r["container"] for r in samples})
    n_expected = len(all_trial_ids)

    per_container = {}
    gaps: list[str] = []
    for c in containers:
        row = {}
        for phase in _PHASES:
            covered = by_container_phase.get((c, phase), set())
            row[f"{phase}_trials"] = len(covered)
            missing = sorted(set(all_trial_ids) - covered)
            if missing:
                gaps.append(f"{c}/{phase}: missing {len(missing)} of {n_expected} trials: {missing[:5]}")
        per_container[c] = row

    return {
        "n_distinct_trial_ids": n_expected,
        "trial_ids": all_trial_ids,
        "containers": containers,
        "per_container_phase_coverage": per_container,
        "gaps": gaps,
        "clean": len(gaps) == 0,
    }


def run_data_quality(samples: list[dict]) -> dict:
    return {
        "null_values": check_null_values(samples),
        "container_coverage": check_container_coverage(samples),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="RQ3 data-quality checks (companion to 04_overhead.py) — see module docstring."
    )
    parser.add_argument("--samples", default="results/overhead_samples.jsonl",
                        help="Phase-tagged sample log path (default: results/overhead_samples.jsonl)")
    parser.add_argument("--out", default="results/04_data_quality.json",
                        help="Output file path (default: results/04_data_quality.json)")
    args = parser.parse_args(argv)

    samples = _load_samples(args.samples)
    logger.info("Loaded %d samples from %s", len(samples), args.samples)

    if not samples:
        logger.error("No samples found — nothing to check.")
        return 1

    result = run_data_quality(samples)

    n_null = result["null_values"]["n_null_cpu_cores"] + result["null_values"]["n_null_rss_mib"]
    n_gaps = len(result["container_coverage"]["gaps"])
    if n_null:
        logger.warning("Found %d null-valued samples", n_null)
    if n_gaps:
        logger.warning("Found %d container/phase coverage gap(s):", n_gaps)
        for g in result["container_coverage"]["gaps"]:
            logger.warning("  %s", g)
    if not n_null and not n_gaps:
        logger.info(
            "Clean: 0 null values, all %d container(s) have full trial coverage "
            "across both phases for all %d distinct trial(s).",
            len(result["container_coverage"]["containers"]),
            result["container_coverage"]["n_distinct_trial_ids"],
        )

    output = json.dumps(result, indent=2)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(output)
    logger.info("Results written to %s", args.out)
    print(output)

    return 1 if (n_null or n_gaps) else 0


if __name__ == "__main__":
    sys.exit(main())
