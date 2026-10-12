#!/usr/bin/env python3
"""
mttr_logger.py — Decomposed MTTR measurement and persistence (MAPE-K Knowledge phase).

Thesis Eq. 3.1, §3.7.2 — MTTR is decomposed into four sequential phases:
  T_detect   — fault injected (t_0) to Alertmanager webhook arrives at orchestrator
  T_classify — webhook arrives to fault classifier emits routing decision
  T_remediate— routing decision passed to actuator to actuator reports terminal status
  T_validate — actuator terminal status to Monitor→Analyse cycle confirms satisfied state
  T_MTTR     = T_detect + T_classify + T_remediate + T_validate

instance_failure trials only splice a fifth phase between
T_remediate and T_validate:
  T_observability_restore — actuator terminal status to diagnostic agents
                             (Filebeat, OTel Agent) restored and verified
                             forwarding data on the recreated instance
  T_MTTR (instance_failure) = T_detect + T_classify + T_remediate
                               + T_observability_restore + T_validate
T_remediate's own definition and measurement window are unchanged for
every fault class, including instance_failure — the new phase is spliced
in strictly after T_remediate ends, so RQ1/RQ2 comparisons across fault
classes remain apples-to-apples.

Each trial is written as a single JSON-Lines record — the sole persistent
format defined by the thesis (Ch3 Table 3.5, Ch4 Table 4.8). Records are
mirrored to S3 by the self_healing role.

JSONL schema (Ch4 Table 4.8):
  trial_id, system_under_test, target, fault_type, actuator, playbook,
  t_fault_injected_ms, t_detected_ms, t_classified_ms, t_remediated_ms,
  t_observability_restored_ms, t_validated_ms,
  T_detect_seconds, T_classify_seconds, T_remediate_seconds,
  T_observability_restore_seconds, T_validate_seconds, T_MTTR_seconds,
  recovery_status, retry_count, circuit_breaker, drift_detected, escalation_reason
"""
import json
import logging
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_DEFAULT_RESULTS_DIR = Path(os.environ.get("RESULTS_DIR", "")) or (
    Path(__file__).resolve().parent.parent / "results"
)

JSONL_PATH = _DEFAULT_RESULTS_DIR / "mttr_log.jsonl"

# System under test identifier (S1 = self-healing pipeline)
SYSTEM_UNDER_TEST = os.environ.get("SYSTEM_UNDER_TEST", "S1")

# Canonical field order for the JSONL record (Ch4 Table 4.8). Kept as a
# named list (rather than inlined only in save()) so tests can assert
# schema completeness without duplicating the field list.
RECORD_FIELDNAMES = [
    "trial_id",
    "system_under_test",
    "target",
    "fault_type",
    "actuator",
    "playbook",
    "t_fault_injected_ms",
    "t_detected_ms",
    "t_classified_ms",
    "t_remediated_ms",
    "t_observability_restored_ms",
    "t_validated_ms",
    "T_detect_seconds",
    "T_classify_seconds",
    "T_remediate_seconds",
    "T_observability_restore_seconds",
    "T_validate_seconds",
    "T_MTTR_seconds",
    "recovery_status",
    "retry_count",
    "circuit_breaker",
    "drift_detected",
    "escalation_reason",
]


class MTTRLogger:
    """
    Stateful MTTR recorder for a single remediation trial.

    Usage:
        mttr = MTTRLogger(alert, target="target-2")
        mttr.record_detection()                        # on webhook receipt
        mttr.record_classification(actuator, action)   # after classify_and_route()
        mttr.record_remediation(retry_count=0)         # after execute_remediation()
        mttr.record_validation()                       # after HealthChecker confirms
        mttr.save(recovery_status="success", ...)      # persist JSONL record
    """

    def __init__(self, alert: dict, target: str = "unknown"):
        self.fault_type       = alert.get("labels", {}).get("fault_type", "unknown")
        self.target           = target
        self.system_under_test = SYSTEM_UNDER_TEST
        self.actuator         = None
        self.playbook         = None

        # Experimental-unit identity adopted from the harness correlation
        # record.  (fault_type, target, block) is the pairing key that joins an
        # S1 observation to its S2 counterpart for the paired H1 analysis;
        # -1 means the trial was not run against a campaign schedule and is
        # therefore ineligible for pairing.
        self.block: int = -1
        self.trial_id, self._t_fault_injected_ms = self._resolve_trial_correlation(alert)

        # Wall-clock timestamps (milliseconds since epoch)
        self._t_detected_ms:    int | None = None
        self._t_classified_ms:  int | None = None
        self._t_remediated_ms:  int | None = None
        self._t_observability_restored_ms: int | None = None
        # Facts the actuator reported about its own execution (Execute
        # layer -> Knowledge layer).  Only disk_saturation populates it.
        self._actuator_report: dict = {}
        self._t_validated_ms:   int | None = None

        # Monotonic timestamps for delta accuracy
        self._mono_detect:    float | None = None
        self._mono_classify:  float | None = None
        self._mono_remediate: float | None = None
        # instance_failure only — restoring Filebeat/OTel
        # Agent after Terraform recreates the instance. Stays None for the
        # other five fault classes, so every downstream phase computation
        # correctly falls back to today's behaviour for them.
        self._mono_observability_restore: float | None = None
        self._mono_validate:  float | None = None

        self._trial_number: int = _next_trial_number(self.fault_type)
        _ensure_files()

    def _resolve_trial_correlation(self, alert: dict) -> tuple[str, int | None]:
        """
        Resolve (trial_id, t_fault_injected_ms) in priority order (Ch4 §4.6.1):

          1. Explicit alert annotations (trial_id + injection_timestamp) —
             covers synthetic/direct webhook posts (integration tests) and
             any harness capable of injecting the alert payload itself.
          2. Correlation-file lookup by (target, fault_type) recency — the
             real Prometheus-triggered campaign path. The harness cannot
             know the orchestrator's trial_id in advance (it does not exist
             until this webhook arrives), so exact trial_id matching is not
             possible for genuine alerts; matching by (target, fault_type,
             recency) is the mechanism Ch4 §4.6.1 describes ("the logger
             joins t0 to the per-trial record at close-trial time"). When a
             match is found, its trial_id is ADOPTED here so the harness's
             correlation record and this MTTR record share one identity.
          3. Self-generated ID + injection approximated as detection time —
             degenerate fallback for manual/ad hoc alerts with no harness
             correlation record at all. Logged as a correlation miss because
             it means T_detect will read ~0 for this trial.
        """
        annotations  = alert.get("annotations", {})
        ann_trial_id = annotations.get("trial_id")
        ann_inj_raw  = annotations.get("injection_timestamp")

        if ann_trial_id and ann_inj_raw:
            return ann_trial_id, int(float(ann_inj_raw) * 1000)

        try:
            from experiments.correlation.correlate import find_recent_injection
            # self.target is the full EC2 Name tag from the Prometheus
            # node_name label (e.g. "self-healing-thesis-experiment-target-3"),
            # but experiments/inject/inject_once.py's --target CLI argument
            # and its correlation records use the short form ("target-3") —
            # confirmed live: every trial logged "No trial correlation found"
            # and fell back to a self-generated trial_id with T_detect
            # approximated as 0, because find_recent_injection()'s exact
            # string match on `target` never succeeded for ANY fault class.
            # Normalise to the short suffix both sides actually share.
            short_target_match = re.search(r"target-\d+$", self.target)
            short_target = short_target_match.group(0) if short_target_match else self.target
            # find_recent_injection()'s default window_seconds=120 assumes
            # only scrape-interval + alert for-window latency (per its own
            # docstring), but disk_saturation's t0-to-webhook latency also
            # includes the physical time to `dd`-fill the disk before the
            # 60s sustained-alert threshold even starts counting — confirmed
            # live: t0 to webhook receipt measured ~192s for disk_saturation,
            # already past the 120s window. 300s matches the thesis's own
            # SLO ceiling (acceptance criterion) and comfortably
            # covers every fault class's worst-case pre-alert latency.
            record = find_recent_injection(
                target=short_target, fault_type=self.fault_type, window_seconds=300,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("Correlation-file lookup unavailable: %s", exc)
            record = None

        if record:
            self.block = int(record.get("block", -1))
            return record["trial_id"], record["t0_epoch_ms"]

        fallback_id = f"t-{datetime.now(timezone.utc).strftime('%Y-%m-%d')}-{str(uuid.uuid4())[:8]}"
        logger.warning(
            "No trial correlation found for target=%s fault_type=%s — "
            "self-generated trial_id=%s; T_detect will approximate injection "
            "as detection time for this record.",
            self.target, self.fault_type, fallback_id,
        )
        return fallback_id, None

    # ── Phase timestamp recorders ─────────────────────────────────────────────

    def record_detection(self) -> None:
        """Call immediately on webhook receipt (T_detect starts here)."""
        now_ms = _now_ms()
        mono   = time.monotonic()
        self._t_detected_ms = now_ms
        self._mono_detect   = mono
        # Best-effort: if no injection timestamp provided, approximate as detection
        if self._t_fault_injected_ms is None:
            self._t_fault_injected_ms = now_ms

    def record_classification(self, actuator: str, action: str) -> None:
        """Call after classify_and_route() returns."""
        self._t_classified_ms = _now_ms()
        self._mono_classify   = time.monotonic()
        self.actuator = actuator
        self.playbook = action

    def record_remediation(self, retry_count: int = 0) -> None:
        """Call after execute_remediation() returns terminal status."""
        self._t_remediated_ms = _now_ms()
        self._mono_remediate  = time.monotonic()
        self._retry_count     = retry_count

    def record_actuator_report(self, report: dict | None) -> None:
        """
        Accept structured facts the actuator observed while acting.

        Used for the final filesystem utilisation reported by disk_cleanup.yml,
        which is the evidence distinguishing a zero-exit dispatch from an alert
        that actually cleared.  Ignores anything falsy so a
        playbook that reports nothing is not an error.
        """
        if report:
            self._actuator_report.update(report)

    def record_observability_restore(self) -> None:
        """
 instance_failure only . Call after
        execute.py::restore_diagnostic_agents() AND
        observability_checker.py::ObservabilityChecker.wait_until_forwarding()
        have both succeeded — never immediately after the Ansible playbook
        alone. Marks the end of the new T_observability_restore phase,
        which sits between T_remediate and T_validate.
        """
        self._t_observability_restored_ms = _now_ms()
        self._mono_observability_restore  = time.monotonic()

    def record_validation(self) -> None:
        """Call when Monitor→Analyse cycle confirms the alert has cleared."""
        self._t_validated_ms = _now_ms()
        self._mono_validate  = time.monotonic()

    # ── Derived metrics ───────────────────────────────────────────────────────

    def total_seconds(self) -> float:
        """
        T_MTTR = T_detect + T_classify + T_remediate + T_validate, or
        T_detect + T_classify + T_remediate + T_observability_restore +
 T_validate for instance_failure trials . No
        special-casing needed here: mono_validate is always the final
        checkpoint regardless of whether the new phase ran in between, so
        this telescoping sum absorbs it automatically.
        """
        if self._mono_detect is None or self._mono_validate is None:
            return -1.0
        # T_detect is the wall-clock interval from fault injection to webhook
        # receipt (external to the orchestrator).  The monotonic phases cover
        # only the orchestrator's own span (classify → remediate → validate).
        t_detect_s = 0.0
        if self._t_fault_injected_ms is not None and self._t_detected_ms is not None:
            t_detect_s = (self._t_detected_ms - self._t_fault_injected_ms) / 1000.0
        return round(t_detect_s + (self._mono_validate - self._mono_detect), 3)

    def _phase_seconds(self, start: float | None, end: float | None) -> float:
        if start is None or end is None:
            return -1.0
        return round(end - start, 3)

    # ── Persistence ───────────────────────────────────────────────────────────

    def save(
        self,
        recovery_status: str = "unknown",
        retry_count: int | None = None,
        circuit_breaker: str = "closed",
        drift_detected: bool = False,
        escalation_reason: str | None = None,
    ) -> None:
        """
        Persist the trial record as one JSON-Lines line (Ch3 Table 3.5,
        Ch4 Table 4.8 — the sole persistence format the thesis defines).

        Asserts the decomposed-MTTR phase-sum invariant (Thesis Eq. 3.1,
) before writing. A violation is a FATAL error that
        terminates the process to prevent silent corruption of the dataset.

        Appended atomically (single-worker Gunicorn ensures no concurrent
        writes on the same trial).
        """
        actual_retry  = retry_count if retry_count is not None else getattr(self, "_retry_count", 0)
        # Four thesis phases (Eq. 3.1, §3.7.2), plus a fifth for
        # instance_failure only:
        #   T_detect               — fault injection → webhook arrival   (wall-clock delta)
        #   T_classify              — webhook arrival → routing decision  (mono: A→B)
        #   T_remediate             — routing decision → actuator terminal (mono: B→C)
        #   T_observability_restore — actuator terminal → diagnostic agents
        #                             restored + forwarding-verified       (mono: C→C', instance_failure only)
        #   T_validate              — [C' or C] → alert cleared            (mono: C'→D or C→D)
        # Sum = T_detect_wc + (D − A) = total_seconds()  ✓  (unaffected by
        # whether the new phase is present, since it just splits C→D into
        # two consecutive intervals rather than changing the span itself).
        if self._t_fault_injected_ms is not None and self._t_detected_ms is not None:
            T_detect = round((self._t_detected_ms - self._t_fault_injected_ms) / 1000.0, 3)
        else:
            T_detect = 0.0
        T_classify  = self._phase_seconds(self._mono_detect,    self._mono_classify)
        T_remediate = self._phase_seconds(self._mono_classify,  self._mono_remediate)
        T_observability_restore = self._phase_seconds(
            self._mono_remediate, self._mono_observability_restore,
        )
        # T_validate's start point shifts to the end of the new phase when
        # present (instance_failure), else falls back to mono_remediate —
        # identical to today's value for the other five fault classes.
        _validate_start = (
            self._mono_observability_restore
            if self._mono_observability_restore is not None
            else self._mono_remediate
        )
        T_validate  = self._phase_seconds(_validate_start, self._mono_validate)
        T_MTTR      = self.total_seconds()

        # ── Phase-sum invariant assertion (Thesis Eq. 3.1, §3.7.2) ──────────
        # T_MTTR = T_detect + T_classify + T_remediate + T_validate, or with
        # + T_observability_restore spliced in for instance_failure trials.
        # Tolerance is _PHASE_SUM_TOLERANCE (3 ms, defined below).  Each phase is
        # rounded to millisecond precision on capture, so a four- or five-term
        # sum can differ from the independently computed total by a few rounding
        # units without anything being wrong.  1 ms was too tight and was widened.
        # Only assert when all required phases completed (validated success
        # path) — instance_failure additionally requires
        # mono_observability_restore to be set; the other five fault
        # classes never touch it, so it's excluded from their precondition.
        _required_checkpoints = [
            self._mono_detect, self._mono_classify,
            self._mono_remediate, self._mono_validate,
        ]
        if self.fault_type == "instance_failure":
            _required_checkpoints.append(self._mono_observability_restore)
        if (
            recovery_status == "success"
            and all(v is not None for v in _required_checkpoints)
            and T_MTTR > 0
        ):
            decomposed_sum = T_detect + T_classify + T_remediate + T_validate
            if T_observability_restore >= 0:
                decomposed_sum += T_observability_restore
            _assert_phase_sum_invariant(T_MTTR, decomposed_sum, self.trial_id)

        record = {
            "trial_id":             self.trial_id,
            "system_under_test":    self.system_under_test,
            "target":               self.target,
            "fault_type":           self.fault_type,
            "block":                self.block,
            "actuator":             self.actuator or "unknown",
            "playbook":             self.playbook or "",
            "t_fault_injected_ms":  self._t_fault_injected_ms or -1,
            "t_detected_ms":        self._t_detected_ms or -1,
            "t_classified_ms":      self._t_classified_ms or -1,
            "t_remediated_ms":      self._t_remediated_ms or -1,
            "t_observability_restored_ms": self._t_observability_restored_ms or -1,
            "t_validated_ms":       self._t_validated_ms or -1,
            "T_detect_seconds":     T_detect,
            "T_classify_seconds":   T_classify,
            "T_remediate_seconds":  T_remediate,
            "T_observability_restore_seconds": T_observability_restore,
            "T_validate_seconds":   T_validate,
            "T_MTTR_seconds":       T_MTTR,
            "recovery_status":      recovery_status,
            "retry_count":          actual_retry,
            "circuit_breaker":      circuit_breaker,
            "drift_detected":       drift_detected,
            "escalation_reason":    escalation_reason or "",
        }

        # Conditional field: only disk_saturation trials carry the final
        # filesystem utilisation, and only when the playbook reported it.
        # Absent rather than sentinel-valued, so a reader cannot mistake a
        # non-reporting class for a 0% measurement.
        if self.fault_type == "disk_saturation":
            final_pct = self._actuator_report.get("final_disk_pct")
            if final_pct is not None:
                record["final_disk_pct"] = final_pct

        _assert_record_schema(record)

        with JSONL_PATH.open("a") as fh:
            fh.write(json.dumps(record) + "\n")

        logger.info(
            "Trial record saved: trial_id=%s T_MTTR=%.1fs status=%s",
            self.trial_id, T_MTTR, recovery_status,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────


# The closed recovery_status vocabulary, matching Thesis Table 4.9 and the
# literals actually passed at every mttr.save() call site in app.py.  Kept as a
# set so an invented value fails here rather than entering the dataset and
# silently widening the schema the manuscript declares.
_VALID_RECOVERY_STATUS = frozenset({
    "success",
    "retry_exhausted",
    "aborted_plan",
    "timeout",
    "diagnostic_bootstrap_failed",
})


def _assert_record_schema(record: dict) -> None:
    """
    Structural validation of a trial record, run after the phase-sum invariant
    and before the JSONL append.

    The phase-sum check catches arithmetic that does not reconcile.  It cannot
    catch a record that is internally consistent but semantically wrong -- a
    sentinel written where a real value belongs, a successful trial carrying an
    escalation reason, a negative retry count.  Those would survive into the
    dataset and be indistinguishable from real measurements at analysis time.

    Raises RuntimeError; the caller treats that as fatal and does not persist.
    """
    problems: list[str] = []

    if "block" not in record:
        problems.append("record is missing the block field (experimental-unit identity)")
    else:
        blk = record["block"]
        if not isinstance(blk, int) or blk == 0 or blk < -1:
            problems.append(
                f"block={blk!r} is not a valid experimental-unit index "
                "(expected a positive schedule block, or -1 for an ad-hoc trial)"
            )

    status = record.get("recovery_status")
    if status not in _VALID_RECOVERY_STATUS:
        problems.append(
            f"recovery_status={status!r} is outside the closed vocabulary "
            f"{sorted(_VALID_RECOVERY_STATUS)}"
        )

    if record.get("retry_count", 0) < 0:
        problems.append(f"retry_count={record.get('retry_count')} is negative")

    if record.get("recovery_status") == "success":
        if record.get("escalation_reason", "") != "":
            problems.append("recovery_status=success but escalation_reason is set")
        if record.get("T_MTTR_seconds", -1) < 0:
            problems.append("recovery_status=success but T_MTTR_seconds is a sentinel")
    elif record.get("escalation_reason", "") == "":
        problems.append(
            f"recovery_status={record.get('recovery_status')!r} carries no escalation_reason"
        )

    # The observability-restore phase belongs to instance_failure alone.  On any
    # other class both fields must be the sentinel, never a real measurement.
    obs_ms  = record.get("t_observability_restored_ms")
    obs_sec = record.get("T_observability_restore_seconds")
    if record.get("fault_type") != "instance_failure":
        if obs_ms != -1 or obs_sec != -1.0:
            problems.append(
                f"fault_type={record.get('fault_type')!r} carries an observability-restore "
                f"value (ms={obs_ms}, s={obs_sec}); expected the -1 / -1.0 sentinels"
            )

    # final_disk_pct is scoped to disk_saturation and must be a real
    # percentage when present -- never a sentinel standing in for "unknown".
    if "final_disk_pct" in record:
        if record.get("fault_type") != "disk_saturation":
            problems.append(
                f"fault_type={record.get('fault_type')!r} carries final_disk_pct; "
                "the field is scoped to disk_saturation"
            )
        pct = record["final_disk_pct"]
        if not isinstance(pct, int) or not 0 <= pct <= 100:
            problems.append(f"final_disk_pct={pct!r} is not an integer percentage in 0..100")

    if problems:
        raise RuntimeError(
            "MTTR record failed schema validation and was NOT persisted | "
            f"trial_id={record.get('trial_id')} | " + "; ".join(problems)
        )

def _now_ms() -> int:
    """Current UTC time in milliseconds since epoch."""
    return int(datetime.now(timezone.utc).timestamp() * 1000)


def _ensure_files() -> None:
    """Create the results directory and JSONL file if needed."""
    _DEFAULT_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    JSONL_PATH.touch(exist_ok=True)


_PHASE_SUM_TOLERANCE = 3e-3  # 3 milliseconds — Thesis Eq. 3.1 assertion tolerance


def _assert_phase_sum_invariant(
    T_MTTR: float,
    decomposed_sum: float,
    trial_id: str,
) -> None:
    """
    Assert the decomposed-MTTR phase-sum invariant (Thesis Eq. 3.1):

        T_MTTR = T_detect + T_classify + T_remediate + T_validate

    Tolerance accounts for floating-point rounding only. T_MTTR
    (total_seconds()) is a single round(..., 3) of the combined span, while
    decomposed_sum sums FOUR independently round(..., 3)'d phase values —
    each independent rounding can shift its value by up to 0.5 ms, so the
    two computations can legitimately diverge by up to 4x0.5ms (decomposed
    phases) + 0.5ms (T_MTTR's own rounding) = 2.5 ms even with zero actual
    sequencing bug. The previous 1 ms tolerance was mathematically tighter
    than this design's own worst case — confirmed live: a genuinely
    successful trial (T_MTTR=90.582s, decomposed_sum=90.581s, residual
    exactly 1.000ms) was rejected and its record discarded entirely, purely
    from this rounding accumulation, not a timestamp-sequencing bug.
    A violation indicates a bug in the timestamp recording sequence and
    MUST NOT be silently swallowed — it would corrupt the empirical dataset.
    This function raises a fatal RuntimeError on violation.
    """
    residual = abs(decomposed_sum - T_MTTR)
    if residual >= _PHASE_SUM_TOLERANCE:
        raise RuntimeError(
            f"FATAL — Decomposed-MTTR phase-sum invariant VIOLATED | "
            f"trial_id={trial_id} "
            f"T_MTTR={T_MTTR:.6f}s "
            f"decomposed_sum={decomposed_sum:.6f}s "
            f"residual={residual:.6f}s "
            f"tolerance={_PHASE_SUM_TOLERANCE:.0e}s. "
            f"This indicates a bug in the MTTR timestamp recording sequence. "
            f"The trial record has NOT been written to prevent dataset corruption."
        )
    import logging as _logging
    _logging.getLogger(__name__).debug(
        "Phase-sum invariant OK | trial_id=%s residual=%.6fs", trial_id, residual
    )


def _next_trial_number(fault_type: str) -> int:
    """Count existing JSONL records for this fault_type and return the next trial number."""
    if not JSONL_PATH.exists():
        return 1
    try:
        count = 0
        for line in JSONL_PATH.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                if json.loads(line).get("fault_type") == fault_type:
                    count += 1
            except json.JSONDecodeError:
                continue
        return count + 1
    except Exception:  # noqa: BLE001
        return 1
