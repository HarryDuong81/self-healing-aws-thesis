"""
tests/unit/test_h3_upper_bound.py — Unit tests for analysis/h3_upper_bound.py.

H3 (Thesis §5.3.3): the 95th percentile of the OBSERVED T_MTTR is an upper
bound on the true T_MTTR, never an estimate of it — do not subtract the
polling-jitter margin from the percentile (Round-11 C11-3). The properties
under test:

  - Only successful trials for the requested system are admitted; other
    systems, failed trials, and the schema's -1.0 "not applicable" sentinel
    must never enter the percentile calculation.
  - exclude_fault_types is an explicit, reported supplementary filter, never
    a silent drop from the primary all-six-classes figure.
  - compute_conformance()'s exact binomial CI uses the Clopper-Pearson
    method specifically, not a normal-approximation interval.

Thesis reference: §5.3.3, §3.1.3.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ANALYSIS = Path(__file__).resolve().parent.parent.parent / "analysis"
if str(_ANALYSIS) not in sys.path:
    sys.path.insert(0, str(_ANALYSIS))

_spec = importlib.util.spec_from_file_location("h3_mod", _ANALYSIS / "h3_upper_bound.py")
h3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(h3)

pytest.importorskip("scipy")


def _rec(system, fault, mttr, status="success"):
    return {
        "system_under_test": system, "fault_type": fault,
        "recovery_status": status, "T_MTTR_seconds": mttr,
    }


# ── Record admission ─────────────────────────────────────────────────────────

def test_only_the_requested_system_is_admitted():
    records = [_rec("S1", "cpu_overload", 100.0), _rec("S3", "cpu_overload", 999.0)]
    result = h3.compute_h3(records, system="S1")
    assert result["n_trials"] == 1
    assert result["p95_T_MTTR_obs_seconds"] == 100.0


def test_only_successful_trials_are_admitted():
    records = [
        _rec("S1", "cpu_overload", 100.0, status="success"),
        _rec("S1", "cpu_overload", 500.0, status="aborted_plan"),
    ]
    result = h3.compute_h3(records, system="S1")
    assert result["n_trials"] == 1


def test_the_not_applicable_sentinel_is_excluded():
    """T_MTTR_seconds == -1.0 is the schema's sentinel for a row that isn't
    a real completed trial (e.g. S3's capacity-only rows) — it must never
    enter the percentile calculation as if it were a real duration."""
    records = [
        _rec("S1", "cpu_overload", 100.0),
        _rec("S1", "cpu_overload", -1.0),
    ]
    result = h3.compute_h3(records, system="S1")
    assert result["n_trials"] == 1


def test_no_successful_records_returns_a_failed_not_a_crashed_result():
    result = h3.compute_h3([], system="S1")
    assert result["n_trials"] == 0
    assert result["h3_pass"] is False
    assert result["p95_T_MTTR_obs_seconds"] is None
    assert result["conformance"] is None


# ── Nearest-rank percentile ─────────────────────────────────────────────────

def test_nearest_rank_p95_of_58_values_is_the_56th():
    """k = ceil(0.95 * 58) = ceil(55.1) = 56.  The earlier round()-based rule
    returned the 55th value, which is not the nearest-rank percentile."""
    values = [float(i) for i in range(1, 59)]
    assert h3._percentile(values, 95) == 56.0


def test_nearest_rank_exact_integer_rank_is_not_pushed_up_by_float_error():
    """0.95 * 20 = 19 exactly: the 19th value, not the 20th."""
    values = [float(i) for i in range(1, 21)]
    assert h3._percentile(values, 95) == 19.0


def test_nearest_rank_matches_the_integer_definition_for_every_small_n():
    for n in range(1, 301):
        values = [float(i) for i in range(1, n + 1)]
        for p in (50, 95):
            k = -(-p * n // 100)          # ceil(p * n / 100) in integer arithmetic
            assert h3._percentile(values, p) == float(max(k, 1)), (n, p)


def test_nearest_rank_median_of_an_odd_count_is_the_middle_value():
    values = [float(i) for i in range(1, 58)]      # n = 57
    assert h3._percentile(values, 50) == 29.0      # ceil(28.5) = 29


def test_nearest_rank_of_a_single_value_and_of_nothing():
    assert h3._percentile([7.0], 95) == 7.0
    assert h3._percentile([], 95) != h3._percentile([], 95)   # NaN


def test_descriptive_percentile_uses_the_same_rule():
    sys.path.insert(0, str(_ANALYSIS))
    desc = importlib.import_module("01_descriptive")
    values = [float(i) for i in range(1, 59)]
    assert desc._percentile(values, 95) == 56.0
    assert desc._percentile([], 95) is None


# ── The percentile / SLO check itself ────────────────────────────────────────

def test_h3_passes_when_p95_is_at_or_below_the_slo():
    records = [_rec("S1", "cpu_overload", v) for v in [50.0, 100.0, 150.0, 200.0, 250.0]]
    result = h3.compute_h3(records, system="S1", slo=300)
    assert result["h3_pass"] is True
    assert result["p95_T_MTTR_obs_seconds"] <= 300


def test_h3_fails_when_p95_exceeds_the_slo():
    records = [_rec("S1", "instance_failure", v) for v in [400.0, 500.0, 600.0, 700.0, 800.0]]
    result = h3.compute_h3(records, system="S1", slo=300)
    assert result["h3_pass"] is False


def test_upper_bound_interpretation_field_is_always_present_and_fixed():
    """The H3 argument is monotonicity (raw_obs >= true pointwise), not
    estimation — this field's value must never change or be omitted, and
    nothing in this module may subtract the polling-jitter margin from p95."""
    records = [_rec("S1", "cpu_overload", 100.0)]
    result = h3.compute_h3(records, system="S1")
    assert result["upper_bound_interpretation"] == "raw_obs_ge_true_pointwise"
    assert result["polling_jitter_max_seconds"] == 20
    # p95 of a single value is that value, unmodified by the 20s jitter bound.
    assert result["p95_T_MTTR_obs_seconds"] == 100.0


# ── exclude_fault_types: explicit supplementary filter, never a silent drop ──

def test_exclude_fault_types_is_reported_not_silent():
    records = [
        _rec("S1", "cpu_overload", 100.0),
        _rec("S1", "instance_failure", 900.0),
    ]
    result = h3.compute_h3(records, system="S1", exclude_fault_types=("instance_failure",))
    assert result["n_trials"] == 1
    assert result["excluded_fault_types"] == ["instance_failure"]
    assert "instance_failure" in result["fault_scope"]


def test_no_exclusion_reports_all_six_fault_classes_scope():
    records = [_rec("S1", "cpu_overload", 100.0)]
    result = h3.compute_h3(records, system="S1")
    assert result["excluded_fault_types"] == []
    assert result["fault_scope"] == "all six canonical fault classes"


# ── compute_conformance ──────────────────────────────────────────────────────

def test_conformance_counts_trials_at_or_under_the_slo():
    values = [100.0, 200.0, 300.0, 400.0, 500.0]  # 3 of 5 <= 300
    result = h3.compute_conformance(values, slo=300)
    assert result["n_conforming"] == 3
    assert result["n_trials"] == 5
    assert result["proportion_conforming"] == 0.6


def test_conformance_uses_the_exact_clopper_pearson_method_not_a_normal_approximation():
    values = [100.0] * 10
    result = h3.compute_conformance(values, slo=300)
    assert result["ci_method"] == "clopper_pearson_exact"
    assert result["ci_confidence_level"] == 0.95
    # All 10 conform: exact CI's lower bound must be well above 0 but the
    # upper bound capped at (or effectively at) 1.0 — a normal-approximation
    # interval would instead produce a degenerate [1.0, 1.0] at proportion=1.
    assert 0.0 < result["ci_lower"] < 1.0
    assert result["ci_upper"] == 1.0


def test_conformance_is_embedded_in_the_full_h3_result():
    records = [_rec("S1", "cpu_overload", v) for v in [100.0, 200.0, 300.0]]
    result = h3.compute_h3(records, system="S1", slo=300)
    assert result["conformance"]["n_conforming"] == 3
    assert result["conformance"]["n_trials"] == 3
