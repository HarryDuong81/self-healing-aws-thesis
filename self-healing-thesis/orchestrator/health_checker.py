#!/usr/bin/env python3
"""
health_checker.py — Two-stage verification for the MAPE-K Validate step.

Thesis Ch3 §3.7.2 / Ch4 §4.6.2, Appendix 4.A.9:

  Stage E3 (fast, in-process) — an immediate sanity probe run right after the
  actuator returns. It gives the retry loop deterministic feedback within a
  few seconds. It is NOT the canonical validation signal.

  Canonical t_clear (Validate phase) — committed only when the originating
  Prometheus alert clears on the next Monitor -> Analyse cycle. Implemented
  here by polling the Prometheus ALERTS series at 5-second intervals. The
  Execute layer never reads sensor data directly; validation is delegated
  to the Monitor channel.

canonical query shape (Ch4 Appendix 4.A.9):
    ALERTS{fault_type="<fault_type>", node_name="<target>", alertstate="firing"}
An empty result means the alert is no longer firing — t_clear is committed.
"""
from __future__ import annotations

import logging
import os
import time

import boto3
import requests

logger = logging.getLogger(__name__)

PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus:9090")


def alert_still_firing(fault_type: str, target: str) -> bool | None:
    """
    Read-only freshness check on the canonical ALERTS series.

    Returns True if (fault_type, target) is firing in Prometheus right now,
    False if it is not, and None if Prometheus could not be queried. The
    orchestrator is single-worker, so a webhook can wait behind a running
    remediation and be handled after the alert it announces has cleared.
    """
    query = (
        f'ALERTS{{fault_type="{fault_type}",'
        f'node_name="{target}",alertstate="firing"}}'
    )
    try:
        resp = requests.get(
            f"{PROMETHEUS_URL}/api/v1/query",
            params={"query": query},
            timeout=3,
        )
        resp.raise_for_status()
        return bool(resp.json().get("data", {}).get("result", []))
    except (requests.RequestException, ValueError) as exc:
        logger.warning(
            "ALERTS freshness check failed (failing open): target=%s "
            "fault_type=%s exc=%s", target, fault_type, exc,
        )
        return None


class HealthChecker:
    """
    Two-stage health verification for a single (target, fault_type) trial.

    Stage E3 (`is_healthy` / `e3_sanity_check`) is a fast, in-process HTTP
    probe of the target's /healthz endpoint — used only to give the retry
    loop quick feedback between actuator attempts.

    `wait_until_healthy` is the canonical Validate-phase check: it polls the
    Prometheus ALERTS series for the originating alert every 5 seconds until
    it clears or `timeout` elapses. This is the signal that commits t_clear
    (Thesis Eq. 3.1) and must never be substituted with a direct probe of the
    target (Ch3 §3.2.1 — the Execute pillar never reads sensor data
    directly; validation is delegated to the Monitor -> Analyse channel).
    """

    _POLL_INTERVAL_SECONDS = 5   # Thesis §3.7.2 canonical poll granularity

    def __init__(
        self,
        target: str,
        fault_type: str,
        instance: str = "",
        port: int = 80,
        path: str = "/healthz",
    ):
        self.target     = target
        self.fault_type = fault_type
        self.instance    = instance
        host = self._resolve_host(target, instance)
        self.e3_url = f"http://{host}:{port}{path}"

    @staticmethod
    def _resolve_host(target: str, instance: str) -> str:
        """Resolve a target name or instance label to a bare IP for the E3 probe."""
        return instance.split(":")[0] if instance else target

    # ── Canonical Validate-phase check (t_clear) ─────────────────────────────

    def wait_until_healthy(self, timeout: int) -> bool:
        """
        Poll the Prometheus ALERTS series for (fault_type, target) every
        5 seconds until it is no longer firing, or `timeout` seconds elapse.

        Returns True the moment the alert clears (this is the canonical
        t_clear commit point); False on timeout.
        """
        deadline = time.monotonic() + timeout
        query = (
            f'ALERTS{{fault_type="{self.fault_type}",'
            f'node_name="{self.target}",alertstate="firing"}}'
        )

        while time.monotonic() < deadline:
            time.sleep(self._POLL_INTERVAL_SECONDS)
            try:
                resp = requests.get(
                    f"{PROMETHEUS_URL}/api/v1/query",
                    params={"query": query},
                    timeout=5,
                )
                resp.raise_for_status()
                result = resp.json().get("data", {}).get("result", [])
            except requests.RequestException as exc:
                logger.warning(
                    "Prometheus ALERTS query failed (will retry): target=%s "
                    "fault_type=%s exc=%s",
                    self.target, self.fault_type, exc,
                )
                continue

            if not result:
                logger.info(
                    "Alert cleared — canonical t_clear | target=%s fault_type=%s",
                    self.target, self.fault_type,
                )
                return True

        logger.error(
            "Validation timeout after %ds | target=%s fault_type=%s",
            timeout, self.target, self.fault_type,
        )
        return False

    # ── Stage E3 — fast in-process sanity probe (retry-loop feedback only) ──

    def e3_sanity_check(self, instance_id: str | None = None) -> bool:
        """
        Stage E3 (Thesis §3.7.2) — fast internal sanity check.

        Checks:
          1. HTTP /healthz returns 200
          2. (Optional) EC2 SSM online status

        Called immediately after the actuator returns; result feeds retry
        decisions only and is NEVER the canonical t_clear commit.
        """
        if not self.is_healthy():
            return False

        if instance_id:
            try:
                ssm = boto3.client(
                    "ssm",
                    region_name=os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1"),
                )
                resp = ssm.describe_instance_information(
                    Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
                )
                info_list = resp.get("InstanceInformationList", [])
                if info_list and info_list[0].get("PingStatus") != "Online":
                    logger.warning("SSM ping status not Online for %s", instance_id)
                    return False
            except Exception as exc:  # noqa: BLE001
                logger.debug("SSM status check skipped: %s", exc)

        return True

    def is_healthy(self) -> bool:
        """Single bounded Stage E3 probe: a synchronous GET capped by a 3 s timeout."""
        try:
            return requests.get(self.e3_url, timeout=3).status_code == 200
        except requests.RequestException:
            return False
