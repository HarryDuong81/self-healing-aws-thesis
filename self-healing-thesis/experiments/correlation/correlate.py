#!/usr/bin/env python3
"""
experiments/correlation/correlate.py — Persist and retrieve the
trial_id ↔ t_0 (fault injection timestamp) mapping to/from S3.

Each trial records its injection timestamp (t_0) separately from the
orchestrator's webhook receipt timestamp (t_detected) because t_0 is
wall-clock time on the experiment control node, not inside the orchestrator.

The correlation record is written atomically to S3 at injection time
and later joined with the MTTR JSONL record in the analysis notebooks
to compute the true T_detect = t_detected_ms − t_0_ms.

Usage:
    from experiments.correlation.correlate import record_injection, load_map

    # At injection time:
    record_injection(trial_id="t-2025-01-15-a1b2c3d4", t0_epoch_ms=1736956800000)

    # In analysis:
    mapping = load_map(s3_prefix="s3://its-thesis-data/campaign/")
    t0 = mapping["t-2025-01-15-a1b2c3d4"]
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import boto3

_REGION       = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")

# Shared with orchestrator/mttr_logger.py's _DEFAULT_RESULTS_DIR resolution
# so both processes read/write the same file when RESULTS_DIR points at a
# shared volume (Thesis Ch4 §4.6.1 — the harness-written correlation file
# joined by the logger at close-trial time).
_LOCAL_CACHE = (
    Path(os.environ["RESULTS_DIR"]) / "correlation.jsonl"
    if os.environ.get("RESULTS_DIR")
    else Path(__file__).resolve().parent.parent.parent / "results" / "correlation.jsonl"
)


def record_injection(
    trial_id: str,
    t0_epoch_ms: int,
    fault_type: str = "",
    target: str = "",
    block: int = -1,
    s3_prefix: Optional[str] = None,
) -> None:
    """
    Write a correlation record mapping trial_id → t_0_ms.

    Appended to a local JSONL file and optionally mirrored to S3.
    """
    record = {
        "trial_id":    trial_id,
        "t0_epoch_ms": t0_epoch_ms,
        "t0_iso":      datetime.fromtimestamp(t0_epoch_ms / 1000, tz=timezone.utc).isoformat(),
        "fault_type":  fault_type,
        "target":      target,
        # Experimental-unit identity. (fault_type, target, block) is the
        # pairing key joining an S1 observation to its S2 counterpart; -1
        # marks a trial run outside a campaign schedule (smoke test, dev run),
        # which is therefore not eligible for the paired H1 analysis.
        "block":       block,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }

    _LOCAL_CACHE.parent.mkdir(parents=True, exist_ok=True)
    with _LOCAL_CACHE.open("a") as fh:
        fh.write(json.dumps(record) + "\n")

    if s3_prefix:
        _mirror_to_s3(record, s3_prefix)


def load_map(
    local_path: Optional[str] = None,
    s3_prefix: Optional[str] = None,
) -> dict[str, int]:
    """
    Load the trial_id → t0_epoch_ms mapping from local JSONL or S3.

    Returns a dict keyed by trial_id.
    """
    if s3_prefix and not local_path:
        _fetch_from_s3(s3_prefix)

    src = Path(local_path) if local_path else _LOCAL_CACHE
    if not src.exists():
        return {}

    mapping: dict[str, int] = {}
    for line in src.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            mapping[rec["trial_id"]] = rec["t0_epoch_ms"]
        except (json.JSONDecodeError, KeyError):
            continue
    return mapping


class DuplicateInjectionError(RuntimeError):
    """A second injection of the same (fault_type, target) inside the
    correlation window would make the trial's experimental unit ambiguous."""


# The orchestrator resolves a trial's identity by looking up the most recent
# injection for (target, fault_type) within this window.  Two injections of the
# same combination inside it are indistinguishable to that lookup, so the second
# trial could adopt the first trial's block and silently corrupt the H1 pairing.
CORRELATION_WINDOW_SECONDS = 300


def assert_no_recent_injection(
    target: str,
    fault_type: str,
    window_seconds: int = CORRELATION_WINDOW_SECONDS,
    local_path: Optional[str] = None,
) -> None:
    """
    Pre-flight guard: refuse to inject if the same (fault_type, target) was
    already injected inside the correlation window.

    The failure this prevents is silent. Both trials would produce
    schema-valid records; only the experimental-unit identity would be wrong,
    and nothing downstream could detect it. Raising here converts an
    operational convention into a constraint the harness enforces.

    Raises DuplicateInjectionError; the caller may override deliberately.
    """
    prior = find_recent_injection(
        target=target,
        fault_type=fault_type,
        window_seconds=window_seconds,
        local_path=local_path,
    )
    if prior is None:
        return
    age_s = (int(datetime.now(timezone.utc).timestamp() * 1000) - prior["t0_epoch_ms"]) / 1000.0
    raise DuplicateInjectionError(
        f"({fault_type}, {target}) was already injected {age_s:.0f}s ago "
        f"(trial_id={prior.get('trial_id')}, block={prior.get('block')}); the "
        f"correlation window is {window_seconds}s. Injecting again now would let "
        f"the orchestrator adopt the earlier trial's experimental unit. Wait "
        f"{max(0, window_seconds - age_s):.0f}s, or pass --allow-duplicate-injection "
        f"if this trial is deliberately not part of the paired campaign."
    )


def find_recent_injection(
    target: str,
    fault_type: str,
    before_ms: Optional[int] = None,
    window_seconds: int = 120,
    local_path: Optional[str] = None,
) -> Optional[dict]:
    """
    Find the most recent injection record for (target, fault_type) within
    `window_seconds` before `before_ms` (default: now).

    This is the orchestrator-side counterpart to record_injection(): the
    harness cannot know the orchestrator's self-generated trial_id in
    advance (it doesn't exist until the webhook is received), so exact-match
    lookup by trial_id is not possible for genuine Prometheus-triggered
    alerts. Matching by (target, fault_type, recency) instead lets
    orchestrator/mttr_logger.py adopt the harness's trial_id and t0 for the
    real campaign path (Thesis Ch4 §4.6.1).

    window_seconds=120 comfortably covers Tdetect (bounded by the 15s
    Prometheus scrape interval plus each rule's for-window, at most 60s for
    instance_failure) with headroom (Thesis §3.1.3).

    Returns the matching record dict (trial_id, t0_epoch_ms, ...) or None.
    """
    reference_ms = before_ms if before_ms is not None else int(time.time() * 1000)
    window_ms = window_seconds * 1000

    src = Path(local_path) if local_path else _LOCAL_CACHE
    if not src.exists():
        return None

    candidates = []
    for line in src.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("target") != target or rec.get("fault_type") != fault_type:
            continue
        t0 = rec.get("t0_epoch_ms")
        if t0 is None or not (reference_ms - window_ms <= t0 <= reference_ms):
            continue
        candidates.append(rec)

    if not candidates:
        return None
    # max() returns the FIRST maximal element, so records sharing a
    # millisecond would resolve to the earliest-written one — the opposite of
    # "most recent". Scanning in reverse makes the last-appended record win a
    # tie, which is what the caller means by recency.
    return max(reversed(candidates), key=lambda r: r["t0_epoch_ms"])


def _mirror_to_s3(record: dict, s3_prefix: str) -> None:
    """Append the record to the S3 correlation log."""
    parts  = s3_prefix.lstrip("s3://").split("/", 1)
    bucket = parts[0]
    prefix = parts[1].rstrip("/") if len(parts) > 1 else ""
    key    = f"{prefix}/correlation.jsonl" if prefix else "correlation.jsonl"

    s3 = boto3.client("s3", region_name=_REGION)

    # Append: fetch existing, append new line, put back
    try:
        existing = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
    except s3.exceptions.NoSuchKey:
        existing = ""

    new_content = existing + json.dumps(record) + "\n"
    s3.put_object(
        Bucket=bucket, Key=key,
        Body=new_content.encode(),
        ContentType="application/x-ndjson",
    )


def _fetch_from_s3(s3_prefix: str) -> None:
    """Download the S3 correlation log to the local cache path."""
    parts  = s3_prefix.lstrip("s3://").split("/", 1)
    bucket = parts[0]
    prefix = parts[1].rstrip("/") if len(parts) > 1 else ""
    key    = f"{prefix}/correlation.jsonl" if prefix else "correlation.jsonl"

    s3 = boto3.client("s3", region_name=_REGION)
    try:
        content = s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode()
        _LOCAL_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _LOCAL_CACHE.write_text(content)
    except Exception:
        pass
