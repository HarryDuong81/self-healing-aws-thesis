#!/usr/bin/env python3
"""
experiments/trials/run_campaign.py — 60-trial S1 campaign runner.

Generates the randomised S1 trial schedule (fixed seed for reproducibility),
executes each trial via run_one.py, collects results, and mirrors the dataset
and config to S3.

Trial protocol (Thesis §3.1.2, Table 3.2):
    S1 — Self-healing pipeline:   60 trials (10 per fault class, all 6 classes)

S2 and S3 are both deliberately NOT executed by this harness, for the same
underlying reason: a --system value that looked wired up but silently was
not, producing mislabelled data from a full, otherwise-successful run.

  S2 (manual SRE baseline) requires a human to actually perform the
  remediation with the orchestrator stopped, which this automated runner
  cannot do safely unattended. See experiments/trials/run_manual_trial.py.

  S3 (AWS Auto Scaling baseline) requires terraform/baseline to be applied
  first — a prerequisite this harness has no way to verify — and uses a
  materially different actuator (an ASG, not the three tagged S1/S2 target
  instances) that inject_once.py cannot address by its TargetName-based
  target resolution. This harness previously built a 30-row S3 schedule and
  dispatched it through run_one.py anyway; run_one.py's --system flag never
  did anything beyond a log line, so every one of those trials silently ran
  as an ordinary S1 remediation, mislabelled system_under_test="S1" by the
  orchestrator's own SYSTEM_UNDER_TEST env var default. The failure surfaced
  only at analysis time (02_wilcoxon.py's H2 check finding n=0 for S3), after
  a full unattended run had already completed. See
  experiments/trials/run_s3_trial.py for the real S3 procedure, run
  separately once terraform/baseline is confirmed applied.

Usage:
    python experiments/trials/run_campaign.py \\
        --config experiments/trials/trial_config.yml \\
        --out s3://its-thesis-data/campaign/

    # Dry-run (print schedule only):
    python experiments/trials/run_campaign.py \\
        --config experiments/trials/trial_config.yml \\
        --dry-run

Exit codes:
    0 — campaign completed (all trials attempted)
    1 — campaign aborted (fatal error)

Thesis reference: §4.4 Run the 60-trial S1 campaign, §3.1.2, Table 3.2
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import boto3
import yaml

from overhead_sampler import OverheadSampler
from run_one import _MTTR_WAIT_TIMEOUT_DEFAULT, _MTTR_WAIT_TIMEOUT_INSTANCE_FAILURE

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# run_one.py's own pre-injection gate (wait_steady_state(window=30, timeout=180))
# runs sequentially BEFORE its record-wait timeout, not concurrently with it, so
# a run_one.py subprocess's true worst-case runtime is this plus whichever of
# the two _MTTR_WAIT_TIMEOUT_* constants above applies to the trial's fault
# type — not just the record-wait figure alone. Mirrors the literal 180 in
# run_one.py's own wait_steady_state() call; keep the two in sync if that
# call's timeout ever changes.
_STEADY_STATE_GATE_TIMEOUT_SECONDS = 180

# Small fixed allowance on top of run_one.py's own worst-case runtime for
# Python interpreter startup/shutdown and subprocess plumbing overhead — not
# meant to absorb slowness in the pipeline itself, which the two constants
# above already budget for.
_SUBPROCESS_OVERHEAD_BUFFER_SECONDS = 60

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("run_campaign")


def _load_config(config_path: str) -> dict:
    with open(config_path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _write_s2_schedule(s2_schedule: list[dict]) -> None:
    """
    Write the S2 execution schedule to results/s2_schedule.jsonl.

    S2 runs manually (run_manual_trial.py) but must cover exactly the same
    experimental units as S1 for the paired H1 analysis to be valid.  Writing
    the list before the campaign starts fixes the pairing a priori; the
    operator works through it in order, passing --block from each row.
    """
    out = _REPO_ROOT / "results" / "s2_schedule.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w") as fh:
        for row in s2_schedule:
            fh.write(json.dumps(row) + "\n")
    logger.info("S2 pairing schedule written to %s (%d units)", out, len(s2_schedule))


def _build_schedule(cfg: dict, seed: int) -> list[dict]:
    """
    Build the 60-trial S1 schedule.

    S1 only. S3 is deliberately NOT dispatched by this harness — see the
    module docstring for why, and experiments/trials/run_s3_trial.py for the
    real S3 procedure. (Deployment-session fix: this function used to also
    build a 30-row S3 schedule and _run_trial() unconditionally launched
    run_one.py for every row, S3 included — but run_one.py's --system flag
    is decorative (it reaches a log line, nothing else) and the orchestrator
    always stamps system_under_test from its own SYSTEM_UNDER_TEST env var,
    which every real deployment defaults to "S1". Every "S3" row therefore
    ran as an ordinary, mislabelled S1 trial, and — since inject_once.py
    resolves targets by the TargetName tag the ASG instance never carries —
    could not have addressed the ASG at all regardless. The failure was
    silent until analysis: 02_wilcoxon.py's H2 check found n=0 for S3 and
    raised ValidationError only after a full unattended run had already
    completed. This is the same shape of bug AD-016 already fixed for S2,
    for the same reason: a --system value that looks wired up but silently
    is not. Fixed the same way — removed from the automated harness,
    run separately, once terraform/baseline is confirmed applied.)

    S1: each of the 6 fault classes × 10 reps = 60 trials, grouped into 10
    blocks of 6 (one per fault class), shuffled within each block.
    """
    rng = random.Random(seed)
    systems   = cfg["systems"]
    targets   = cfg["targets"]
    schedule  = []
    trial_num = 0

    # S1: 10 blocks × 6 faults = 60 trials
    faults_s1 = systems["S1"]["faults"]
    reps       = systems["S1"]["reps_per_fault"]   # 10

    for block_idx in range(reps):
        block_trials = []
        fault_order = list(faults_s1)
        rng.shuffle(fault_order)

        for fault in fault_order:
            target = targets[trial_num % len(targets)]
            trial_num += 1
            block_trials.append({
                "trial_num":   trial_num,
                "block":       block_idx + 1,
                "system":      "S1",
                "fault_type":  fault,
                "target":      target,
            })

        rng.shuffle(block_trials)
        schedule.extend(block_trials)

    # S2 schedule: the same experimental units as S1, in the same order.
    # This is what makes the H1 comparison prospectively paired rather than
    # retrospectively matched — every S1 unit (fault_type, target, block) has
    # exactly one S2 counterpart, and the operator executes them from this
    # list.  S2 is NOT dispatched by this harness (it requires a human at the
    # keyboard, see run_manual_trial.py); the schedule is written out so the
    # pairing is fixed before either arm runs.
    s2_schedule = [
        {**t, "system": "S2", "trial_num": None}
        for t in schedule if t["system"] == "S1"
    ]
    _write_s2_schedule(s2_schedule)

    logger.info(
        "Schedule built: %d S1 trials | S2 pairing schedule: %d units | "
        "S3 not included — run separately via run_s3_trial.py",
        len(schedule),
        len(s2_schedule),
    )
    return schedule


def _run_trial(
    trial: dict,
    cfg: dict,
    region: str,
    prometheus_url: str,
    total: int,
) -> dict:
    """Execute one trial via run_one.py subprocess. Returns result summary."""
    trial_id = (
        f"t-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-{str(uuid.uuid4())[:8]}"
    )
    fault_durations = cfg.get("fault_durations", {})
    duration = fault_durations.get(trial["fault_type"], 120)

    script = _REPO_ROOT / "experiments" / "trials" / "run_one.py"
    cmd = [
        sys.executable, str(script),
        "--system",    trial["system"],
        "--fault",     trial["fault_type"],
        "--target",    trial["target"],
        "--block",     str(trial.get("block") or -1),
        "--trial-id",  trial_id,
        "--duration",  str(duration),
        "--region",    region,
        "--prometheus-url", prometheus_url,
    ]

    t_start = time.monotonic()
    logger.info(
        "Trial %d/%d | system=%s fault=%s target=%s trial_id=%s",
        trial["trial_num"], total,
        trial["system"], trial["fault_type"], trial["target"], trial_id,
    )

    # RQ3 overhead sampling (Thesis §3.7.3).  The sampler runs for the whole
    # trial at 5-second resolution.  Phase assignment is deferred to stop(),
    # where every buffered sample is tagged against the authoritative t_0 that
    # run_one.py commits to results/correlation.jsonl at injection.  Nothing
    # here infers t_0: run_one.py's steady-state gate can take longer than the
    # configured window, and a timer keyed to that window would record genuine
    # steady-state activity as `active`.  Sampling failures never abort a
    # trial — the sampler swallows and logs them.
    sampler = OverheadSampler(trial_id=trial_id, fault_type=trial["fault_type"])
    sampler.start()

    # run_one.py's own worst-case runtime is sequential, not the record-wait
    # figure alone: wait_steady_state() runs to completion BEFORE injection,
    # then the record-wait timeout runs AFTER it. A single fault-type-blind
    # subprocess timeout here (previously cfg.get("slo_seconds", 300) + 300 =
    # 600s for every fault class) left instance_failure with zero margin —
    # its own budget is 180 + 900 = 1080s, 480s beyond what this subprocess
    # was ever allowed to run. A trial killed here doesn't lose the
    # remediation itself (the orchestrator keeps running independently of
    # this subprocess), but it does get misrecorded as a failure in
    # campaign_run.jsonl, and the next scheduled trial's own steady-state
    # gate can then fail too if the orchestrator hasn't finished clearing the
    # previous alert yet — a single slow, otherwise-successful trial
    # cascading into its neighbours.
    mttr_wait_timeout = (
        _MTTR_WAIT_TIMEOUT_INSTANCE_FAILURE if trial["fault_type"] == "instance_failure"
        else _MTTR_WAIT_TIMEOUT_DEFAULT
    )
    subprocess_timeout = (
        _STEADY_STATE_GATE_TIMEOUT_SECONDS
        + mttr_wait_timeout
        + _SUBPROCESS_OVERHEAD_BUFFER_SECONDS
    )

    try:
        result = subprocess.run(
            cmd,
            capture_output=False,
            timeout=subprocess_timeout,
        )
    finally:
        n_samples = sampler.stop()
    elapsed = time.monotonic() - t_start

    return {
        **trial,
        "trial_id":       trial_id,
        "returncode":     result.returncode,
        "elapsed_seconds": round(elapsed, 1),
        "slo_pass":       result.returncode == 0,
        "overhead_samples": n_samples,
    }


def _mirror_to_s3(local_path: Path, s3_prefix: str, region: str) -> None:
    """
    Best-effort upload of a local file to S3.

    Off-host backup here is opt-in — it only happens at all when --out is
    passed (default: None, see main()'s argparse) — and this write happens
    once per trial inside the main campaign loop. A transient S3 failure
    (network blip, throttling, permissions) must not abort the remaining
    scheduled trials just because a backup copy of already-safely-written
    local JSONL couldn't be mirrored; the local results/ files are the
    authoritative copy either way (deployment-session fix — this used to
    propagate any exception straight out of the trial loop, silently
    ending the campaign early with no indication the cause was S3, not a
    trial failure).
    """
    if not s3_prefix.startswith("s3://"):
        return
    parts  = s3_prefix.lstrip("s3://").split("/", 1)
    bucket = parts[0]
    prefix = parts[1].rstrip("/") if len(parts) > 1 else ""
    key    = f"{prefix}/{local_path.name}" if prefix else local_path.name
    try:
        s3 = boto3.client("s3", region_name=region)
        s3.upload_file(str(local_path), bucket, key)
        logger.info("Uploaded %s → s3://%s/%s", local_path.name, bucket, key)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "S3 mirror failed for %s (campaign continues; local copy in "
            "results/ is unaffected): %s", local_path.name, exc,
        )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the 60-trial S1 self-healing experiment campaign. "
                     "S2 and S3 are run separately — see run_manual_trial.py and run_s3_trial.py."
    )
    parser.add_argument(
        "--config", default="experiments/trials/trial_config.yml",
        help="Path to trial_config.yml (default: experiments/trials/trial_config.yml)",
    )
    parser.add_argument(
        "--out", default=None,
        help="S3 prefix for result mirroring (e.g. s3://its-thesis-data/campaign/)",
    )
    parser.add_argument(
        "--region", default=os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1"),
    )
    parser.add_argument(
        "--prometheus-url", default="http://localhost:9090",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print schedule without executing trials",
    )
    parser.add_argument(
        "--start-from", type=int, default=1,
        help="Resume campaign from trial number N (default: 1)",
    )
    args = parser.parse_args(argv)

    cfg      = _load_config(args.config)
    seed     = cfg.get("random_seed", 42)
    schedule = _build_schedule(cfg, seed)

    if args.dry_run:
        for trial in schedule:
            print(json.dumps(trial))
        logger.info("Dry-run complete — %d trials in schedule", len(schedule))
        return 0

    # ── Mirror config to S3 ────────────────────────────────────────────────────
    if args.out and cfg.get("output", {}).get("mirror_config", True):
        _mirror_to_s3(Path(args.config), args.out, args.region)

    results_dir = _REPO_ROOT / "results"
    results_dir.mkdir(exist_ok=True)
    run_log = results_dir / "campaign_run.jsonl"

    results: list[dict] = []
    errors:  int        = 0

    for trial in schedule:
        if trial["trial_num"] < args.start_from:
            logger.debug("Skipping trial %d (start-from=%d)", trial["trial_num"], args.start_from)
            continue

        cooldown = cfg.get("inter_trial_cooldown_seconds", 120)
        if results:
            logger.info("Inter-trial cooldown: %ds", cooldown)
            time.sleep(cooldown)

        try:
            result = _run_trial(trial, cfg, args.region, args.prometheus_url, len(schedule))
        except Exception as exc:
            logger.error("Trial %d failed with exception: %s", trial["trial_num"], exc)
            result = {
                **trial,
                "trial_id":       "error",
                "returncode":     -1,
                "elapsed_seconds": -1,
                "slo_pass":       False,
                "error":          str(exc),
            }
            errors += 1

        results.append(result)
        with run_log.open("a") as fh:
            fh.write(json.dumps(result) + "\n")

        if args.out:
            _mirror_to_s3(run_log, args.out, args.region)
            mttr_log = _REPO_ROOT / "results" / "mttr_log.jsonl"
            if mttr_log.exists():
                _mirror_to_s3(mttr_log, args.out, args.region)
            mttr_log_s3 = _REPO_ROOT / "results" / "mttr_log_s3.jsonl"
            if mttr_log_s3.exists():
                _mirror_to_s3(mttr_log_s3, args.out, args.region)

    # ── Summary ────────────────────────────────────────────────────────────────
    total  = len(results)
    passed = sum(1 for r in results if r.get("slo_pass"))
    logger.info(
        "Campaign complete | trials=%d slo_pass=%d errors=%d",
        total, passed, errors,
    )
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
