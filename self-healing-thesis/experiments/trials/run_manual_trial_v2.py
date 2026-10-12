#!/usr/bin/env python3
"""
experiments/trials/run_manual_trial_v2.py — Protocol v2 (symmetric-timing)
pilot harness for the S2 manual-SRE baseline.

Deliberately independent of run_manual_trial.py (never imports from or
modifies it): the two protocols must never be mixed, and their output files
are kept separate (results/mttr_log_s2.jsonl vs. results/mttr_log_s2_pilot.jsonl,
the latter tagged protocol_version="v2_symmetric" on every record) so
provenance stays unambiguous.

Shakedown vs. pilot vs. official output is likewise separated by the script
itself, not by convention: --shakedown routes to mttr_log_s2_shakedown.jsonl
(run_type="shakedown", trial_id must start with "shakedown-"), --official
routes to mttr_log_s2_v2_official.jsonl (run_type="official", trial_id must
start with "official-"), and the default (neither flag) routes to
mttr_log_s2_pilot.jsonl (run_type="pilot"). The three flags/prefixes/files
are mutually exclusive and cross-enforced, so shakedown or pilot data cannot
leak into the official 60-trial dataset (or vice versa) even by mistake.
None of these three destinations' records are ever deleted; each is
retained as its own permanent audit trail.

PROTOCOL-FREEZE IMPLEMENTATION NOTE (added when --official was introduced,
after the 12-trial pilot passed Gate A): the --official execution mode, its
dedicated output file, official- trial-ID validation, extension of the
accepted fault-class vocabulary to all six classes in --official mode only,
and the three added _VALIDATE_TIMEOUT_S entries change ONLY campaign
routing and execution scope -- which trials the harness permits and where
their records are written. They do NOT alter the frozen S2-v2 measurement
protocol: t_fault remains the same authoritative correlation-record
timestamp; the same detect/notify/ack/investigate/classify/plan/remediate/
validate phase sequence and instance_failure's additional
T_observability_restore phase are unchanged; the same masking rule (fault
class and remediation commands never shown to the operator) is unchanged;
the same monotonic-chain and phase-sum invariants (fatal, pre-write) are
unchanged; T_MTTR's definition and N/pairing rules are unchanged. The added
420s timeouts for memory_exhaustion/disk_saturation/service_crash are an
execution/validation-poll budget only, not a new SLO -- the predefined 300s
T_MTTR SLO criterion is evaluated at analysis time, not enforced here; a
trial taking longer than 300s but less than its timeout is still a valid,
recorded measurement.

Why this protocol exists: the legacy S2 harness (a) starts its own clock
only once the operator is already at the keyboard, giving S2 no equivalent
to S1's real T_detect, and (b) prints the exact fix command, so "manual
remediation" was actually "execute a provided answer," not diagnosis. This
script fixes both: both arms now start at t_fault (the same milestone S1's
own T_detect is measured from) and end at t_validated (the same Prometheus
ALERTS-clearing signal HealthChecker uses), and the operator is never told
the fault class or the fix -- they must detect, classify, and remediate
using the same observability stack a human SRE would actually have.

This is a PILOT instrument only (12 trials, 3 fault classes: instance_failure,
cpu_overload, configuration_drift). Per the locked decision-gate contract,
its results decide protocol validity (Gate A), never whether the existing
H1 finding is retained or a full campaign is run -- that decision is not
data-dependent on this pilot's outcome.

Timestamp sequence (7 phases, all fault classes; instance_failure gets an
8th phase spliced between remediation and validation, mirroring S1's own
AD-011 special case):

    t_fault -> t_alert -> t_notification -> t_ack -> t_classified
             -> t_actuate -> t_remediated [-> t_observability_restored]
             -> t_validated

    T_MTTR = T_detect + T_notify_delivery + T_human_response + T_diagnosis
           + T_planning + T_remediation [+ T_observability_restore]
           + T_validation

t_fault is NOT the return of a blocking inject_once.py call -- inject_once.py
blocks on the injected command's own full duration for the five
Ansible-actuated classes, which would place t_fault at the END of the fault
window, not the start. Instead: inject_once.py is launched as a detached
subprocess (mirroring its own async-dispatch pattern), and t_fault is read
back from the correlation record it writes via record_injection() BEFORE
issuing the actual injection command (experiments/correlation/correlate.py) --
the exact same value orchestrator/mttr_logger.py adopts as S1's own
t_fault_injected_ms. This is the one point in this script that must reuse an
existing mechanism rather than a plausible-looking reimplementation, because
comparability to S1 depends on it being the identical definition, not an
approximation.

Fault class and the fix are never revealed to the operator. The classifier
menu lists the full six-class closed vocabulary (Thesis Table 3.3 / this
codebase's CLAUDE.md Section 6) regardless of pilot scope, so an operator
cannot infer the pilot's 3-class restriction from the menu itself. A
misclassification does not discard the trial -- it is recorded (both
fault_type ground truth and operator_classification) and excluded only from
the primary paired-timing comparison, never from the dataset.

Usage:
    # Pilot trial (writes to mttr_log_s2_pilot.jsonl, run_type="pilot"):
    python3 experiments/trials/run_manual_trial_v2.py \\
        --fault cpu_overload --target target-1 --block 3 --trial-id pilot-cpu-01

    # Shakedown trial (writes to mttr_log_s2_shakedown.jsonl, run_type="shakedown";
    # never counted toward the pilot or official campaign):
    python3 experiments/trials/run_manual_trial_v2.py \\
        --fault cpu_overload --target target-1 --block 1 \\
        --trial-id shakedown-cpu-01 --shakedown

    # Official campaign trial (writes to mttr_log_s2_v2_official.jsonl,
    # run_type="official"; the only mode allowing all six fault classes):
    python3 experiments/trials/run_manual_trial_v2.py \\
        --fault memory_exhaustion --target target-2 --block 5 \\
        --trial-id official-mem-05 --official

    python3 experiments/trials/run_manual_trial_v2.py \\
        --fault instance_failure --target target-2 --block 7 --force --official \\
        --trial-id official-instfail-07

    # Calibration trial (writes to mttr_log_s2_calibration.jsonl, run_type="calibration";
    # all six classes allowed; never part of the official dataset). Identical operator
    # protocol and identical t_alert_ms semantics (first appearance of the ALERTS
    # series, i.e. the pending state); additionally a background recorder, invisible
    # to the operator, stores when the alert was first seen pending and first seen
    # firing (t_alert_pending_seen_ms / t_alert_firing_seen_ms, -1 = not observed).
    python3 experiments/trials/run_manual_trial_v2.py \\
        --fault cpu_overload --target target-1 --block -1 \\
        --trial-id calib-cpu-01 --calibration
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
_JSONL_PATH_PILOT      = _RESULTS_DIR / "mttr_log_s2_pilot.jsonl"
_JSONL_PATH_SHAKEDOWN  = _RESULTS_DIR / "mttr_log_s2_shakedown.jsonl"
_JSONL_PATH_OFFICIAL   = _RESULTS_DIR / "mttr_log_s2_v2_official.jsonl"
_JSONL_PATH_CALIBRATION = _RESULTS_DIR / "mttr_log_s2_calibration.jsonl"

sys.path.insert(0, str(_REPO_ROOT))
from experiments.correlation.correlate import find_recent_injection  # noqa: E402
# Confirmed live against the monitoring host (2026-09-17): the deployed
# orchestrator package lives at /opt/orchestrator/src/, not
# /opt/orchestrator/orchestrator/ as CLAUDE.md's documented repo layout
# describes -- this host's actual layout, not the repo's own directory name,
# is what this import must match.
from src.observability_checker import ObservabilityChecker  # noqa: E402

_PROTOCOL_VERSION   = "v2_symmetric"
_TARGET_NAME_PREFIX = "self-healing-thesis-experiment-"

# Pilot scope was locked to these three (thesis proposal, decision-gate §1):
# instance_failure (currently non-significant, p=0.055 -- a control),
# cpu_overload (currently the largest S1/S2 gap), configuration_drift
# (currently the highest S2 variance, most exposed to the answer-key removal).
# Still enforced for pilot/shakedown modes post-freeze -- the historical
# scope boundary is preserved, not loosened retroactively. --official mode
# is the only one permitted to use the full six-class vocabulary below.
_PILOT_FAULT_CLASSES = ["instance_failure", "cpu_overload", "configuration_drift"]

# Full six-class closed vocabulary (CLAUDE.md Section 6) -- always shown in
# full on the classification menu regardless of mode, so the menu itself
# never hints which classes are in scope for the running mode. Also the
# --fault vocabulary for --official mode (see _PILOT_FAULT_CLASSES above).
_CLASSIFICATION_MENU = [
    "cpu_overload", "memory_exhaustion", "disk_saturation",
    "service_crash", "configuration_drift", "instance_failure",
]

# Matches run_one.py's own fault-type-aware validation timeout (Thesis
# §7.4): instance_failure's full pipeline (detect + diagnosis + planning +
# remediation + observability-restore + validation) can exceed 400s on its
# own, dominated by cloud-init on the recreated instance; the five
# Ansible-actuated classes share the tighter budget. This is an
# execution/validation-poll budget only, not the SLO -- the predefined 300s
# T_MTTR SLO criterion is evaluated at analysis time; a trial that finishes
# in, say, 350s is still a valid recorded measurement that simply fails the
# SLO, not one this timeout should have cut off.
_VALIDATE_TIMEOUT_S = {
    "instance_failure":    900,
    "cpu_overload":        420,
    "configuration_drift": 420,
    "memory_exhaustion":   420,
    "disk_saturation":     420,
    "service_crash":       420,
}

# Matches mttr_logger.py's own find_recent_injection() window_seconds=300
# rationale: 300s comfortably covers every fault class's worst-case
# pre-alert latency, including disk_saturation's dd-fill time (not in this
# pilot, but kept consistent rather than inventing a second constant).
_ALERT_WAIT_TIMEOUT_S = 300

_ALERT_POLL_INTERVAL_S = 5  # matches HealthChecker's own poll interval (§5.4)

# Closed recovery_status vocabulary for this protocol -- kept separate from
# mttr_logger.py's _VALID_RECOVERY_STATUS (S1's own set) since the failure
# modes here are S2-v2-specific (no retry_exhausted/aborted_plan/timeout --
# those are Execute-layer/Plan-layer concepts that don't apply to a human
# operator).
_VALID_RECOVERY_STATUS = frozenset({
    "success",
    "alert_never_fired",
    "manual_validation_timeout",
    "diagnostic_bootstrap_failed",
})

_PHASE_SUM_TOLERANCE = 3e-3  # seconds; matches mttr_logger.py's own tolerance


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
    candidates = [i for r in resp["Reservations"] for i in r["Instances"]]
    if not candidates:
        raise RuntimeError(f"No running instance found for target={target}")
    candidates.sort(key=lambda i: i["LaunchTime"], reverse=True)
    return candidates[0]["InstanceId"]


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _inject_fault_detached(target: str, fault: str, duration: int, trial_id: str,
                            block: int, region: str) -> None:
    """Fire-and-forget: inject_once.py blocks on the injected command's own
    full duration internally (SSM SendCommand terminal-state polling), so it
    must never be awaited here -- t_fault is instead read back from the
    correlation record it writes before issuing that command (see
    _resolve_t_fault)."""
    inject_script = _REPO_ROOT / "experiments" / "inject" / "inject_once.py"
    cmd = [
        sys.executable, str(inject_script),
        "--target", target, "--fault", fault,
        "--duration", str(duration), "--region", region,
        "--run-id", trial_id, "--block", str(block),
    ]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _resolve_t_fault(target: str, fault_type: str, trial_id: str,
                      poll_timeout_s: float = 15.0, poll_interval_s: float = 0.5) -> int:
    """
    Read back the authoritative t_fault_injected_ms from
    results/correlation.jsonl -- the same value orchestrator/mttr_logger.py
    adopts for S1 (Thesis §5.13/AD-010). Fails closed: if the correlation
    record never appears, this trial CANNOT be made comparable to S1's own
    t_fault semantics, so there is no silent fallback to time.time() here
    (that would reintroduce exactly the asymmetry this protocol exists to
    remove).
    """
    deadline = time.monotonic() + poll_timeout_s
    while time.monotonic() < deadline:
        record = find_recent_injection(target=target, fault_type=fault_type, window_seconds=60)
        if record is not None:
            if record.get("trial_id") != trial_id:
                raise RuntimeError(
                    f"FATAL — correlation record trial_id mismatch: expected {trial_id!r}, "
                    f"found {record.get('trial_id')!r}. A concurrent/stray injection for the "
                    f"same (target, fault_type) may have raced this trial. Aborting rather than "
                    f"risk adopting the wrong t0."
                )
            return int(record["t0_epoch_ms"])
        time.sleep(poll_interval_s)
    raise RuntimeError(
        f"FATAL — no correlation record for trial_id={trial_id} appeared within "
        f"{poll_timeout_s}s of dispatching inject_once.py. Cannot establish a t_fault value "
        f"comparable to S1's own semantics; aborting rather than approximate."
    )


def _query_alerts_firing(prometheus_url: str, fault_type: str, node_name: str) -> bool:
    import urllib.parse
    import urllib.request

    query = f'ALERTS{{fault_type="{fault_type}",node_name="{node_name}"}}'
    url = f"{prometheus_url}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    with urllib.request.urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read())
    return bool(data.get("data", {}).get("result"))


def _query_alert_states(prometheus_url: str, fault_type: str, node_name: str) -> set:
    """Return the set of alertstate values ({'pending'}, {'firing'}, both, or empty)
    currently reported by the ALERTS series for this fault_type/node_name. Used only
    by the calibration recorder; _query_alerts_firing above is intentionally left
    unchanged (it matches either state, so it detects the pending state)."""
    import urllib.parse
    import urllib.request

    query = f'ALERTS{{fault_type="{fault_type}",node_name="{node_name}"}}'
    url = f"{prometheus_url}/api/v1/query?" + urllib.parse.urlencode({"query": query})
    with urllib.request.urlopen(url, timeout=5) as resp:
        data = json.loads(resp.read())
    return {r.get("metric", {}).get("alertstate", "") for r in data.get("data", {}).get("result", [])}


def _wait_for_alert_firing(prometheus_url: str, fault_type: str, node_name: str,
                            timeout: int) -> Optional[int]:
    """Poll until the alert starts firing (t_alert). Returns epoch ms, or
    None on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if _query_alerts_firing(prometheus_url, fault_type, node_name):
                return _now_ms()
        except Exception as exc:
            print(f"  (poll error, retrying: {exc})", file=sys.stderr)
        time.sleep(_ALERT_POLL_INTERVAL_S)
    return None


def _wait_for_alert_clear(prometheus_url: str, fault_type: str, node_name: str,
                           timeout: int) -> Optional[int]:
    """Poll until the alert stops firing (t_validated). Returns epoch ms, or
    None on timeout. Same signal HealthChecker uses (AD-008)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if not _query_alerts_firing(prometheus_url, fault_type, node_name):
                return _now_ms()
        except Exception as exc:
            print(f"  (poll error, retrying: {exc})", file=sys.stderr)
        time.sleep(_ALERT_POLL_INTERVAL_S)
    return None


def _prompt_classification() -> str:
    print("\nClassify the incident:")
    for i, name in enumerate(_CLASSIFICATION_MENU, start=1):
        print(f"  [{i}] {name}")
    while True:
        choice = input("Enter the number of your classification: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(_CLASSIFICATION_MENU):
            return _CLASSIFICATION_MENU[int(choice) - 1]
        print("  Invalid choice, try again.")


def _assert_monotonic_chain(events: list[tuple[str, Optional[int]]]) -> None:
    """events is an ordered list of (name, epoch_ms_or_None). Only called on
    the success path, where every event is expected to be present (mirrors
    mttr_logger.py's own 'only assert on the validated success path'
    qualification). Fatal on violation -- aborts before the record is
    written, per the locked contract."""
    present = [(n, v) for n, v in events if v is not None]
    for (name_a, t_a), (name_b, t_b) in zip(present, present[1:]):
        if t_b < t_a:
            raise RuntimeError(
                f"FATAL — monotonic timestamp invariant VIOLATED: {name_b}={t_b} < "
                f"{name_a}={t_a}. This indicates a bug in the timestamp recording sequence. "
                f"The trial record has NOT been written."
            )


def _assert_phase_sum(T_MTTR: float, phases: list[float], trial_id: str) -> None:
    decomposed_sum = round(sum(p for p in phases if p >= 0), 3)
    residual = abs(decomposed_sum - T_MTTR)
    if residual >= _PHASE_SUM_TOLERANCE:
        raise RuntimeError(
            f"FATAL — Decomposed-MTTR phase-sum invariant VIOLATED | trial_id={trial_id} "
            f"T_MTTR={T_MTTR:.6f}s decomposed_sum={decomposed_sum:.6f}s "
            f"residual={residual:.6f}s tolerance={_PHASE_SUM_TOLERANCE:.0e}s. "
            f"The trial record has NOT been written to prevent dataset corruption."
        )


def _assert_record_schema(record: dict) -> None:
    problems: list[str] = []

    blk = record.get("block")
    if not isinstance(blk, int) or blk == 0 or blk < -1:
        problems.append(f"block={blk!r} is not a valid experimental-unit index")

    status = record.get("recovery_status")
    if status not in _VALID_RECOVERY_STATUS:
        problems.append(f"recovery_status={status!r} outside closed vocabulary {sorted(_VALID_RECOVERY_STATUS)}")

    if record.get("recovery_status") == "success" and record.get("T_MTTR_seconds", -1) < 0:
        problems.append("recovery_status=success but T_MTTR_seconds is a sentinel")

    obs_ms  = record.get("t_observability_restored_ms")
    obs_sec = record.get("T_observability_restore_seconds")
    if record.get("fault_type") != "instance_failure":
        if obs_ms != -1 or obs_sec != -1.0:
            problems.append(
                f"fault_type={record.get('fault_type')!r} carries an observability-restore "
                f"value; expected the -1 / -1.0 sentinels"
            )

    if problems:
        raise RuntimeError(
            "S2-v2 record failed schema validation and was NOT persisted | "
            f"trial_id={record.get('trial_id')} | " + "; ".join(problems)
        )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one protocol-v2 (symmetric-timing) S2 pilot trial."
    )
    # No choices= restriction here -- the valid --fault vocabulary depends on
    # which mode (--official vs pilot/shakedown) is selected, validated below
    # after parsing rather than statically, since argparse's choices= can't
    # express a cross-argument condition.
    parser.add_argument("--fault", required=True, choices=_CLASSIFICATION_MENU)
    parser.add_argument("--target", required=True, choices=["target-1", "target-2", "target-3"])
    parser.add_argument("--block", type=int, required=True,
                         help="Schedule block index; (fault_type, target, block) pairs this "
                              "trial with its S1 counterpart.")
    parser.add_argument("--duration", type=int, default=600,
                         help="Fault duration for self-clearing classes (default 600s, long "
                              "enough that the operator's own action ends it, not the "
                              "injector's own timeout).")
    parser.add_argument("--region", default="ap-southeast-1")
    parser.add_argument("--prometheus-url", default="http://localhost:9090")
    parser.add_argument("--trial-id", default=None)
    parser.add_argument("--force", action="store_true",
                         help="Skip the orchestrator-stopped precondition check")
    parser.add_argument("--shakedown", action="store_true",
                         help="Route this trial to results/mttr_log_s2_shakedown.jsonl "
                              "(run_type='shakedown') instead of the pilot dataset. Use for "
                              "end-to-end validation runs before the pilot counter starts; "
                              "shakedown records are never read by h1_pilot_comparison.py.")
    parser.add_argument("--official", action="store_true",
                         help="Route this trial to results/mttr_log_s2_v2_official.jsonl "
                              "(run_type='official') -- the frozen-protocol 60-trial campaign "
                              "(6 fault classes x 10 repetitions), the only mode permitted to "
                              "use the full six-class vocabulary. Mutually exclusive with "
                              "--shakedown.")
    parser.add_argument("--calibration", action="store_true",
                         help="Route this trial to results/mttr_log_s2_calibration.jsonl "
                              "(run_type='calibration', trial_id prefix 'calib-'). Same operator "
                              "protocol and timestamps as the other modes; additionally records "
                              "when the alert was first seen pending and first seen firing. "
                              "Allows all six fault classes. Mutually exclusive with "
                              "--shakedown and --official.")
    args = parser.parse_args(argv)

    if sum([args.shakedown, args.official, args.calibration]) > 1:
        print("ERROR: --shakedown, --official and --calibration are mutually exclusive.", file=sys.stderr)
        return 2

    if args.calibration:
        mode, prefix, jsonl_path = "calibration", "calib-", _JSONL_PATH_CALIBRATION
    elif args.official:
        mode, prefix, jsonl_path = "official", "official-", _JSONL_PATH_OFFICIAL
    elif args.shakedown:
        mode, prefix, jsonl_path = "shakedown", "shakedown-", _JSONL_PATH_SHAKEDOWN
    else:
        mode, prefix, jsonl_path = "pilot", "pilot-", _JSONL_PATH_PILOT

    if not (args.official or args.calibration) and args.fault not in _PILOT_FAULT_CLASSES:
        print(
            f"ERROR: --fault {args.fault!r} is only valid with --official or --calibration. Pilot/shakedown "
            f"mode is restricted to {_PILOT_FAULT_CLASSES} (the frozen pilot scope, preserved "
            f"post-freeze rather than loosened retroactively).",
            file=sys.stderr,
        )
        return 2

    _OTHER_PREFIXES = {"official-", "shakedown-", "pilot-", "calib-"} - {prefix}
    if args.trial_id is None:
        args.trial_id = f"{prefix}{args.fault}-{args.target}-{int(time.time())}"
    elif not args.trial_id.startswith(prefix):
        print(
            f"ERROR: mode={mode!r} requires --trial-id to start with {prefix!r} "
            f"(got {args.trial_id!r}). This prefix, plus the separate output file and "
            f"run_type field, is the provenance guardrail that keeps {mode} data isolated "
            f"from the other two datasets.",
            file=sys.stderr,
        )
        return 2
    elif any(args.trial_id.startswith(p) for p in _OTHER_PREFIXES):
        print(
            f"ERROR: trial_id {args.trial_id!r} starts with a prefix belonging to a different "
            f"mode than the one selected ({mode}, expects {prefix!r}).",
            file=sys.stderr,
        )
        return 2

    run_type = mode

    if not args.force and _orchestrator_is_active():
        print(
            "ERROR: orchestrator.service is still active. It will race you to the fix, "
            "invalidating the manual-response measurement.\n"
            "Stop it first:  sudo systemctl stop orchestrator\n"
            "(or pass --force if you've already accounted for this)",
            file=sys.stderr,
        )
        return 2

    duration = 0 if args.fault == "instance_failure" else args.duration
    trial_id = args.trial_id
    node_name = f"{_TARGET_NAME_PREFIX}{args.target}"

    print(f"=== S2-v2 {run_type} trial | trial_id={trial_id} target={args.target} "
          f"block={args.block} output={jsonl_path.name} ===")
    print("(fault class withheld from here on — do not reveal it to the operator)")

    # ---- calibration only: background alert-state recorder (silent) ------
    recorder = None
    if args.calibration:
        from experiments.trials.alert_state_recorder import AlertStateRecorder  # noqa: E402
        recorder = AlertStateRecorder(
            lambda: _query_alert_states(args.prometheus_url, args.fault, node_name), interval_s=1.0)
        recorder.start()

    def _calibration_fields() -> dict:
        """Stop the recorder (idempotent) and return its record fields; {} outside calibration mode."""
        if recorder is None:
            return {}
        recorder.stop()
        return recorder.as_fields()

    # ---- t_fault: dispatch, then read back the authoritative t0 ----------
    _inject_fault_detached(args.target, args.fault, duration, trial_id, args.block, args.region)
    t_fault_ms = _resolve_t_fault(args.target, args.fault, trial_id)
    print(f"[t_fault]        {t_fault_ms}  (confirmed via correlation record)")

    # ---- t_alert -----------------------------------------------------------
    t_alert_ms = _wait_for_alert_firing(args.prometheus_url, args.fault, node_name, _ALERT_WAIT_TIMEOUT_S)
    if t_alert_ms is None:
        return _finalize_failure(trial_id, args, node_name, t_fault_ms, "alert_never_fired",
                                  "alert_never_fired", jsonl_path, run_type,
                                  extra={"alert_state_fields": _calibration_fields()})
    print(f"[t_alert]        {t_alert_ms}")

    # ---- t_notification / t_ack --------------------------------------------
    t_notification_ms = _now_ms()
    print(f"[t_notification]  {t_notification_ms}  (delta from t_alert: "
          f"{(t_notification_ms - t_alert_ms) / 1000.0:.3f}s — should be ~0, not "
          f"stalled by any prior blocking step)")
    print("\n" + "=" * 70)
    print("CRITICAL INCIDENT — condition: firing — target: " + args.target)
    print("Press ENTER to acknowledge.")
    print("=" * 70)
    input()
    t_ack_ms = _now_ms()
    print(f"[t_ack]           {t_ack_ms}")

    # ---- investigation (no dashboard build — existing stack only) --------
    print(
        "\nInvestigate using the existing observability stack (Prometheus, Grafana, "
        "ELK, Jaeger) — no fault-specific hints will be given."
    )

    # ---- t_classified --------------------------------------------------
    operator_classification = _prompt_classification()
    t_classified_ms = _now_ms()
    print(f"[t_classified]    {t_classified_ms}")

    misclassified = operator_classification != args.fault

    # ---- t_actuate -----------------------------------------------------
    input("\nState (to yourself) the remediation you intend to run, then press ENTER "
          "to begin execution... ")
    t_actuate_ms = _now_ms()
    _planning_gap_s = (t_actuate_ms - t_classified_ms) / 1000.0
    print(f"[t_actuate]       {t_actuate_ms}  (T_planning so far: {_planning_gap_s:.3f}s"
          + ("  ** WARNING: near-zero — check the two prompts aren't collapsing into "
             "one event **" if _planning_gap_s < 0.05 else "") + ")")

    # ---- t_remediated ----------------------------------------------------
    print("\nExecute your remediation now via a separate SSM session.")
    input("Press ENTER once you have completed the manual fix... ")
    t_remediated_ms = _now_ms()
    print(f"[t_remediated]    {t_remediated_ms}")

    # ---- instance_failure only: t_observability_restored -----------------
    t_observability_restored_ms = -1
    observability_restore_failed = False
    if args.fault == "instance_failure":
        print("\nWaiting for the recreated instance's SSM Agent to come online...")
        new_instance_id = _resolve_instance_id(args.target, args.region)
        ssm_client = boto3.client("ssm", region_name=args.region)
        deadline = time.monotonic() + 300
        online = False
        while time.monotonic() < deadline:
            resp = ssm_client.describe_instance_information(
                InstanceInformationFilterList=[{"key": "InstanceIds", "valueSet": [new_instance_id]}]
            )
            infos = resp.get("InstanceInformationList", [])
            if infos and infos[0].get("PingStatus") == "Online":
                online = True
                break
            time.sleep(5)
        if not online:
            print("TIMEOUT waiting for new instance's SSM Agent to come online.", file=sys.stderr)
            observability_restore_failed = True
        else:
            print("\nRestore the diagnostic-path agents (Filebeat, OTel Agent) on the "
                  "recreated instance now, matching the automated pipeline's own restore step.")
            input("Press ENTER once diagnostic-agent restoration has completed... ")
            print("Verifying diagnostic-path forwarding (Elasticsearch + OTel self-telemetry)...")
            checker = ObservabilityChecker(target=node_name, instance_id=new_instance_id)
            if checker.wait_until_forwarding(timeout=300):
                t_observability_restored_ms = _now_ms()
                print(f"[t_observability_restored] {t_observability_restored_ms}")
            else:
                print("TIMEOUT — diagnostic agents never confirmed forwarding.", file=sys.stderr)
                observability_restore_failed = True

        if observability_restore_failed:
            return _finalize_failure(
                trial_id, args, node_name, t_fault_ms, "diagnostic_bootstrap_failed",
                "diagnostic_bootstrap_failed", jsonl_path, run_type,
                extra={
                    "operator_classification": operator_classification,
                    "misclassified": misclassified,
                    "t_alert_ms": t_alert_ms, "t_notification_ms": t_notification_ms,
                    "t_ack_ms": t_ack_ms, "t_classified_ms": t_classified_ms,
                    "t_actuate_ms": t_actuate_ms, "t_remediated_ms": t_remediated_ms,
                    "alert_state_fields": _calibration_fields(),
                },
            )

    # ---- t_validated -------------------------------------------------------
    timeout = _VALIDATE_TIMEOUT_S[args.fault]
    print(f"\nWaiting for Prometheus to confirm the alert has cleared (timeout={timeout}s)...")
    t_validated_ms = _wait_for_alert_clear(args.prometheus_url, args.fault, node_name, timeout)
    if t_validated_ms is None:
        return _finalize_failure(
            trial_id, args, node_name, t_fault_ms, "manual_validation_timeout",
            "manual_validation_timeout", jsonl_path, run_type,
            extra={
                "operator_classification": operator_classification,
                "misclassified": misclassified,
                "t_alert_ms": t_alert_ms, "t_notification_ms": t_notification_ms,
                "t_ack_ms": t_ack_ms, "t_classified_ms": t_classified_ms,
                "t_actuate_ms": t_actuate_ms, "t_remediated_ms": t_remediated_ms,
                "t_observability_restored_ms": t_observability_restored_ms,
                "alert_state_fields": _calibration_fields(),
            },
        )
    print(f"[t_validated]     {t_validated_ms}")
    if recorder is not None:
        recorder.stop()  # stop observing at the validation instant, so no later re-fire can be recorded as 'first firing'

    # ---- assemble phases -----------------------------------------------
    T_detect          = round((t_alert_ms - t_fault_ms) / 1000.0, 3)
    T_notify_delivery = round((t_notification_ms - t_alert_ms) / 1000.0, 3)
    T_human_response  = round((t_ack_ms - t_notification_ms) / 1000.0, 3)
    T_diagnosis        = round((t_classified_ms - t_ack_ms) / 1000.0, 3)
    T_planning         = round((t_actuate_ms - t_classified_ms) / 1000.0, 3)
    T_remediation      = round((t_remediated_ms - t_actuate_ms) / 1000.0, 3)
    if args.fault == "instance_failure":
        T_observability_restore = round((t_observability_restored_ms - t_remediated_ms) / 1000.0, 3)
        T_validation = round((t_validated_ms - t_observability_restored_ms) / 1000.0, 3)
    else:
        T_observability_restore = -1.0
        T_validation = round((t_validated_ms - t_remediated_ms) / 1000.0, 3)
    T_MTTR = round((t_validated_ms - t_fault_ms) / 1000.0, 3)

    event_chain = [
        ("t_fault", t_fault_ms), ("t_alert", t_alert_ms),
        ("t_notification", t_notification_ms), ("t_ack", t_ack_ms),
        ("t_classified", t_classified_ms), ("t_actuate", t_actuate_ms),
        ("t_remediated", t_remediated_ms),
    ]
    if args.fault == "instance_failure":
        event_chain.append(("t_observability_restored", t_observability_restored_ms))
    event_chain.append(("t_validated", t_validated_ms))

    phases = [T_detect, T_notify_delivery, T_human_response, T_diagnosis,
              T_planning, T_remediation, T_observability_restore, T_validation]
    decomposed_sum = round(sum(p for p in phases if p >= 0), 3)

    # Shakedown-visible summary, printed BEFORE the invariants are asserted so
    # a violation's cause is visible in context, not just a bare traceback.
    print("\n--- Phase summary (pre-invariant-check) ---")
    print(f"  T_detect                = {T_detect}s")
    print(f"  T_notify_delivery       = {T_notify_delivery}s")
    print(f"  T_human_response        = {T_human_response}s")
    print(f"  T_diagnosis             = {T_diagnosis}s")
    print(f"  T_planning              = {T_planning}s")
    print(f"  T_remediation           = {T_remediation}s")
    if args.fault == "instance_failure":
        print(f"  T_observability_restore = {T_observability_restore}s")
    print(f"  T_validation            = {T_validation}s")
    print(f"  decomposed_sum          = {decomposed_sum}s")
    print(f"  T_MTTR (t_validated - t_fault) = {T_MTTR}s")
    print(f"  residual                = {abs(decomposed_sum - T_MTTR):.6f}s "
          f"(tolerance {_PHASE_SUM_TOLERANCE:.0e}s)")

    _assert_monotonic_chain(event_chain)
    print("  monotonic-chain invariant: PASS")
    _assert_phase_sum(T_MTTR, phases, trial_id)
    print("  phase-sum invariant:       PASS")

    record = {
        "protocol_version":         _PROTOCOL_VERSION,
        "run_type":                 run_type,
        "trial_id":                 trial_id,
        "system_under_test":        "S2",
        "target":                   node_name,
        "fault_type":               args.fault,
        "operator_classification":  operator_classification,
        "misclassified":            misclassified,
        "block":                    args.block,
        "actuator":                 "manual",
        "t_fault_injected_ms":      t_fault_ms,
        "t_alert_ms":               t_alert_ms,
        "t_notification_ms":        t_notification_ms,
        "t_ack_ms":                 t_ack_ms,
        "t_classified_ms":          t_classified_ms,
        "t_actuate_ms":             t_actuate_ms,
        "t_remediated_ms":          t_remediated_ms,
        "t_observability_restored_ms": t_observability_restored_ms,
        "t_validated_ms":           t_validated_ms,
        "T_detect_seconds":                T_detect,
        "T_notify_delivery_seconds":       T_notify_delivery,
        "T_human_response_seconds":        T_human_response,
        "T_diagnosis_seconds":             T_diagnosis,
        "T_planning_seconds":              T_planning,
        "T_remediation_seconds":           T_remediation,
        "T_observability_restore_seconds": T_observability_restore,
        "T_validation_seconds":            T_validation,
        "T_MTTR_seconds":                  T_MTTR,
        "recovery_status":          "success",
        "escalation_reason":        "",
    }
    record.update(_calibration_fields())
    _assert_record_schema(record)

    _RESULTS_DIR.mkdir(exist_ok=True)
    with jsonl_path.open("a") as fh:
        fh.write(json.dumps(record) + "\n")

    print(f"\n=== Trial END | id={trial_id} run_type={run_type} T_MTTR={T_MTTR}s "
          f"status=success misclassified={misclassified} ===")
    print(f"Record appended to {jsonl_path}")
    return 0


def _finalize_failure(trial_id: str, args, node_name: str, t_fault_ms: int,
                       recovery_status: str, escalation_reason: str,
                       jsonl_path: Path, run_type: str,
                       extra: Optional[dict] = None) -> int:
    """Failure records are always written (never discarded), tagged with the
    matching status, and excluded from the primary paired-timing comparison
    by the analysis script rather than by omission here."""
    extra = extra or {}
    record = {
        "protocol_version":  _PROTOCOL_VERSION,
        "run_type":          run_type,
        "trial_id":          trial_id,
        "system_under_test": "S2",
        "target":            node_name,
        "fault_type":        args.fault,
        "operator_classification": extra.get("operator_classification", ""),
        "misclassified":     extra.get("misclassified", None),
        "block":             args.block,
        "actuator":          "manual",
        "t_fault_injected_ms":         t_fault_ms,
        "t_alert_ms":                  extra.get("t_alert_ms", -1),
        "t_notification_ms":           extra.get("t_notification_ms", -1),
        "t_ack_ms":                    extra.get("t_ack_ms", -1),
        "t_classified_ms":             extra.get("t_classified_ms", -1),
        "t_actuate_ms":                extra.get("t_actuate_ms", -1),
        "t_remediated_ms":             extra.get("t_remediated_ms", -1),
        "t_observability_restored_ms": extra.get("t_observability_restored_ms", -1),
        "t_validated_ms":              -1,
        "T_detect_seconds":                -1.0,
        "T_notify_delivery_seconds":       -1.0,
        "T_human_response_seconds":        -1.0,
        "T_diagnosis_seconds":             -1.0,
        "T_planning_seconds":              -1.0,
        "T_remediation_seconds":           -1.0,
        "T_observability_restore_seconds": -1.0,
        "T_validation_seconds":            -1.0,
        "T_MTTR_seconds":                  -1.0,
        "recovery_status":  recovery_status,
        "escalation_reason": escalation_reason,
    }
    record.update(extra.get("alert_state_fields", {}))
    _assert_record_schema(record)

    _RESULTS_DIR.mkdir(exist_ok=True)
    with jsonl_path.open("a") as fh:
        fh.write(json.dumps(record) + "\n")

    print(f"\n=== Trial END | id={trial_id} run_type={run_type} status={recovery_status} "
          f"(excluded from primary timing comparison; retained for reliability analysis) ===",
          file=sys.stderr)
    print(f"Record appended to {jsonl_path}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
