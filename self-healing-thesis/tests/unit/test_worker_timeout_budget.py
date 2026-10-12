"""
tests/unit/test_worker_timeout_budget.py — Gunicorn's worker timeout must exceed
what a single MAPE-K dispatch can actually take.

handle_alert() runs _run_mape_k() synchronously inside the Flask request handler
under a single sync worker, so gunicorn's --timeout is a hard ceiling on total
per-alert duration. It has to be compared against the worst-case SUM of every
bounded wait one dispatch can make, not against any individual subprocess
timeout and not against the harness's own poll.

That comparison had never been made. --timeout was 960s while the per-class sums
run from 1231s (configuration_drift) to 2280s (instance_failure), so every fault
class could breach it without any individual subprocess timing out. A breach is
worse than a lost trial: gunicorn SIGKILLs the worker, so mttr.save() never runs
and no terminal record is written; the child ansible/terraform process survives
unless it was started in its own session; the respawned worker starts with an
empty circuit breaker and lease tracker, both of which live only in process
memory; and Alertmanager re-delivers the still-firing alert within its 5m
repeat_interval into that fresh worker, which no longer knows a remediation is
in flight.

These tests derive both sides from the files that actually configure them, so a
change to a subprocess timeout, a retry policy, or a validation window fails here
rather than silently eroding the margin.

Thesis reference: §3.5.3, §4.4.1, §4.4.3
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent.parent
_ORCHESTRATOR = _ROOT / "orchestrator"
sys.path.insert(0, str(_ORCHESTRATOR))

import execute  # noqa: E402

_DOCKERFILE = _ORCHESTRATOR / "Dockerfile"
_ENV_TMPL = _ROOT / "ansible" / "roles" / "self_healing" / "templates" / "orchestrator.env.j2"
_SERVICE_TMPL = _ROOT / "ansible" / "roles" / "self_healing" / "templates" / "orchestrator.service.j2"
_ROUTING = _ORCHESTRATOR / "routing.yaml"
_APP_PY = _ORCHESTRATOR / "app.py"

_PLAN_PY = _ORCHESTRATOR / "plan.py"


def _drift_gate_seconds() -> int:
    """
    Worst case of plan.py::drift_validation's terraform subprocess: its timeout
    plus the SIGTERM->SIGKILL grace it allows before giving up (the second
    communicate() call). Read from source text, in that order, rather than
    hardcoded here -- a copy of the timeout alone omitted the 10 s grace and
    understated the dispatch ceiling by 10 s. Fails loudly if the structure
    changes, so the budget cannot silently stop covering it.
    """
    calls = re.findall(r"\.communicate\(timeout=(\d+)\)", _PLAN_PY.read_text(encoding="utf-8"))
    assert len(calls) == 2, (
        f"plan.py has {len(calls)} timed communicate() calls; expected the timeout "
        f"and the SIGTERM grace. Update _drift_gate_seconds() with the new structure."
    )
    return int(calls[0]) + int(calls[1])


def _obs_forwarding_seconds() -> int:
    """
    app.py's _OBS_FORWARDING_TIMEOUT_SECONDS, read from source text rather
    than imported -- app.py has module-level side effects at import time
    (Flask app construction, OTel tracing init) that are unsafe to trigger
    from a unit test. Matches the same source-derivation pattern already
    used above for the Dockerfile and env template values.
    """
    m = re.search(
        r"^_OBS_FORWARDING_TIMEOUT_SECONDS = (\d+)",
        _APP_PY.read_text(encoding="utf-8"), re.M,
    )
    assert m, "app.py no longer defines _OBS_FORWARDING_TIMEOUT_SECONDS"
    return int(m.group(1))


def _docker_timeout() -> int:
    m = re.search(r'"--timeout",\s*"(\d+)"', _DOCKERFILE.read_text(encoding="utf-8"))
    assert m, "Dockerfile no longer passes --timeout to gunicorn"
    return int(m.group(1))


def _env_timeout() -> int:
    m = re.search(r"^GUNICORN_TIMEOUT=(\d+)", _ENV_TMPL.read_text(encoding="utf-8"), re.M)
    assert m, "orchestrator.env.j2 no longer defines GUNICORN_TIMEOUT"
    return int(m.group(1))


def _routes() -> dict:
    return yaml.safe_load(_ROUTING.read_text(encoding="utf-8"))["fault_classes"]


def _ceiling(fault: str, route: dict) -> int:
    """Worst-case seconds one dispatch of this fault class can occupy the worker."""
    validation = route["validation_window"]
    if fault == "instance_failure":
        # Each terraform command can also spend its SIGTERM->SIGKILL grace after
        # timing out, which is what lets it release the DynamoDB state lock.
        return (
            _drift_gate_seconds()
            + execute._TERRAFORM_INIT_TIMEOUT_SECONDS + execute._TERRAFORM_GRACE_SECONDS
            + execute._TERRAFORM_PLAN_TIMEOUT_SECONDS + execute._TERRAFORM_GRACE_SECONDS   # plan -out
            + execute._TERRAFORM_PLAN_TIMEOUT_SECONDS + execute._TERRAFORM_GRACE_SECONDS   # show -json
            + execute._TERRAFORM_TIMEOUT_SECONDS + execute._TERRAFORM_APPLY_GRACE_SECONDS  # apply
            + execute._SSM_ONLINE_TIMEOUT_SECONDS          # wait for the new instance
            + execute._ANSIBLE_TIMEOUT_SECONDS             # diagnostic_agents.yml
            + _obs_forwarding_seconds()
            + validation
        )
    attempts = route["max_retries"] + 1
    return (
        attempts * execute._ANSIBLE_TIMEOUT_SECONDS
        + sum(route.get("backoff_seconds") or [])
        + validation
    )


class TestDispatchCeilingCountsGrace:
    def test_drift_gate_includes_its_sigterm_grace(self):
        """The drift check's budget is its timeout plus the grace, not the timeout alone."""
        src = _PLAN_PY.read_text(encoding="utf-8")
        timeout = int(re.search(r"proc\.communicate\(timeout=(\d+)\)", src).group(1))
        assert _drift_gate_seconds() > timeout

    def test_instance_failure_ceiling_is_the_documented_figure(self):
        """
        Pin the figure the thesis (Ch.4 section 4.4) and CLAUDE.md quote, so a change to any
        bounded wait is a deliberate edit to those documents too.
        """
        assert _ceiling("instance_failure", _routes()["instance_failure"]) == 2530


class TestTimeoutSourcesAgree:
    def test_container_and_host_timeouts_match(self):
        """Two deployment paths, one value. Drift between them is invisible at runtime."""
        assert _docker_timeout() == _env_timeout()

    def test_service_unit_reads_the_env_value(self):
        """
        The systemd unit must not hardcode a third copy. It previously did, which
        left GUNICORN_TIMEOUT in the env file dead and free to disagree.
        """
        unit = _SERVICE_TMPL.read_text(encoding="utf-8")
        assert "--timeout ${GUNICORN_TIMEOUT}" in unit
        assert not re.search(r"--timeout\s+\d+", unit)


class TestWorkerTimeoutExceedsDispatchCeiling:
    @pytest.mark.parametrize("fault", sorted(_routes()))
    def test_each_class_fits(self, fault):
        route = _routes()[fault]
        ceiling = _ceiling(fault, route)
        timeout = _env_timeout()
        assert timeout > ceiling, (
            f"gunicorn --timeout {timeout}s does not cover {fault}'s worst-case "
            f"dispatch of {ceiling}s. A dispatch that reaches this ceiling is "
            f"SIGKILLed mid-request: no MTTR record, an orphaned actuator process, "
            f"and a respawned worker with empty lease/circuit-breaker state."
        )

    def test_margin_is_not_marginal(self):
        """At least 5% headroom over the worst class, so ordinary variance cannot breach it."""
        worst = max(_ceiling(f, r) for f, r in _routes().items())
        assert _env_timeout() >= worst * 1.05


class TestActuatorChildrenAreIsolated:
    def test_every_subprocess_run_starts_a_new_session(self):
        """
        start_new_session=True puts each actuator child in its own process group.
        Without it a worker-timeout SIGKILL orphans a running terraform apply that
        may still hold the DynamoDB state lock.

        Matches subprocess.run( and subprocess.Popen( -- plan.py's own
        drift_validation() moved from subprocess.run(timeout=...) to
        Popen + communicate(timeout=...) on 2026-10-01 (confirmed live: the
        old pattern SIGKILLed terraform on every timeout, which orphaned its
        own DynamoDB lock every single time, since SIGKILL cannot be
        intercepted and Terraform's deferred lock-release code never runs).
        Both call forms need the same process-group isolation this test
        guards, so both are in scope here.
        """
        # Match code, not prose. Counting raw substrings would also pick up the
        # explanatory comment in execute.py that names both strings, which
        # happens to keep the two totals equal and would mask a real omission.
        call_re = re.compile(r"^[ \t]*(?:\w+[ \t]*=[ \t]*)?subprocess\.(?:run|Popen)\(", re.M)
        sess_re = re.compile(r"^[ \t]*start_new_session=True,[ \t]*$", re.M)
        for module in ("execute.py", "plan.py"):
            src = (_ORCHESTRATOR / module).read_text(encoding="utf-8")
            calls = len(call_re.findall(src))
            sessions = len(sess_re.findall(src))
            assert calls > 0, f"{module}: expected at least one subprocess.run/Popen call"
            assert sessions == calls, (
                f"{module}: {calls} subprocess call site(s) but {sessions} "
                f"start_new_session=True — every actuator child must be isolated."
            )
