"""
tests/unit/test_plan.py — Unit tests for orchestrator/plan.py

Covers:
  - CircuitBreaker: Closed → Open transition at threshold θ=3
  - CircuitBreaker: Open state blocks further alerts
  - CircuitBreaker: Half-Open probe admission after cooldown
  - CircuitBreaker: Reset on probe success
  - LeaseTracker: acquire() returns True when no lease held
  - LeaseTracker: acquire() returns False when lease active
  - LeaseTracker: expired lease is silently reclaimed
  - LeaseTracker: release() clears the lease

Thesis reference: §3.5.3 Plan-layer policies
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_ORCHESTRATOR = Path(__file__).resolve().parent.parent.parent / "orchestrator"
sys.path.insert(0, str(_ORCHESTRATOR))

from plan import CircuitBreaker, LeaseTracker


class TestCircuitBreaker:

    def _cb(self, threshold: int = 3, window: float = 30.0, cooldown: float = 30.0) -> CircuitBreaker:
        return CircuitBreaker(
            alert_threshold=threshold,
            window_seconds=window,
            cooldown_seconds=cooldown,
        )

    def test_initial_state_is_closed(self):
        cb = self._cb()
        assert cb.state("target-1", "cpu_overload") == "closed"

    def test_first_two_alerts_keep_breaker_closed(self):
        cb = self._cb()
        cb.check("target-1", "cpu_overload")
        assert cb.check("target-1", "cpu_overload") == "closed"

    def test_third_alert_trips_breaker(self):
        cb = self._cb(threshold=3)
        cb.check("target-1", "cpu_overload")
        cb.check("target-1", "cpu_overload")
        state = cb.check("target-1", "cpu_overload")
        assert state == "open"

    def test_open_state_blocks_additional_alerts(self):
        cb = self._cb(threshold=3)
        cb.check("target-1", "cpu_overload")
        cb.check("target-1", "cpu_overload")
        cb.check("target-1", "cpu_overload")  # trips
        state = cb.check("target-1", "cpu_overload")
        assert state == "open"

    def test_different_targets_are_independent(self):
        cb = self._cb(threshold=3)
        cb.check("target-1", "cpu_overload")
        cb.check("target-1", "cpu_overload")
        cb.check("target-1", "cpu_overload")  # trips for target-1
        assert cb.check("target-2", "cpu_overload") == "closed"

    def test_different_fault_types_are_independent(self):
        cb = self._cb(threshold=3)
        cb.check("target-1", "cpu_overload")
        cb.check("target-1", "cpu_overload")
        cb.check("target-1", "cpu_overload")  # trips cpu_overload
        assert cb.check("target-1", "service_crash") == "closed"

    def test_half_open_probe_admitted_after_cooldown(self):
        cb = self._cb(threshold=3, window=30.0, cooldown=0.1)   # very short cooldown for test
        cb.check("target-1", "cpu_overload")
        cb.check("target-1", "cpu_overload")
        cb.check("target-1", "cpu_overload")   # trips
        time.sleep(0.15)   # cooldown elapsed
        state = cb.check("target-1", "cpu_overload")
        assert state == "half_open"

    def test_state_read_only_does_not_record_alert(self):
        # threshold=3: one check() (count=1), then state() must NOT record —
        # if it did, the next check() would hit count=3 and trip open. The
        # original version of this test called check() twice before state(),
        # which reaches the threshold on the assertion's own check() call
        # regardless of whether state() records, so it could never fail.
        cb = self._cb(threshold=3)
        cb.check("target-1", "cpu_overload")
        # Read-only state query — must not count as a second alert
        cb.state("target-1", "cpu_overload")
        assert cb.check("target-1", "cpu_overload") == "closed"

    def test_half_open_reached_despite_alerts_during_cooldown(self):
        """
        Regression test for a livelock confirmed live in production: a
        persistently firing alert (Alertmanager repeat_interval << Tcool)
        redelivers every few seconds while the breaker is Open. Those
        redeliveries are not new failures — they must not reset the
        cooldown clock. Once Tcool has elapsed since the ORIGINAL open
        time, the breaker must reach Half-Open regardless of how many
        suppressed alerts arrived in the interim.
        """
        cb = self._cb(threshold=3, window=30.0, cooldown=0.3)
        cb.check("target-1", "service_crash")
        cb.check("target-1", "service_crash")
        cb.check("target-1", "service_crash")   # trips — breaker Open

        # Simulate repeat-interval redeliveries of the same still-firing
        # alert arriving faster than the cooldown elapses.
        for _ in range(4):
            time.sleep(0.1)
            state = cb.check("target-1", "service_crash")
            if state == "half_open":
                break
        else:
            state = cb.check("target-1", "service_crash")

        assert state == "half_open", (
            "breaker never reached half_open — cooldown was reset by "
            "redelivered alerts (livelock)"
        )

    def test_half_open_single_alert_reopens_immediately(self):
        """
        Per the class docstring's state table: "Half-Open -> Open: another
        alert arrives within Δt of the probe." A single alert during the
        probe window must reopen immediately — it must not require
        accumulating a fresh θ alerts as if the breaker were Closed.
        """
        cb = self._cb(threshold=3, window=10.0, cooldown=0.1)
        cb.check("target-1", "service_crash")
        cb.check("target-1", "service_crash")
        cb.check("target-1", "service_crash")   # trips — Open
        time.sleep(0.15)
        assert cb.check("target-1", "service_crash") == "half_open"   # probe admitted

        state = cb.check("target-1", "service_crash")
        assert state == "open", (
            "a single alert during Half-Open must reopen immediately, "
            "not require a fresh θ alerts"
        )

    def test_half_open_closes_after_quiet_window(self):
        """
        Per the class docstring: "Half-Open -> Closed: probe alert succeeds
        without further alerts in Δt." After the probe, Δt of silence
        transitions to Closed, and a subsequent alert is treated as a new,
        independent single strike.
        """
        cb = self._cb(threshold=3, window=0.2, cooldown=0.1)
        cb.check("target-1", "service_crash")
        cb.check("target-1", "service_crash")
        cb.check("target-1", "service_crash")   # trips — Open
        time.sleep(0.15)
        assert cb.check("target-1", "service_crash") == "half_open"   # probe admitted

        time.sleep(0.25)   # Δt elapses with no further alerts
        assert cb.state("target-1", "service_crash") == "closed"
        assert cb.check("target-1", "service_crash") == "closed"

    def test_opened_at_not_reset_by_alerts_within_cooldown(self):
        cb = self._cb(threshold=3, window=30.0, cooldown=0.3)
        cb.check("target-1", "service_crash")
        cb.check("target-1", "service_crash")
        cb.check("target-1", "service_crash")   # trips at t=0

        time.sleep(0.2)
        assert cb.check("target-1", "service_crash") == "open"   # still within cooldown

        time.sleep(0.2)   # cumulative 0.4s since original open > 0.3s cooldown
        assert cb.check("target-1", "service_crash") == "half_open"


class TestLeaseTracker:

    def _lt(self, ttl: float = 60.0) -> LeaseTracker:
        return LeaseTracker(ttl_seconds=ttl)

    def test_acquire_returns_true_when_no_lease(self):
        lt = self._lt()
        assert lt.acquire("target-1") is True

    def test_acquire_returns_false_when_lease_active(self):
        lt = self._lt()
        lt.acquire("target-1")
        assert lt.acquire("target-1") is False

    def test_release_allows_reacquire(self):
        lt = self._lt()
        lt.acquire("target-1")
        lt.release("target-1")
        assert lt.acquire("target-1") is True

    def test_expired_lease_silently_reclaimed(self):
        lt = self._lt(ttl=0.05)   # 50 ms TTL
        lt.acquire("target-1")
        time.sleep(0.1)            # wait for expiry
        assert lt.acquire("target-1") is True

    def test_different_targets_are_independent(self):
        lt = self._lt()
        lt.acquire("target-1")
        assert lt.acquire("target-2") is True

    def test_release_of_unheld_lease_is_noop(self):
        lt = self._lt()
        lt.release("target-1")   # should not raise
        assert lt.acquire("target-1") is True

    def test_trial_id_stored_with_lease(self):
        lt = self._lt()
        lt.acquire("target-1", trial_id="t-abc123")
        # Attempting acquire again while active — False regardless of trial_id
        assert lt.acquire("target-1", trial_id="t-xyz456") is False
