"""
experiments/trials/overhead_sampler.py — phase-tagged resource sampler for RQ3.

Collects per-container CPU and resident-memory samples from the Docker Engine
API at a fixed interval while a trial runs, tagging every sample with the
experimental phase it falls in.  Aggregation is performed afterwards by
analysis/04_overhead.py; this module only records.

Design notes
------------
Sampling interval is 5 s rather than 15 s.  Five of the six fault classes
complete in roughly 20-35 s, so a 15 s interval would yield one or two samples
per trial and the per-trial median would degenerate to a single observation.

Two phases are recorded per trial:

    steady   the 60 s steady-state window that precedes fault injection
    active   the interval [t_0, t_clear] during which the loop handles the fault

RQ3's primary figure is computed from `steady` samples, preserving the
steady-state definition given in Thesis Section 3.7.3.  `active` samples give
the secondary figure: what fault handling adds on top of that.

Phase assignment is not timed.  Samples are buffered with their own wall-clock
timestamps and tagged at stop() by comparison against the authoritative t_0
that run_one.py records in results/correlation.jsonl at the moment of
injection.  A timer keyed to the configured steady-state window would mis-tag
every sample taken between the window elapsing and the gate actually clearing:
if wait_steady_state() needs 73 s rather than 60 s, thirteen seconds of genuine
steady-state activity would be recorded as `active` and would contaminate the
secondary figure.  Reading t_0 after the fact removes that failure mode
entirely, at the cost of holding one trial's samples in memory — roughly seven
hundred rows for the longest fault class.

Measurement source is the Docker Engine API (/containers/{id}/stats over
/var/run/docker.sock), not the cAdvisor REST API this module originally used.

Deployment-session correction (2026-09-21): the original cAdvisor-based
design was confirmed live to silently collect zero rows across the entire
60-trial official S1 campaign. Root cause: this host's Docker 28.5.2 uses
the containerd snapshotter image store, and the pinned cAdvisor v0.49.1
(Ch.4 Table 4.2) cannot resolve the read-write layer ID any docker-*.scope
cgroup needs under that store ("failed to identify the read-write layer ID
... no such file or directory", confirmed live in cadvisor's own container
logs). This is not endpoint-specific: cAdvisor's manager fails to register
these containers internally at discovery time, before any REST path is
queried, and a raw-cgroup/systemd fallback query (confirmed live via
Prometheus: `count by (id, name, image) (container_memory_working_set_bytes)`
and `container_cpu_usage_seconds_total{name=""}`) returns only host-level
systemd services/slices -- zero series for any docker-*.scope container,
under any label set. So the data was never retrospectively recoverable from
Prometheus either, and no REST-path change inside cAdvisor could have fixed
it. The Docker Engine API sidesteps this cleanly: it identifies containers
by live ID from the running daemon and never touches image-layer metadata,
so the containerd-snapshotter incompatibility does not apply to it.

This is a real, disclosable change to the RQ3 measurement instrument, not a
transparent swap -- CLAUDE.md §5.7 documents cAdvisor specifically as "the
measurement instrument" for RQ3, and the memory figure below is Docker's own
"usage minus reclaimable cache" working-set approximation (the only thing
cgroup v2 exposes), not the raw cgroup v1 `memory.rss` field cAdvisor used
to expose. Both cgroup v2 (confirmed live via
`/sys/fs/cgroup/cgroup.controllers`) and unprivileged Docker-socket access
for the `ubuntu` user (confirmed live) were verified on this host before
this rewrite was applied.

Output: one JSON-Lines record per sample, appended to results/overhead_samples.jsonl.
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("overhead_sampler")

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Resolve the results directory exactly as experiments/correlation/correlate.py
# and orchestrator/mttr_logger.py do, so all three processes agree on one
# location when RESULTS_DIR is set (harness/orchestrator co-location, §5.13).
_RESULTS_DIR = (
    Path(os.environ["RESULTS_DIR"])
    if os.environ.get("RESULTS_DIR")
    else _REPO_ROOT / "results"
)
_SAMPLES_PATH = _RESULTS_DIR / "overhead_samples.jsonl"
_CORRELATION_PATH = _RESULTS_DIR / "correlation.jsonl"

SAMPLE_INTERVAL_SECONDS = 5.0
DOCKER_SOCKET = "/var/run/docker.sock"
HOST_VCPU = 2  # t3.medium -- same denominator analysis/04_overhead.py uses

# The eight deliberately deployed observability services.  cAdvisor itself is
# sampled too, but recorded under its own name so that it can be reported
# separately as measurement overhead rather than folded into this aggregate.
OBSERVABILITY_CONTAINERS = {
    "prometheus", "alertmanager", "grafana",
    "elasticsearch", "logstash", "kibana",
    "otelcol", "jaeger",
}
MEASUREMENT_INSTRUMENT = "cadvisor"


class _UnixHTTPConnection(http.client.HTTPConnection):
    """http.client over a Unix domain socket -- stdlib only, no `docker` pip dep."""

    def __init__(self, path: str, timeout: float = 10.0) -> None:
        super().__init__("localhost", timeout=timeout)
        self._unix_path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._unix_path)


def _docker_get(path: str, socket_path: str = DOCKER_SOCKET, timeout: float = 10.0):
    conn = _UnixHTTPConnection(socket_path, timeout=timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status != 200:
            raise RuntimeError(f"Docker API {path} returned {resp.status}: {body[:200]!r}")
        return json.loads(body.decode())
    finally:
        conn.close()


def _list_target_containers(socket_path: str = DOCKER_SOCKET) -> dict[str, str]:
    """Map observability container name -> live container ID, straight from the
    Docker daemon. Deliberately bypasses cAdvisor -- see the module docstring's
    2026-09-21 correction for why cAdvisor cannot see these containers at all
    under this host's containerd-snapshotter image store."""
    containers = _docker_get("/containers/json", socket_path)
    out: dict[str, str] = {}
    for c in containers:
        names = [n.lstrip("/").lower() for n in c.get("Names", [])]
        for n in names:
            if n in OBSERVABILITY_CONTAINERS or n == MEASUREMENT_INSTRUMENT:
                out[n] = c["Id"]
                break
    return out


def _parse_docker_stats(stats: dict) -> dict:
    """CPU in cores and memory in MiB from one non-streaming Docker stats call.

    Docker's non-streaming stats response already carries both a current and
    a previous CPU sample (`cpu_stats` / `precpu_stats`), so one call gives a
    full delta -- unlike the old cAdvisor-based design, which needed two polls.

    Memory uses `usage - inactive_file` (cgroup v2, confirmed on this host) as
    a working-set approximation, matching what `docker stats` itself reports
    -- not literally RSS, since cgroup v2 has no single "rss" stat. This is a
    real measurement-definition change from the original cAdvisor-sourced
    figure (module docstring), not a drop-in equivalent.
    """
    cpu_now, cpu_prev = stats.get("cpu_stats", {}), stats.get("precpu_stats", {})
    total_now = cpu_now.get("cpu_usage", {}).get("total_usage")
    total_prev = cpu_prev.get("cpu_usage", {}).get("total_usage")
    sys_now, sys_prev = cpu_now.get("system_cpu_usage"), cpu_prev.get("system_cpu_usage")
    online_cpus = cpu_now.get("online_cpus") or HOST_VCPU
    if None in (total_now, total_prev, sys_now, sys_prev):
        return {"cpu_cores": None, "rss_mib": None}
    sys_delta = sys_now - sys_prev
    cpu_cores = ((total_now - total_prev) / sys_delta) * online_cpus if sys_delta > 0 else 0.0

    mem = stats.get("memory_stats", {})
    mem_stats = mem.get("stats", {})
    cache = mem_stats.get("inactive_file", mem_stats.get("cache", 0))
    rss_mib = max(0, mem.get("usage", 0) - cache) / (1024 * 1024)

    # Deployment-session fix (2026-09-22): confirmed live, 3 samples out of
    # 1536 in the first RQ3 campaign (2/1536 on kibana, 1/1536 on grafana),
    # each paired with an anomalously tiny rss_mib (0.79-2.32 MiB) -- the
    # signature of a container caught exactly mid-restart, where cpu_stats
    # reflects a freshly-reset counter from the new process while
    # precpu_stats still holds the tail of the old process's cumulative
    # usage, producing a physically impossible negative delta. Kibana's
    # OOM-crash tendency is already documented (§2); this is that same
    # behaviour surfacing here. A single such sample can dominate a
    # per-trial median when the sample count for that (trial, phase,
    # container) is small -- confirmed live: it moved the reported RQ3
    # steady-state CPU capacity figure by 7.6 percentage points before being
    # caught and filtered out by hand. Rejecting it here, matching the
    # existing None-skip convention in _sample_once(), means future runs
    # never collect it in the first place.
    if cpu_cores < 0:
        return {"cpu_cores": None, "rss_mib": None}

    return {"cpu_cores": round(cpu_cores, 6), "rss_mib": round(rss_mib, 2)}


class OverheadSampler:
    """Background sampler bound to one trial.

    Usage:
        s = OverheadSampler(trial_id, fault_type)
        s.start()          # samples until stopped
        ...                # run the trial
        s.stop()           # tags every sample against t_0, then flushes
    """

    def __init__(self, trial_id: str, fault_type: str,
                 docker_socket: str = DOCKER_SOCKET,
                 interval: float = SAMPLE_INTERVAL_SECONDS,
                 samples_path: Path = _SAMPLES_PATH) -> None:
        self.trial_id = trial_id
        self.fault_type = fault_type
        self.docker_socket = docker_socket
        self.interval = interval
        self.samples_path = samples_path
        self._buffer: list[dict] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._written = 0

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        self.samples_path.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info("Overhead sampler started | trial=%s interval=%.0fs",
                    self.trial_id, self.interval)

    def _lookup_t0_ms(self) -> Optional[int]:
        """Authoritative t_0 for this trial, as recorded by run_one.py."""
        path = _CORRELATION_PATH
        if not path.exists():
            return None
        t0 = None
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("trial_id") == self.trial_id and rec.get("t0_epoch_ms"):
                t0 = int(rec["t0_epoch_ms"])
        return t0

    def stop(self) -> int:
        """Halt sampling, tag every buffered sample against t_0, and flush."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.interval + 5)

        t0_ms = self._lookup_t0_ms()
        if t0_ms is None:
            # No injection record: the trial aborted before t_0 was committed,
            # so every sample necessarily precedes injection.
            logger.warning("No t_0 in correlation.jsonl for trial=%s — "
                           "tagging all %d samples as steady",
                           self.trial_id, len(self._buffer))
        for row in self._buffer:
            row["phase"] = ("active" if t0_ms is not None and row["ts_ms"] >= t0_ms
                            else "steady")
            row.pop("ts_ms", None)

        with self.samples_path.open("a", encoding="utf-8") as fh:
            for row in self._buffer:
                fh.write(json.dumps(row) + "\n")
        self._written = len(self._buffer)
        n_active = sum(1 for r in self._buffer if r["phase"] == "active")
        logger.info("Overhead sampler stopped | trial=%s samples=%d "
                    "(steady=%d active=%d)", self.trial_id, self._written,
                    self._written - n_active, n_active)
        self._buffer = []
        return self._written

    # ── worker ───────────────────────────────────────────────────────────────
    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._sample_once()
            except Exception as exc:                                   # noqa: BLE001
                # A failed sample must never abort a trial; RQ3 tolerates gaps.
                logger.warning("Overhead sample failed (continuing): %s", exc)
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.0, self.interval - elapsed))

    def _sample_once(self) -> None:
        targets = _list_target_containers(self.docker_socket)
        now = datetime.now(timezone.utc)
        ts = now.isoformat()
        ts_ms = int(now.timestamp() * 1000)
        rows = []
        for name, container_id in targets.items():
            try:
                raw = _docker_get(f"/containers/{container_id}/stats?stream=false",
                                   self.docker_socket)
            except Exception as exc:                                   # noqa: BLE001
                logger.warning("Docker stats fetch failed for %s: %s", name, exc)
                continue
            m = _parse_docker_stats(raw)
            if m["cpu_cores"] is None:
                continue
            rows.append({
                "trial_id":   self.trial_id,
                "fault_type": self.fault_type,
                "ts":         ts,
                "ts_ms":      ts_ms,
                "container":  name,
                "instrument": name == MEASUREMENT_INSTRUMENT,
                "cpu_cores":  m["cpu_cores"],
                "rss_mib":    m["rss_mib"],
            })
        self._buffer.extend(rows)
