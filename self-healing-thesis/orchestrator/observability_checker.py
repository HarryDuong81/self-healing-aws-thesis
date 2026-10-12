#!/usr/bin/env python3
"""
observability_checker.py — Forwarding verification for the MAPE-K
Knowledge/Validate phase, diagnostic-path telemetry.

Companion to health_checker.py, kept as a separate class rather than folded
into HealthChecker: the two check fundamentally different signals
(Prometheus ALERTS clearing — the canonical fault-cleared t_clear — vs.
Elasticsearch/OTel-Collector forwarding evidence for the diagnostic-path
agents restored after an instance_failure recreation). Keeping them apart
preserves HealthChecker's single responsibility and makes both
independently testable.

Deliberately act-nothing: this module only reads (Elasticsearch queries,
SSM RunCommand to read an OTel agent's own self-telemetry endpoint). All
restoration actions (installing/restarting Filebeat and the OTel Agent)
live in orchestrator/execute.py::restore_diagnostic_agents() — this class
is called only AFTER that function has already succeeded, to confirm the
restored agents are genuinely forwarding data, not just running.

Scope note: this is best-effort forwarding evidence (agent-side
export-success counters + monitoring-host-side Elasticsearch receipt), not
a fully airtight cross-system trace — a reasonable bar for this
verification gate.
"""
from __future__ import annotations

import logging
import os
import re
import time

import boto3
import requests

logger = logging.getLogger(__name__)

ELASTICSEARCH_URL = os.environ.get("ELASTICSEARCH_URL", "http://localhost:9200")
_AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")

# otelcol-contrib's own internal self-telemetry server, NOT the 8889
# `prometheus` pipeline-data exporter defined in
# roles/otel_collector/tasks/main.yml's config — that port only carries
# hostmetrics data, never the collector's own export-success/failure
# counters. Confirmed live: this internal server binds 127.0.0.1:8888
# automatically regardless of anything under `exporters:` (same behaviour
# noted in observability/otelcol/config.yaml's header comment for the
# central collector) — curling 8889 for `otelcol_exporter_*` always came
# back empty, while 8888 has them.
_OTEL_AGENT_METRICS_PORT = 8888

_METRIC_LINE_RE = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(?P<value>[-+0-9.eE]+)\s*$"
)


class ObservabilityChecker:
    """
    Verifies that the diagnostic-path agents (Filebeat, OTel Agent) restored
    by execute.py::restore_diagnostic_agents() are actually forwarding data,
    not just running as active services.
    """

    _POLL_INTERVAL_SECONDS = 5

    def __init__(self, target: str, instance_id: str):
        self.target      = target
        self.instance_id = instance_id
        # Floor for the Elasticsearch receipt check.  Without it the query
        # matches any document ever indexed under this node_name -- including
        # documents shipped by the *terminated* instance before recreation --
        # so the gate would pass while the restored Filebeat forwarded nothing.
        # The check must observe a document indexed after the restore began.
        self._since_ms = int(time.time() * 1000)

    def wait_until_forwarding(self, timeout: int) -> bool:
        """
        Poll both checks every 5 seconds until both pass, or `timeout`
        seconds elapse. Returns True only once Elasticsearch has a recent
        Filebeat-sourced document for this target AND the OTel agent's own
        self-telemetry confirms a successful export with zero failures.
        """
        deadline = time.monotonic() + timeout
        es_ok = False
        otel_ok = False

        while time.monotonic() < deadline:
            if not es_ok:
                es_ok = self._check_elasticsearch_forwarding()
            if not otel_ok:
                otel_ok = self._check_otel_agent_forwarding()

            if es_ok and otel_ok:
                logger.info(
                    "Observability forwarding confirmed | target=%s instance_id=%s",
                    self.target, self.instance_id,
                )
                return True

            time.sleep(self._POLL_INTERVAL_SECONDS)

        logger.error(
            "Observability forwarding timeout after %ds | target=%s "
            "elasticsearch_ok=%s otel_agent_ok=%s",
            timeout, self.target, es_ok, otel_ok,
        )
        return False

    # ── Elasticsearch receipt check (Filebeat -> Logstash -> Elasticsearch) ──

    def _check_elasticsearch_forwarding(self) -> bool:
        try:
            resp = requests.get(
                f"{ELASTICSEARCH_URL}/_search",
                params={"size": 0},
                json={
                    "query": {
                        "bool": {
                            "filter": [
                                {"term":  {"node_name.keyword": self.target}},
                                {"range": {"@timestamp": {"gte": self._since_ms,
                                                          "format": "epoch_millis"}}},
                            ]
                        }
                    }
                },
                timeout=5,
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", {}).get("total", {})
            count = hits.get("value", 0) if isinstance(hits, dict) else hits
            return count > 0
        except requests.RequestException as exc:
            logger.debug(
                "Elasticsearch forwarding check failed (will retry): target=%s exc=%s",
                self.target, exc,
            )
            return False

    # ── OTel Agent self-telemetry check (export success/failure counters) ───

    def _check_otel_agent_forwarding(self) -> bool:
        try:
            ssm = boto3.client("ssm", region_name=_AWS_REGION)
            resp = ssm.send_command(
                InstanceIds=[self.instance_id],
                DocumentName="AWS-RunShellScript",
                Parameters={
                    "commands": [
                        f"curl -s -m 5 http://localhost:{_OTEL_AGENT_METRICS_PORT}/metrics"
                    ]
                },
                TimeoutSeconds=30,
            )
            command_id = resp["Command"]["CommandId"]
            invocation = self._wait_for_ssm_command(ssm, command_id, self.instance_id)
            if invocation is None or invocation.get("Status") != "Success":
                return False

            # No "_total" suffix - otelcol's internal self-telemetry metrics
            # don't follow the Prometheus counter-naming convention.
            # Confirmed live: curling :8888/metrics on a target shows
            # "otelcol_exporter_sent_metric_points" (bare), not
            # "otelcol_exporter_sent_metric_points_total" - the old names
            # here never matched a single line, so _sum_metric() always
            # returned None and this check could never pass.
            metrics_text = invocation.get("StandardOutputContent", "")
            sent   = self._sum_metric(metrics_text, "otelcol_exporter_sent_metric_points")
            failed = self._sum_metric(metrics_text, "otelcol_exporter_send_failed_metric_points")
            return sent is not None and sent > 0 and (failed is None or failed == 0)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "OTel agent forwarding check failed (will retry): target=%s "
                "instance_id=%s exc=%s",
                self.target, self.instance_id, exc,
            )
            return False

    @staticmethod
    def _wait_for_ssm_command(
        ssm: "boto3.client",
        command_id: str,
        instance_id: str,
        timeout_seconds: int = 30,
    ) -> dict | None:
        """Poll a single SSM RunCommand invocation to a terminal state."""
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                invocation = ssm.get_command_invocation(
                    CommandId=command_id, InstanceId=instance_id,
                )
            except ssm.exceptions.InvocationDoesNotExist:
                time.sleep(2)
                continue
            except Exception:  # noqa: BLE001
                time.sleep(2)
                continue
            if invocation["Status"] not in ("Pending", "InProgress", "Delayed"):
                return invocation
            time.sleep(2)
        return None

    @staticmethod
    def _sum_metric(metrics_text: str, metric_name: str) -> float | None:
        """
        Sum all series values for `metric_name` in a Prometheus text
        exposition payload. Returns None if the metric name never appears
        at all (distinct from a genuine 0 total).
        """
        total = None
        for line in metrics_text.splitlines():
            match = _METRIC_LINE_RE.match(line.strip())
            if not match or match.group("name") != metric_name:
                continue
            total = (total or 0.0) + float(match.group("value"))
        return total
