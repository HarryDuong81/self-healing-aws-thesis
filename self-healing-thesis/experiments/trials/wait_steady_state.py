#!/usr/bin/env python3
"""
experiments/trials/wait_steady_state.py — Block until all six fault-class
alert rules are in state INACTIVE (no FIRING alerts).

Used as the pre-trial gate in run_campaign.py and as a standalone check
after infrastructure deployment.

Exit codes:
    0 — steady state achieved within timeout
    1 — timeout expired with active alerts still present

Usage:
    python experiments/trials/wait_steady_state.py --window 60
    python experiments/trials/wait_steady_state.py --window 60 --timeout 300 \\
        --prometheus-url http://localhost:9090

Thesis reference: §4.2 Cold deploy step 4, §3.1.3 steady-state hypothesis
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from typing import Optional

import requests

logging.basicConfig(
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    level=logging.INFO,
    stream=sys.stdout,
)
logger = logging.getLogger("wait_steady_state")

# Six canonical fault classes — Thesis §6, Table 3.3
_FAULT_ALERT_NAMES = {
    "CPUOverload",
    "MemoryExhaustion",
    "DiskSaturation",
    "ServiceCrash",
    "ConfigurationDrift",
    "InstanceFailure",
}


def _query_active_alerts(prometheus_url: str) -> Optional[list[dict]]:
    """Return the list of currently FIRING alerts from Prometheus, or None
    if Prometheus could not be reached. None is NOT the same as "no alerts
    firing" — conflating the two let this script report "steady state
    confirmed" after a window where Prometheus was never once successfully
    queried (confirmed live: Prometheus not yet deployed produced a clean
    "confirmed" result instead of a loud failure)."""
    url = f"{prometheus_url.rstrip('/')}/api/v1/alerts"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return [
            a for a in data.get("data", {}).get("alerts", [])
            if a.get("state") == "firing"
        ]
    except requests.RequestException as exc:
        logger.warning("Prometheus unreachable: %s", exc)
        return None


def _active_fault_alerts(prometheus_url: str) -> Optional[list[dict]]:
    """Return only the fault-class alerts that are currently FIRING, or
    None if Prometheus was unreachable (see _query_active_alerts)."""
    all_alerts = _query_active_alerts(prometheus_url)
    if all_alerts is None:
        return None
    return [
        a for a in all_alerts
        if a.get("labels", {}).get("alertname") in _FAULT_ALERT_NAMES
    ]


def wait_steady_state(
    window: int,
    timeout: int = 600,
    poll_interval: int = 5,
    prometheus_url: str = "http://localhost:9090",
) -> bool:
    """
    Block until all fault-class alert rules are inactive for `window` seconds.

    Returns True if steady state was achieved, False if timeout expired.

    The `window` parameter implements the Rosenthal & Jones (2020) steady-state
    hypothesis verification: the system must show no FIRING fault alerts for
    the entire window duration before a trial is allowed to start.
    """
    logger.info(
        "Waiting for steady state: 0 firing fault alerts for %ds (timeout=%ds)",
        window, timeout,
    )
    deadline = time.monotonic() + timeout
    clean_since: Optional[float] = None

    while time.monotonic() < deadline:
        firing = _active_fault_alerts(prometheus_url)

        if firing is None:
            # Unreachable is not the same as clean — do not let it count
            # toward the clean window (see _query_active_alerts).
            logger.info("No signal from Prometheus — resetting clean window")
            clean_since = None
        elif firing:
            alert_names = [a["labels"].get("alertname") for a in firing]
            logger.info("Still firing: %s — resetting clean window", alert_names)
            clean_since = None
        else:
            now = time.monotonic()
            if clean_since is None:
                clean_since = now
                logger.info("No fault alerts firing — clean window started")
            elapsed = now - clean_since
            if elapsed >= window:
                logger.info(
                    "Steady state confirmed: 0 firing alerts for %.1fs (required: %ds)",
                    elapsed, window,
                )
                return True
            logger.debug("Clean for %.1fs / %ds required", elapsed, window)

        time.sleep(poll_interval)

    final_alerts = _active_fault_alerts(prometheus_url)
    logger.error(
        "Timed out after %ds waiting for steady state. Last firing alerts: %s",
        timeout,
        "Prometheus unreachable" if final_alerts is None
        else [a["labels"].get("alertname") for a in final_alerts],
    )
    return False


# Labels every Ansible-actuated fault rule must carry through to the alert
# payload.  ec2_instance_id is consumed by the Plan layer's SSM readiness gate
# (plan.transport_validation); node_name resolves the Ansible target.  A rule
# whose aggregation clause drops either one disables a safety gate silently at
# runtime, so this is checked against the *loaded* rules in Prometheus rather
# than against the file on disk -- committing the file is not the same as the
# container having reloaded it.
_REQUIRED_RULE_LABELS = ("ec2_instance_id", "node_name")
_ANSIBLE_ACTUATED_ALERTS = (
    "CPUOverload", "MemoryExhaustion", "DiskSaturation",
    "ServiceCrash", "ConfigurationDrift",
)


def verify_loaded_rule_labels(prometheus_url: str) -> bool:
    """
    Confirm the rules Prometheus has actually loaded preserve the labels the
    orchestrator's safety gates depend on.

    An aggregating rule (`sum by (...)`, `avg by (...)`) only emits the labels
    named in its `by` clause.  A rule that aggregates without listing these
    labels produces alerts the Plan layer cannot evaluate, and gate 3 then
    fails closed on every trial of that fault class.
    """
    url = f"{prometheus_url.rstrip('/')}/api/v1/rules"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        groups = resp.json().get("data", {}).get("groups", [])
    except (requests.RequestException, ValueError) as exc:
        logger.error("Rule-label preflight: cannot read %s: %s", url, exc)
        return False

    loaded = {
        r.get("name"): r.get("query", "")
        for g in groups for r in g.get("rules", [])
        if r.get("type") == "alerting"
    }

    ok = True
    for alert in _ANSIBLE_ACTUATED_ALERTS:
        query = loaded.get(alert)
        if query is None:
            logger.error("Rule-label preflight: alert %s is not loaded in Prometheus", alert)
            ok = False
            continue
        # Only aggregating rules can drop labels; a bare selector keeps them all.
        if " by (" not in query and " by(" not in query:
            continue
        missing = [lbl for lbl in _REQUIRED_RULE_LABELS if lbl not in query]
        if missing:
            logger.error(
                "Rule-label preflight: %s aggregates without preserving %s — "
                "the Plan layer's SSM readiness gate will fail closed on every "
                "%s trial. Add the label(s) to the rule's by(...) clause and "
                "reload Prometheus. Loaded query: %s",
                alert, ", ".join(missing), alert, query,
            )
            ok = False

    if ok:
        logger.info("Rule-label preflight: all Ansible-actuated rules preserve %s",
                 ", ".join(_REQUIRED_RULE_LABELS))
    return ok


def verify_orchestrator_worker_model() -> bool:
    """
    Confirm the *running* orchestrator matches the serialised execution model
    the experimental design assumes.

    Gunicorn promotes a sync worker to the threaded gthread class whenever
    --threads is greater than 1, so a unit file carrying `--threads 4` yields a
    multi-threaded orchestrator and concurrent _run_mape_k() executions.  Per-
    trial timing isolation -- the basis of the phase decomposition -- is not
    guaranteed in that configuration.

    Editing the template is not enough: the running process keeps its original
    command line until the service is restarted.  This reads the live command
    line from /proc, so it fails whenever the host has not been redeployed.
    """
    import glob

    masters: list[tuple[int, list[str]]] = []
    for entry in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(entry, "rb") as fh:
                argv = fh.read().decode("utf-8", "replace").split("\x00")
        except OSError:
            continue
        argv = [a for a in argv if a]
        if not argv or not any("gunicorn" in a for a in argv):
            continue
        if not any(a.startswith("app:") for a in argv):
            continue
        try:
            pid = int(entry.split("/")[2])
        except (IndexError, ValueError):
            continue
        masters.append((pid, argv))

    if not masters:
        logger.error(
            "Worker-model preflight: no running gunicorn orchestrator process "
            "found. Deploy and start it before running a campaign: "
            "ansible-playbook ansible/playbooks/deploy.yml "
            "-i ansible/inventory/aws_ec2.yml --tags orchestrator"
        )
        return False

    ok = True
    for pid, argv in masters:
        cmd = " ".join(argv)
        if any(a == "--threads" or a.startswith("--threads=") for a in argv):
            logger.error(
                "Worker-model preflight: pid %d still runs with --threads, so "
                "gunicorn is using the threaded gthread worker and alert "
                "processing is NOT serialised. Redeploy the orchestrator role "
                "and restart the service. Live command line: %s", pid, cmd,
            )
            ok = False
        # The claim being protected is "one worker, synchronous"; the worker
        # count is half of that and was previously unchecked.
        if "--workers" in argv:
            try:
                workers = int(argv[argv.index("--workers") + 1])
            except (IndexError, ValueError):
                workers = -1
            if workers != 1:
                logger.error(
                    "Worker-model preflight: pid %d runs with --workers %s, not 1. "
                    "Concurrent MAPE-K executions break per-trial timing isolation. "
                    "Live command line: %s", pid, workers, cmd,
                )
                ok = False
        else:
            logger.error(
                "Worker-model preflight: pid %d does not pin --workers; gunicorn's "
                "default is 1 but the experimental design should state it. "
                "Live command line: %s", pid, cmd,
            )
            ok = False

        if "--worker-class" not in argv:
            logger.error(
                "Worker-model preflight: pid %d does not pin --worker-class "
                "sync; the running configuration predates the fix. Live "
                "command line: %s", pid, cmd,
            )
            ok = False

    if ok:
        logger.info(
            "Worker-model preflight: orchestrator running single-worker, "
            "worker-class sync, no --threads (%d process(es) checked)",
            len(masters),
        )
    return ok


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Wait for all six fault-class Prometheus alerts to be inactive."
    )
    parser.add_argument(
        "--window", type=int, default=60,
        help="Required clean (no-alert) duration in seconds (default: 60)",
    )
    parser.add_argument(
        "--timeout", type=int, default=600,
        help="Maximum wait time in seconds before giving up (default: 600)",
    )
    parser.add_argument(
        "--poll-interval", type=int, default=5,
        help="Prometheus poll interval in seconds (default: 5)",
    )
    parser.add_argument(
        "--prometheus-url", default="http://localhost:9090",
        help="Prometheus base URL (default: http://localhost:9090)",
    )
    parser.add_argument(
        "--skip-label-check", action="store_true",
        help="Skip the loaded-rule label preflight (not recommended)",
    )
    args = parser.parse_args(argv)

    # Preflight: the loaded rules must preserve the labels the safety gates
    # need, before spending the steady-state window waiting.
    if not args.skip_label_check:
        preflight_ok = verify_loaded_rule_labels(args.prometheus_url)
        # The worker-model check reads /proc, so it is meaningful only when this
        # script runs on the monitoring host — which the co-location
        # requirement already mandates for the harness.
        preflight_ok = verify_orchestrator_worker_model() and preflight_ok
        if not preflight_ok:
            logger.error("Aborting: preflight failed — do not start a campaign.")
            return 2

    ok = wait_steady_state(
        window=args.window,
        timeout=args.timeout,
        poll_interval=args.poll_interval,
        prometheus_url=args.prometheus_url,
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
