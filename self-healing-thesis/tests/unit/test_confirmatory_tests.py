"""
tests/unit/test_confirmatory_tests.py — Unit tests for analysis/02_wilcoxon.py.

The property under test is that the statistical test is chosen by the
experimental design and never by the runtime sample sizes:

    experimental unit -> dependency structure -> hypothesis -> test

H1 (S1 vs S2) is paired by construction over (fault_type, target, block);
H2 (S1 vs S3) is independent because S3 has no shared unit identity. There is
no fallback between them. A population that does not match its declared design
must raise ValidationError, so that an invalid design surfaces as a failure
rather than as a silently substituted test.

Thesis reference: §3.1.5 (statistical analysis), §5.3.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ANALYSIS = Path(__file__).resolve().parent.parent.parent / "analysis"
if str(_ANALYSIS) not in sys.path:
    sys.path.insert(0, str(_ANALYSIS))

_spec = importlib.util.spec_from_file_location("wilcoxon_mod", _ANALYSIS / "02_wilcoxon.py")
wx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wx)

pytest.importorskip("scipy")

_FAULTS = ["cpu_overload", "memory_exhaustion", "disk_saturation",
           "service_crash", "configuration_drift", "instance_failure"]
_TARGETS = ["target-1", "target-2", "target-3"]


def _rec(system, fault, target, block, mttr, status="success"):
    return {
        "trial_id": f"{system}-{fault}-{target}-{block}",
        "system_under_test": system,
        "fault_type": fault,
        "target": target,
        "block": block,
        "recovery_status": status,
        "T_MTTR_seconds": mttr,
    }


def _paired_campaign(s1_mttr=30.0, s2_mttr=300.0):
    """60 units, each executed once in S1 and once in S2."""
    records, n = [], 0
    for block in range(1, 11):
        for fault in _FAULTS:
            target = _TARGETS[n % 3]
            n += 1
            records.append(_rec("S1", fault, target, block, s1_mttr + n * 0.1))
            records.append(_rec("S2", fault, target, block, s2_mttr + n * 0.1))
    return records


def _h2_population():
    """10 S1 + 10 S3 instance_failure trials."""
    recs = []
    for i in range(10):
        recs.append(_rec("S1", "instance_failure", _TARGETS[i % 3], i + 1, 90.0 + i))
        recs.append(_rec("S3", "instance_failure", "asg-instance", -1, 200.0 + i))
    return recs


# ── H1: paired, and paired for the right reason ─────────────────────────────

def test_h1_runs_paired_wilcoxon_on_sixty_units():
    out = wx.run_wilcoxon_h1(_paired_campaign())
    assert out["test"] == "wilcoxon_signed_rank"
    assert out["paired"] is True
    assert out["n_pairs"] == 60
    assert out["experimental_unit"] == "(fault_type, target, block)"
    assert out["effect_size"]["measure"] == "matched_pairs_rank_biserial"
    assert out["validation_status"] == "passed"


def test_h1_fails_when_a_unit_has_no_counterpart():
    """A missing S2 trial must stop the analysis, not shrink the sample."""
    records = _paired_campaign()
    records = [r for r in records
               if not (r["system_under_test"] == "S2" and r["block"] == 5
                       and r["fault_type"] == "cpu_overload")]
    with pytest.raises(wx.ValidationError, match="no S2 counterpart"):
        wx.run_wilcoxon_h1(records)


def test_h1_fails_on_duplicate_unit():
    records = _paired_campaign()
    records.append(_rec("S1", "cpu_overload", "target-1", 1, 42.0))
    with pytest.raises(wx.ValidationError, match="more than once"):
        wx.run_wilcoxon_h1(records)


def test_h1_fails_when_a_trial_carries_no_schedule_block():
    """Ad-hoc trials (block = -1) are excluded from the arm by _arm() rather
    than flagged as a validation problem directly (see the _arm() tests
    below) -- but excluding a record that WAS part of the campaign design
    orphans its counterpart unit, so this still surfaces as a
    ValidationError, just via the missing-unit check."""
    records = _paired_campaign()
    for r in records:
        if r["system_under_test"] == "S2" and r["block"] == 3:
            r["block"] = -1
    with pytest.raises(wx.ValidationError, match="no S2 counterpart"):
        wx.run_wilcoxon_h1(records)


def test_h1_never_falls_back_to_an_unpaired_test():
    """The historic defect: mismatched lengths silently became Mann-Whitney."""
    records = [r for r in _paired_campaign() if r["system_under_test"] == "S1"]
    with pytest.raises(wx.ValidationError):
        wx.run_wilcoxon_h1(records)


# ── _arm(): the block <= 0 exclusion boundary ───────────────────────────────
#
# Deployment-session addition (2026-09-21): _arm() used to pass every
# analysable record through to validate_h1_pairing/validate_h2_populations,
# which then raised on block <= 0 as if it were always a corrupted record.
# But an analysable record (recovery_status=success, T_MTTR>0) with block=-1
# is exactly what a genuine, successful RQ3 scoped-measurement trial or one
# of §15.3's accidental live injections looks like -- a documented
# non-campaign sentinel, not an error. _arm() now excludes block <= 0
# records itself; these four tests pin that exclusion boundary and confirm
# it doesn't quietly swallow an actually-corrupted campaign record too.

def test_arm_excludes_ad_hoc_block_sentinel():
    """block = -1 (inject_once.py's default) must never enter a population,
    even though it is fully analysable on its own terms."""
    rec = _rec("S1", "cpu_overload", "target-1", -1, 42.0)
    assert wx._arm([rec], "S1") == []


def test_arm_includes_real_schedule_block():
    rec = _rec("S1", "cpu_overload", "target-1", 3, 42.0)
    assert wx._arm([rec], "S1") == [rec]


def test_arm_excludes_missing_block_key():
    rec = _rec("S1", "cpu_overload", "target-1", 3, 42.0)
    del rec["block"]
    assert wx._arm([rec], "S1") == []


@pytest.mark.parametrize("bad_block", [None, "3"])
def test_arm_excludes_wrong_typed_block(bad_block):
    """isinstance guard: a stringified block ("3") must not slip past a
    bare `> 0` comparison -- Python happily compares "3" > 0 as True only
    if someone drops the isinstance check first."""
    rec = _rec("S1", "cpu_overload", "target-1", bad_block, 42.0)
    assert wx._arm([rec], "S1") == []


def test_arm_exclusion_does_not_hide_a_corrupted_campaign_record():
    """The property the module docstring claims, demonstrated rather than
    just asserted: dropping a genuine campaign record because its block was
    lost (e.g. a broken schedule join) still surfaces as a raised
    ValidationError via the orphaned-counterpart-unit check -- not a
    silently shrunk 59-pair H1 result that would understate the design."""
    records = _paired_campaign()
    for r in records:
        if r["system_under_test"] == "S1" and r["block"] == 7 \
                and r["fault_type"] == "disk_saturation":
            r["block"] = None  # simulates a corrupted schedule join
    with pytest.raises(wx.ValidationError, match="no S1 counterpart"):
        wx.run_wilcoxon_h1(records)


# ── H2: independent, scoped to instance_failure ─────────────────────────────

def test_h2_runs_mann_whitney_on_ten_versus_ten():
    out = wx.run_mann_whitney_h2(_h2_population())
    assert out["test"] == "mann_whitney_u"
    assert out["paired"] is False
    assert out["n1"] == 10 and out["n2"] == 10
    assert out["fault_scope"] == "instance_failure only"
    assert out["validation_status"] == "passed"


def test_h2_excludes_other_fault_classes_from_s1():
    """The historic defect: all 60 S1 trials were compared against S3."""
    records = _h2_population() + [
        _rec("S1", "cpu_overload", "target-1", 1, 20.0),
        _rec("S1", "service_crash", "target-2", 1, 25.0),
    ]
    out = wx.run_mann_whitney_h2(records)
    assert out["n1"] == 10, "non-instance_failure S1 trials leaked into H2"


def test_h2_fails_on_wrong_population_size():
    records = [r for r in _h2_population()
               if not (r["system_under_test"] == "S3" and r["T_MTTR_seconds"] > 205.0)]
    with pytest.raises(wx.ValidationError, match="the design specifies 10"):
        wx.run_mann_whitney_h2(records)


def test_h2_ignores_capacity_only_sentinel_rows():
    """S3 cpu/memory rows carry T_MTTR = -1.0 and must not enter any test."""
    records = _h2_population() + [
        _rec("S3", "cpu_overload", "asg-instance", -1, -1.0, status="capacity_added"),
        _rec("S3", "memory_exhaustion", "asg-instance", -1, -1.0, status="capacity_added"),
    ]
    out = wx.run_mann_whitney_h2(records)
    assert out["n2"] == 10


# ── Orchestration: a failure is reported, not swallowed ─────────────────────

def test_compute_tests_records_validation_failure_without_raising():
    records = _h2_population()          # no S2 arm at all -> H1 must fail
    out = wx.compute_tests(records)
    assert out["H1_S1_vs_S2"]["validation_status"] == "failed"
    assert out["H1_S1_vs_S2"]["test"] is None
    assert out["H2_S1_vs_S3"]["validation_status"] == "passed"


def test_bonferroni_adjusted_alpha_is_applied():
    out = wx.run_wilcoxon_h1(_paired_campaign())
    assert out["alpha"] == 0.05
    assert out["alpha_adjusted"] == 0.025
    assert out["significant"] == (out["p_value"] < 0.025)
