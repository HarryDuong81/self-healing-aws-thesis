#!/usr/bin/env python3
"""
analysis/04_overhead.py — RQ3 observability overhead analysis (Thesis §5.4).

Computes per-pillar CPU and memory overhead of the eight deliberately-deployed
observability containers relative to the empty-host baseline captured before
the Docker Compose stack was started (§11.2).

cAdvisor is the measurement instrument and is EXCLUDED from the aggregate
(Thesis §3.7.3, Table 4.3 footnote).

Eight containers counted in RQ3 aggregate:
    prometheus, alertmanager, grafana, elasticsearch, logstash, kibana,
    jaeger, otelcol

Data sources:
  --baseline  : host_baseline.json  (captured before `docker compose up -d`)
  --cadvisor  : cAdvisor API endpoint (default http://localhost:8080)
                or a pre-captured JSON snapshot

Output: results/04_overhead.json

Usage:
    python analysis/04_overhead.py \\
        --baseline s3://its-thesis-data/baseline/host_baseline.json \\
        --cadvisor http://monitoring-host:8080 \\
        --out results/04_overhead.json
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import Optional

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(message)s",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("04_overhead")

# Eight observability containers (Table 4.3); cAdvisor excluded (§5.7)
_OBSERVABILITY_CONTAINERS = {
    "prometheus",
    "alertmanager",
    "grafana",
    "elasticsearch",
    "logstash",
    "kibana",
    "jaeger",
    "otelcol",
}

_PILLARS = {
    "metrics":     {"prometheus", "alertmanager", "grafana"},
    "logs":        {"elasticsearch", "logstash", "kibana"},
    "traces":      {"jaeger", "otelcol"},
}

# Host capacity denominator — t3.medium (2 vCPUs, 4 GiB RAM). Single source
# of truth: every CPU/memory percentage-of-capacity figure in this module
# (both compute_overhead()'s aggregate and the phase-tagged per-trial
# aggregation below) normalises against these two constants.
HOST_VCPU = 2
HOST_MEM_MIB = 4096


def _load_baseline(baseline_path: str) -> dict:
    if baseline_path.startswith("s3://"):
        import boto3
        parts  = baseline_path.lstrip("s3://").split("/", 1)
        bucket = parts[0]
        key    = parts[1]
        s3     = boto3.client("s3", region_name=os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1"))
        return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode())
    return json.loads(Path(baseline_path).read_text())


def _fetch_cadvisor_snapshot(cadvisor_url: str) -> dict:
    """
    Fetch container stats from cAdvisor /api/v1.3/containers.
    Returns a dict keyed by container name.
    """
    import urllib.request
    url = cadvisor_url.rstrip("/") + "/api/v1.3/containers/docker"
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = json.loads(resp.read())
    return data


def _parse_cadvisor_container(container_data: dict) -> dict:
    """Extract current CPU (cores) and memory (MiB) from a cAdvisor container record."""
    stats = container_data.get("stats", [])
    if len(stats) < 2:
        return {"cpu_cores": None, "rss_mib": None}

    # Use last two data points to compute CPU usage rate
    prev, curr = stats[-2], stats[-1]
    cpu_ns_delta = (
        curr["cpu"]["usage"]["total"] - prev["cpu"]["usage"]["total"]
    )
    ts_delta_ns = (
        _iso_to_ns(curr["timestamp"]) - _iso_to_ns(prev["timestamp"])
    )
    cpu_cores = cpu_ns_delta / ts_delta_ns if ts_delta_ns > 0 else 0.0

    rss_bytes = curr.get("memory", {}).get("rss", 0)
    rss_mib   = rss_bytes / (1024 * 1024)

    return {"cpu_cores": round(cpu_cores, 6), "rss_mib": round(rss_mib, 2)}


def _iso_to_ns(ts: str) -> int:
    """Convert ISO-8601 timestamp string to nanoseconds."""
    from datetime import datetime
    ts = ts.rstrip("Z") + "+00:00" if ts.endswith("Z") else ts
    dt = datetime.fromisoformat(ts)
    return int(dt.timestamp() * 1e9)


def _load_snapshot_file(path: str) -> dict:
    return json.loads(Path(path).read_text())


def compute_overhead(baseline: dict, container_stats: dict) -> dict:
    """
    baseline: dict with host_baseline_cpu_used_pct_mean and host_baseline_rss_mib_mean
    container_stats: dict keyed by container name with cpu_cores and rss_mib
    """
    baseline_cpu_pct = baseline.get("host_baseline_cpu_used_pct_mean", 0.0)
    baseline_rss_mib = baseline.get("host_baseline_rss_mib_mean", 0.0)

    pillar_results: dict = {}
    total_cpu_cores: float = 0.0
    total_rss_mib:   float = 0.0

    for pillar, members in _PILLARS.items():
        pillar_cpu: float = 0.0
        pillar_rss: float = 0.0
        per_container: dict = {}
        for name in members:
            stats = container_stats.get(name, {})
            cpu   = stats.get("cpu_cores") or 0.0
            rss   = stats.get("rss_mib")   or 0.0
            per_container[name] = {"cpu_cores": cpu, "rss_mib": rss}
            pillar_cpu += cpu
            pillar_rss += rss
        pillar_results[pillar] = {
            "cpu_cores_sum": round(pillar_cpu, 4),
            "rss_mib_sum":   round(pillar_rss, 2),
            "containers":    per_container,
        }
        total_cpu_cores += pillar_cpu
        total_rss_mib   += pillar_rss

    # Report overhead as % of available CPU (HOST_VCPU — see module-level constant)
    cpu_overhead_pct = round((total_cpu_cores / HOST_VCPU) * 100, 2)

    return {
        # ── Baseline ──────────────────────────────────────────────────────────
        "host_baseline": {
            "cpu_used_pct_mean": round(baseline_cpu_pct, 2),
            "rss_mib_mean":      round(baseline_rss_mib, 2),
        },
        # ── Per-pillar overhead ───────────────────────────────────────────────
        "pillars": pillar_results,
        # ── Aggregate (8 containers; cAdvisor excluded) ─────────────────────
        "aggregate": {
            "n_containers":              len(_OBSERVABILITY_CONTAINERS),
            "cadvisor_excluded":         True,
            "total_cpu_cores":           round(total_cpu_cores, 4),
            "total_rss_mib":             round(total_rss_mib, 2),
            "cpu_overhead_pct_of_2vcpu": cpu_overhead_pct,
        },
        "note": (
            "cAdvisor is the measurement instrument and is excluded from the "
 "aggregate per Thesis §3.7.3, Table 4.3 footnote."
        ),
    }




# ── RQ3 two-stage aggregation over phase-tagged samples ──────────────────────
#
# Stage 1: within each trial, and within each phase, take the median across the
#          samples of that trial.  This gives every trial one summary value.
# Stage 2: across trials, take the median of those per-trial values, with the
#          interquartile range as the dispersion measure.
#
# Pooling raw samples across the whole campaign would weight long trials more
# heavily than short ones — instance_failure runs roughly twenty times longer
# than service_crash — so the two stages are not interchangeable.
#
# Normalisation is against host capacity (HOST_VCPU/HOST_MEM_MIB, defined
# above), not against the idle-host baseline.  The idle baseline is a
# near-zero, unstable denominator; it is retained as a descriptive
# reference only.

_SAMPLES_DEFAULT = "results/overhead_samples.jsonl"


def _median(xs: list[float]) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def _quantile(xs: list[float], q: float) -> Optional[float]:
    """Nearest-rank quantile; matches the percentile convention in h3_upper_bound.py."""
    if not xs:
        return None
    s = sorted(xs)
    k = max(1, min(len(s), int(round(q * len(s) + 0.5))))
    return s[k - 1]


def _iqr(xs: list[float]) -> Optional[float]:
    if len(xs) < 2:
        return None
    q1, q3 = _quantile(xs, 0.25), _quantile(xs, 0.75)
    return None if q1 is None or q3 is None else round(q3 - q1, 6)


def _load_samples(path: str) -> list[dict]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def aggregate_phase_samples(samples: list[dict]) -> dict:
    """Two-stage aggregation, reported per phase and per container."""
    out: dict = {"phases": {}, "sample_count": len(samples)}
    phases = sorted({r["phase"] for r in samples})
    for phase in phases:
        rows = [r for r in samples if r["phase"] == phase]
        trials = sorted({r["trial_id"] for r in rows})

        # stage 1 — per trial, per container
        per_trial_cpu: dict[str, list[float]] = {}
        per_trial_mem: dict[str, list[float]] = {}
        stack_cpu_by_trial: list[float] = []
        stack_mem_by_trial: list[float] = []
        instrument_cpu_by_trial: list[float] = []
        instrument_mem_by_trial: list[float] = []

        for t in trials:
            trows = [r for r in rows if r["trial_id"] == t]
            stack_cpu, stack_mem = 0.0, 0.0
            for c in sorted({r["container"] for r in trows}):
                crows = [r for r in trows if r["container"] == c]
                cpu = _median([r["cpu_cores"] for r in crows])
                mem = _median([r["rss_mib"] for r in crows])
                if cpu is None:
                    continue
                per_trial_cpu.setdefault(c, []).append(cpu)
                per_trial_mem.setdefault(c, []).append(mem)
                if crows[0].get("instrument"):
                    instrument_cpu_by_trial.append(cpu)
                    instrument_mem_by_trial.append(mem)
                else:
                    stack_cpu += cpu
                    stack_mem += mem
            stack_cpu_by_trial.append(stack_cpu)
            stack_mem_by_trial.append(stack_mem)

        # stage 2 — across trials
        per_container = {}
        for c in sorted(per_trial_cpu):
            per_container[c] = {
                "cpu_cores_median": _median(per_trial_cpu[c]),
                "cpu_cores_iqr":    _iqr(per_trial_cpu[c]),
                "cpu_cores_p95":    _quantile(per_trial_cpu[c], 0.95),
                "rss_mib_median":   _median(per_trial_mem[c]),
                "rss_mib_iqr":      _iqr(per_trial_mem[c]),
                "rss_mib_p95":      _quantile(per_trial_mem[c], 0.95),
                "trials":           len(per_trial_cpu[c]),
            }
        cpu_med = _median(stack_cpu_by_trial)
        mem_med = _median(stack_mem_by_trial)
        out["phases"][phase] = {
            "trials": len(trials),
            "aggregate_eight_services": {
                "cpu_cores_median":   cpu_med,
                "cpu_cores_iqr":      _iqr(stack_cpu_by_trial),
                "cpu_pct_capacity":   round(100.0 * cpu_med / HOST_VCPU, 2) if cpu_med is not None else None,
                "cpu_cores_p95":      _quantile(stack_cpu_by_trial, 0.95),
                "rss_mib_median":     mem_med,
                "rss_mib_iqr":        _iqr(stack_mem_by_trial),
                "rss_pct_capacity":   round(100.0 * mem_med / HOST_MEM_MIB, 2) if mem_med is not None else None,
                "rss_mib_p95":        _quantile(stack_mem_by_trial, 0.95),
            },
            "measurement_instrument_cadvisor": {
                "cpu_cores_median": _median(instrument_cpu_by_trial),
                "rss_mib_median":   _median(instrument_mem_by_trial),
                "note": "reported separately; not included in the eight-service aggregate",
            },
            "per_container": per_container,
        }
    out["capacity_denominator"] = {"vcpu": HOST_VCPU, "memory_mib": HOST_MEM_MIB,
                                   "instance_type": "t3.medium"}
    out["rq3_primary_phase"] = "steady"
    out["rq3_secondary_phase"] = "active"
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="RQ3 observability overhead analysis (Thesis §5.4)."
    )
    parser.add_argument("--baseline", required=True,
                        help="host_baseline.json local path or s3://bucket/key")
    parser.add_argument("--samples", default=_SAMPLES_DEFAULT,
                        help="Phase-tagged sample log written by "
                             "experiments/trials/overhead_sampler.py during the campaign.")
    parser.add_argument("--cadvisor", default="http://localhost:8080",
                        help="cAdvisor base URL or local JSON snapshot path")
    parser.add_argument("--snapshot-only", action="store_true",
                        help="Take a single live cAdvisor snapshot instead of "
                             "aggregating the campaign sample log. Diagnostic "
                             "use only; produces no median, IQR, or percentile.")
    parser.add_argument("--out", default="results/04_overhead.json",
                        help="Output file path (default: results/04_overhead.json)")
    args = parser.parse_args(argv)

    baseline = _load_baseline(args.baseline)
    logger.info("Baseline loaded: cpu=%.1f%% rss=%.1f MiB",
                baseline.get("host_baseline_cpu_used_pct_mean", 0),
                baseline.get("host_baseline_rss_mib_mean", 0))

    # ── Primary path: two-stage aggregation over phase-tagged samples ────────
    #
    # The campaign writes results/overhead_samples.jsonl through
    # experiments/trials/overhead_sampler.py: one record per container per
    # 5-second tick, each already tagged `steady` or `active` by comparison
    # against the trial's t_0.  This is the path that produces the RQ3 figure
    # reported in Thesis Table 5.8.
    #
    # The single-snapshot path below it is retained only as a live spot-check
    # for operators (`--snapshot-only`); it cannot support a median, an IQR,
    # or a percentile, and is never the source of a reported result.
    samples_path = Path(args.samples)
    if not args.snapshot_only and samples_path.exists():
        samples = _load_samples(str(samples_path))
        if not samples:
            logger.error("No samples found in %s — has the campaign run?", samples_path)
            return 1
        logger.info("Loaded %d phase-tagged samples from %s", len(samples), samples_path)
        result = aggregate_phase_samples(samples)
        result["host_baseline_reference"] = {
            "cpu_used_pct_mean": baseline.get("host_baseline_cpu_used_pct_mean"),
            "rss_mib_mean":      baseline.get("host_baseline_rss_mib_mean"),
            "note": ("Descriptive reference only. RQ3 normalises against host "
                     "capacity, not against this baseline, because idle-host "
                     "consumption is a near-zero and unstable denominator."),
        }
        result["method"] = ("two-stage aggregation of 5 s phase-tagged cAdvisor "
                            "samples: median per trial and phase, then median "
                            "with IQR across trials")
    else:
        if not args.snapshot_only:
            logger.warning("Sample log %s not found — falling back to a single "
                           "snapshot. This yields no median, IQR, or percentile "
                           "and must not be used for a reported RQ3 result.",
                           samples_path)
        if args.cadvisor.startswith("http"):
            logger.info("Fetching cAdvisor container stats from %s", args.cadvisor)
            raw = _fetch_cadvisor_snapshot(args.cadvisor)
            container_stats: dict = {}
            for cpath, cdata in raw.get("subcontainers", {}).items():
                name = cdata.get("name", "").lstrip("/")
                if name:
                    container_stats[name] = _parse_cadvisor_container(cdata)
        else:
            logger.info("Loading cAdvisor snapshot from %s", args.cadvisor)
            raw = _load_snapshot_file(args.cadvisor)
            container_stats = raw
        result = compute_overhead(baseline, container_stats)
        result["method"] = "single snapshot (spot-check only — not a reported result)"

    output = json.dumps(result, indent=2)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(output)
    logger.info("Overhead analysis written to %s", args.out)
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
