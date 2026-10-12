#!/usr/bin/env python3
"""
experiments/trials/run_s3_trial.py — S3 (AWS Auto Scaling baseline) trial.

Two distinct measurement modes, because AWS Auto Scaling's remediation
mechanism is fundamentally different across fault classes:

instance_failure — genuine remediation, directly comparable to S1/S2.
    The ASG's own EC2 health check detects the terminated instance and
    launches a replacement — the fault is actually resolved, the same way
    S1's Terraform-replace resolves it. T_MTTR_seconds is real here and IS
    fed into RQ2's Wilcoxon signed-rank comparison against S1/S2.

cpu_overload / memory_exhaustion — NOT genuine remediation, NOT Wilcoxon-
    eligible. Per terraform/modules/asg_baseline/main.tf's own comments, ASG's
    only available response to these is scaling OUT (launching a SECOND
    instance) — it never touches the original, still-overloaded instance.
    The injected stress-ng process keeps running on that instance,
    undisturbed, until its own --timeout expires; ASG doesn't fix it, it
    routes around it. Reusing the T_MTTR "time until resolved" framing here
    would just measure our own injection script's --duration setting, not
    anything about AWS's actual behaviour — a meaningless number dressed up
    as a result.

    Instead this records T_capacity_restore_seconds (fault injection to a
    second instance InService+Healthy — the ASG's actual native response)
    and T_alarm_reaction_seconds (CloudWatch alarm entering ALARM state to
    capacity restored — isolates AWS's own reaction speed from whatever
    alarm-threshold tuning affects detection latency). T_MTTR_seconds is
    explicitly written as -1.0 (the same "not applicable" sentinel used
    throughout this schema) so a Wilcoxon script naively reading
    T_MTTR_seconds cannot accidentally pull these rows into RQ2's primary
    comparison. recovery_status is "capacity_added", not "success" — this
    is Chapter 6 supplementary-analysis material, demonstrating that native
    cloud auto-scaling restores serving capacity rather than performing
    in-place fault remediation, not a second S1-equivalent result.

Precondition: the S3 baseline ASG must already be provisioned —
    cd terraform/baseline && terraform apply

Usage:
    python3 experiments/trials/run_s3_trial.py --fault instance_failure
    python3 experiments/trials/run_s3_trial.py --fault cpu_overload
    python3 experiments/trials/run_s3_trial.py --fault memory_exhaustion --duration 120
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import boto3
import yaml

_REPO_ROOT      = Path(__file__).resolve().parent.parent.parent
_RESULTS_DIR    = _REPO_ROOT / "results"
_JSONL_PATH     = _RESULTS_DIR / "mttr_log_s3.jsonl"
_SCENARIOS_FILE = _REPO_ROOT / "experiments" / "inject" / "scenarios.yml"

_DEFAULT_ASG_NAME = "self-healing-thesis-experiment-asg-baseline"

_CAPACITY_ALARM_NAMES = {
    "cpu_overload":      "self-healing-thesis-experiment-asg-cpu-overload",
    "memory_exhaustion": "self-healing-thesis-experiment-asg-memory-exhaustion",
}

# Base fields shared by every S3 record (matches the other mttr_log_*.jsonl
# schemas for consistency); capacity-restore fields are appended only for
# cpu_overload/memory_exhaustion trials, left absent (not zero — genuinely
# not part of the schema) for instance_failure trials.
_RECORD_FIELDNAMES = [
    "trial_id", "system_under_test", "target", "fault_type", "actuator", "playbook",
    "t_fault_injected_ms", "t_detected_ms", "t_classified_ms", "t_remediated_ms",
    "t_observability_restored_ms", "t_validated_ms",
    "T_detect_seconds", "T_classify_seconds", "T_remediate_seconds",
    "T_observability_restore_seconds", "T_validate_seconds", "T_MTTR_seconds",
    "recovery_status", "retry_count", "circuit_breaker", "drift_detected",
    "escalation_reason",
]
_CAPACITY_FIELDNAMES = [
    "t_alarm_state_ms", "T_alarm_reaction_seconds", "T_capacity_restore_seconds",
]


def _get_asg_instance(asg_name: str, region: str) -> str:
    asg = boto3.client("autoscaling", region_name=region)
    resp = asg.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
    groups = resp.get("AutoScalingGroups", [])
    if not groups:
        raise RuntimeError(
            f"ASG {asg_name!r} not found — provision it first: "
            f"cd terraform/baseline && terraform apply"
        )
    instances = [i for i in groups[0]["Instances"] if i["LifecycleState"] == "InService"]
    if not instances:
        raise RuntimeError(f"No InService instance currently in ASG {asg_name!r}")
    return instances[0]["InstanceId"]


def _wait_for_replacement(
    asg_name: str, old_instance_id: str, region: str, timeout: int, poll_interval: int = 10,
) -> Optional[tuple[str, float]]:
    """Poll the ASG until a NEW instance ID is InService + Healthy.
    Returns (new_instance_id, monotonic_clear_time) or None on timeout."""
    asg = boto3.client("autoscaling", region_name=region)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = asg.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
        groups = resp.get("AutoScalingGroups", [])
        if groups:
            for inst in groups[0]["Instances"]:
                if (
                    inst["InstanceId"] != old_instance_id
                    and inst["LifecycleState"] == "InService"
                    and inst["HealthStatus"] == "Healthy"
                ):
                    return inst["InstanceId"], time.monotonic()
        time.sleep(poll_interval)
    return None


def _run_instance_failure(args, trial_id: str) -> dict:
    old_instance_id = _get_asg_instance(args.asg_name, args.region)
    print(f"ASG: {args.asg_name}  current instance: {old_instance_id}")

    ec2 = boto3.client("ec2", region_name=args.region)
    t0 = time.monotonic()
    t_fault_injected_ms = int(time.time() * 1000)
    print(f"Terminating {old_instance_id} at {datetime.now(timezone.utc).isoformat()} ...")
    ec2.terminate_instances(InstanceIds=[old_instance_id])

    print(f"Waiting for ASG to launch and health-check a replacement (timeout={args.timeout}s)...")
    result = _wait_for_replacement(args.asg_name, old_instance_id, args.region, args.timeout)

    if result is None:
        print("TIMEOUT waiting for replacement instance — recording as failure.", file=sys.stderr)
        recovery_status = "s3_validation_timeout"
        t_mttr_seconds = -1.0
        new_instance_id = ""
        t_validated_ms = -1
    else:
        new_instance_id, t_clear_mono = result
        recovery_status = "success"
        t_validated_ms = int(time.time() * 1000)
        t_mttr_seconds = round(t_clear_mono - t0, 3)
        print(f"Replacement instance {new_instance_id} is InService + Healthy. T_MTTR={t_mttr_seconds}s")

    record = {
        "trial_id": trial_id,
        "system_under_test": "S3",
        "target": args.asg_name,
        "fault_type": args.fault,
        "actuator": "aws_asg",
        "playbook": "",
        "t_fault_injected_ms": t_fault_injected_ms,
        "t_detected_ms": -1,
        "t_classified_ms": -1,
        "t_remediated_ms": t_validated_ms,
        "t_observability_restored_ms": -1,
        "t_validated_ms": t_validated_ms,
        "T_detect_seconds": -1.0,
        "T_classify_seconds": -1.0,
        "T_remediate_seconds": t_mttr_seconds,
        "T_observability_restore_seconds": -1.0,
        "T_validate_seconds": 0.0,
        "T_MTTR_seconds": t_mttr_seconds,
        "recovery_status": recovery_status,
        "retry_count": 0,
        "circuit_breaker": "n/a",
        "drift_detected": False,
        "escalation_reason": "" if recovery_status == "success" else "s3_validation_timeout",
    }
    print(f"\n=== Trial END | id={trial_id} T_MTTR={t_mttr_seconds}s status={recovery_status} "
          f"old={old_instance_id} new={new_instance_id or 'n/a'} ===")
    return record


def _inject_ssm_fault(instance_id: str, fault: str, duration: int, region: str, run_id: str) -> None:
    """Fire-and-forget SSM injection, reusing the exact scenario scripts
    inject_once.py uses — not waiting for completion, since that blocks for
    the fault's own --duration and we need to start polling for the alarm
    and scale-out immediately."""
    with open(_SCENARIOS_FILE, "r", encoding="utf-8") as fh:
        scenarios = yaml.safe_load(fh)["scenarios"]
    command_script = scenarios[fault]["command"]
    env_prefix = f"export DURATION={duration}\nexport RUN_ID={run_id}\n"
    ssm = boto3.client("ssm", region_name=region)
    ssm.send_command(
        InstanceIds=[instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [env_prefix + command_script]},
        Comment=f"S3 capacity-restore trial fault={fault} run_id={run_id}",
        TimeoutSeconds=max(duration + 180, 180),
    )


def _wait_for_alarm_state(
    alarm_name: str, region: str, timeout: int, min_timestamp_ms: float, poll_interval: int = 10,
) -> Optional[float]:
    """Poll until the named CloudWatch alarm enters ALARM state via a
    transition that happened at or after min_timestamp_ms (the caller's own
    fault-injection timestamp). Returns the AWS-reported
    StateUpdatedTimestamp as an epoch-ms int, or None on timeout.

    CONFIRMED LIVE: an earlier revision accepted any currently-ALARM state
    with no freshness check. Under the cpu_overload alarm's M-of-N config
    (evaluation_periods=2, datapoints_to_alarm=1), a fired alarm can take
    close to 2 minutes to cycle back to OK. When two reps run back-to-back,
    the next rep's poll can observe the PREVIOUS rep's still-cycling ALARM
    state and report it as its own -- producing a "fired" timestamp that
    predates this rep's own injection (T_alarm_reaction became meaningless,
    and the whole record measured nothing about this rep's fault). The
    min_timestamp_ms gate rejects any transition older than this rep's own
    injection and keeps polling past it, rather than accepting stale state."""
    cw = boto3.client("cloudwatch", region_name=region)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = cw.describe_alarms(AlarmNames=[alarm_name])
        for alarm in resp.get("MetricAlarms", []):
            if alarm["StateValue"] == "ALARM":
                state_ms = alarm["StateUpdatedTimestamp"].timestamp() * 1000
                if state_ms >= min_timestamp_ms:
                    return state_ms
        time.sleep(poll_interval)
    return None


def _wait_for_capacity_restored(
    asg_name: str, baseline_instance_id: str, region: str, timeout: int, poll_interval: int = 10,
) -> Optional[tuple[float, str]]:
    """Poll until a SECOND instance (distinct from the original, still-
    overloaded one) is InService + Healthy — i.e. the ASG has actually
    finished scaling out. Returns (monotonic time observed, new instance
    ID) or None on timeout. The new instance ID is needed by the caller to
    scale back down to exactly the pre-injection instance afterward."""
    asg = boto3.client("autoscaling", region_name=region)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = asg.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
        groups = resp.get("AutoScalingGroups", [])
        if groups:
            healthy = [
                i for i in groups[0]["Instances"]
                if i["LifecycleState"] == "InService" and i["HealthStatus"] == "Healthy"
            ]
            if len(healthy) >= 2:
                new_instances = [i["InstanceId"] for i in healthy if i["InstanceId"] != baseline_instance_id]
                if new_instances:
                    return time.monotonic(), new_instances[0]
        time.sleep(poll_interval)
    return None


def _scale_back_down(asg_name: str, new_instance_id: str, region: str) -> None:
    """Undo the scale-out this trial triggered, so the next rep starts from
    exactly one instance again. Nothing in this ASG's own configuration
    does this automatically: scale_in_on_recovery exists as a policy but no
    CloudWatch alarm is wired to invoke it (confirmed live — the ASG sat at
    2 healthy instances indefinitely after a cpu_overload trial until
    terminated by hand). Terminating the newly-added instance with
    ShouldDecrementDesiredCapacity=True both removes it and drops desired
    capacity back to 1 in a single call, rather than leaving desired at 2
    with only one instance running."""
    asg = boto3.client("autoscaling", region_name=region)
    print(f"Scaling back down: terminating {new_instance_id} (the instance this trial added)...")
    try:
        asg.terminate_instance_in_auto_scaling_group(
            InstanceId=new_instance_id, ShouldDecrementDesiredCapacity=True,
        )
    except Exception as exc:
        print(f"  WARNING: scale-back-down failed ({exc}) — check the ASG by hand "
              f"before the next trial.", file=sys.stderr)


def _wait_for_ssm_online(instance_id: str, region: str, timeout: int = 180, poll_interval: int = 5) -> bool:
    """EC2 LifecycleState=InService/HealthStatus=Healthy (the ASG's own
    checks) don't guarantee the SSM Agent has finished registering yet,
    especially on a freshly-launched instance — confirmed live:
    ssm.send_command raised InvalidInstanceId ("Instances not in a valid
    state for account") against an ASG instance that was already
    InService+Healthy by EC2's own accounting. Mirrors the same PingStatus
    check S1's Plan-layer Gate 3 (plan.py) performs before dispatch."""
    ssm = boto3.client("ssm", region_name=region)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
        )
        for info in resp.get("InstanceInformationList", []):
            if info.get("PingStatus") == "Online":
                return True
        time.sleep(poll_interval)
    return False


def _run_capacity_restore(args, trial_id: str) -> dict:
    baseline_instance_id = _get_asg_instance(args.asg_name, args.region)
    alarm_name = _CAPACITY_ALARM_NAMES[args.fault]
    print(f"ASG: {args.asg_name}  baseline instance: {baseline_instance_id}  alarm: {alarm_name}")
    print(
        f"NOTE: this fault is NOT fixed by ASG — {baseline_instance_id} stays overloaded until "
        f"its own --duration={args.duration}s expires. Measuring capacity-restore latency, not "
        f"T_MTTR (see module docstring)."
    )

    print(f"Waiting for SSM Agent on {baseline_instance_id} to be Online...")
    if not _wait_for_ssm_online(baseline_instance_id, args.region):
        raise RuntimeError(f"SSM Agent on {baseline_instance_id} never came Online")

    t0 = time.monotonic()
    t_fault_injected_ms = int(time.time() * 1000)
    print(f"Injecting {args.fault} on {baseline_instance_id} at {datetime.now(timezone.utc).isoformat()} ...")
    _inject_ssm_fault(baseline_instance_id, args.fault, args.duration, args.region, trial_id)

    print(f"Waiting for CloudWatch alarm {alarm_name!r} to enter ALARM state (timeout={args.timeout}s)...")
    t_alarm_state_ms = _wait_for_alarm_state(alarm_name, args.region, args.timeout, t_fault_injected_ms)
    if t_alarm_state_ms is None:
        print("TIMEOUT waiting for alarm to fire.", file=sys.stderr)
        return _build_capacity_record(
            trial_id, args, t_fault_injected_ms, None, None,
            recovery_status="s3_alarm_timeout", escalation_reason="s3_alarm_timeout",
        )
    print(f"Alarm fired at {datetime.fromtimestamp(t_alarm_state_ms / 1000, tz=timezone.utc).isoformat()}")

    print(f"Waiting for a second instance to be InService + Healthy (timeout={args.timeout}s)...")
    result = _wait_for_capacity_restored(
        args.asg_name, baseline_instance_id, args.region, args.timeout
    )
    if result is None:
        print("TIMEOUT waiting for scale-out to complete.", file=sys.stderr)
        return _build_capacity_record(
            trial_id, args, t_fault_injected_ms, t_alarm_state_ms, None,
            recovery_status="s3_capacity_timeout", escalation_reason="s3_capacity_timeout",
        )
    t_capacity_mono, new_instance_id = result

    t_capacity_restore_seconds = round(t_capacity_mono - t0, 3)
    t_alarm_reaction_seconds = round(
        t_capacity_mono - (t0 + (t_alarm_state_ms - t_fault_injected_ms) / 1000), 3
    )
    print(
        f"Capacity restored. T_capacity_restore={t_capacity_restore_seconds}s "
        f"T_alarm_reaction={t_alarm_reaction_seconds}s"
    )
    _scale_back_down(args.asg_name, new_instance_id, args.region)
    return _build_capacity_record(
        trial_id, args, t_fault_injected_ms, t_alarm_state_ms, t_capacity_mono,
        recovery_status="capacity_added", escalation_reason="",
        t_capacity_restore_seconds=t_capacity_restore_seconds,
        t_alarm_reaction_seconds=t_alarm_reaction_seconds,
        t0_mono=t0,
    )


def _build_capacity_record(
    trial_id, args, t_fault_injected_ms, t_alarm_state_ms, t_capacity_mono,
    recovery_status, escalation_reason,
    t_capacity_restore_seconds=-1.0, t_alarm_reaction_seconds=-1.0, t0_mono=None,
) -> dict:
    record = {
        "trial_id": trial_id,
        "system_under_test": "S3",
        "target": args.asg_name,
        "fault_type": args.fault,
        "actuator": "aws_asg",
        "playbook": "",
        "t_fault_injected_ms": t_fault_injected_ms,
        "t_detected_ms": int(t_alarm_state_ms) if t_alarm_state_ms is not None else -1,
        "t_classified_ms": -1,
        "t_remediated_ms": -1,
        "t_observability_restored_ms": -1,
        "t_validated_ms": int(time.time() * 1000) if t_capacity_mono is not None else -1,
        "T_detect_seconds": -1.0,
        "T_classify_seconds": -1.0,
        "T_remediate_seconds": -1.0,
        "T_observability_restore_seconds": -1.0,
        "T_validate_seconds": -1.0,
        # Deliberately -1.0: NOT a real MTTR measurement, NOT Wilcoxon-
        # eligible for RQ2 — see module docstring. Use
        # T_capacity_restore_seconds / T_alarm_reaction_seconds instead.
        "T_MTTR_seconds": -1.0,
        "recovery_status": recovery_status,
        "retry_count": 0,
        "circuit_breaker": "n/a",
        "drift_detected": False,
        "escalation_reason": escalation_reason,
        "t_alarm_state_ms": int(t_alarm_state_ms) if t_alarm_state_ms is not None else -1,
        "T_alarm_reaction_seconds": t_alarm_reaction_seconds,
        "T_capacity_restore_seconds": t_capacity_restore_seconds,
    }
    return record


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run one S3 (AWS Auto Scaling) trial.")
    parser.add_argument(
        "--fault", required=True,
        choices=["instance_failure", "cpu_overload", "memory_exhaustion"],
    )
    parser.add_argument("--asg-name", default=_DEFAULT_ASG_NAME)
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument("--trial-id", default=None)
    parser.add_argument("--duration", type=int, default=120,
                         help="cpu_overload/memory_exhaustion fault duration (ignored for instance_failure)")
    parser.add_argument("--timeout", type=int, default=600,
                         help="Max seconds to wait at each polling stage")
    args = parser.parse_args(argv)

    trial_id = args.trial_id or f"s3-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-{args.fault}-{int(time.time())}"

    if args.fault == "instance_failure":
        record = _run_instance_failure(args, trial_id)
        fieldnames = _RECORD_FIELDNAMES
    else:
        record = _run_capacity_restore(args, trial_id)
        fieldnames = _RECORD_FIELDNAMES + _CAPACITY_FIELDNAMES

    _RESULTS_DIR.mkdir(exist_ok=True)
    with _JSONL_PATH.open("a") as fh:
        fh.write(json.dumps({k: record[k] for k in fieldnames}) + "\n")

    print(f"Record appended to {_JSONL_PATH}")
    return 0 if record["recovery_status"] in ("success", "capacity_added") else 1


if __name__ == "__main__":
    sys.exit(main())
