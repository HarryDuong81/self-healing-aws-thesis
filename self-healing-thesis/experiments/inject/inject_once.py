#!/usr/bin/env python3
"""
experiments/inject/inject_once.py — Single-shot fault injector.

Submits an AWS SSM SendCommand (or EC2 TerminateInstances for instance_failure)
to inject one of the six canonical fault classes onto a named target instance.
Bypasses the campaign runner — intended for development smoke tests and CI.

Usage:
    python experiments/inject/inject_once.py \\
        --target target-2 \\
        --fault  cpu_overload \\
        --duration 60

    # instance_failure terminates via EC2 API, no duration:
    python experiments/inject/inject_once.py \\
        --target target-1 \\
        --fault  instance_failure

Connection model:
    Faults 1–5 (Ansible path) → SSM SendCommand → SSM Agent on target
    Fault 6 (instance_failure) → EC2 TerminateInstances API directly

Exit codes:
    0 — command submitted and completed successfully (or EC2 terminated)
    1 — SSM command failed / timed out
    2 — argument / configuration error

Thesis reference: §7.3 Inject a new fault for a smoke test
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Optional

import boto3
import yaml

_REPO_ROOT_FOR_IMPORT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT_FOR_IMPORT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT_FOR_IMPORT))
from experiments.correlation.correlate import (  # noqa: E402
    DuplicateInjectionError, assert_no_recent_injection, record_injection,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("inject_once")

_REPO_ROOT     = Path(__file__).resolve().parent.parent.parent
_SCENARIOS_FILE = Path(__file__).parent / "scenarios.yml"
_REGION        = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")

# Resolved at runtime from Terraform output or environment
_S3_BUCKET     = os.environ.get("SSM_BUCKET", "")


def _load_scenarios() -> dict:
    with open(_SCENARIOS_FILE, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data


def _resolve_instance_id(target: str, region: str) -> str:
    """
    Resolve a logical target name (target-1, target-2, target-3) to an EC2
    instance ID by looking up the TargetName tag.
    """
    ec2 = boto3.client("ec2", region_name=region)
    resp = ec2.describe_instances(
        Filters=[
            # NOT tag:NodeName — target instances have no such tag at all
            # (only the monitoring instance does, value "monitoring").
            # Target instances are tagged TargetName=target-N. Confirmed
            # live: every one of the first ~35 campaign trials failed
            # fault injection with "No running instance found with tag
            # NodeName=...", 100% failure rate, because this filter was
            # querying a tag key that doesn't exist on any target.
            {"Name": "tag:TargetName",          "Values": [target]},
            {"Name": "instance-state-name",     "Values": ["running"]},
            {"Name": "tag:Project",             "Values": ["self-healing-thesis"]},
        ]
    )
    reservations = resp.get("Reservations", [])
    if not reservations:
        raise ValueError(
            f"No running instance found with tag TargetName={target!r} "
            f"in project self-healing-thesis ({region})"
        )
    instance_id = reservations[0]["Instances"][0]["InstanceId"]
    logger.info("Resolved %s -> %s", target, instance_id)
    return instance_id


def _wait_ssm_command(
    ssm: "boto3.client",
    command_id: str,
    instance_id: str,
    poll_interval: int = 5,
    timeout_seconds: int = 300,
) -> dict:
    """Poll until the SSM command reaches a terminal state or times out."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            resp = ssm.get_command_invocation(
                CommandId=command_id,
                InstanceId=instance_id,
            )
        except ssm.exceptions.InvocationDoesNotExist:
            # SendCommand returns a CommandId immediately, but the
            # invocation record isn't always queryable right away — a
            # brief propagation delay before the first successful
            # GetCommandInvocation call is expected AWS behavior, not a
            # real failure. Confirmed live: calling this with zero delay
            # after SendCommand raised InvocationDoesNotExist outright.
            logger.info("SSM command %s not yet registered - retrying", command_id)
            time.sleep(poll_interval)
            continue
        status = resp["StatusDetails"]
        logger.info("SSM command %s status: %s", command_id, status)
        if status in ("Success", "Failed", "Cancelled", "TimedOut", "DeliveryTimedOut"):
            return resp
        time.sleep(poll_interval)
    raise TimeoutError(
        f"SSM command {command_id} did not reach terminal state within {timeout_seconds}s"
    )


def _inject_ssm(
    scenario: dict,
    instance_id: str,
    region: str,
    s3_bucket: str,
    duration: int,
    run_id: str,
) -> None:
    """Submit an SSM RunShellScript command for faults 1–5."""
    ssm = boto3.client("ssm", region_name=region)

    command_script = scenario["command"]
    # Inject the duration env-var so the script respects --duration
    env_prefix = f"export DURATION={duration}\nexport RUN_ID={run_id}\n"
    full_script = env_prefix + command_script

    send_kwargs: dict = {
        "InstanceIds":  [instance_id],
        "DocumentName": "AWS-RunShellScript",
        "Parameters":   {"commands": [full_script]},
        "Comment":      f"self-healing inject fault={scenario['fault_type']} run_id={run_id}",
        # AWS-side execution limit for the SSM command itself. Confirmed
        # live: AWS-RunShellScript waits for the ENTIRE backgrounded process
        # tree (e.g. `stress-ng ... &`) to exit, not just the script's own
        # foreground statements, and stress-ng's actual completion can run
        # noticeably past its own --timeout under memory pressure on a
        # no-swap host (observed ~90s to >240s for the same DURATION=120
        # memory_exhaustion request). duration + 60 was too tight and let
        # AWS force-kill the command before a real result came back.
        "TimeoutSeconds": max(duration + 180, 180),
    }
    if s3_bucket:
        send_kwargs["OutputS3BucketName"] = s3_bucket
        send_kwargs["OutputS3KeyPrefix"]  = f"experiment-output/{run_id}"

    resp = ssm.send_command(**send_kwargs)
    command_id = resp["Command"]["CommandId"]
    logger.info(
        "SSM SendCommand submitted | CommandId=%s instance=%s fault=%s run_id=%s",
        command_id, instance_id, scenario["fault_type"], run_id,
    )

    result = _wait_ssm_command(ssm, command_id, instance_id, timeout_seconds=duration + 180)
    status = result["StatusDetails"]
    if status != "Success":
        logger.error(
            "SSM command failed | status=%s stdout=%s stderr=%s",
            status,
            result.get("StandardOutputContent", ""),
            result.get("StandardErrorContent", ""),
        )
        raise RuntimeError(f"SSM command {command_id} ended with status={status}")

    logger.info(
        "SSM command succeeded | CommandId=%s fault=%s",
        command_id, scenario["fault_type"],
    )


def _inject_instance_failure(
    instance_id: str,
    region: str,
    run_id: str,
) -> None:
    """Terminate the EC2 instance to simulate instance_failure (Thesis §3.6.2)."""
    ec2 = boto3.client("ec2", region_name=region)
    logger.info(
        "EC2 TerminateInstances | instance=%s run_id=%s", instance_id, run_id
    )
    ec2.terminate_instances(InstanceIds=[instance_id])
    logger.info("Instance %s termination requested.", instance_id)


def main(argv: Optional[list[str]] = None) -> int:
    data = _load_scenarios()
    valid_faults = list(data["scenarios"].keys())

    parser = argparse.ArgumentParser(
        description="Single-shot fault injector for the self-healing thesis experiment harness.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--target", required=True,
        choices=["target-1", "target-2", "target-3"],
        help="Logical target name",
    )
    parser.add_argument(
        "--fault", required=True,
        choices=valid_faults,
        metavar="FAULT_TYPE",
        help=f"Fault class: {', '.join(valid_faults)}",
    )
    parser.add_argument(
        "--duration", type=int, default=None,
        help="Fault duration in seconds (overrides scenario default)",
    )
    parser.add_argument(
        "--region", default=_REGION,
        help=f"AWS region (default: {_REGION})",
    )
    parser.add_argument(
        "--s3-bucket", default=_S3_BUCKET,
        help="S3 bucket for SSM command output (optional)",
    )
    parser.add_argument(
        "--run-id", default=None,
        help="Explicit run ID for correlation (auto-generated if omitted)",
    )
    parser.add_argument(
        "--allow-duplicate-injection", action="store_true",
        help="Bypass the correlation-window guard. Only for trials that are "
             "deliberately outside the paired campaign; a duplicate inside the "
             "window can corrupt experimental-unit identity silently.",
    )
    parser.add_argument(
        "--block", type=int, default=-1,
        help="Campaign schedule block index; part of the (fault_type, target, "
             "block) experimental-unit key used for prospective S1-S2 pairing. "
             "Default -1 marks an ad-hoc trial that is not schedule-paired.",
    )
    args = parser.parse_args(argv)

    scenario  = data["scenarios"][args.fault]
    run_id    = args.run_id or str(uuid.uuid4())
    duration  = args.duration if args.duration is not None else scenario.get("default_duration", 120)

    logger.info(
        "Injecting fault | fault=%s target=%s duration=%ds run_id=%s",
        args.fault, args.target, duration, run_id,
    )

    try:
        instance_id = _resolve_instance_id(args.target, args.region)
    except Exception as exc:
        logger.error("Failed to resolve instance ID: %s", exc)
        return 2

    # t0 (Thesis §3.1.2 step iii): recorded immediately before the actual
    # fault-injection command is issued, and persisted to the correlation
    # file so orchestrator/mttr_logger.py can join it at close-trial time
    # (Ch4 §4.6.1) via find_recent_injection(target, fault_type).
    if not args.allow_duplicate_injection:
        try:
            assert_no_recent_injection(target=args.target, fault_type=args.fault)
        except DuplicateInjectionError as exc:
            logger.error("Refusing to inject: %s", exc)
            return 2

    t0_epoch_ms = int(time.time() * 1000)
    try:
        record_injection(
            trial_id=run_id,
            t0_epoch_ms=t0_epoch_ms,
            fault_type=args.fault,
            block=args.block,
            target=args.target,
            s3_prefix=os.environ.get("CORRELATION_S3_PREFIX"),
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to record injection correlation (non-fatal): %s", exc)

    try:
        if args.fault == "instance_failure":
            _inject_instance_failure(instance_id, args.region, run_id)
        else:
            _inject_ssm(scenario, instance_id, args.region, args.s3_bucket, duration, run_id)
    except Exception as exc:
        logger.error("Injection failed: %s", exc)
        return 1

    logger.info("Injection complete | fault=%s target=%s run_id=%s", args.fault, args.target, run_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
