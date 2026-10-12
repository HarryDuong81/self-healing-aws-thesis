#!/usr/bin/env python3
"""
experiments/trials/run_manual_trial.py — Guided S2 (manual SRE baseline) trial.

Wraps a single manual-remediation trial: injects a fault, prints the exact
commands a human operator should run to fix it (mirroring what the
equivalent Ansible playbook does — see experiments/inject/scenarios.yml's
own `cleanup:` blocks), waits for the operator to actually perform the fix
in a separate terminal and confirm, polls Prometheus's canonical ALERTS
series for t_clear (the same signal S1's HealthChecker uses — 
AD-008), computes T_MTTR, and appends a JSON-Lines record to
results/mttr_log_s2.jsonl in a schema comparable to the S1 log
(orchestrator/mttr_logger.py RECORD_FIELDNAMES).

This script never executes the fix itself — that would defeat the point of
a *manual* baseline. It only times the human's own actions.

Precondition: the orchestrator must be stopped first, so it cannot race the
human to the fix:
    sudo systemctl stop orchestrator
This script checks that and aborts (unless --force) if it's still running.

Caveat — self-clearing faults: cpu_overload / memory_exhaustion /
disk_saturation clear on their own after --duration seconds regardless of
the operator's action (stress-ng's own --timeout / the fill script's own
sleep, not this harness). Pick a --duration comfortably longer than you
expect the manual fix to take, or the measured time will be contaminated by
the fault's own auto-expiry rather than genuine human response time. This
script defaults to 600s for those three fault classes (vs. the 120s used in
S1 trials) and prints a reminder.

Only T_MTTR (total) is treated as directly comparable to S1's T_MTTR for
RQ1/RQ2 — the phase breakdown doesn't map cleanly onto a human operator, so
T_detect/T_classify are recorded as not-applicable (-1) and T_remediate is
approximated as "time from injection to the operator confirming the fix is
applied," with T_validate covering the remaining wait for Prometheus to
confirm the alert has actually cleared.

Usage:
    python3 experiments/trials/run_manual_trial.py \\
        --fault cpu_overload --target target-1

    python3 experiments/trials/run_manual_trial.py \\
        --fault instance_failure --target target-2 --force
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import boto3

_REPO_ROOT   = Path(__file__).resolve().parent.parent.parent
_RESULTS_DIR = _REPO_ROOT / "results"
_JSONL_PATH  = _RESULTS_DIR / "mttr_log_s2.jsonl"

_TARGET_NAME_PREFIX = "self-healing-thesis-experiment-"

# Self-clearing fault classes (see module docstring caveat) get a generous
# default duration so the operator's own action — not the injector's own
# timeout — is what actually ends the fault.
_DEFAULT_DURATION = {
    "cpu_overload":        600,
    "memory_exhaustion":   600,
    "disk_saturation":     600,
    "service_crash":       0,
    "configuration_drift": 0,
    "instance_failure":    0,
}

# Manual fix commands, mirroring each fault's Ansible playbook / the
# equivalent cleanup: block in experiments/inject/scenarios.yml. Printed
# for the operator to run themselves via a separate SSM session — this
# script does not execute them.
_MANUAL_FIX_COMMANDS = {
    "cpu_overload":        ["pkill -x stress-ng"],
    "memory_exhaustion":   ["pkill -x stress-ng"],
    "disk_saturation":     ["rm -f /var/tmp/self-healing-disk-fill"],
    "service_crash":       ["systemctl start nginx"],
    "configuration_drift": [
        "mv /etc/nginx/conf.d/healthz.conf.bak /etc/nginx/conf.d/healthz.conf",
        "systemctl reload nginx",
    ],
}

_RECORD_FIELDNAMES = [
    "trial_id", "system_under_test", "target", "fault_type", "block", "actuator", "playbook",
    "t_fault_injected_ms", "t_detected_ms", "t_classified_ms", "t_remediated_ms",
    "t_observability_restored_ms", "t_validated_ms",
    "T_detect_seconds", "T_classify_seconds", "T_remediate_seconds",
    "T_observability_restore_seconds", "T_validate_seconds", "T_MTTR_seconds",
    "recovery_status", "retry_count", "circuit_breaker", "drift_detected",
    "escalation_reason",
]


def _orchestrator_is_active() -> bool:
    result = subprocess.run(
        ["systemctl", "is-active", "orchestrator"],
        capture_output=True, text=True,
    )
    return result.stdout.strip() == "active"


def _resolve_instance_id(target: str, region: str) -> str:
    ec2 = boto3.client("ec2", region_name=region)
    resp = ec2.describe_instances(Filters=[
        {"Name": "tag:TargetName", "Values": [target]},
        {"Name": "instance-state-name", "Values": ["running"]},
    ])
    candidates = [
        i for r in resp["Reservations"] for i in r["Instances"]
    ]
    if not candidates:
        raise RuntimeError(f"No running instance found for target={target}")
    candidates.sort(key=lambda i: i["LaunchTime"], reverse=True)
    return candidates[0]["InstanceId"]


def _inject_fault_async(target: str, fault: str, duration: int, trial_id: str, region: str) -> None:
    """Fire-and-forget: submits the injection without blocking on its own
    completion (duration-based faults' SSM command otherwise blocks for the
    full --duration, which would stall this script waiting for the operator
    to act first)."""
    inject_script = _REPO_ROOT / "experiments" / "inject" / "inject_once.py"
    cmd = [
        sys.executable, str(inject_script),
        "--target", target, "--fault", fault,
        "--duration", str(duration), "--region", region,
        "--run-id", trial_id,
    ]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _query_alerts_firing(prometheus_url: str, fault_type: str, node_name: str) -> bool:
    import urllib.parse
    import urllib.request

    query = f'ALERTS{{fault_type="{fault_type}",node_name="{node_name}"}}'
    url = f"{prometheus_url}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read())
    return bool(data.get("data", {}).get("result"))


def _wait_for_alert_clear(prometheus_url: str, fault_type: str, node_name: str, timeout: int) -> Optional[float]:
    """Poll until the alert stops firing. Returns the monotonic timestamp
    it cleared at, or None on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if not _query_alerts_firing(prometheus_url, fault_type, node_name):
                return time.monotonic()
        except Exception as exc:
            print(f"  (poll error, retrying: {exc})", file=sys.stderr)
        time.sleep(5)
    return None


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run one guided S2 (manual SRE) trial.")
    parser.add_argument("--fault", required=True, choices=list(_DEFAULT_DURATION))
    parser.add_argument("--target", required=True, choices=["target-1", "target-2", "target-3"])
    parser.add_argument("--duration", type=int, default=None,
                         help="Fault duration override (default: see module docstring)")
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument("--prometheus-url", default="http://localhost:9090")
    parser.add_argument("--trial-id", default=None)
    parser.add_argument("--block", type=int, required=True,
                        help="Schedule block index from results/s2_schedule.jsonl. "
                             "Required: (fault_type, target, block) is the "
                             "experimental-unit key that pairs this S2 trial with "
                             "its S1 counterpart for the H1 analysis.")
    parser.add_argument("--force", action="store_true",
                         help="Skip the orchestrator-stopped precondition check")
    parser.add_argument("--timeout", type=int, default=600,
                         help="Max seconds to wait for the alert to clear after confirmation")
    args = parser.parse_args(argv)

    if not args.force and _orchestrator_is_active():
        print(
            "ERROR: orchestrator.service is still active. It will race you to the fix, "
            "invalidating the manual-response measurement.\n"
            "Stop it first:  sudo systemctl stop orchestrator\n"
            "(or pass --force if you've already accounted for this)",
            file=sys.stderr,
        )
        return 2

    duration = args.duration if args.duration is not None else _DEFAULT_DURATION[args.fault]
    trial_id = args.trial_id or f"s2-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-{args.fault}-{args.target}"
    node_name = f"{_TARGET_NAME_PREFIX}{args.target}"

    instance_id = _resolve_instance_id(args.target, args.region)
    print(f"Target: {args.target} ({instance_id})")
    print(f"Fault:  {args.fault}  (duration={duration}s, trial_id={trial_id})")

    if args.fault in _DEFAULT_DURATION and duration > 0:
        print(
            f"NOTE: this fault self-clears after {duration}s regardless of your action — "
            "complete the manual fix well before then."
        )

    t0 = time.monotonic()
    t_fault_injected_ms = int(time.time() * 1000)
    print(f"\nInjecting fault at {datetime.now(timezone.utc).isoformat()} ...")
    _inject_fault_async(args.target, args.fault, duration, trial_id, args.region)
    time.sleep(5)  # let the SSM command actually start before the operator acts

    print("\n" + "=" * 70)
    if args.fault == "instance_failure":
        print("MANUAL FIX — instance_failure has no SSM fix command; a human")
        print("would recreate the instance directly, e.g.:")
        print(f"  cd terraform/targets && terraform apply "
              f"-replace='module.compute.aws_instance.targets[\"{args.target}\"]' -auto-approve")
    else:
        print(f"MANUAL FIX — run this on {instance_id} via a separate SSM session:")
        print(f"  aws ssm send-command --instance-ids {instance_id} "
              f"--document-name \"AWS-RunShellScript\" --parameters 'commands={json.dumps(_MANUAL_FIX_COMMANDS[args.fault])}' "
              f"--output text --query \"Command.CommandId\"")
    print("=" * 70)
    input("\nPress Enter once you have completed the manual fix... ")

    t_confirmed_ms = int(time.time() * 1000)
    t_remediate_seconds = round((t_confirmed_ms - t_fault_injected_ms) / 1000, 3)

    print(f"\nWaiting for Prometheus to confirm the alert has cleared (timeout={args.timeout}s)...")
    t_clear_mono = _wait_for_alert_clear(args.prometheus_url, args.fault, node_name, args.timeout)

    if t_clear_mono is None:
        print("TIMEOUT waiting for alert to clear — recording as failure.", file=sys.stderr)
        recovery_status = "manual_validation_timeout"
        t_mttr_seconds = -1.0
        t_validate_seconds = -1.0
        t_validated_ms = -1
    else:
        recovery_status = "success"
        t_validated_ms = int(time.time() * 1000)
        t_mttr_seconds = round((t_clear_mono - t0), 3)
        t_validate_seconds = round(t_mttr_seconds - t_remediate_seconds, 3)

    record = {
        "trial_id": trial_id,
        "system_under_test": "S2",
        "target": node_name,
        "fault_type": args.fault,
        # Experimental-unit identity: pairs this S2 observation with the S1
        # trial carrying the same (fault_type, target, block).
        "block": args.block,
        "actuator": "manual",
        "playbook": "",
        "t_fault_injected_ms": t_fault_injected_ms,
        "t_detected_ms": -1,
        "t_classified_ms": -1,
        "t_remediated_ms": t_confirmed_ms,
        "t_observability_restored_ms": -1,
        "t_validated_ms": t_validated_ms,
        "T_detect_seconds": -1.0,
        "T_classify_seconds": -1.0,
        "T_remediate_seconds": t_remediate_seconds,
        "T_observability_restore_seconds": -1.0,
        "T_validate_seconds": t_validate_seconds,
        "T_MTTR_seconds": t_mttr_seconds,
        "recovery_status": recovery_status,
        "retry_count": 0,
        "circuit_breaker": "n/a",
        "drift_detected": False,
        "escalation_reason": "" if recovery_status == "success" else "manual_validation_timeout",
    }

    _RESULTS_DIR.mkdir(exist_ok=True)
    with _JSONL_PATH.open("a") as fh:
        fh.write(json.dumps({k: record[k] for k in _RECORD_FIELDNAMES}) + "\n")

    print(f"\n=== Trial END | id={trial_id} T_MTTR={t_mttr_seconds}s status={recovery_status} ===")
    print(f"Record appended to {_JSONL_PATH}")
    return 0 if recovery_status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
