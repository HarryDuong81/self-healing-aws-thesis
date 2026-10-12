"""
tests/unit/test_data_quality.py — Unit tests for analysis/04_data_quality.py.

A read-only diagnostic companion to 04_overhead.py: it either finds gaps in
the phase-tagged sample log (worth investigating before trusting 04_overhead's
output) or confirms there are none. Two independent checks:

  1. null cpu_cores/rss_mib values — a sample with a null value carried no
     usable data (e.g. overhead_sampler.py's own negative-CPU-delta
     rejection) and must be surfaced explicitly, not silently absorbed.
  2. per-container, per-phase trial coverage — every container should have
     at least one sample in both "steady" and "active" for every trial_id
     present in the file; a container missing from one phase of one trial
     (e.g. mid-restart during every poll tick of that phase) would silently
     understate its contribution without ever raising an error.

Thesis reference: §5.4 (RQ3 data underlying it).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ANALYSIS = Path(__file__).resolve().parent.parent.parent / "analysis"
if str(_ANALYSIS) not in sys.path:
    sys.path.insert(0, str(_ANALYSIS))

_spec = importlib.util.spec_from_file_location("data_quality_mod", _ANALYSIS / "04_data_quality.py")
dq = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dq)


def _sample(trial_id, phase, container, cpu=1.0, mem=10.0):
    return {"trial_id": trial_id, "phase": phase, "container": container,
            "cpu_cores": cpu, "rss_mib": mem}


# ── check_null_values ────────────────────────────────────────────────────────

def test_null_values_are_counted_and_surfaced_not_silently_dropped():
    samples = [
        _sample("t1", "steady", "prometheus", cpu=None, mem=10.0),
        _sample("t1", "steady", "prometheus", cpu=1.0, mem=None),
        _sample("t1", "active", "prometheus", cpu=1.0, mem=10.0),  # clean
    ]
    result = dq.check_null_values(samples)
    assert result["n_samples_total"] == 3
    assert result["n_null_cpu_cores"] == 1
    assert result["n_null_rss_mib"] == 1
    assert len(result["null_cpu_examples"]) == 1
    assert len(result["null_mem_examples"]) == 1


def test_clean_samples_report_zero_nulls():
    samples = [_sample("t1", "steady", "prometheus"), _sample("t1", "active", "prometheus")]
    result = dq.check_null_values(samples)
    assert result["n_null_cpu_cores"] == 0
    assert result["n_null_rss_mib"] == 0


# ── check_container_coverage ─────────────────────────────────────────────────

def test_full_coverage_across_all_trials_and_phases_is_clean():
    samples = []
    for trial in ("t1", "t2"):
        for phase in ("steady", "active"):
            for container in ("prometheus", "grafana"):
                samples.append(_sample(trial, phase, container))
    result = dq.check_container_coverage(samples)
    assert result["clean"] is True
    assert result["gaps"] == []
    assert result["n_distinct_trial_ids"] == 2


def test_a_container_missing_from_one_phase_of_one_trial_is_a_gap():
    """This is the exact failure mode the module exists to catch: a
    container mid-restart during every poll tick of one phase leaves a real,
    silent hole that a naive per-container total would never surface."""
    samples = [
        _sample("t1", "steady", "prometheus"),
        _sample("t1", "active", "prometheus"),
        _sample("t2", "steady", "prometheus"),
        # t2's "active" phase never got a prometheus sample.
    ]
    result = dq.check_container_coverage(samples)
    assert result["clean"] is False
    assert len(result["gaps"]) == 1
    assert "prometheus/active" in result["gaps"][0]
    assert "1 of 2" in result["gaps"][0]


def test_gap_report_names_the_specific_missing_trial_ids():
    samples = [
        _sample("t1", "steady", "prometheus"),
        _sample("t2", "steady", "prometheus"),
        # only t1 has an "active" sample for prometheus
        _sample("t1", "active", "prometheus"),
    ]
    result = dq.check_container_coverage(samples)
    assert result["per_container_phase_coverage"]["prometheus"]["steady_trials"] == 2
    assert result["per_container_phase_coverage"]["prometheus"]["active_trials"] == 1
    assert any("t2" in g for g in result["gaps"])


def test_container_present_in_only_one_phase_across_every_trial_is_still_a_gap():
    """A container that legitimately never runs during (e.g.) the active
    phase for any trial is exactly as real a gap as a sporadic one — the
    check does not special-case "always missing" as expected behaviour."""
    samples = [_sample("t1", "steady", "cadvisor"), _sample("t2", "steady", "cadvisor")]
    result = dq.check_container_coverage(samples)
    assert result["clean"] is False
    assert any("cadvisor/active" in g for g in result["gaps"])


# ── run_data_quality (combined) ──────────────────────────────────────────────

def test_run_data_quality_combines_both_checks():
    samples = [_sample("t1", "steady", "prometheus"), _sample("t1", "active", "prometheus")]
    result = dq.run_data_quality(samples)
    assert "null_values" in result
    assert "container_coverage" in result
    assert result["container_coverage"]["clean"] is True
