"""
tests/unit/test_analysis_admission.py — Unit tests for the data-admission
predicates in analysis/01_descriptive.py.

The MTTR record schema encodes three distinct semantic states in a single
numeric field, and the analysis layer must keep them distinct:

    -1.0   phase not applicable to this fault class
     0.0   phase applies and was measured; duration was zero
    >0.0   phase applies and was measured; positive duration

A phase duration of exactly 0.0 is a real observation. Phases are rounded to
millisecond precision on capture, so a sub-millisecond phase — T_classify in
particular, being an in-memory lookup against an already-loaded routing table —
legitimately records 0.0. An admission predicate of `> 0` discards those
observations and understates n; the correct predicate for phase durations is
`>= 0`.

T_MTTR_seconds is the exception: a completed trial cannot take zero seconds,
and -1.0 is its own non-applicability sentinel (S3 capacity-only rows), so
`> 0` is correct there.

Thesis reference: §3.7.2 (decomposed recovery-time measurement model).
"""
from __future__ import annotations

import sys
from pathlib import Path

_ANALYSIS = Path(__file__).resolve().parent.parent.parent / "analysis"
sys.path.insert(0, str(_ANALYSIS))

import importlib

desc = importlib.import_module("01_descriptive")


# ── Phase-duration admission ─────────────────────────────────────────────────

def test_phase_sentinel_is_excluded():
    """-1.0 marks non-applicability and must never enter a distribution."""
    assert desc._admits_phase({"T_observability_restore_seconds": -1.0},
                              "T_observability_restore_seconds") is False


def test_phase_zero_is_admitted():
    """0.0 is a real measurement, not a missing value — this is the fix."""
    assert desc._admits_phase({"T_classify_seconds": 0.0},
                              "T_classify_seconds") is True


def test_phase_positive_is_admitted():
    assert desc._admits_phase({"T_classify_seconds": 0.003},
                              "T_classify_seconds") is True


def test_phase_missing_key_is_excluded():
    """An absent field defaults to the sentinel, not to zero."""
    assert desc._admits_phase({}, "T_classify_seconds") is False


# ── Total-MTTR admission ─────────────────────────────────────────────────────

def test_total_sentinel_is_excluded():
    assert desc._admits_total({"T_MTTR_seconds": -1.0}) is False


def test_total_zero_is_excluded():
    """A completed trial cannot take zero seconds; unlike a phase, 0.0 here
    indicates a defect rather than a fast measurement."""
    assert desc._admits_total({"T_MTTR_seconds": 0.0}) is False


def test_total_positive_is_admitted():
    assert desc._admits_total({"T_MTTR_seconds": 20.7}) is True


# ── End-to-end effect on the reported statistics ─────────────────────────────

def _record(fault_type: str, **phases) -> dict:
    base = {
        "system_under_test": "S1",
        "fault_type":        fault_type,
        "recovery_status":   "success",
        "T_MTTR_seconds":    20.0,
        "T_detect_seconds":            10.0,
        "T_classify_seconds":           1.0,
        "T_remediate_seconds":          5.0,
        "T_observability_restore_seconds": -1.0,
        "T_validate_seconds":           4.0,
    }
    base.update(phases)
    return base


def test_zero_duration_phase_counts_toward_n():
    """A 0.0 T_classify must appear in the population, not be filtered out."""
    records = [
        _record("cpu_overload", T_classify_seconds=0.0),
        _record("cpu_overload", T_classify_seconds=0.002),
    ]
    out = desc.compute_descriptive(records)
    stats = out["S1"]["by_fault_type"]["cpu_overload"]["phases"]["T_classify"]
    assert stats["n"] == 2, "the zero-duration observation was dropped"
    assert stats["min"] == 0.0


def test_observability_sentinel_excluded_from_non_instance_failure():
    """The five Ansible-actuated classes contribute nothing to this phase."""
    records = [_record("cpu_overload"), _record("service_crash")]
    out = desc.compute_descriptive(records)
    stats = out["S1"]["phases_overall"]["T_observability_restore"]
    assert stats["n"] == 0
    assert stats["mean"] is None


def test_non_applicable_phase_is_marked_not_zero_sample():
    """'Not applicable' and 'zero valid observations' are different findings.

    Reporting the observability phase as n = 0 for a class it never applies to
    would be indistinguishable from a class where every trial failed to record
    it. The per-class artefact must say which one it is.
    """
    out = desc.compute_descriptive([_record("cpu_overload")])
    stats = out["S1"]["by_fault_type"]["cpu_overload"]["phases"]["T_observability_restore"]
    assert stats["applicable"] is False
    assert "n" not in stats, "a non-applicable phase must not report a sample count"


def test_applicable_phase_reports_a_sample_count():
    """The same field on instance_failure carries real statistics."""
    out = desc.compute_descriptive(
        [_record("instance_failure", T_observability_restore_seconds=240.0)]
    )
    stats = out["S1"]["by_fault_type"]["instance_failure"]["phases"]["T_observability_restore"]
    assert stats["applicable"] is True
    assert stats["n"] == 1 and stats["mean"] == 240.0


def test_applicable_but_unobserved_is_distinct_from_not_applicable(caplog):
    """Third state: the phase applies, trials exist, nothing was recorded.

    This is a collection failure, not a design property, and must be
    distinguishable from both the N/A case and a healthy population — in the
    artefact and in the operator's console.
    """
    out = desc.compute_descriptive([_record("instance_failure")])  # sentinel left at -1.0
    stats = out["S1"]["by_fault_type"]["instance_failure"]["phases"]["T_observability_restore"]
    assert stats["applicable"] is True, "must not be reported as not-applicable"
    assert stats["n"] == 0, "an applicable phase reports its (empty) sample count"
    assert any("investigate before reporting" in m for m in caplog.messages), \
        "an applicable-but-unobserved phase must warn, not fail silently"


def test_pooled_restricted_phase_records_its_population():
    """A pooled figure restricted to one class must say so in the artefact."""
    out = desc.compute_descriptive(
        [_record("cpu_overload"),
         _record("instance_failure", T_observability_restore_seconds=240.0)]
    )
    pooled = out["S1"]["phases_overall"]
    assert pooled["T_observability_restore"]["population"] == ["instance_failure"]
    assert "population" not in pooled["T_classify"], "unrestricted phases carry no marker"


def test_observability_phase_admitted_for_instance_failure():
    records = [
        _record("instance_failure", T_observability_restore_seconds=240.0),
        _record("cpu_overload"),
    ]
    out = desc.compute_descriptive(records)
    stats = out["S1"]["phases_overall"]["T_observability_restore"]
    assert stats["n"] == 1
    assert stats["mean"] == 240.0
