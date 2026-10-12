#!/usr/bin/env python3
"""
experiments/trials/capture_host_baseline.py — Capture t3.medium host idle
baseline BEFORE the observability containers start.

Records CPU (idle %) and RSS (resident set size in MiB) at fixed intervals
for a specified duration, then writes a JSON record to local file and
optionally mirrors to S3.

The result is the `host_baseline` row reported alongside the per-pillar
observability overhead in Thesis §5.4 (Round-8 C8-1 forward commitment).

Usage:
    python experiments/trials/capture_host_baseline.py \\
        --duration 600 --interval 15 \\
        --out s3://its-thesis-data/baseline/host_baseline.json

    # Local output only:
    python experiments/trials/capture_host_baseline.py \\
        --duration 600 --interval 15 \\
        --out results/host_baseline.json

Exit codes:
    0 — baseline captured and written
    1 — error during capture

Thesis reference: §4.3, §5.4 RQ3 observability overhead, Round-8 C8-1
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import psutil

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("capture_host_baseline")


def _cpu_idle_pct() -> float:
    """Return the current system-wide CPU idle percentage (1-second sample)."""
    times = psutil.cpu_times_percent(interval=1)
    return round(times.idle, 2)


def _host_rss_mib() -> float:
    """Return total RSS of all processes in MiB (proxy for used RAM)."""
    total_rss = sum(
        p.info["memory_info"].rss
        for p in psutil.process_iter(["memory_info"])
        if p.info["memory_info"] is not None
    )
    return round(total_rss / (1024 ** 2), 2)


def _write_output(record: dict, out_path: str) -> None:
    """Write the baseline record to local file and/or S3."""
    payload = json.dumps(record, indent=2)

    if out_path.startswith("s3://"):
        import boto3
        parts   = out_path[5:].split("/", 1)
        bucket  = parts[0]
        key     = parts[1] if len(parts) > 1 else "host_baseline.json"
        region  = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")
        s3      = boto3.client("s3", region_name=region)
        s3.put_object(Bucket=bucket, Key=key, Body=payload.encode(), ContentType="application/json")
        logger.info("Uploaded baseline to s3://%s/%s", bucket, key)
        # Also write locally to results/
        local_path = Path("results") / "host_baseline.json"
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(payload)
        logger.info("Local copy: %s", local_path)
    else:
        local_path = Path(out_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_text(payload)
        logger.info("Baseline written to %s", local_path)


def capture_baseline(duration: int, interval: int) -> dict:
    """
    Collect CPU idle % and host RSS at `interval`-second ticks for `duration`
    seconds, then compute summary statistics.
    """
    samples: list[dict] = []
    logger.info(
        "Capturing host baseline: duration=%ds interval=%ds (%d samples)",
        duration, interval, duration // interval,
    )
    t_start = time.monotonic()
    deadline = t_start + duration

    while time.monotonic() < deadline:
        ts          = datetime.now(timezone.utc).isoformat()
        cpu_idle    = _cpu_idle_pct()
        rss_mib     = _host_rss_mib()

        samples.append({
            "timestamp":  ts,
            "cpu_idle_pct": cpu_idle,
            "rss_mib":      rss_mib,
        })
        logger.info("t=%.0fs  cpu_idle=%.1f%%  rss=%.1f MiB",
                    time.monotonic() - t_start, cpu_idle, rss_mib)

        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(interval, remaining))

    # Summary statistics
    cpu_idles = [s["cpu_idle_pct"] for s in samples]
    rss_mibs  = [s["rss_mib"]      for s in samples]

    def _stats(values: list[float]) -> dict:
        n = len(values)
        if n == 0:
            return {}
        sorted_v = sorted(values)
        mean = sum(values) / n
        p50  = sorted_v[n // 2]
        p95  = sorted_v[int(n * 0.95)]
        return {"mean": round(mean, 2), "p50": round(p50, 2), "p95": round(p95, 2),
                "min": round(sorted_v[0], 2), "max": round(sorted_v[-1], 2)}

    record = {
        "baseline_type":        "host_idle",
        "captured_at":          datetime.now(timezone.utc).isoformat(),
        "duration_seconds":     duration,
        "interval_seconds":     interval,
        "sample_count":         len(samples),
        "cpu_idle_pct":         _stats(cpu_idles),
        "cpu_used_pct":         _stats([100 - x for x in cpu_idles]),
        "rss_mib":              _stats(rss_mibs),
        "samples":              samples,
        # ── Fields required by Thesis §5.4 ────────────────────────────────────
        # The observability overhead table reports:
        #   host_baseline_cpu_used_pct_mean
        #   host_baseline_rss_mib_mean
        "host_baseline_cpu_used_pct_mean": round(100 - sum(cpu_idles) / len(cpu_idles), 2),
        "host_baseline_rss_mib_mean":      round(sum(rss_mibs) / len(rss_mibs), 2),
    }
    return record


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Capture t3.medium host idle CPU and RSS before observability containers start. "
            "Output used as the host_baseline row in Thesis §5.4 Table."
        )
    )
    parser.add_argument(
        "--duration", type=int, default=600,
        help="Capture duration in seconds (default: 600 = 10 minutes)",
    )
    parser.add_argument(
        "--interval", type=int, default=15,
        help="Sampling interval in seconds (default: 15)",
    )
    parser.add_argument(
        "--out", default="results/host_baseline.json",
        help="Output path (local file or s3://bucket/key, default: results/host_baseline.json)",
    )
    args = parser.parse_args(argv)

    try:
        record = capture_baseline(args.duration, args.interval)
        _write_output(record, args.out)
        logger.info(
            "Baseline captured: cpu_used=%.1f%% rss=%.1f MiB (mean over %d samples)",
            record["host_baseline_cpu_used_pct_mean"],
            record["host_baseline_rss_mib_mean"],
            record["sample_count"],
        )
        return 0
    except Exception as exc:
        logger.error("Baseline capture failed: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
