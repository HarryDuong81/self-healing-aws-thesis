#!/usr/bin/env python3
"""
plan.py — Plan-layer safety preconditions for the MAPE-K self-healing pipeline.

Implements the four pre-Execute gates in canonical order (Thesis §3.5.3):

  1. Circuit Breaker   — per (target, fault_type) tuple
  2. Lease Tracker     — per-instance concurrency control
  3. Transport         — SSM Agent reachability
  4. Drift Validation  — Terraform -refresh-only (Terraform path only)

All policy parameters are declared here as constants (policy/mechanism
separation).  The Execute layer (execute.py) only reads the retry
schedule; it does not make admission decisions.

Circuit Breaker state machine:
  Closed    — normal operation; alerts increment the alert-history counter.
  Open      — remediation suppressed; SNS escalation emitted; auto-resets after
              Tcool = 30 min from last alert.
  Half-Open — transition: after Tcool elapses, the next alert is admitted as a
              single probe.  If the probe succeeds, transition back to Closed.
              If the probe triggers another alert within Δt, transition to Open.
"""

from __future__ import annotations

import json
import re
import logging
import os
import signal
import subprocess
import time
from collections import defaultdict, deque
from threading import Lock
from typing import Optional

import boto3

logger = logging.getLogger(__name__)

# ── Policy constants (Thesis §3.5.3) ──────────────────────────────────────────
# Changing these between trials of a single campaign invalidates the dataset.

_CB_ALERT_THRESHOLD  = 3           # θ: alerts within window → Open
_CB_WINDOW_SECONDS   = 30 * 60    # Δt = 30 minutes
_CB_COOLDOWN_SECONDS = 30 * 60    # Tcool = 30 minutes

_LEASE_TTL_DEFAULT   = 600        # seconds — covers max remediation + validation

_TERRAFORM_TARGETS_DIR = os.environ.get(
    "TERRAFORM_TARGETS_DIR",
    os.path.join(os.path.dirname(__file__), "..", "terraform", "targets"),
)

_AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")

# DynamoDB state lock (Thesis Ch3 §3.3.2 / §3.7.1 Table 3.5, Ch4 §4.2.2).
# Must match the table provisioned by terraform/modules/storage and the
# targets workspace's backend.tf -backend-config="dynamodb_table=...".
_DYNAMODB_LOCK_TABLE = os.environ.get("DYNAMODB_LOCK_TABLE", "")
_TF_STATE_BUCKET     = os.environ.get("TF_STATE_BUCKET", "")
_TF_STATE_KEY        = os.environ.get("TF_STATE_KEY", "targets/terraform.tfstate")


# =============================================================================
# 1. Circuit Breaker
# =============================================================================

class CircuitBreaker:
    """
    3-state circuit breaker tracked per (target, fault_type) tuple.

    State transitions:
      Closed  → Open:      ≥ θ alerts in Δt window
      Open    → Half-Open: Tcool elapses after the last-seen alert
      Half-Open → Closed:  probe alert succeeds without further alerts in Δt
      Half-Open → Open:    another alert arrives within Δt of the probe

    Thread safety: guarded by a single reentrant lock; safe under multi-threaded
    WSGI but the design is single-worker Gunicorn so contention is negligible.
    """

    def __init__(
        self,
        alert_threshold: int   = _CB_ALERT_THRESHOLD,
        window_seconds: float  = _CB_WINDOW_SECONDS,
        cooldown_seconds: float = _CB_COOLDOWN_SECONDS,
    ) -> None:
        self._threshold = alert_threshold
        self._window    = window_seconds
        self._cooldown  = cooldown_seconds

        # alert_history[key] = deque of monotonic timestamps (bounded to 50)
        self._history: dict[tuple[str, str], deque[float]] = defaultdict(
            lambda: deque(maxlen=50)
        )
        # opened_at[key] = monotonic timestamp when breaker opened
        self._opened_at: dict[tuple[str, str], float] = {}
        # half_open[key] = monotonic timestamp the probe was admitted.
        # Tracked separately from _opened_at/_history so that a single
        # alert arriving during the probe window reopens immediately
        # (θ alerts is the Closed-state trip condition; Half-Open is a
        # single-strike probe per the class docstring's state table).
        self._half_open: dict[tuple[str, str], float] = {}

        self._lock = Lock()

    def check(self, target: str, fault_type: str) -> str:
        """
        Record this alert and return the current state string.

        Returns:
          "closed"    — proceed with remediation
          "half_open" — single probe admitted
          "open"      — remediation must be suppressed
        """
        key = (target, fault_type)
        now = time.monotonic()

        with self._lock:
            # Half-Open: a probe was admitted and we're watching for either
            # a quiet Δt window (→ Closed) or another alert within it
            # (→ Open, single-strike — the probe already used its one
            # chance). Checked before recording, since this branch decides
            # for itself how the arriving alert should be recorded.
            if key in self._half_open:
                probe_time = self._half_open[key]
                if now - probe_time >= self._window:
                    # Δt elapsed with no further alerts since the probe —
                    # the probe succeeded. This alert is new, independent
                    # signal against a now-Closed breaker.
                    del self._half_open[key]
                    self._history[key].clear()
                    self._history[key].append(now)
                    logger.info(
                        "Circuit breaker CLOSED | target=%s fault_type=%s "
                        "(probe succeeded — no further alerts within %.0fs)",
                        target, fault_type, self._window,
                    )
                    return "closed"
                else:
                    del self._half_open[key]
                    self._opened_at[key] = now
                    self._history[key].clear()
                    self._history[key].append(now)
                    logger.warning(
                        "Circuit breaker RE-OPENED | target=%s fault_type=%s "
                        "(alert arrived %.0fs after probe, within the %.0fs "
                        "window — single-strike Half-Open failure)",
                        target, fault_type, now - probe_time, self._window,
                    )
                    return "open"

            # Record the new alert
            self._history[key].append(now)

            # Determine current state
            if key in self._opened_at:
                opened = self._opened_at[key]
                # Check for cooldown expiry (transition to Half-Open).
                #
                # Tcool is anchored to the ORIGINAL open timestamp and must
                # never be reset by alerts that arrive while already Open.
                # Alertmanager keeps redelivering a still-firing alert every
                # repeat_interval (5 min, routing config) regardless of
                # whether remediation is actually being attempted — those
                # redeliveries are not new failures, they're the same
                # unresolved incident being re-announced. The previous
                # implementation both (a) reset _opened_at on every such
                # redelivery, and (b) gated the Half-Open transition on
                # "at most 1 alert in the trailing Δt window", which is
                # never true once repeat_interval < Δt for a persistent
                # fault. Together those made Half-Open unreachable and the
                # breaker stayed OPEN indefinitely — confirmed live: a
                # ServiceCrash alert kept the breaker open for 12+ hours
                # because every 5-minute redelivery restarted the cooldown.
                # Once Δt has genuinely elapsed since opening, admit exactly
                # one probe unconditionally.
                if now - opened >= self._cooldown:
                    logger.info(
                        "Circuit breaker HALF-OPEN probe admitted | "
                        "target=%s fault_type=%s (cooldown of %.0fs elapsed "
                        "since opening)", target, fault_type, self._cooldown,
                    )
                    del self._opened_at[key]
                    self._half_open[key] = now
                    self._history[key].clear()
                    self._history[key].append(now)
                    return "half_open"
                else:
                    logger.warning(
                        "Circuit breaker OPEN | target=%s fault_type=%s "
                        "(opened %.0fs ago, cooldown %.0fs remaining)",
                        target, fault_type,
                        now - opened,
                        self._cooldown - (now - opened),
                    )
                    return "open"

            # Breaker is closed — check if threshold is reached
            recent = self._recent(key, now)
            if len(recent) >= self._threshold:
                self._opened_at[key] = now
                logger.error(
                    "Circuit breaker TRIPPED | target=%s fault_type=%s "
                    "(%d alerts in %.0fs window). Cooldown: %.0fs.",
                    target, fault_type,
                    len(recent), self._window, self._cooldown,
                )
                return "open"

            return "closed"

    def _recent(self, key: tuple[str, str], now: float) -> list[float]:
        """Return timestamps within the sliding window."""
        return [t for t in self._history[key] if now - t <= self._window]

    def state(self, target: str, fault_type: str) -> str:
        """Read-only state query without recording a new alert."""
        key = (target, fault_type)
        now = time.monotonic()
        with self._lock:
            if key in self._half_open:
                probe_time = self._half_open[key]
                if now - probe_time >= self._window:
                    return "closed"
                return "half_open"
            if key not in self._opened_at:
                return "closed"
            opened = self._opened_at[key]
            if now - opened >= self._cooldown:
                return "half_open"
            return "open"

    def reset(self, target: str, fault_type: str) -> None:
        """Manually reset the breaker to Closed (operator intervention)."""
        key = (target, fault_type)
        with self._lock:
            self._history.pop(key, None)
            self._opened_at.pop(key, None)
            self._half_open.pop(key, None)
        logger.info(
            "Circuit breaker manually reset to Closed | target=%s fault_type=%s",
            target, fault_type,
        )


# =============================================================================
# 2. Lease Tracker
# =============================================================================

class LeaseTracker:
    """
    Per-instance concurrency control via TTL-bounded leases.

    Prevents simultaneous remediations against the same target instance.
    A lease acquired at time T expires at T + ttl_seconds regardless of
    whether the remediation completes.  The Execute layer must release the
    lease on completion (success or failure).

    Thread safety: guarded by a per-tracker lock.
    """

    def __init__(self, ttl_seconds: int = _LEASE_TTL_DEFAULT) -> None:
        self._ttl  = ttl_seconds
        # leases[target] = (monotonic expiry timestamp, trial_id)
        self._leases: dict[str, tuple[float, str]] = {}
        self._lock  = Lock()

    def acquire(self, target: str, trial_id: str = "") -> bool:
        """
        Attempt to acquire the lease for `target`.

        Returns True on success, False if the lease is currently held.
        Expired leases are silently reclaimed on entry.
        """
        now = time.monotonic()
        with self._lock:
            if target in self._leases:
                expiry, held_by = self._leases[target]
                if now < expiry:
                    logger.warning(
                        "Lease HELD for target=%s (held by trial_id=%s, "
                        "expires in %.0fs)",
                        target, held_by, expiry - now,
                    )
                    return False
                # Expired — reclaim silently
                logger.info(
                    "Reclaimed expired lease for target=%s (was held by %s)",
                    target, held_by,
                )
            self._leases[target] = (now + self._ttl, trial_id)
            logger.debug("Lease acquired | target=%s ttl=%ds", target, self._ttl)
            return True

    def release(self, target: str) -> None:
        """Release the lease for `target`.  Safe to call if not held."""
        with self._lock:
            if self._leases.pop(target, None) is not None:
                logger.debug("Lease released | target=%s", target)

    def is_held(self, target: str) -> bool:
        """Check if the lease is currently held (non-expired)."""
        now = time.monotonic()
        with self._lock:
            if target not in self._leases:
                return False
            expiry, _ = self._leases[target]
            return now < expiry


# =============================================================================
# 3. Transport Validation
# =============================================================================

def transport_validation(target: str, instance_id: Optional[str] = None) -> bool:
    """
    SSM control-plane readiness check for `target` before dispatching.

    Queries describe_instance_information() and passes only when the instance
    is present in the SSM inventory with PingStatus == "Online".  This is a
    control-plane readiness signal; it does not exercise the SSH transport the
    Ansible actuator actually uses.  The name is retained for continuity with
    the escalation_reason value "transport_unreachable", which is part of the
    closed vocabulary of the MTTR record schema.

    Returns True when the target reports ready, False otherwise.

    Missing-label path: the check requires the `ec2_instance_id` alert label.
    Every fault-class rule must preserve that label through any aggregation it
    performs (see the `by (...)` clause of CPUOverload).  If the label is
    absent the gate cannot be evaluated, and this function FAILS CLOSED --
    returning False so the remediation aborts and escalates rather than
    dispatching past an unevaluated safety gate.

    This behaviour was previously fail-open (`return True`), which silently
    bypassed gate 3 for every cpu_overload trial, because that rule's
    `by (instance, node_name)` clause dropped `ec2_instance_id`.  An
    unevaluable gate is treated as a failed gate.
    """
    if not instance_id:
        logger.error(
            "SSM readiness gate FAILED CLOSED | target=%s — the alert carried "
            "no ec2_instance_id label, so readiness could not be evaluated. "
            "Check that the firing rule preserves that label through its "
            "aggregation clause.",
            target,
        )
        return False

    try:
        ssm = boto3.client("ssm", region_name=_AWS_REGION)
        response = ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
        )
        info_list = response.get("InstanceInformationList", [])
        if not info_list:
            logger.error(
                "SSM transport validation FAILED | target=%s instance_id=%s "
                "— instance not found in SSM inventory",
                target, instance_id,
            )
            return False
        ping_status = info_list[0].get("PingStatus", "Unknown")
        if ping_status != "Online":
            logger.error(
                "SSM transport validation FAILED | target=%s instance_id=%s "
                "PingStatus=%s",
                target, instance_id, ping_status,
            )
            return False
        logger.debug(
            "SSM transport validation OK | target=%s instance_id=%s",
            target, instance_id,
        )
        logger.info(
            "SSM readiness gate PASSED | target=%s instance_id=%s PingStatus=%s",
            target, instance_id, ping_status,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "SSM transport validation ERROR | target=%s instance_id=%s exc=%s",
            target, instance_id, exc,
        )
        return False


# =============================================================================
# 4a. DynamoDB stale-lock precondition (Thesis Ch4 §4.2.2)
# =============================================================================

def dynamodb_lock_status(
    table_name: str = _DYNAMODB_LOCK_TABLE,
    bucket: str = _TF_STATE_BUCKET,
    key: str = _TF_STATE_KEY,
) -> Optional[dict]:
    """
    Query the DynamoDB state-lock table directly for an existing lock on the
    targets workspace state file.

    "If the Plan layer finds a stale lock — that is, a lock held with no
    active operation — the remediation is aborted and an SNS escalation is
    emitted instead of waiting indefinitely." (Ch4 §4.2.2)

    This is a fast pre-check that short-circuits before invoking
    `terraform plan -refresh-only`, rather than waiting on Terraform's own
    internal lock-acquisition retry/backoff. Any lock found — whether held
    by a genuinely in-flight operation (e.g. an in-progress CI/CD deployment,
) or a truly orphaned one — is treated identically: abort
    now rather than block the trial indefinitely.

    Returns the lock's Info attribute dict if a lock is currently held,
    else None. Best-effort: if table_name/bucket are not configured
    (DYNAMODB_LOCK_TABLE / TF_STATE_BUCKET env vars unset), skips the check
    and returns None so drift_validation() can still proceed via Terraform's
    own lock handling.
    """
    if not table_name or not bucket:
        logger.debug(
            "dynamodb_lock_status: DYNAMODB_LOCK_TABLE/TF_STATE_BUCKET not "
            "configured — skipping pre-check"
        )
        return None

    lock_id = f"{bucket}/{key}"
    try:
        dynamodb = boto3.client("dynamodb", region_name=_AWS_REGION)
        resp = dynamodb.get_item(
            TableName=table_name,
            Key={"LockID": {"S": lock_id}},
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "dynamodb_lock_status: DynamoDB query failed for lock_id=%s: %s",
            lock_id, exc,
        )
        return None

    item = resp.get("Item")
    if not item:
        return None

    info_raw = item.get("Info", {}).get("S", "{}")
    try:
        info = json.loads(info_raw)
    except json.JSONDecodeError:
        info = {"raw": info_raw}

    logger.warning(
        "DynamoDB lock HELD | lock_id=%s info=%s", lock_id, info,
    )
    return info


# =============================================================================
# 4b. Drift Validation (Terraform path only)
# =============================================================================

def drift_validation(
    target: str,
    terraform_dir: str = _TERRAFORM_TARGETS_DIR,
) -> tuple[bool, bool, str]:
    """
    Run `terraform plan -refresh-only -json` to detect state drift before applying.

    Called only on the Terraform path (instance_failure fault class).

    Preceded by a DynamoDB stale-lock precondition (Ch4 §4.2.2): if the
    targets workspace state lock is currently held by anyone, the trial
    aborts immediately (returns False, False, "lock_held") rather than
 waiting on Terraform's own lock-acquisition retry. Per ,
    app.py deliberately maps this specific case to
    escalation_reason="out_of_scope_drift" too (lock contention isolates the
    runtime path from a concurrent CI/CD deployment, which is conceptually
    the same "can't safely proceed" outcome even though no drift was
    actually observed).

    Returns:
      (proceed: bool, drift_detected: bool, reason: str)

    `reason` is one of: "" (no drift, proceed), "lock_held",
    "out_of_scope_drift", "scoped_drift" (proceed, drift_detected=True),
    "terraform_error", "timeout", "binary_not_found". app.py uses this to
    assign escalation_reason precisely — a subprocess timeout or a
    terraform-error exit code means drift status was never actually
    determined, and must not be reported as "out_of_scope_drift" with
    drift_detected=True (deployment-session fix: this used to be hardcoded
    at the app.py call site regardless of the real cause).

    If drift is detected in resources outside the faulted instance's address,
    remediation is aborted (returns False, True, "out_of_scope_drift").
    Drift scoped exclusively to the faulted target's resource is acceptable
    (that is precisely what we intend to remediate) and returns
    (True, True, "scoped_drift").

    Scope containment is parsed from Terraform's documented, versioned JSON
    plan output (`-json`, one JSON object per line — see
    https://developer.hashicorp.com/terraform/internals/json-format), not
    from the human-readable plan text. The human-readable format carries no
    cross-version compatibility guarantee; a Terraform minor-version bump
    could silently break a text-based scope check without any error
    (deployment-session correction — the previous implementation matched
    lines starting with "~"/"-"/"+" against the human-readable plan).

    A non-zero plan exit code for reasons other than drift (provider error,
    backend lock contention) also returns (False, False, "terraform_error")
    with an error log.
    """
    if dynamodb_lock_status() is not None:
        logger.error(
            "Drift validation aborted: DynamoDB state lock is held | target=%s",
            target,
        )
        return False, False, "lock_held"

    # Popen + communicate(timeout=...), not subprocess.run(timeout=...):
    # confirmed live 2026-10-01 that a timeout here orphans Terraform's own
    # DynamoDB state lock every single time. subprocess.run's timeout
    # handling calls Popen.kill() -- SIGKILL -- which the kernel delivers
    # unconditionally and which Go cannot intercept, so Terraform's own
    # deferred lock-release code never runs. Three separate orphaned locks
    # this session (two from genuine 120s timeouts, confirmed via each
    # lock's own "Created" timestamp lining up exactly) each silently
    # blocked every subsequent plan/apply -- automated and manual -- until
    # a human ran `terraform force-unlock`. SIGTERM first gives Terraform's
    # own signal handling a chance to release the lock cleanly; SIGKILL is
    # now only a last-resort fallback if it ignores SIGTERM.
    try:
        proc = subprocess.Popen(
            ["terraform", "plan", "-refresh-only", "-detailed-exitcode",
             "-no-color", "-json"],
            cwd=terraform_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=120)
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            logger.error(
                "Drift validation: terraform plan exceeded 120s -- sending "
                "SIGTERM for graceful shutdown (lets it release its own "
                "state lock) before falling back to SIGKILL"
            )
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                logger.error(
                    "Drift validation: terraform ignored SIGTERM for 10s "
                    "-- escalating to SIGKILL (state lock may still be "
                    "orphaned; this is now the rare case, not the norm)"
                )
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                proc.communicate()
            logger.error("Drift validation: terraform plan timed out after 120s")
            return False, False, "timeout"

        # terraform plan -detailed-exitcode exit codes:
        #   0 — no changes (no drift)
        #   1 — error
        #   2 — diff detected (drift present)
        if returncode == 0:
            logger.info(
                "Drift validation: no drift detected | target=%s", target
            )
            return True, False, ""

        if returncode == 2:
            # Drift exists — parse the JSON UI-message stream for
            # "resource_drift" entries (the stable, documented schema for
            # -refresh-only drift reporting) and check each address against
            # the faulted target's own resource.
            # Scope rule: drift is in-scope only if the drifting resource is
            # keyed to the faulted target.  Terraform's for_each key is the
            # scope discriminator, NOT the resource type — every per-target
            # resource (the instance itself, its CloudWatch alarms) carries
            # the same key, and all of them are expected to drift when that
            # target is terminated.  Anything else that drifted -- a
            # sibling target, or an unkeyed shared resource in this root
            # configuration -- is out of scope and aborts the remediation.
            #
            # Addresses are matched on the for_each key rather than on a
            # literal address prefix.  Terraform reports fully-qualified
            # addresses including the module path (for example
            # module.compute.aws_instance.targets["target-2"]), so a prefix
            # test against a module-relative address such as
            # aws_instance.targets never matches in production and would
            # silently render this gate inert.
            # `target` here is whatever the caller's own alert-derived
            # identifier looks like -- app.py passes labels["node_name"],
            # which carries the FULL Name-tag string (e.g.
            # "self-healing-thesis-experiment-target-3"), not Terraform's
            # own for_each key ("target-3"). CONFIRMED LIVE: comparing the
            # raw `target` value against `resource_key` never matched,
            # which made this gate treat the faulted target's OWN expected
            # drift as "out of scope" on every single automated
            # instance_failure trial dispatched via the real Alertmanager
            # webhook path -- a 100%-reproducible failure that escalated
            # every such trial and left the target instance permanently
            # unreplaced. Extract the short "target-N" suffix before
            # comparing, so this works regardless of which prefixed or
            # bare form the caller happens to pass in.
            m_target = re.search(r'(target-\d+)$', target)
            target_key = m_target.group(1) if m_target else target

            out_of_scope: list[str] = []
            for line in stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get("type") != "resource_drift":
                    continue
                resource = msg.get("change", {}).get("resource", {})
                addr = resource.get("addr", "")
                key = resource.get("resource_key")
                if key is None:
                    # Older/partial payloads may omit resource_key; recover it
                    # from the address's own ["..."] index expression.
                    m = re.search(r'\["([^"]+)"\]\s*$', addr)
                    key = m.group(1) if m else None
                if key != target_key:
                    out_of_scope.append(addr or "<unknown address>")

            if out_of_scope:
                logger.error(
                    "Drift validation: OUT-OF-SCOPE drift detected — aborting. "
                    "Drifted addresses: %s", out_of_scope[:5],
                )
                return False, True, "out_of_scope_drift"

            logger.info(
                "Drift validation: drift scoped to resources keyed %r — proceeding",
                target,
            )
            return True, True, "scoped_drift"

        # returncode == 1: terraform error
        logger.error(
            "Drift validation: terraform plan exited %d. stderr: %s",
            returncode, stderr[:500],
        )
        return False, False, "terraform_error"

    except FileNotFoundError:
        logger.error("Drift validation: terraform binary not found in PATH")
        return False, False, "binary_not_found"
    except Exception as exc:  # noqa: BLE001
        logger.error("Drift validation: unexpected error: %s", exc)
        return False, False, "terraform_error"
