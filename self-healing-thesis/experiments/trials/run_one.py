#!/usr/bin/env python3
"""
experiments/trials/run_one.py — Execute a single instrumented trial.

Injects the specified fault onto the target, waits for orchestrator remediation,
polls the MTTR log for the result record, and verifies the SLO assertion.

S1 is the only valid --system value. Neither baseline can be run through this
script, for the same underlying reason in both cases: this script always injects
into target-1/2/3 and always waits for a record the orchestrator writes, and the
orchestrator stamps every record it writes with SYSTEM_UNDER_TEST, which every
real deployment leaves at "S1".

  S2 (manual SRE baseline) requires a human to perform the remediation with the
  orchestrator stopped, which an automated script cannot do. Use
  experiments/trials/run_manual_trial.py.

  S3 (AWS Auto Scaling baseline) requires injecting into the ASG's own instance,
  which inject_once.py cannot address — it resolves targets by the TargetName
  tag, and the ASG's instance carries none. Use
  experiments/trials/run_s3_trial.py.

Deployment-session fix: --system used to accept "S3", but the flag never
branched any behaviour — it reached one log line and nothing else. A trial run
as "S3" therefore injected into a normal target, was remediated by the
orchestrator as ordinary S1 work, and was recorded as S1. The value was removed
from choices rather than wired up, matching the earlier removal of "S2".

Usage:
    # Single S1 trial (self-healing pipeline):
    python experiments/trials/run_one.py \\
        --system S1 --fault cpu_overload --target target-1

    # With staging workspace:
    python experiments/trials/run_one.py \\
        --system S1 --fault service_crash --target target-2 \\
        --workspace staging

    # Override durations and output:
    python experiments/trials/run_one.py \\
        --system S1 --fault memory_exhaustion --target target-3 \\
        --duration 120 --trial-id custom-trial-001

Exit codes:
    0 — trial completed (result is in MTTR log regardless of SLO pass/fail)
    1 — trial failed (injection or monitoring error)
    2 — configuration / argument error

Thesis reference: §7.4 Run a single trial against staging
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("run_one")

_CONFIG_FILE  = Path(__file__).parent / "trial_config.yml"
_RESULTS_DIR  = _REPO_ROOT / "results"
_JSONL_PATH   = _RESULTS_DIR / "mttr_log.jsonl"
_SLO_SECONDS  = 300

# instance_failure's total wall-clock time (T_detect + T_remediate +
# T_observability_restore + T_validate) can exceed _SLO_SECONDS + 120s on
# its own — confirmed live: T_detect alone measured ~137s (vs. the ~60-75s
# the shared timeout assumed), and T_observability_restore adds another
# ~200s+ on top of T_remediate + T_validate (dominated by a freshly
# recreated instance's own cloud-init wait inside diagnostic_agents.yml's
# apt_lock_guard role). The shared 420s budget the other five fault classes
# use is too tight for this one — it was cutting off in-flight remediations
# before their terminal record was written, leaving only a duplicate
# alert-delivery's fast lease_held rejection to be found instead.
_MTTR_WAIT_TIMEOUT_DEFAULT          = _SLO_SECONDS + 120
_MTTR_WAIT_TIMEOUT_INSTANCE_FAILURE = 900


def _load_config() -> dict:
    with open(_CONFIG_FILE, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _inject_fault(
    target: str,
    fault: str,
    duration: int,
    trial_id: str,
    region: str,
    block: int = -1,
) -> None:
    """Call inject_once.py as a subprocess, passing trial_id as run-id."""
    import subprocess
    inject_script = _REPO_ROOT / "experiments" / "inject" / "inject_once.py"
    cmd = [
        sys.executable, str(inject_script),
        "--target",   target,
        "--fault",    fault,
        "--duration", str(duration),
        "--region",   region,
        "--run-id",   trial_id,
        "--block",    str(block),
    ]
    logger.info("Injecting | cmd=%s", " ".join(cmd))
    result = subprocess.run(cmd, timeout=duration + 180)
    if result.returncode not in (0,):
        raise RuntimeError(f"inject_once.py exited {result.returncode}")


def _wait_for_mttr_record(
    trial_id: str,
    timeout: int = 400,
    poll_interval: int = 5,
) -> Optional[dict]:
    """
    Poll the MTTR JSONL log for a record matching trial_id.
    Returns the record dict if found within timeout, else None.

    Alertmanager can re-deliver the same firing alert more than once before
    it resolves; every delivery now correlates to the SAME trial_id (see
    orchestrator/mttr_logger.py's correlation-file lookup), so more than one
    record can share a trial_id — one real remediation attempt plus zero or
    more "lease_held" duplicates that the Plan-layer lease gate correctly
 skips . Confirmed live: the duplicate's aborted_plan
    record can be appended to the JSONL log before the real attempt's
    terminal record (Ansible + validation takes longer than an instant lease
    rejection), so a naive first-match scan can return the duplicate's
    aborted_plan instead of the trial's actual outcome. Skip lease_held
    records and keep polling for the real terminal record; only fall back to
    a lease_held record if nothing better ever arrives before the deadline.
    """
    deadline = time.monotonic() + timeout
    fallback: Optional[dict] = None
    while time.monotonic() < deadline:
        if _JSONL_PATH.exists():
            for line in _JSONL_PATH.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("trial_id") != trial_id:
                    continue
                if rec.get("escalation_reason") == "lease_held":
                    fallback = rec
                    continue
                return rec
        time.sleep(poll_interval)
    return fallback


def run_trial(
    system: str,
    fault: str,
    target: str,
    duration: int,
    trial_id: str,
    prometheus_url: str,
    region: str,
    block: int = -1,
) -> dict:
    """
    Execute one trial: inject → wait for record → validate SLO.

    Returns the MTTR record dict.
    Raises RuntimeError if the record is not found within the timeout.
    """
    from experiments.trials.wait_steady_state import wait_steady_state

    logger.info(
        "=== Trial START | id=%s system=%s fault=%s target=%s ===",
        trial_id, system, fault, target,
    )

    # Pre-trial steady-state gate
    steady = wait_steady_state(
        window=30,
        timeout=180,
        prometheus_url=prometheus_url,
    )
    if not steady:
        raise RuntimeError("Pre-trial steady state not achieved — aborting trial")

    t_inject = datetime.now(timezone.utc)
    logger.info("t_inject=%s", t_inject.isoformat())

    _inject_fault(target, fault, duration, trial_id, region, block)

    record_wait_timeout = (
        _MTTR_WAIT_TIMEOUT_INSTANCE_FAILURE if fault == "instance_failure"
        else _MTTR_WAIT_TIMEOUT_DEFAULT
    )
    logger.info(
        "Waiting for MTTR record | trial_id=%s timeout=%ds",
        trial_id, record_wait_timeout,
    )
    rec = _wait_for_mttr_record(trial_id, timeout=record_wait_timeout)

    if rec is None:
        raise RuntimeError(
            f"MTTR record not found for trial_id={trial_id} within timeout"
        )

    t_mttr = rec.get("T_MTTR_seconds", -1)
    status = rec.get("recovery_status", "unknown")

    slo_pass = t_mttr < _SLO_SECONDS and t_mttr > 0
    logger.info(
        "=== Trial END | id=%s T_MTTR=%.1fs status=%s SLO=%s ===",
        trial_id, t_mttr, status, "PASS" if slo_pass else "FAIL",
    )

    # SLO assertion — acceptance criterion
    assert rec["T_MTTR_seconds"] < _SLO_SECONDS, (
        f"SLO violation: T_MTTR={t_mttr}s >= {_SLO_SECONDS}s for trial {trial_id}"
    )

    return rec


def main(argv: Optional[list[str]] = None) -> int:
    cfg = _load_config()
    valid_faults = list(cfg["scenarios"]) if "scenarios" in cfg else [
        "cpu_overload", "memory_exhaustion", "disk_saturation",
        "service_crash", "configuration_drift", "instance_failure",
    ]

    parser = argparse.ArgumentParser(
        description="Run a single instrumented trial against a target instance."
    )
    parser.add_argument("--system",    required=True, choices=["S1"],
                        help="S1 only. S3 goes through run_s3_trial.py and S2 "
                             "through run_manual_trial.py — see the module docstring.")
    parser.add_argument("--fault",     required=True, choices=valid_faults)
    parser.add_argument("--target",    required=True,
                        choices=["target-1", "target-2", "target-3"])
    parser.add_argument("--duration",  type=int, default=None,
                        help="Fault duration in seconds (overrides config default)")
    parser.add_argument("--trial-id",  default=None,
                        help="Explicit trial ID (auto-generated if omitted)")
    parser.add_argument("--workspace", default="production",
                        help="Terraform workspace label (default: production)")
    parser.add_argument("--prometheus-url", default="http://localhost:9090")
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument("--block", type=int, default=-1,
                        help="Campaign schedule block index; part of the "
                             "(fault_type, target, block) experimental-unit key "
                             "used for prospective S1-S2 pairing. Default -1 = "
                             "ad-hoc trial, ineligible for the paired analysis.")
    args = parser.parse_args(argv)

    trial_id = args.trial_id or (
        f"t-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-{str(uuid.uuid4())[:8]}"
    )
    fault_durations = cfg.get("fault_durations", {})
    duration = args.duration if args.duration is not None \
               else fault_durations.get(args.fault, 120)

    sys.path.insert(0, str(_REPO_ROOT))

    try:
        rec = run_trial(
            system=args.system,
            fault=args.fault,
            target=args.target,
            duration=duration,
            trial_id=trial_id,
            prometheus_url=args.prometheus_url,
            region=args.region,
            block=args.block,
        )
        logger.info("Trial record: %s", json.dumps(rec, indent=2))
        return 0
    except AssertionError as exc:
        logger.error("SLO assertion failed: %s", exc)
        return 1
    except Exception as exc:
        logger.error("Trial error: %s", exc, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
