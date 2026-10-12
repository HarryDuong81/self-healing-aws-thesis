"""
tests/unit/test_overhead.py — Unit tests for analysis/04_overhead.py.

Two independent code paths live in this module and must not be conflated:

  1. aggregate_phase_samples() — the two-stage (per-trial median, then
     per-container median+IQR+p95 across trials) aggregation over
     experiments/trials/overhead_sampler.py's phase-tagged sample log. This
     is the path that produces the reported RQ3 figure (Thesis Table 5.8).
     Pooling raw samples across trials would weight a long trial
     (instance_failure) far more heavily than a short one (service_crash);
     the two-stage design exists specifically to avoid that.
  2. compute_overhead() — a single-snapshot cAdvisor-vs-baseline comparison,
     retained only as an operator spot-check. It cannot produce a median,
     IQR, or percentile and must never be the source of a reported result.

Thesis reference: §5.4, §3.7.3.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ANALYSIS = Path(__file__).resolve().parent.parent.parent / "analysis"
if str(_ANALYSIS) not in sys.path:
    sys.path.insert(0, str(_ANALYSIS))

_spec = importlib.util.spec_from_file_location("overhead_mod", _ANALYSIS / "04_overhead.py")
oh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(oh)


# ── Stage-1/stage-2 aggregation ──────────────────────────────────────────────

def _sample(trial_id, phase, container, cpu, mem, instrument=False):
    return {
        "trial_id": trial_id, "phase": phase, "container": container,
        "cpu_cores": cpu, "rss_mib": mem, "instrument": instrument,
    }


def test_stage_one_takes_median_per_trial_per_container():
    """Multiple ticks within one trial collapse to a single per-trial value
    before stage 2 ever sees them — a trial with more sampling ticks than
    another must not outweigh it."""
    samples = [
        _sample("t1", "steady", "prometheus", 0.10, 10.0),
        _sample("t1", "steady", "prometheus", 0.20, 20.0),
        _sample("t1", "steady", "prometheus", 0.30, 30.0),
    ]
    out = oh.aggregate_phase_samples(samples)
    per_container = out["phases"]["steady"]["per_container"]["prometheus"]
    assert per_container["cpu_cores_median"] == 0.20
    assert per_container["rss_mib_median"] == 20.0
    assert per_container["trials"] == 1


def test_stage_two_median_not_mean_across_trials():
    """A single outlier trial must not shift the aggregate the way a mean
    would — this is the whole reason stage 2 uses a median."""
    samples = []
    for i, cpu in enumerate([0.1, 0.1, 0.1, 10.0]):  # 3 normal + 1 wild outlier
        samples.append(_sample(f"t{i}", "steady", "prometheus", cpu, 10.0))
    out = oh.aggregate_phase_samples(samples)
    # Median of [0.1, 0.1, 0.1, 10.0] is (0.1+0.1)/2 = 0.1, not the
    # mean-dragged 2.575.
    assert out["phases"]["steady"]["per_container"]["prometheus"]["cpu_cores_median"] == 0.1


def test_long_trial_does_not_outweigh_short_trial_in_the_aggregate():
    """The property the module docstring states directly: instance_failure
    sampled for many ticks must not dominate a short-lived fault class's
    contribution to the aggregate, because stage 1 already collapsed each
    trial to one value before stage 2 runs."""
    # trial A: 1 sample tick (a short trial)
    samples = [_sample("short", "steady", "prometheus", 1.0, 100.0)]
    # trial B: 50 sample ticks (a long trial), all at a much lower value
    for i in range(50):
        samples.append(_sample("long", "steady", "prometheus", 0.01, 1.0))
    out = oh.aggregate_phase_samples(samples)
    med = out["phases"]["steady"]["per_container"]["prometheus"]["cpu_cores_median"]
    # Two trials contribute two per-trial values regardless of tick count:
    # median of [1.0, 0.01] = 0.505, not something close to 0.01 (which a
    # naive pool-all-samples approach would produce, since 50 of 51 raw
    # samples are 0.01).
    assert med == 0.505


def test_measurement_instrument_reported_separately_from_the_stack_aggregate():
    """cAdvisor (instrument=True) must never be summed into the eight-service
    aggregate (Thesis §3.7.3, Table 4.3 footnote) — it is the measurement
    tool, not one of the deliberately-deployed services being measured."""
    samples = [
        _sample("t1", "steady", "prometheus", 1.0, 100.0, instrument=False),
        _sample("t1", "steady", "cadvisor", 0.5, 50.0, instrument=True),
    ]
    out = oh.aggregate_phase_samples(samples)
    phase = out["phases"]["steady"]
    assert phase["aggregate_eight_services"]["cpu_cores_median"] == 1.0
    assert phase["measurement_instrument_cadvisor"]["cpu_cores_median"] == 0.5


def test_capacity_percentage_normalises_against_fixed_host_capacity():
    """RQ3's primary figure is % of the host's fixed 2 vCPU / 4096 MiB
    capacity, not the empty-host baseline (a near-zero, unstable
    denominator) — the module docstring is explicit about this."""
    samples = [_sample("t1", "steady", "prometheus", 1.0, 2048.0)]
    out = oh.aggregate_phase_samples(samples)
    agg = out["phases"]["steady"]["aggregate_eight_services"]
    assert agg["cpu_pct_capacity"] == round(100.0 * 1.0 / oh.HOST_VCPU, 2)
    assert agg["rss_pct_capacity"] == round(100.0 * 2048.0 / oh.HOST_MEM_MIB, 2)


def test_multiple_phases_are_kept_independent():
    """steady and active must be aggregated separately, never pooled — they
    represent different points in a trial's lifecycle."""
    samples = [
        _sample("t1", "steady", "prometheus", 1.0, 10.0),
        _sample("t1", "active", "prometheus", 5.0, 50.0),
    ]
    out = oh.aggregate_phase_samples(samples)
    assert set(out["phases"]) == {"steady", "active"}
    assert out["phases"]["steady"]["aggregate_eight_services"]["cpu_cores_median"] == 1.0
    assert out["phases"]["active"]["aggregate_eight_services"]["cpu_cores_median"] == 5.0


def test_capacity_denominator_and_primary_phase_are_reported():
    samples = [_sample("t1", "steady", "prometheus", 1.0, 10.0)]
    out = oh.aggregate_phase_samples(samples)
    assert out["capacity_denominator"] == {
        "vcpu": oh.HOST_VCPU, "memory_mib": oh.HOST_MEM_MIB, "instance_type": "t3.medium",
    }
    assert out["rq3_primary_phase"] == "steady"
    assert out["rq3_secondary_phase"] == "active"


# ── Single-snapshot spot-check path (compute_overhead) ───────────────────────

def test_snapshot_path_excludes_cadvisor_from_the_aggregate():
    baseline = {"host_baseline_cpu_used_pct_mean": 5.0, "host_baseline_rss_mib_mean": 200.0}
    container_stats = {
        "prometheus": {"cpu_cores": 0.1, "rss_mib": 100.0},
        "alertmanager": {"cpu_cores": 0.05, "rss_mib": 50.0},
        "grafana": {"cpu_cores": 0.05, "rss_mib": 50.0},
        "elasticsearch": {"cpu_cores": 0.2, "rss_mib": 400.0},
        "logstash": {"cpu_cores": 0.15, "rss_mib": 300.0},
        "kibana": {"cpu_cores": 0.1, "rss_mib": 250.0},
        "jaeger": {"cpu_cores": 0.05, "rss_mib": 60.0},
        "otelcol": {"cpu_cores": 0.05, "rss_mib": 40.0},
        "cadvisor": {"cpu_cores": 999.0, "rss_mib": 999.0},  # must not appear in aggregate
    }
    out = oh.compute_overhead(baseline, container_stats)
    assert out["aggregate"]["n_containers"] == 8
    assert out["aggregate"]["cadvisor_excluded"] is True
    assert out["aggregate"]["total_cpu_cores"] < 1.0  # would be >999 if cadvisor leaked in
    for pillar in out["pillars"].values():
        assert "cadvisor" not in pillar["containers"]


def test_snapshot_path_missing_container_defaults_to_zero_not_error():
    baseline = {"host_baseline_cpu_used_pct_mean": 0.0, "host_baseline_rss_mib_mean": 0.0}
    out = oh.compute_overhead(baseline, {})  # no containers reported at all
    assert out["aggregate"]["total_cpu_cores"] == 0.0
    assert out["aggregate"]["total_rss_mib"] == 0.0
