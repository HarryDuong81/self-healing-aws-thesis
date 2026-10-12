"""Unit tests for the calibration schedule generator and the host-side driver (no Prometheus, no AWS)."""
import json
import os
import tempfile
from pathlib import Path

from experiments.trials import make_calibration_schedule as mk
from experiments.trials import run_calibration_driver as drv


def test_schedule_meets_constraints_for_many_seeds():
    for seed in range(40):
        rows = mk.build(seed)
        assert mk.check(rows) == []
        assert len(rows) == 18
        assert [r["trial_id"] for r in rows] == [f"calib-{i:02d}" for i in range(1, 19)]


def test_trial_ids_never_contain_a_fault_class():
    rows = mk.build(7)
    for r in rows:
        assert not any(c in r["trial_id"] for c in mk.CLASSES)


def test_check_flags_a_target_reused_after_instance_failure():
    rows = mk.build(3)
    i = next(k for k, r in enumerate(rows) if r["fault"] == "instance_failure" and k + 1 < len(rows))
    rows[i + 1]["target"] = rows[i]["target"]
    assert any("reused" in p for p in mk.check(rows))


def test_main_prints_no_schedule_contents_and_sets_private_mode():
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "s.jsonl")
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            assert mk.main(["--out", out, "--seed", "1"]) == 0
        text = buf.getvalue()
        assert not any(c in text for c in mk.CLASSES) and not any(t in text for t in mk.TARGETS)
        rows = mk.load_schedule(out)  # _meta line is skipped
        assert len(rows) == 18
        first = json.loads(Path(out).read_text().splitlines()[0])
        assert "_meta" in first and first["_meta"]["seed"] == 1
        assert mk.main(["--out", out]) == 2  # refuses to overwrite


def test_build_cmd_contains_the_calibration_flag_and_row_values():
    row = {"seq": 4, "trial_id": "calib-04", "fault": "cpu_overload", "target": "target-2", "block": -1}
    cmd = drv.build_cmd(row, "python3", Path("/x/h.py"))
    assert cmd[:2] == ["python3", str(Path("/x/h.py"))]
    assert "--calibration" in cmd and cmd[cmd.index("--trial-id") + 1] == "calib-04"
    assert cmd[cmd.index("--block") + 1] == "-1"


def _q(active, ups):
    def q(expr):
        return active if expr.startswith("ALERTS") else ups
    return q


def _up(v):
    return {"value": [0, str(v)]}


def test_gate_ok_requires_no_active_alert_and_three_targets_up():
    assert drv.gate_ok(_q([], [_up(1)] * 3))
    assert not drv.gate_ok(_q([{"m": 1}], [_up(1)] * 3))
    assert not drv.gate_ok(_q([], [_up(1), _up(1), _up(0)]))
    assert not drv.gate_ok(_q([], [_up(1), _up(1)]))


def test_wait_for_gate_needs_consecutive_good_polls_and_times_out():
    seq = iter([True, True, False, True, True, True, True])
    calls = []

    def q(expr):
        if expr.startswith("ALERTS"):
            calls.append(1)
            return [] if next(seq) else [{"m": 1}]
        return [_up(1)] * 3

    t = [0.0]
    assert drv.wait_for_gate(q, timeout_s=100, consecutive=4, interval_s=1, sleep=lambda s: t.__setitem__(0, t[0] + s), now=lambda: t[0])
    t2 = [0.0]
    assert not drv.wait_for_gate(_q([{"m": 1}], [_up(1)] * 3), timeout_s=10, consecutive=2, interval_s=1,
                                 sleep=lambda s: t2.__setitem__(0, t2[0] + s), now=lambda: t2[0])


def test_dry_run_prints_only_trial_ids(capsys):
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "s.jsonl")
        mk.main(["--out", out, "--seed", "5"])
        capsys.readouterr()
        assert drv.main(["--schedule", out, "--dry-run", "--start", "2", "--end", "3"]) == 0
        printed = capsys.readouterr().out
    assert printed.strip().splitlines() == ["would run calib-02", "would run calib-03"]
    assert not any(c in printed for c in mk.CLASSES)
