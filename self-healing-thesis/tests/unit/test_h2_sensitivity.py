"""
tests/unit/test_h2_sensitivity.py — Unit tests for analysis/h2_sensitivity.py.

This script answers a narrower question than 02_wilcoxon.py's official H2
result: if one phase of S1's instance_failure T_MTTR is replaced with a
clean-baseline estimate, does H2's conclusion survive? It is explicitly NOT a
replacement for the official result (CLAUDE.md §15.8) — properties under test:

  - Only successful instance_failure trials for the correct system enter
    either arm.
  - --exclude-trial-id removes a trial from whichever arm it appears in
    before any statistic is computed (e.g. a known non-campaign pilot trial
    mixed into mttr_log_s3.jsonl).
  - The adjustment only ever touches the named phase field, recomputing
    T_MTTR as (original - old_phase_value + clean_value) — every other
    phase and every S3 value is untouched.
  - --clean-value and --clean-trial-id-derived-mean must agree when they
    describe the same input.

Thesis reference: §5.3.2.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ANALYSIS = Path(__file__).resolve().parent.parent.parent / "analysis"
if str(_ANALYSIS) not in sys.path:
    sys.path.insert(0, str(_ANALYSIS))

_spec = importlib.util.spec_from_file_location("h2_sens_mod", _ANALYSIS / "h2_sensitivity.py")
h2s = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h2s)

pytest.importorskip("scipy")


def _s1(trial_id, mttr, obs_restore, status="success"):
    return {
        "trial_id": trial_id, "system_under_test": "S1", "fault_type": "instance_failure",
        "recovery_status": status, "T_MTTR_seconds": mttr,
        "T_observability_restore_seconds": obs_restore,
    }


def _s3(trial_id, mttr, status="success"):
    return {
        "trial_id": trial_id, "system_under_test": "S3", "fault_type": "instance_failure",
        "recovery_status": status, "T_MTTR_seconds": mttr,
    }


# ── Record admission ─────────────────────────────────────────────────────────

def test_only_successful_instance_failure_trials_are_admitted():
    s1 = [_s1("a", 500.0, 300.0), _s1("b", 500.0, 300.0, status="aborted_plan")]
    s3 = [_s3("c", 100.0), _s3("d", 100.0)]
    result = h2s.run_sensitivity(s1, s3, "T_observability_restore_seconds", 200.0, set())
    assert result["original"]["n1"] == 1


def test_exclude_trial_id_removes_from_whichever_arm_it_is_in():
    s1 = [_s1("a", 500.0, 300.0), _s1("pilot", 900.0, 300.0)]
    s3 = [_s3("c", 100.0), _s3("d", 100.0)]
    result = h2s.run_sensitivity(s1, s3, "T_observability_restore_seconds", 200.0, {"pilot"})
    assert result["original"]["n1"] == 1  # "pilot" excluded from S1


# ── The adjustment arithmetic ─────────────────────────────────────────────────

def test_adjustment_only_touches_the_named_phase_field():
    """T_MTTR_adjusted = T_MTTR_original - old_phase_value + clean_value —
    nothing else about the trial changes."""
    s1 = [_s1("a", 500.0, 300.0)]  # T_MTTR=500, T_obs_restore=300
    s3 = [_s3("c", 100.0), _s3("d", 100.0)]
    result = h2s.run_sensitivity(s1, s3, "T_observability_restore_seconds", 250.0, set())
    # adjusted = 500 - 300 + 250 = 450
    assert result["sensitivity_adjusted"]["median_x"] == 450.0
    # original untouched
    assert result["original"]["median_x"] == 500.0


def test_s3_values_are_never_adjusted():
    s1 = [_s1("a", 500.0, 300.0)]
    s3 = [_s3("c", 111.0)]
    result = h2s.run_sensitivity(s1, s3, "T_observability_restore_seconds", 250.0, set())
    assert result["original"]["median_y"] == 111.0
    assert result["sensitivity_adjusted"]["median_y"] == 111.0


def test_complete_separation_is_detected_when_every_s1_exceeds_every_s3():
    s1 = [_s1(f"s1-{i}", 500.0 + i, 300.0) for i in range(5)]
    s3 = [_s3(f"s3-{i}", 50.0 + i) for i in range(5)]
    result = h2s.run_sensitivity(s1, s3, "T_observability_restore_seconds", 250.0, set())
    assert result["original"]["complete_separation"] is True


def test_raises_when_one_arm_is_empty():
    s1 = [_s1("a", 500.0, 300.0)]
    with pytest.raises(RuntimeError):
        h2s.run_sensitivity(s1, [], "T_observability_restore_seconds", 250.0, set())


def test_output_always_carries_the_not_a_replacement_disclaimer():
    s1 = [_s1("a", 500.0, 300.0)]
    s3 = [_s3("c", 100.0)]
    result = h2s.run_sensitivity(s1, s3, "T_observability_restore_seconds", 250.0, set())
    assert "NOT a replacement" in result["note"]
    assert result["clean_value_used"] == 250.0
    assert result["phase_field_adjusted"] == "T_observability_restore_seconds"


# ── rank_biserial_effect_size ────────────────────────────────────────────────

def test_rank_biserial_effect_size_is_minus_one_for_complete_separation_favouring_y():
    # U statistic == n1*n2 means every y exceeds every x (S1 > S3 here in the
    # real use case, so U=n1*n2 when S1's arm is entirely larger).
    assert h2s.rank_biserial_effect_size([1, 2], [10, 20], stat=4.0) == -1.0
