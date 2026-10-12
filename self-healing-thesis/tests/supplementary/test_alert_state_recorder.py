"""Unit tests for experiments/trials/alert_state_recorder.py (no Prometheus, no threads started)."""
from experiments.trials.alert_state_recorder import AlertStateRecorder


def _rec(seq, times):
    it = iter(seq)
    tt = iter(times)

    def q():
        v = next(it)
        if isinstance(v, Exception):
            raise v
        return v
    return AlertStateRecorder(q, interval_s=1.0, clock_ms=lambda: next(tt))


def test_records_first_pending_then_first_firing_only():
    r = _rec([set(), {"pending"}, {"pending"}, {"firing"}, {"firing"}], [100, 200, 300, 400, 500])
    for _ in range(5):
        r.poll_once()
    assert r.first_pending_ms == 200  # later pending samples never overwrite
    assert r.first_firing_ms == 400
    assert r.n_polls == 5


def test_unseen_states_stay_none_and_serialise_as_minus_one():
    r = _rec([set(), set()], [1, 2])
    r.poll_once(); r.poll_once()
    f = r.as_fields()
    assert f["t_alert_pending_seen_ms"] == -1 and f["t_alert_firing_seen_ms"] == -1


def test_firing_seen_without_pending_is_not_backfilled():
    r = _rec([{"firing"}], [50])
    r.poll_once()
    assert r.first_pending_ms is None and r.first_firing_ms == 50


def test_poll_errors_are_counted_not_raised():
    r = _rec([RuntimeError("boom"), {"pending"}], [20])
    r.poll_once(); r.poll_once()
    assert r.n_poll_errors == 1 and r.n_polls == 1 and r.first_pending_ms == 20
