#!/usr/bin/env python3
"""
app.py — Flask entry point for the MAPE-K self-healing orchestrator.

Endpoints:
  POST /alert    Alertmanager webhook receiver (MAPE-K Monitor → trigger)
  GET  /healthz  Liveness probe (Stage E3 — Thesis §3.7.2)
  GET  /metrics  Prometheus scrape (pipeline-internal counters)

Architecture notes:
  - Single Gunicorn worker (--workers 1) serialises concurrent alert handling
    and makes the in-memory state of plan.py (circuit breaker, lease tracker)
    consistent without locking.
  - Port 9094 is fixed in the thesis text and dataset (Table 4.3, Listing 4.9).
    Do NOT change to 5000 or any other port.
  - The alert dispatch path is intentionally synchronous within the request
    handler so that T_classify begins immediately on webhook receipt and the
    MTTR monotonic clock does not include Flask dispatch overhead.

Priority-resolution matrix (Thesis §3.5.1):
  If multiple alerts arrive within a 15-second deduplication window, only the
  highest-weight alert is actioned.  Sibling alerts are suppressed for 15 s.
  This prevents the orchestrator from spawning concurrent remediations on the
  same target during a compound failure event.

  Severity weights:
    instance_failure    → 100
    service_crash       → 80
    cpu_overload        → 60
    memory_exhaustion   → 60
    disk_saturation     → 40
    configuration_drift → 20

Deployment:
  gunicorn --bind 0.0.0.0:9094 --workers 1 --worker-class sync --timeout 2700 app:app

  --timeout 2700: handle_alert() below dispatches _run_mape_k() SYNCHRONOUSLY,
  inline in the request handler, so gunicorn's worker timeout is a hard ceiling
  on total per-alert MAPE-K duration. It must exceed the worst-case sum of every
  bounded wait a single dispatch can make, not the typical duration. Those sums,
  from routing.yaml and this package's own subprocess timeouts, are 2460s for
  instance_failure (drift 120 + init 120 + plan 180 + show 180 + apply 600 +
  SSM-online 180 + diagnostic restore 600 + forwarding check 300 + validation
  180) and up to 1895s for the retry-equipped Ansible classes (3 x 600 + backoff
  + validation). The previous 960s was below all six, sized only against the
  harness's 900s poll rather than against what one dispatch can actually take.

  forwarding check raised 120s -> 300s (deployment-session decision,
  2026-09-14): confirmed live across a real S1 campaign that a genuinely
  successful Terraform+Ansible restore (T_remediate observed at 76-83s) can
  still need more than 120s for a freshly-booted instance's Filebeat to
  index its first document -- an occasional, real timing-margin failure
  (recovery_status=diagnostic_bootstrap_failed), not a code defect. See
  _OBS_FORWARDING_TIMEOUT_SECONDS below.

  Exceeding it is not an ordinary trial failure: gunicorn SIGKILLs the worker
  mid-request, so mttr.save() never runs and no terminal record is written; the
  respawned worker starts with an empty circuit breaker and lease tracker, both
  of which live only in process memory; and Alertmanager re-delivers the still-
  firing alert within its 5m repeat_interval, into a worker that no longer knows
  a remediation is in flight. tests/unit/test_worker_timeout_budget.py pins the
  invariant so this cannot drift again.
"""

from __future__ import annotations

import logging
import os
import time
from collections import OrderedDict
from datetime import datetime, timezone
from threading import Lock

from flask import Flask, jsonify, request
from prometheus_client import Counter, Gauge, Histogram
from prometheus_flask_exporter import PrometheusMetrics

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from alert_log import record_alert_receipt
from classifier import (classify_and_route, get_retry_policy,
                        get_validation_window, needs_observability_restore)
from execute import UnsafePlanScopeError, dispatch, restore_diagnostic_agents
from health_checker import HealthChecker, alert_still_firing
from mttr_logger import MTTRLogger
from observability_checker import ObservabilityChecker
from plan import CircuitBreaker, LeaseTracker, transport_validation, drift_validation

# ── OpenTelemetry initialisation ──────────────────────────────────────────────

def _init_tracing() -> trace.Tracer:
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otelcol:4317")
    service  = os.environ.get("OTEL_SERVICE_NAME", "self-healing-orchestrator")
    try:
        provider = TracerProvider()
        provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
        )
        trace.set_tracer_provider(provider)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "OTel tracer init failed (%s) — proceeding without distributed traces.", exc
        )
    return trace.get_tracer(service)


tracer = _init_tracing()

# ── Flask application ──────────────────────────────────────────────────────────

app = Flask(__name__)
FlaskInstrumentor().instrument_app(app)
RequestsInstrumentor().instrument()

metrics = PrometheusMetrics(app, group_by="endpoint")
metrics.info("orchestrator_info", "Self-healing orchestrator", version="4.0.0")

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

# ── Timeout constants ────────────────────────────────────────────────────────

# instance_failure only: how long ObservabilityChecker waits for confirmed
# Filebeat/OTel forwarding after diagnostic_agents.yml completes, before
# recovery_status=diagnostic_bootstrap_failed. See the module docstring
# above for why this was raised from 120s to 300s. Referenced by name (not
# copied) from tests/unit/test_worker_timeout_budget.py's own regex-based
# extraction, so the gunicorn --timeout invariant cannot silently drift out
# of sync with this value again.
_OBS_FORWARDING_TIMEOUT_SECONDS = 300

# ── Custom Prometheus metrics ─────────────────────────────────────────────────

MTTR_HISTOGRAM = Histogram(
    "mttr_seconds",
    "End-to-end MTTR per fault class and actuator (RQ1 primary metric)",
    labelnames=["fault_type", "actuator"],
    buckets=[5, 10, 20, 30, 60, 120, 180, 300, 600],
)
REMEDIATION_COUNTER = Counter(
    "remediation_total",
    "Total remediation dispatch attempts by fault class, actuator, and outcome",
    labelnames=["fault_type", "actuator", "outcome"],
)
CIRCUIT_BREAKER_GAUGE = Gauge(
    "circuit_breaker_open",
    "Circuit breaker state per (target, fault_type): 0=closed, 1=open, 0.5=half_open",
    labelnames=["target", "fault_type"],
)
SUPPRESSED_COUNTER = Counter(
    "alert_suppressed_total",
    "Alerts suppressed by the priority-resolution deduplication window",
    labelnames=["fault_type"],
)

# ── Priority-resolution matrix ────────────────────────────────────────────────
# Scalar severity weights used to select the highest-priority alert when
# multiple alerts arrive within the 15-second deduplication window.
# (Thesis §3.5.1, Priority Matrix P)

_SEVERITY_WEIGHT: dict[str, int] = {
    "instance_failure":    100,
    "service_crash":        80,
    "cpu_overload":         60,
    "memory_exhaustion":    60,
    "disk_saturation":      40,
    "configuration_drift":  20,
}

_DEDUP_WINDOW_SECONDS: float = 15.0

# In-memory suppressor cache: (target, fault_type) → expiry monotonic timestamp.
# OrderedDict to enable O(1) eviction of the oldest entry.
_suppressor: dict[tuple[str, str], float] = OrderedDict()
_suppressor_lock = Lock()

# ── Plan-layer singletons ──────────────────────────────────────────────────────

_circuit_breaker = CircuitBreaker(
    alert_threshold=3,
    window_seconds=30 * 60,   # Δt = 30 minutes
    cooldown_seconds=30 * 60, # T_cool = 30 minutes
)
_lease_tracker = LeaseTracker(ttl_seconds=600)


# ── Webhook endpoint ──────────────────────────────────────────────────────────

@app.route("/alert", methods=["POST"])
def handle_alert():
    """
    Receive Alertmanager webhook payload.  Dispatch the highest-priority
    firing alert per (target, fault_type) through the MAPE-K pipeline.

    Returns HTTP 200 for all well-formed payloads; individual alert errors
    are logged internally and escalated but do not affect the HTTP response
    (Alertmanager interprets non-2xx as a delivery failure and retries).
    """
    payload = request.get_json(silent=True)
    if not payload:
        logger.warning("Empty or non-JSON webhook body from %s", request.remote_addr)
        return jsonify({"error": "empty_payload"}), 400

    firing = [a for a in payload.get("alerts", []) if a.get("status") == "firing"]
    if not firing:
        return jsonify({"status": "no_firing_alerts"}), 200

    logger.info("Webhook received: %d firing alert(s)", len(firing))

    firing = _sort_by_priority(firing)

    processed = suppressed = 0
    for alert in firing:
        labels     = alert.get("labels", {})
        fault_type = labels.get("fault_type", "unknown")
        instance   = labels.get("instance", "unknown")
        target     = (
            labels.get("node_name")
            or labels.get("target")
            or instance.split(":")[0]
        )

        # Every alert in the firing list is a "received" alert for RQ4's
        # false-positive-rate metric (§3.7.3) — including ones the dedup
        # window below suppresses. Each branch below logs it exactly once,
        # so a suppressed sibling is never silently dropped from the
        # denominator.
        #
        # Stale-alert gate: the single worker can be busy for minutes, so a
        # webhook raised while a replacement instance was still booting may
        # be handled only after that alert has cleared. Dispatching it would
        # open a second MAPE-K cycle against a healthy target, count toward
        # the circuit breaker, and re-open it after a successful half-open
        # probe. Fails open when Prometheus is unreachable.
        if alert_still_firing(fault_type, target) is False:
            logger.info(
                "Stale alert dropped: already cleared in Prometheus | "
                "fault_type=%s target=%s ec2_instance_id=%s",
                fault_type, target, labels.get("ec2_instance_id"),
            )
            record_alert_receipt(
                target=target, fault_type=fault_type, instance=instance,
                suppressed=True,
                ec2_instance_id=labels.get("ec2_instance_id"),
            )
            suppressed += 1
            continue

        now = time.monotonic()
        with _suppressor_lock:
            if _check_and_register_suppression(target, fault_type, now):
                logger.info(
                    "Alert suppressed by dedup window: fault_type=%s target=%s",
                    fault_type, target,
                )
                SUPPRESSED_COUNTER.labels(fault_type=fault_type).inc()
                record_alert_receipt(
                    target=target, fault_type=fault_type, instance=instance,
                    suppressed=True,
                    ec2_instance_id=labels.get("ec2_instance_id"),
                )
                suppressed += 1
                continue

        record_alert_receipt(
            target=target, fault_type=fault_type, instance=instance,
            suppressed=False,
            ec2_instance_id=labels.get("ec2_instance_id"),
        )
        _run_mape_k(alert, target=target, fault_type=fault_type, instance=instance)
        processed += 1

    return jsonify({
        "status": "received",
        "processed": processed,
        "suppressed": suppressed,
    }), 200


@app.route("/healthz", methods=["GET"])
def healthz():
    """
    Stage E3 liveness probe (Thesis §3.7.2).
    Fast internal sanity check — does not block on external dependencies.
    """
    return jsonify({
        "status": "ok",
        "service": "self-healing-orchestrator",
        "version": "4.0.0",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }), 200


# ── MAPE-K pipeline ───────────────────────────────────────────────────────────

def _run_mape_k(
    alert: dict,
    *,
    target: str,
    fault_type: str,
    instance: str,
) -> None:
    """
    Execute one complete MAPE-K iteration for a single firing alert.

    Phase timeline and MTTR contributions (Thesis Eq. 3.1):
      Monitor   — complete on webhook arrival; commits t_alert, ending T_detect
      Analyse   → classify_and_route()           → commits t_plan, ending T_classify
      Plan      → CB + lease + transport + drift → runs after t_plan, so its cost
                                                   falls inside T_remediate
      Execute   → dispatch() with retry policy   → commits t_actuate, ending T_remediate
      Knowledge → HealthChecker polls t_clear    → ends T_validate; MTTRLogger persists

    There is no T_plan phase.  The decomposition has four phases (five on
    instance_failure, where T_observability_restore splices in between
    T_remediate and T_validate).  t_plan is an epoch, not a phase: it is the
    boundary at which T_classify ends and T_remediate begins, which is why the
    Plan-layer safety gates — executing after that boundary — are counted
    within T_remediate rather than within T_classify.
    """
    with tracer.start_as_current_span(
        "mape_k_loop",
        attributes={"fault_type": fault_type, "target": target},
    ) as root_span:

        mttr = MTTRLogger(alert, target=target)
        mttr.record_detection()
        logger.info(
            "MAPE-K start | trial_id=%s fault_type=%s target=%s",
            mttr.trial_id, fault_type, target,
        )

        # ── Analyse: fault classification ─────────────────────────────────────
        actuator = action = None
        with tracer.start_as_current_span("analyse"):
            try:
                actuator, action = classify_and_route(alert)
                mttr.record_classification(actuator=actuator, action=action)
                logger.info(
                    "Classified | actuator=%s action=%s trial_id=%s",
                    actuator, action, mttr.trial_id,
                )
            except (ValueError, KeyError) as exc:
                logger.error("Classification error: %s", exc)
                root_span.set_attribute("error", True)
                # escalation_reason must stay inside the closed value set
                # (Thesis Table 4.8); the exception text is carried by the
                # logger.error above, not smuggled into the enum field.
                mttr.save(
                    recovery_status="aborted_plan",
                    escalation_reason="classification_error",
                )
                return

        # ── Plan: four safety preconditions in canonical order (§3.5.3) ──────
        # 1. Circuit Breaker  2. Lease  3. Transport  4. Drift (Terraform only)
        with tracer.start_as_current_span("plan"):

            # 1. Circuit Breaker
            cb_state = _circuit_breaker.check(target, fault_type)
            CIRCUIT_BREAKER_GAUGE.labels(
                target=target, fault_type=fault_type
            ).set({"closed": 0, "half_open": 0.5, "open": 1}.get(cb_state, 0))

            if cb_state == "open":
                logger.warning(
                    "Circuit breaker OPEN | target=%s fault_type=%s — suppressing",
                    target, fault_type,
                )
                _escalate(alert, reason="circuit_breaker_open")
                mttr.save(
                    recovery_status="aborted_plan",
                    escalation_reason="circuit_breaker_open",
                    circuit_breaker="open",
                )
                return

            # 2. Lease Tracker — block concurrent remediations on same instance
            lease_acquired = _lease_tracker.acquire(target)
            if not lease_acquired:
                logger.warning(
                    "Lease held for target=%s — concurrent remediation in flight. Skipping.",
                    target,
                )
                _escalate(alert, reason="lease_held")
                mttr.save(
                    recovery_status="aborted_plan",
                    escalation_reason="lease_held",
                )
                return

            try:
                # 3. Transport Validation — SSM agent reachability (Ansible
                # path only). The terraform actuator (instance_failure)
                # dispatches via the AWS provider's own credentials, never
                # over SSM — and the whole premise of instance_failure is
                # that the instance is gone, so checking whether IT is
                # SSM-reachable is checking the wrong thing entirely.
                # Confirmed live: once the label-key bug below was fixed and
                # this check started actually running, it correctly found
                # the just-terminated instance unreachable via SSM and then
                # wrongly aborted the Terraform remediation that never
                # needed SSM in the first place. Same shape as Gate 4 below,
                # which is already correctly scoped to actuator=="terraform".
                #
                # Label key must match prometheus.yml.j2's node_exporter job,
                # which publishes "ec2_instance_id" (not "instance_id") —
                # confirmed live: this previously read the wrong key, so
                # instance_id was always None and transport_validation()
                # silently took its "no instance_id — skip check" branch on
                # every alert, never actually validating SSM reachability.
                if actuator == "ansible":
                    instance_id = alert.get("labels", {}).get("ec2_instance_id")
                    if not transport_validation(target, instance_id=instance_id):
                        logger.error("Transport validation failed for target=%s", target)
                        _escalate(alert, reason="transport_unreachable")
                        mttr.save(
                            recovery_status="aborted_plan",
                            escalation_reason="transport_unreachable",
                        )
                        return

                # 4. Drift Validation (Terraform path only — §3.5.4)
                drift_detected = False
                if actuator == "terraform":
                    drift_ok, drift_detected, drift_reason = drift_validation(target)
                    if not drift_ok:
                        # "lock_held" and "out_of_scope_drift" both report as
                        # escalation_reason="out_of_scope_drift" per Thesis
                        # §5.12 (lock contention is treated the same as
                        # genuine out-of-scope drift: neither is safe to
                        # proceed past). "timeout"/"terraform_error"/
                        # "binary_not_found" mean drift status was never
                        # actually determined — reporting those as
                        # out_of_scope_drift with drift_detected=True would
                        # be a false record (deployment-session fix; this
                        # was previously hardcoded to True unconditionally).
                        escalation_reason = (
                            "out_of_scope_drift"
                            if drift_reason in ("lock_held", "out_of_scope_drift")
                            else "drift_check_error"
                        )
                        logger.error(
                            "Drift validation aborted for target=%s — reason=%s",
                            target, drift_reason,
                        )
                        _escalate(alert, reason=escalation_reason)
                        mttr.save(
                            recovery_status="aborted_plan",
                            escalation_reason=escalation_reason,
                            drift_detected=drift_detected,
                        )
                        return

                # ── Execute: actuator dispatch with retry policy ────────────
                policy       = get_retry_policy(fault_type)
                max_retries  = policy["max_retries"]
                backoff_secs = policy["backoff_seconds"]

                with tracer.start_as_current_span(
                    "execute",
                    attributes={"actuator": actuator, "action": action},
                ):
                    actuator_report: dict = {}
                    try:
                        success, final_retry = _execute_with_retry(
                            actuator, action, alert,
                            fault_type=fault_type, target=target,
                            trial_id=mttr.trial_id,
                            max_retries=max_retries, backoff_secs=backoff_secs,
                            report_sink=actuator_report,
                        )
                    except UnsafePlanScopeError as exc:
                        # The Terraform plan proposed something other than
                        # creating exactly the one failed target.  Nothing has
                        # been applied — the guard runs before apply — so the
                        # infrastructure is untouched and the trial aborts
                        # without a t_remediated timestamp.
                        logger.error(
                            "Unsafe plan scope, remediation not applied: %s | trial_id=%s",
                            exc, mttr.trial_id,
                        )
                        root_span.set_attribute("error", True)
                        REMEDIATION_COUNTER.labels(
                            fault_type=fault_type,
                            actuator=actuator or "unknown",
                            outcome="unsafe_plan_scope",
                        ).inc()
                        mttr.record_actuator_report(actuator_report)
                        mttr.save(
                            recovery_status="aborted_plan",
                            escalation_reason="unsafe_plan_scope",
                            circuit_breaker=cb_state,
                            drift_detected=drift_detected,
                        )
                        _escalate(alert, reason="unsafe_plan_scope")
                        return
                    mttr.record_actuator_report(actuator_report)
                    if success:
                        REMEDIATION_COUNTER.labels(
                            fault_type=fault_type,
                            actuator=actuator,
                            outcome="success",
                        ).inc()

                if not success:
                    REMEDIATION_COUNTER.labels(
                        fault_type=fault_type,
                        actuator=actuator or "unknown",
                        outcome="retry_exhausted",
                    ).inc()
                    # _execute_with_retry returns (False, max_retries) on
                    # exhaustion; retry_count is retries after the first
                    # attempt (Table 4.9), so max_retries = 2 means 3 attempts.
                    mttr.record_remediation(retry_count=final_retry)
                    mttr.save(
                        recovery_status="retry_exhausted",
                        escalation_reason="retry_exhausted",
                        retry_count=final_retry,
                        circuit_breaker=cb_state,
                        drift_detected=drift_detected,
                    )
                    _escalate(alert, reason="retry_exhausted")
                    return

                mttr.record_remediation(retry_count=final_retry)

                # ── Diagnostic-agent restore (instance_failure only) ────────
                # Runs BEFORE validation so wait_until_healthy below confirms
                # the FULLY restored state, not just the base instance.
                # T_remediate above is unaffected — it already ended at
                # record_remediation(). This adds a new
                # T_observability_restore phase (mttr_logger.py) between
                # T_remediate and T_validate. Execute-layer restore
                # (restore_diagnostic_agents, act-only) and Knowledge-layer
                # verification (ObservabilityChecker, read-only) are kept
                # separate per MAPE-K responsibilities — the Execute layer
                # never reads sensor data directly.
                if needs_observability_restore(fault_type):
                    with tracer.start_as_current_span("observability_restore"):
                        try:
                            instance_id = restore_diagnostic_agents(
                                target, alert, run_id=mttr.trial_id,
                            )
                            obs_checker = ObservabilityChecker(
                                target=target, instance_id=instance_id,
                            )
                            if not obs_checker.wait_until_forwarding(timeout=_OBS_FORWARDING_TIMEOUT_SECONDS):
                                raise RuntimeError(
                                    "Diagnostic agents running but not forwarding data"
                                )
                            mttr.record_observability_restore()
                        except Exception as exc:  # noqa: BLE001
                            REMEDIATION_COUNTER.labels(
                                fault_type=fault_type,
                                actuator=actuator,
                                outcome="diagnostic_bootstrap_failed",
                            ).inc()
                            mttr.save(
                                recovery_status="diagnostic_bootstrap_failed",
                                escalation_reason="diagnostic_bootstrap_failed",
                                retry_count=final_retry,
                                circuit_breaker=cb_state,
                                drift_detected=drift_detected,
                            )
                            logger.error(
                                "Diagnostic-agent restore failed | target=%s "
                                "trial_id=%s exc=%s",
                                target, mttr.trial_id, exc,
                            )
                            _escalate(alert, reason="diagnostic_bootstrap_failed")
                            return

                # ── Knowledge: validate → persist ──────────────────────────
                validation_timeout = get_validation_window(fault_type)
                with tracer.start_as_current_span("validate") as val_span:
                    checker = HealthChecker(
                        target=target, fault_type=fault_type, instance=instance,
                    )
                    val_span.set_attribute("fault_type", fault_type)
                    val_span.set_attribute("node_name", target)

                    if checker.wait_until_healthy(timeout=validation_timeout):
                        mttr.record_validation()
                        elapsed = mttr.total_seconds()
                        mttr.save(
                            recovery_status="success",
                            retry_count=final_retry,
                            circuit_breaker=cb_state,
                            drift_detected=drift_detected,
                        )
                        MTTR_HISTOGRAM.labels(
                            fault_type=fault_type, actuator=actuator
                        ).observe(elapsed)
                        REMEDIATION_COUNTER.labels(
                            fault_type=fault_type,
                            actuator=actuator,
                            outcome="validated",
                        ).inc()
                        val_span.set_attribute("recovered", True)
                        val_span.set_attribute("mttr_s", elapsed)
                        logger.info(
                            "Recovery confirmed | T_MTTR=%.1fs trial_id=%s",
                            elapsed, mttr.trial_id,
                        )
                    else:
                        REMEDIATION_COUNTER.labels(
                            fault_type=fault_type,
                            actuator=actuator,
                            outcome="timeout",
                        ).inc()
                        mttr.save(
                            recovery_status="timeout",
                            escalation_reason="validation_timeout",
                            retry_count=final_retry,
                            circuit_breaker=cb_state,
                            drift_detected=drift_detected,
                        )
                        logger.error(
                            "Validation timeout after %ds | fault_type=%s target=%s "
                            "trial_id=%s",
                            validation_timeout, fault_type, target, mttr.trial_id,
                        )
                        _escalate(alert, reason="validation_timeout")

            finally:
                _lease_tracker.release(target)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _sort_by_priority(alerts: list[dict]) -> list[dict]:
    """
    Sort firing alerts descending by severity weight (Thesis §3.5.1 Priority
    Matrix P). Extracted as a pure function for unit testing —
    see tests/unit/test_priority_arbitration.py.
    """
    return sorted(
        alerts,
        key=lambda a: _SEVERITY_WEIGHT.get(a.get("labels", {}).get("fault_type", ""), 0),
        reverse=True,
    )


def _evict_expired_suppressions(now: float) -> None:
    """Remove expired entries from the in-memory suppressor cache (caller holds lock)."""
    expired = [k for k, exp in _suppressor.items() if exp <= now]
    for k in expired:
        del _suppressor[k]


def _check_and_register_suppression(target: str, fault_type: str, now: float) -> bool:
    """
    Return True if (target, fault_type) is currently suppressed by the
    15-second dedup window (Thesis §3.5.1); otherwise admit it and open a
    new suppression window covering lower-priority siblings.

    Caller must hold `_suppressor_lock`. Extracted as a named function for
    unit testing — see tests/unit/test_priority_arbitration.py.
    """
    key = (target, fault_type)
    _evict_expired_suppressions(now)
    if key in _suppressor:
        return True
    _suppressor[key] = now + _DEDUP_WINDOW_SECONDS
    return False


def _execute_with_retry(
    actuator: str,
    action: str,
    alert: dict,
    *,
    fault_type: str,
    target: str,
    trial_id: str,
    max_retries: int,
    backoff_secs: list[int],
    report_sink: dict | None = None,
) -> tuple[bool, int]:
    """
    Invoke the actuator with geometric-backoff retries (Thesis §3.5.3,
). Returns (success, final_attempt_index).

    T_remediate covers this entire call including all backoff sleeps and
 the SSM transport-negotiation latency inside dispatch (
    §5.2 — do not split it). Extracted as a named function for unit
    testing — see tests/unit/test_retry.py.
    """
    for attempt in range(max_retries + 1):
        if attempt > 0:
            delay = (
                backoff_secs[attempt - 1]
                if attempt - 1 < len(backoff_secs)
                else backoff_secs[-1]
            )
            logger.info(
                "Retry %d/%d for fault_type=%s on target=%s (backoff=%ds) | trial_id=%s",
                attempt, max_retries, fault_type, target, delay, trial_id,
            )
            time.sleep(delay)

        try:
            report = dispatch(actuator, action, alert)
            if report_sink is not None and report:
                report_sink.update(report)
            logger.info(
                "Dispatch succeeded on attempt %d/%d | trial_id=%s",
                attempt + 1, max_retries + 1, trial_id,
            )
            return True, attempt
        except UnsafePlanScopeError:
            # Never retried, and never folded into retry_exhausted: a plan
            # whose scope is wrong is wrong structurally, so the next attempt
            # produces the identical plan.  Propagate so the caller can record
            # escalation_reason="unsafe_plan_scope".
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Dispatch attempt %d/%d failed: %s | trial_id=%s",
                attempt + 1, max_retries + 1, exc, trial_id,
            )

    return False, max_retries


def _escalate(alert: dict, reason: str = "unknown") -> None:
    """
    Human escalation: emit CRITICAL log and publish to SNS if configured.
    SNS_TOPIC_ARN must be set in the environment to enable SNS notification.
    """
    labels = alert.get("labels", {})
    logger.critical(
        "HUMAN ESCALATION REQUIRED | reason=%s fault_type=%s target=%s instance=%s",
        reason,
        labels.get("fault_type", "unknown"),
        labels.get("node_name", labels.get("target", "unknown")),
        labels.get("instance", "unknown"),
    )
    topic_arn = os.environ.get("SNS_TOPIC_ARN")
    if topic_arn:
        try:
            import boto3
            sns = boto3.client(
                "sns",
                region_name=os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1"),
            )
            sns.publish(
                TopicArn=topic_arn,
                Subject=f"Self-Healing Escalation: {reason}",
                Message=(
                    f"reason={reason}\n"
                    f"fault_type={labels.get('fault_type', 'unknown')}\n"
                    f"target={labels.get('node_name', 'unknown')}\n"
                    f"instance={labels.get('instance', 'unknown')}\n"
                    f"timestamp={datetime.now(timezone.utc).isoformat()}"
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("SNS publish failed: %s", exc)


# ── Development entry point ───────────────────────────────────────────────────
# Production: gunicorn --bind 0.0.0.0:9094 --workers 1 --worker-class sync --timeout 2400 app:app

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=9094, debug=False)
