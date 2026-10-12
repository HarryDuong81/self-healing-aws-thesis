"""
tests/unit/test_05_sensitivity.py — Unit tests for analysis/05_sensitivity.py.

Companion to 05_false_positive_rate.py: answers whether the reported RQ4
rate survives excluding alert-log entries already confirmed (by trial_id) to
be an artefact of a since-fixed infrastructure defect (CLAUDE.md §15.6), not
a general reclassification of anything resembling one. Properties under
test:

  - Exclusion is exact-match on matching_trial_ids, not fuzzy/substring.
  - Excluded entries are removed from BOTH numerator and denominator — they
    would never have been delivered at all under the fixed configuration,
    not merely reclassified as true positives.
  - An excluded entry appears once in the output, never double-counted
    between "excluded_entries" and "n_false_positive_remaining_unexcluded".

Thesis reference: §3.7.3.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ANALYSIS = Path(__file__).resolve().parent.parent.parent / "analysis"
if str(_ANALYSIS) not in sys.path:
    sys.path.insert(0, str(_ANALYSIS))

_spec = importlib.util.spec_from_file_location("sens05_mod", _ANALYSIS / "05_sensitivity.py")
s05 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(s05)


def _fp_entry(trial_ids):
    return {"matching_trial_ids": trial_ids, "target": "target-2", "fault_type": "instance_failure"}


def _official(n_fp, n_denom, details):
    return {
        "false_positive_rate": round(n_fp / n_denom, 4),
        "n_false_positive": n_fp,
        "n_alerts_in_denominator": n_denom,
        "false_positive_details": details,
    }


def test_exclusion_is_exact_match_on_trial_id():
    details = [_fp_entry(["t-2026-09-16-4e5eaa5e"]), _fp_entry(["t-2026-09-13-other"])]
    official = _official(n_fp=2, n_denom=10, details=details)
    result = s05.run_sensitivity(official, {"t-2026-09-16-4e5eaa5e"})
    assert result["n_entries_excluded"] == 1
    assert result["excluded_entries"][0]["matching_trial_ids"] == ["t-2026-09-16-4e5eaa5e"]


def test_excluded_entries_are_removed_from_both_numerator_and_denominator():
    details = [_fp_entry(["burst-trial"]) for _ in range(17)] + [_fp_entry(["other"]) for _ in range(28)]
    official = _official(n_fp=45, n_denom=110, details=details)
    result = s05.run_sensitivity(official, {"burst-trial"})
    assert result["sensitivity_adjusted"]["n_false_positive"] == 45 - 17
    assert result["sensitivity_adjusted"]["n_alerts_in_denominator"] == 110 - 17
    assert result["sensitivity_adjusted"]["false_positive_rate"] == round(28 / 93, 4)


def test_official_figures_are_preserved_unmodified_alongside_the_adjustment():
    details = [_fp_entry(["burst-trial"])]
    official = _official(n_fp=45, n_denom=110, details=details)
    result = s05.run_sensitivity(official, {"burst-trial"})
    assert result["official"]["n_false_positive"] == 45
    assert result["official"]["n_alerts_in_denominator"] == 110
    assert result["official"]["false_positive_rate"] == official["false_positive_rate"]


def test_no_matching_trial_id_leaves_the_rate_unchanged():
    details = [_fp_entry(["irrelevant-trial"])]
    official = _official(n_fp=1, n_denom=10, details=details)
    result = s05.run_sensitivity(official, {"not-present-anywhere"})
    assert result["n_entries_excluded"] == 0
    assert result["sensitivity_adjusted"]["n_false_positive"] == 1
    assert result["sensitivity_adjusted"]["n_alerts_in_denominator"] == 10


def test_an_entry_with_multiple_matching_trial_ids_excludes_on_any_intersection():
    details = [_fp_entry(["trial-a", "trial-b"])]
    official = _official(n_fp=1, n_denom=5, details=details)
    result = s05.run_sensitivity(official, {"trial-b"})
    assert result["n_entries_excluded"] == 1


def test_remaining_unexcluded_count_is_complementary_not_double_counted():
    details = [_fp_entry(["excluded-1"]), _fp_entry(["kept-1"]), _fp_entry(["kept-2"])]
    official = _official(n_fp=3, n_denom=20, details=details)
    result = s05.run_sensitivity(official, {"excluded-1"})
    assert result["n_entries_excluded"] == 1
    assert result["n_false_positive_remaining_unexcluded"] == 2
    assert result["n_entries_excluded"] + result["n_false_positive_remaining_unexcluded"] == len(details)


def test_excluding_everything_yields_a_zero_denominator_rate_of_none():
    details = [_fp_entry(["only-one"])]
    official = _official(n_fp=1, n_denom=1, details=details)
    result = s05.run_sensitivity(official, {"only-one"})
    assert result["sensitivity_adjusted"]["n_alerts_in_denominator"] == 0
    assert result["sensitivity_adjusted"]["false_positive_rate"] is None


def test_main_rejects_a_result_file_missing_false_positive_details(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text('{"false_positive_rate": 0.5, "n_false_positive": 1, "n_alerts_in_denominator": 2}')
    exit_code = s05.main([
        "--false-positive-rate-result", str(bad),
        "--exclude-trial-id", "whatever",
        "--out", str(tmp_path / "out.json"),
    ])
    assert exit_code == 2
