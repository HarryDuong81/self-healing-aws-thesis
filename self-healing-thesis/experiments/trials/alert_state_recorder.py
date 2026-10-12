"""
experiments/trials/alert_state_recorder.py -- background recorder for the
Prometheus ALERTS state of one (fault_type, node_name) pair.

Used only by run_manual_trial_v2.py's --calibration mode. The S2-v2 harness
stops its detection clock (t_alert_ms) at the FIRST APPEARANCE of the ALERTS
series, which is the `pending` state; it never records when the alert starts
`firing`. S1's detection clock stops at webhook receipt, which can only happen
after the alert is firing. This recorder measures the missing milestone for S2:
it polls the same series every `interval_s` seconds from before the injection
and keeps the first time it saw each state. It never prints, so the operator is
told nothing about alert state beyond what the unchanged harness already shows.

Resolution: a timestamp is the local time of the poll that first saw the state,
so it lags the true Prometheus sample by at most `interval_s` plus the rule
evaluation interval. A state that is never seen (poll error, state shorter than
the interval) stays None; callers must not substitute a value.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional, Set


def _now_ms() -> int:
    return int(time.time() * 1000)


class AlertStateRecorder:
    def __init__(self, query_states: Callable[[], Set[str]], interval_s: float = 1.0,
                 clock_ms: Callable[[], int] = _now_ms):
        self._query = query_states
        self._interval = interval_s
        self._clock = clock_ms
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.first_pending_ms: Optional[int] = None
        self.first_firing_ms: Optional[int] = None
        self.n_polls = 0
        self.n_poll_errors = 0

    def poll_once(self) -> None:
        try:
            states = self._query()
        except Exception:
            self.n_poll_errors += 1
            return
        now = self._clock()
        self.n_polls += 1
        if "pending" in states and self.first_pending_ms is None:
            self.first_pending_ms = now
        if "firing" in states and self.first_firing_ms is None:
            self.first_firing_ms = now

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self._interval)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="alert-state-recorder", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def as_fields(self) -> dict:
        """Record fields; -1 means 'not observed', matching the harness's sentinel convention."""
        return {
            "t_alert_pending_seen_ms": -1 if self.first_pending_ms is None else self.first_pending_ms,
            "t_alert_firing_seen_ms":  -1 if self.first_firing_ms is None else self.first_firing_ms,
            "alert_state_poll_interval_s": self._interval,
            "alert_state_n_polls": self.n_polls,
            "alert_state_n_poll_errors": self.n_poll_errors,
        }
