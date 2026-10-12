#!/usr/bin/env python3
"""
experiments/trials/dry_run_pairing.py — Pre-campaign dataset-shape verification.

Purpose (Phase C): confirm that the runtime harness produces a dataset whose
shape satisfies the confirmatory analysis in analysis/02_wilcoxon.py, BEFORE
committing days of cloud execution to a campaign whose records might not be
joinable afterwards.

This is a dry run of the DATA PATH, not of the experiment. No AWS call is made,
no fault is injected, and no orchestrator is contacted. What it does exercise,
end to end and through the real modules, is:

    _build_schedule()  ->  record_injection() -> correlation.jsonl
                       ->  MTTRLogger        -> mttr_log.jsonl
                       ->  validate_h1_pairing()

Every trial duration is synthetic. The only properties under test are
structural: that the experimental unit (fault_type, target, block) survives the
harness -> orchestrator boundary, that it is persisted in the authoritative
MTTR log rather than only in the correlation file, and that the resulting S1/S2
populations satisfy the design validate_h1_pairing() presupposes.

S3 is out of scope for this dry run. run_campaign.py::_build_schedule() builds
an S1-only schedule (S3 was removed from the automated harness entirely — see
its own docstring and CLAUDE.md §5.19); there is no S3 row here to synthesize
a record from, and S3 shares no experimental unit with S1 to pair against in
the first place (run_s3_trial.py resolves its instance from the ASG at
runtime and accepts no --target). H2 (S1 vs S3) is a Mann-Whitney U test on
whatever real instance_failure data exists in each arm's own log, not
something this schedule-shape check can exercise — validate it against real
mttr_log.jsonl / mttr_log_s3.jsonl data once run_s3_trial.py has produced
some, not here.

Exit codes:
    0 — the dataset shape satisfies the design
    1 — a structural invariant failed (details on stdout)

Usage:
    python experiments/trials/dry_run_pairing.py
    python experiments/trials/dry_run_pairing.py --keep   # retain the artefacts
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import shutil
import sys
import tempfile
import time
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in (_REPO_ROOT, _REPO_ROOT / "orchestrator", _REPO_ROOT / "experiments" / "trials"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

logging.basicConfig(format="%(levelname)-8s %(message)s", level=logging.INFO, stream=sys.stdout)
logger = logging.getLogger("dry_run_pairing")

_SYNTHETIC_S1_MTTR = 40.0
_SYNTHETIC_S2_MTTR = 400.0


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build_s1_records(schedule, correlate, mttr_logger) -> int:
    """Drive real MTTRLogger instances, one per scheduled S1 trial.

    The block is written only into the correlation record — exactly as the
    live harness does — so that adoption through find_recent_injection() is
    genuinely exercised rather than short-circuited.
    """
    written = 0
    s1_trials = [t for t in schedule if t["system"] == "S1"]
    # Anchor the synthetic injections in the recent past, one second apart, so
    # each lookup falls inside find_recent_injection()'s window and resolves to
    # the trial just injected rather than to a later sibling.
    base_ms = int(time.time() * 1000) - len(s1_trials) * 1000
    for i, trial in enumerate(s1_trials):
        trial_id = f"dry-{trial['block']}-{trial['fault_type']}"
        correlate.record_injection(
            trial_id=trial_id,
            t0_epoch_ms=base_ms + i * 1000,
            fault_type=trial["fault_type"],
            target=trial["target"],
            block=trial["block"],
        )
        alert = {"labels": {"fault_type": trial["fault_type"], "node_name": trial["target"]}}
        log = mttr_logger.MTTRLogger(alert, target=trial["target"])
        log.record_detection()
        log.record_classification(actuator="ansible", action="playbook.yml")
        log.record_remediation(retry_count=0)
        if trial["fault_type"] == "instance_failure":
            log.record_observability_restore()
        log.record_validation()
        log.save(recovery_status="success")
        written += 1
    return written


def _build_s2_records(s2_schedule, path: Path) -> int:
    """Mirror what run_manual_trial.py writes for the human-driven arm."""
    with path.open("a") as fh:
        for i, row in enumerate(s2_schedule):
            fh.write(json.dumps({
                "trial_id": f"dry-s2-{row['block']}-{row['fault_type']}",
                "system_under_test": "S2",
                "target": row["target"],
                "fault_type": row["fault_type"],
                "block": row["block"],
                "actuator": "manual",
                "recovery_status": "success",
                "T_MTTR_seconds": _SYNTHETIC_S2_MTTR + i,
            }) + "\n")
    return len(s2_schedule)


def _check(label: str, ok: bool, detail: str = "") -> bool:
    logger.info("  [%s] %s%s", "PASS" if ok else "FAIL", label, f" — {detail}" if detail else "")
    return ok


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Pre-campaign dataset-shape verification (Phase C).")
    ap.add_argument("--keep", action="store_true", help="retain the temporary artefacts")
    args = ap.parse_args(argv)

    workdir = Path(tempfile.mkdtemp(prefix="dryrun-pairing-"))
    logger.info("Dry-run workspace: %s", workdir)

    run_campaign = _load_module("run_campaign", _REPO_ROOT / "experiments" / "trials" / "run_campaign.py")
    wx = _load_module("wilcoxon_mod", _REPO_ROOT / "analysis" / "02_wilcoxon.py")
    from experiments.correlation import correlate
    import mttr_logger

    # Redirect every artefact into the sandbox; results/ must stay untouched.
    correlate._LOCAL_CACHE = workdir / "correlation.jsonl"
    mttr_logger.JSONL_PATH = workdir / "mttr_log.jsonl"
    run_campaign._REPO_ROOT = workdir

    cfg = yaml.safe_load((_REPO_ROOT / "experiments" / "trials" / "trial_config.yml").read_text())
    schedule = run_campaign._build_schedule(cfg, seed=cfg.get("random_seed", 42))
    s2_schedule = [json.loads(line) for line in
                   (workdir / "results" / "s2_schedule.jsonl").read_text().splitlines() if line.strip()]

    logger.info("Generating synthetic records through the real code path…")
    n1 = _build_s1_records(schedule, correlate, mttr_logger)
    n2 = _build_s2_records(s2_schedule, mttr_logger.JSONL_PATH)
    logger.info("  S1=%d  S2=%d records (S3 out of scope — see module docstring)", n1, n2)

    records = [json.loads(line) for line in mttr_logger.JSONL_PATH.read_text().splitlines() if line.strip()]
    ok = True

    logger.info("Arm sizes")
    ok &= _check("S1 = 60 records", n1 == 60, f"got {n1}")
    ok &= _check("S2 = 60 records", n2 == 60, f"got {n2}")

    logger.info("Persistence of the pairing key")
    s1 = [r for r in records if r["system_under_test"] == "S1"]
    s2 = [r for r in records if r["system_under_test"] == "S2"]
    in_log = [r for r in s1 if isinstance(r.get("block"), int) and r["block"] > 0]
    ok &= _check("block persisted in mttr_log.jsonl for every S1 record",
                 len(in_log) == len(s1), f"{len(in_log)}/{len(s1)}")
    ok &= _check("block adopted from correlation.jsonl, not defaulted",
                 {r["block"] for r in s1} == set(range(1, 11)),
                 f"blocks seen: {sorted({r['block'] for r in s1})}")

    logger.info("Set invariants over the experimental unit")
    u1 = {(r["fault_type"], r["target"], r["block"]) for r in s1}
    u2 = {(r["fault_type"], r["target"], r["block"]) for r in s2}
    ok &= _check("|S1 units| = 60 (unique)", len(u1) == 60, f"got {len(u1)}")
    ok &= _check("S1 \\ S2 = empty", not (u1 - u2), f"{len(u1 - u2)} orphaned")
    ok &= _check("S2 \\ S1 = empty", not (u2 - u1), f"{len(u2 - u1)} orphaned")
    ok &= _check("|S1 ∩ S2| = 60", len(u1 & u2) == 60, f"got {len(u1 & u2)}")

    logger.info("Phase B validation gates")
    try:
        pairs = wx.validate_h1_pairing(wx._arm(records, "S1"), wx._arm(records, "S2"))
        ok &= _check("H1 pairing gate", len(pairs) == 60, f"{len(pairs)} pairs")
    except wx.ValidationError as exc:
        ok &= _check("H1 pairing gate", False, str(exc))
    # H2 (S1 vs S3) is deliberately not exercised here — see the module
    # docstring. This dry run's schedule never contains an S3 row to
    # synthesize a record from, so there is nothing for
    # validate_h2_populations() to validate against; asserting it here would
    # just be re-testing the absence of data this script itself chose not to
    # produce, not the harness.

    logger.info("End-to-end analysis (H1 only — see module docstring for H2/S3)")
    # Call run_wilcoxon_h1() directly rather than compute_tests(), which also
    # runs run_mann_whitney_h2() and logs an ERROR for the H2 arm — accurate
    # for real campaign data, but this dry run never produces an S3 arm to
    # feed it, so that log line would be permanent, expected noise here.
    try:
        res = wx.run_wilcoxon_h1(records)
        ok &= _check(f"H1_S1_vs_S2 → {res.get('test')}", res.get("validation_status") == "passed",
                     res.get("error", ""))
    except wx.ValidationError as exc:
        ok &= _check("H1_S1_vs_S2", False, str(exc))

    if args.keep:
        logger.info("Artefacts retained at %s", workdir)
    else:
        shutil.rmtree(workdir, ignore_errors=True)

    logger.info("")
    logger.info("DRY RUN %s", "PASSED — dataset shape satisfies the design" if ok
                else "FAILED — fix the harness before running the campaign")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
