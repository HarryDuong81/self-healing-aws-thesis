#!/usr/bin/env python3
"""
experiments/trials/capture_steady_state.py — properly powered RQ3 steady-state capture.

Why this exists
---------------
RQ3's primary figure is the steady-state footprint of the observability stack.
Until now it was derived from the pre-injection window of each RQ3 trial, and
that window turns out to be far too short to carry the claim. Measured across
the 12-trial post-fix dataset, the sampler's effective cadence is ~18 s rather
than the 5 s it is configured for, because Docker's

    GET /containers/{id}/stats?stream=false

blocks one to two seconds per container to produce the two readings a CPU delta
needs, and the nine containers are polled serially. The 30 s pre-trial
steady-state gate therefore yields a median of two samples per container per
trial (minimum one), so the first stage of the two-stage aggregation -- "median
within trial and phase" -- does almost no averaging at all.

This script removes the trial from the equation: it samples the idle stack
directly for a configurable duration, with no fault injected and no harness
running, and every sample is steady-state by construction. At the real ~18 s
cadence a ten-minute capture yields roughly 33 samples per container, which is
a defensible basis for a reported median and IQR.

Deliberately NOT parallelised
-----------------------------
Issuing the nine stats calls concurrently would bring the effective cadence
close to the configured 5 s. That is not done here, on purpose: the instrument
must stay identical to the one that produced the campaign's active-phase
figures, or the two phases in Table 5.8 would no longer be measured the same
way. Parallelising is a sensible change for a future campaign, not for a
re-measurement whose whole point is comparability.

What it does not do
-------------------
This is not the empty-host baseline of the cold-deploy procedure. That one runs
with the observability containers stopped and measures the bare host; this one
runs with the stack up and measures the stack. Neither substitutes for the
other.

Usage (on the monitoring host, stack up, no trial running)
----------------------------------------------------------
    cd /opt/orchestrator
    source venv/bin/activate
    export PYTHONPATH=/opt/orchestrator
    python3 experiments/trials/capture_steady_state.py --duration 600

    # then copy results/steady_state_capture.jsonl to the workstation

Exit codes: 0 capture complete; 1 Docker unreachable or no containers found.
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ", level=logging.INFO, stream=sys.stdout,
)
logger = logging.getLogger("capture_steady_state")


def main(argv: list[str] | None = None) -> int:
    from overhead_sampler import (
        _docker_get, _list_target_containers, _parse_docker_stats,
        OBSERVABILITY_CONTAINERS, MEASUREMENT_INSTRUMENT,
        SAMPLE_INTERVAL_SECONDS, HOST_VCPU, _RESULTS_DIR,
    )

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--duration", type=int, default=600,
                    help="Capture length in seconds (default 600).")
    ap.add_argument("--interval", type=float, default=SAMPLE_INTERVAL_SECONDS,
                    help="Requested interval; the real cadence is dominated by "
                         "Docker's own collection time and will be longer.")
    ap.add_argument("--out", default=None,
                    help="Output JSONL (default results/steady_state_capture.jsonl).")
    ap.add_argument("--host-mib", type=float, default=4096.0,
                    help="Memory capacity denominator in MiB (default 4096).")
    args = ap.parse_args(argv)

    out_path = Path(args.out) if args.out \
        else _RESULTS_DIR / "steady_state_capture.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        containers = _list_target_containers()
    except Exception as exc:                                       # noqa: BLE001
        logger.error("Cannot reach the Docker daemon: %s", exc)
        return 1
    if not containers:
        logger.error("No observability containers found. Is the stack up?")
        return 1

    missing = (OBSERVABILITY_CONTAINERS | {MEASUREMENT_INSTRUMENT}) - set(containers)
    if missing:
        logger.warning("Not running, will be absent from the capture: %s",
                       ", ".join(sorted(missing)))

    capture_id = f"steady-{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H%M%SZ')}"
    logger.info("Capture %s | %d containers | duration %ds",
                capture_id, len(containers), args.duration)
    logger.info("Nothing else should be running against this host for the "
                "duration -- no trial, no injection, no redeploy.")

    rows: list[dict] = []
    deadline = time.monotonic() + args.duration
    n_pass = 0
    with out_path.open("w", encoding="utf-8") as fh:
        while time.monotonic() < deadline:
            started = time.monotonic()
            ts = datetime.now(timezone.utc).isoformat()
            for name, cid in sorted(containers.items()):
                try:
                    stats_doc = _docker_get(f"/containers/{cid}/stats?stream=false")
                    parsed = _parse_docker_stats(stats_doc)
                except Exception as exc:                           # noqa: BLE001
                    logger.warning("sample failed | container=%s: %s", name, exc)
                    continue
                if parsed is None:
                    continue
                cpu, mem = parsed.get("cpu_cores"), parsed.get("rss_mib")
                # Same rejection rule the sampler applies: a negative delta is a
                # mid-restart capture, not a measurement.
                if cpu is None or mem is None or cpu < 0:
                    continue
                # trial_id (not capture_id): 04_overhead.py::aggregate_phase_samples()
                # groups stage-1 medians by this exact key. A single constant value
                # across the whole capture is correct, not a workaround -- it makes
                # every container's ~33-35 samples one "trial" for stage 1, so stage
                # 2's median-across-trials degenerates to that same real median
                # rather than being diluted across artificial trial boundaries.
                #
                # instrument must be a boolean flagging the MEASUREMENT INSTRUMENT
                # container specifically (cAdvisor), matching overhead_sampler.py's
                # own convention -- not a truthy data-source string. A truthy value
                # here for every row would silently exclude all eight real services
                # from the aggregate and misclassify them as the instrument instead.
                row = {
                    "trial_id": capture_id, "ts": ts, "container": name,
                    "phase": "steady", "cpu_cores": cpu, "rss_mib": mem,
                    "instrument": name == MEASUREMENT_INSTRUMENT,
                }
                fh.write(json.dumps(row) + "\n")
                rows.append(row)
            fh.flush()
            n_pass += 1
            elapsed = time.monotonic() - started
            if n_pass == 1:
                logger.info("First pass took %.1fs for %d containers -- that is "
                            "the real cadence floor.", elapsed, len(containers))
            time.sleep(max(0.0, args.interval - elapsed))

    if not rows:
        logger.error("Capture produced no usable samples.")
        return 1

    # ── Summary, in the units Table 5.8 uses ─────────────────────────────────
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["container"], []).append(r)

    def q(v, p):
        return float(statistics.quantiles(sorted(v), n=100)[p - 1]) if len(v) > 2 \
            else float(statistics.median(v))

    print("\n" + "=" * 78)
    print(f"RQ3 STEADY-STATE CAPTURE — {capture_id}")
    print(f"passes={n_pass}  samples={len(rows)}  duration={args.duration}s")
    print(f"observed cadence ≈ {args.duration / max(n_pass, 1):.1f}s per pass")
    print("=" * 78)
    print(f"{'container':<16}{'n':>4}{'CPU %2vCPU':>12}{'(IQR)':>9}"
          f"{'RSS MiB':>10}{'(IQR)':>9}")
    agg_cpu, agg_mem = 0.0, 0.0
    for name in sorted(by):
        cpus = [r["cpu_cores"] for r in by[name]]
        mems = [r["rss_mib"] for r in by[name]]
        cpu_med, mem_med = statistics.median(cpus), statistics.median(mems)
        cpu_iqr = q(cpus, 75) - q(cpus, 25)
        mem_iqr = q(mems, 75) - q(mems, 25)
        if name in OBSERVABILITY_CONTAINERS:
            agg_cpu += cpu_med
            agg_mem += mem_med
        tag = "  [instrument]" if name == MEASUREMENT_INSTRUMENT else ""
        print(f"{name:<16}{len(cpus):>4}{cpu_med / HOST_VCPU * 100:>12.2f}"
              f"{cpu_iqr / HOST_VCPU * 100:>9.2f}{mem_med:>10.1f}"
              f"{mem_iqr:>9.1f}{tag}")

    kib = statistics.median([r["cpu_cores"] for r in by["kibana"]]) \
        if "kibana" in by else 0.0
    print("-" * 78)
    print(f"{'EIGHT-SERVICE':<16}{'':>4}{agg_cpu / HOST_VCPU * 100:>12.2f}"
          f"{'':>9}{agg_mem:>10.1f}")
    print(f"  aggregate CPU  = {agg_cpu:.4f} cores "
          f"({agg_cpu / HOST_VCPU * 100:.2f}% of {HOST_VCPU} vCPU)")
    print(f"  aggregate RSS  = {agg_mem:.2f} MiB "
          f"({agg_mem / args.host_mib * 100:.2f}% of {args.host_mib:.0f} MiB)")
    if kib:
        print(f"  Kibana share of CPU aggregate = {kib / agg_cpu * 100:.1f}%")
        print(f"  other seven services          = {agg_cpu - kib:.4f} cores "
              f"({(agg_cpu - kib) / HOST_VCPU * 100:.2f}% of host)")
    print("=" * 78)
    print(f"\nwrote {out_path}")
    print("Report the eight-service aggregate WITH the Kibana split, never alone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
