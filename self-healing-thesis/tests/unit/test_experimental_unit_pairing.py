"""
tests/unit/test_experimental_unit_pairing.py — Unit tests for the
experimental-unit identity that makes the H1 comparison prospectively paired.

Design context (Thesis §3.1.3, §3.1.5):

  H1 (S1 vs S2) is a PAIRED comparison. S1 and S2 act on the same static
  target namespace (target-1/2/3), so an experimental unit can be defined
  a priori as (fault_type, target, block) and executed once in each arm.
  The pairing key must be persisted in the trial record, otherwise the two
  arms cannot be joined at analysis time and the pairing would degenerate
  into post-hoc matching on (fault_type, target) — which is not unique,
  since each fault class runs ten times across three targets.

  H2 (S1 vs S3) is NOT paired: run_s3_trial.py resolves its instance from
  the Auto Scaling Group at runtime and accepts no --target, so S3 shares no
  unit identity with S1. Its analysis is an independent-samples test.

A block of -1 marks a trial run outside any campaign schedule (smoke test,
development run); such trials are ineligible for the paired analysis.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import mttr_logger


# ── Schema: the pairing key must be present and well-formed ─────────────────

def _record(**overrides) -> dict:
    base = {
        "trial_id": "t-1",
        "fault_type": "cpu_overload",
        "block": 1,
        "recovery_status": "success",
        "T_MTTR_seconds": 20.0,
        "retry_count": 0,
        "escalation_reason": "",
        "t_observability_restored_ms": -1,
        "T_observability_restore_seconds": -1.0,
    }
    base.update(overrides)
    return base


def test_missing_block_is_rejected():
    """Without the key persisted, S1 and S2 cannot be joined at all."""
    rec = _record()
    del rec["block"]
    with pytest.raises(RuntimeError, match="missing the block field"):
        mttr_logger._assert_record_schema(rec)


def test_schedule_block_is_accepted():
    mttr_logger._assert_record_schema(_record(block=7))


def test_ad_hoc_sentinel_is_accepted():
    """-1 = ran outside a campaign schedule; valid, but not pairable."""
    mttr_logger._assert_record_schema(_record(block=-1))


def test_zero_is_rejected():
    """Blocks are 1-indexed; 0 usually means an uninitialised value."""
    with pytest.raises(RuntimeError, match="not a valid experimental-unit index"):
        mttr_logger._assert_record_schema(_record(block=0))


def test_negative_other_than_sentinel_is_rejected():
    with pytest.raises(RuntimeError, match="not a valid experimental-unit index"):
        mttr_logger._assert_record_schema(_record(block=-5))


def test_non_integer_is_rejected():
    with pytest.raises(RuntimeError, match="not a valid experimental-unit index"):
        mttr_logger._assert_record_schema(_record(block="1"))


# ── Propagation: harness → correlation record → MTTR record ─────────────────

def test_block_propagates_from_injection_to_trial_record(tmp_path, monkeypatch):
    """The pairing key must survive the harness→orchestrator boundary.

    The harness cannot know the orchestrator's trial_id in advance, so the
    correlation file is the only channel by which the experimental unit
    reaches the record. If block does not travel this path, every S1 record
    would carry the ad-hoc sentinel and H1 could not be paired.
    """
    from experiments.correlation import correlate

    monkeypatch.setattr(correlate, "_LOCAL_CACHE", tmp_path / "correlation.jsonl")
    monkeypatch.setattr(mttr_logger, "JSONL_PATH", tmp_path / "mttr_log.jsonl")

    correlate.record_injection(
        trial_id="t-pair-001",
        t0_epoch_ms=int(time.time() * 1000),
        fault_type="cpu_overload",
        target="target-2",
        block=4,
    )

    alert = {"labels": {"fault_type": "cpu_overload", "node_name": "target-2"}}
    log = mttr_logger.MTTRLogger(alert, target="target-2")

    assert log.trial_id == "t-pair-001", "harness trial_id was not adopted"
    assert log.block == 4, "experimental-unit identity was not adopted"


def test_uncorrelated_trial_falls_back_to_ad_hoc_sentinel(tmp_path, monkeypatch):
    """No correlation record → not schedule-paired, and it says so."""
    from experiments.correlation import correlate

    monkeypatch.setattr(correlate, "_LOCAL_CACHE", tmp_path / "empty.jsonl")
    monkeypatch.setattr(mttr_logger, "JSONL_PATH", tmp_path / "mttr_log.jsonl")

    alert = {"labels": {"fault_type": "service_crash", "node_name": "target-1"}}
    log = mttr_logger.MTTRLogger(alert, target="target-1")

    assert log.block == -1


# ── Schedule: every S1 unit has exactly one S2 counterpart ──────────────────

def test_s2_schedule_mirrors_s1_units_exactly():
    """The S2 arm must cover the same 60 experimental units as S1, once each.

    This is what makes H1 prospectively paired rather than retrospectively
    matched. S3 is deliberately excluded: it has no shared unit identity.
    """
    import importlib.util
    import sys as _sys

    _trials = Path(__file__).resolve().parent.parent.parent / "experiments" / "trials"
    if str(_trials) not in _sys.path:          # run_campaign imports its siblings by bare name
        _sys.path.insert(0, str(_trials))

    spec = importlib.util.spec_from_file_location(
        "run_campaign",
        Path(__file__).resolve().parent.parent.parent
        / "experiments" / "trials" / "run_campaign.py",
    )
    rc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rc)

    cfg = {
        "systems": {
            "S1": {"faults": ["cpu_overload", "memory_exhaustion", "disk_saturation",
                              "service_crash", "configuration_drift", "instance_failure"],
                   "reps_per_fault": 10},
            "S3": {"faults": ["cpu_overload", "memory_exhaustion", "instance_failure"],
                   "reps_per_fault": 10},
        },
        "targets": ["target-1", "target-2", "target-3"],
    }
    schedule = rc._build_schedule(cfg, seed=42)

    s1 = [t for t in schedule if t["system"] == "S1"]
    assert len(s1) == 60

    units = [(t["fault_type"], t["target"], t["block"]) for t in s1]
    assert len(set(units)) == 60, "S1 experimental units are not unique"

    s2_path = Path(rc._REPO_ROOT) / "results" / "s2_schedule.jsonl"
    s2 = [json.loads(line) for line in s2_path.read_text().splitlines() if line.strip()]
    s2_units = [(r["fault_type"], r["target"], r["block"]) for r in s2]

    assert len(s2_units) == 60
    assert set(s2_units) == set(units), "S2 schedule does not mirror S1's units"
    assert all(r["system"] == "S2" for r in s2)


# ── Pre-flight guard: duplicate injection inside the correlation window ─────

def test_duplicate_injection_within_window_is_refused(tmp_path, monkeypatch):
    """A second injection of the same unit would be indistinguishable to the
    orchestrator's lookup, so the harness must refuse it rather than produce
    two schema-valid records with one wrong experimental unit."""
    from experiments.correlation import correlate

    monkeypatch.setattr(correlate, "_LOCAL_CACHE", tmp_path / "correlation.jsonl")
    correlate.record_injection(
        trial_id="t-first", t0_epoch_ms=int(time.time() * 1000),
        fault_type="cpu_overload", target="target-1", block=3,
    )
    with pytest.raises(correlate.DuplicateInjectionError, match="already injected"):
        correlate.assert_no_recent_injection(target="target-1", fault_type="cpu_overload")


def test_guard_allows_a_different_unit(tmp_path, monkeypatch):
    from experiments.correlation import correlate

    monkeypatch.setattr(correlate, "_LOCAL_CACHE", tmp_path / "correlation.jsonl")
    correlate.record_injection(
        trial_id="t-first", t0_epoch_ms=int(time.time() * 1000),
        fault_type="cpu_overload", target="target-1", block=3,
    )
    correlate.assert_no_recent_injection(target="target-2", fault_type="cpu_overload")
    correlate.assert_no_recent_injection(target="target-1", fault_type="service_crash")


def test_guard_allows_the_same_unit_once_the_window_has_passed(tmp_path, monkeypatch):
    from experiments.correlation import correlate

    monkeypatch.setattr(correlate, "_LOCAL_CACHE", tmp_path / "correlation.jsonl")
    stale_ms = int(time.time() * 1000) - (correlate.CORRELATION_WINDOW_SECONDS + 60) * 1000
    correlate.record_injection(
        trial_id="t-old", t0_epoch_ms=stale_ms,
        fault_type="cpu_overload", target="target-1", block=3,
    )
    correlate.assert_no_recent_injection(target="target-1", fault_type="cpu_overload")


def test_tie_on_identical_timestamps_resolves_to_the_last_written(tmp_path, monkeypatch):
    """max() returns the FIRST maximal element; records sharing a millisecond
    must still resolve to the most recently appended one, or a trial can adopt
    an earlier trial's block."""
    from experiments.correlation import correlate

    monkeypatch.setattr(correlate, "_LOCAL_CACHE", tmp_path / "correlation.jsonl")
    same_ms = int(time.time() * 1000)
    correlate.record_injection(trial_id="t-earlier", t0_epoch_ms=same_ms,
                              fault_type="cpu_overload", target="target-1", block=4)
    correlate.record_injection(trial_id="t-later", t0_epoch_ms=same_ms,
                              fault_type="cpu_overload", target="target-1", block=5)

    found = correlate.find_recent_injection(target="target-1", fault_type="cpu_overload")
    assert found["trial_id"] == "t-later"
    assert found["block"] == 5
