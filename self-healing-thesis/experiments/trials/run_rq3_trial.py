#!/usr/bin/env python3
"""
experiments/trials/run_rq3_trial.py — Standalone scoped RQ3 measurement trial.

Wraps a single run_one.py-style trial (inject -> wait for orchestrator
remediation -> SLO check) with OverheadSampler, producing phase-tagged
CPU/RSS samples in results/overhead_samples.jsonl -- without touching any of
run_campaign.py's machinery. No schedule is built; no s2_schedule.jsonl or
campaign_run.jsonl is written.

Why this exists (deployment-session finding, 2026-09-21): the official
60-trial S1 campaign produced zero overhead_samples.jsonl rows, because
overhead_sampler.py originally read cAdvisor's REST API, and cAdvisor
v0.49.1 cannot register any docker-*.scope container under this host's
containerd-snapshotter Docker 28.5.2 image store (confirmed live: "failed to
identify the read-write layer ID", and confirmed unrecoverable retrospectively
via Prometheus -- see overhead_sampler.py's own module docstring for the full
diagnosis). overhead_sampler.py was rewritten to read the Docker Engine API
directly instead, which sidesteps that failure entirely. This script exists
to drive a small, separate re-measurement under the fixed sampler, without
re-running or contaminating the official, already-validated 60-trial dataset.

Deliberately reuses run_one.py's own run_trial() rather than re-implementing
fault injection / MTTR-record polling -- that logic is already correct and
duplicating it here would just be a second place for the same bugs to recur.

block is not exposed as a CLI argument and is always -1 (inject_once.py's own
default). An RQ3 trial is deliberately not part of the paired H1/H2 campaign
schedule (§13.1's sentinel for "non-pairable/non-campaign"), so there is no
legitimate value to pass other than -1.

The resulting MTTR record lands in the same official results/mttr_log.jsonl
the S1 campaign uses -- there is no way to route it elsewhere while
orchestrator.service is live, since the orchestrator's own RESULTS_DIR comes
from /etc/orchestrator's environment file, not this process's shell
environment (confirmed live; setting RESULTS_DIR here would only redirect
this harness's correlation.jsonl write, producing a silent AD-010 correlation
miss against the orchestrator's real one -- the opposite of isolation). This
is safe by construction, not by operator discipline: analysis/02_wilcoxon.py's
_arm() already excludes every block<=0 record from both H1 and H2's
populations before either hypothesis test runs.

orchestrator.service is left running deliberately. RQ3's active-phase figure
is meant to capture genuine remediation load (elevated target metrics, alert
evaluation, the Ansible-generated syslog Filebeat ships during remediation),
not a quiescent stack. Stopping the orchestrator for a "cleaner" run would
produce a less honest number, not a more correct one.

Usage:
    python experiments/trials/run_rq3_trial.py \\
        --fault cpu_overload --target target-1

    python experiments/trials/run_rq3_trial.py \\
        --fault instance_failure --target target-2 --duration 180

Run once per trial -- the same operational rhythm as every other trial in
this project's protocol, not a batch loop. Ten to fifteen invocations across
a representative mix of fault classes is the intended scope; RQ3 needs
distributional coverage of elevated load, not the per-class balance RQ1/RQ2
require.

Exit codes: 0 trial completed and sampler flushed; 1 trial or sampler error.
"""
from __future__ import annotations

import argparse
import logging
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("run_rq3_trial")

_RQ3_BLOCK = -1  # never a real schedule block -- see module docstring


def main(argv: Optional[list[str]] = None) -> int:
    from run_one import _load_config, run_trial
    from overhead_sampler import OverheadSampler

    cfg = _load_config()
    valid_faults = list(cfg["scenarios"]) if "scenarios" in cfg else [
        "cpu_overload", "memory_exhaustion", "disk_saturation",
        "service_crash", "configuration_drift", "instance_failure",
    ]

    parser = argparse.ArgumentParser(
        description="Run one scoped RQ3 resource-overhead measurement trial "
                     "(sampler start -> inject -> wait -> sampler stop)."
    )
    parser.add_argument("--fault", required=True, choices=valid_faults)
    parser.add_argument("--target", required=True,
                         choices=["target-1", "target-2", "target-3"])
    parser.add_argument("--duration", type=int, default=None,
                         help="Fault duration in seconds (overrides config default)")
    parser.add_argument("--trial-id", default=None)
    parser.add_argument("--prometheus-url", default="http://localhost:9090")
    parser.add_argument("--region", default="ap-southeast-1")
    args = parser.parse_args(argv)

    trial_id = args.trial_id or (
        f"rq3-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-{str(uuid.uuid4())[:8]}"
    )
    fault_durations = cfg.get("fault_durations", {})
    duration = args.duration if args.duration is not None \
               else fault_durations.get(args.fault, 120)

    logger.info("=== RQ3 trial START | id=%s fault=%s target=%s block=%d (non-campaign) ===",
                trial_id, args.fault, args.target, _RQ3_BLOCK)

    sampler = OverheadSampler(trial_id=trial_id, fault_type=args.fault)
    sampler.start()
    try:
        rec = run_trial(
            system="S1", fault=args.fault, target=args.target,
            duration=duration, trial_id=trial_id,
            prometheus_url=args.prometheus_url, region=args.region,
            block=_RQ3_BLOCK,
        )
        logger.info(
            "=== RQ3 trial END | id=%s T_MTTR=%.1fs status=%s ===",
            trial_id, rec.get("T_MTTR_seconds", -1), rec.get("recovery_status"),
        )
        return 0
    except AssertionError as exc:
        logger.error("SLO assertion failed: %s", exc)
        return 1
    except Exception as exc:                                       # noqa: BLE001
        logger.error("RQ3 trial error: %s", exc, exc_info=True)
        return 1
    finally:
        n = sampler.stop()
        logger.info("Overhead sampler flushed %d rows for trial_id=%s", n, trial_id)


if __name__ == "__main__":
    sys.exit(main())
