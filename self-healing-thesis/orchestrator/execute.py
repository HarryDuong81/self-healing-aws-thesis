#!/usr/bin/env python3
"""
execute.py — Actuator dispatcher for the MAPE-K Execute phase.

Implements the policy/mechanism separation mandated by Thesis §3.5.3:

  Policy  — declared in routing.yaml and enforced by plan.py (max_retries,
             backoff schedule, validation window, circuit-breaker thresholds).
  Mechanism — this module: runs Ansible or Terraform subprocess, captures
              stdout/stderr, maps exit codes to outcome strings, and raises
              RuntimeError on failure so the caller (app.py) can apply the
              geometric backoff retry sequence.

T_remediate covers the ENTIRE retry-equipped invocation including all backoff
intervals — do not split the backoff sleep out of T_remediate.

Ansible path:
  Invokes `ansible-playbook` over SSH to the target's sshd on port 22.
  Passes --limit <target> so the play runs on exactly one host.
  stdout/stderr are captured and forwarded to the structured logger.

Terraform path:
  Runs `terraform plan -out=<file>` in the targets/ workspace, validates the
  saved plan's scope with assert_plan_scope(), then applies that same saved
  plan.  No `-target` and no `-replace` flag is used — scope is enforced
  structurally by the workspace boundary and then verified explicitly.
  A plan that proposes anything other than creating the single failed target
  raises UnsafePlanScopeError, which app.py maps to
  escalation_reason="unsafe_plan_scope"; it is never retried.
  The plan round-trip falls inside T_remediate by design — the safety check
  is part of the remediation, not a separate phase.
  max_retries = 0 for instance_failure — do not add retries.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import time
import uuid
from pathlib import Path

import boto3

from plan import transport_validation

logger = logging.getLogger(__name__)

# ── Path resolution ────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent

ANSIBLE_PLAYBOOK_DIR = Path(
    os.environ.get(
        "ANSIBLE_PLAYBOOK_DIR",
        str(_REPO_ROOT / "ansible" / "playbooks"),
    )
)
ANSIBLE_INVENTORY = os.environ.get(
    "ANSIBLE_INVENTORY",
    str(_REPO_ROOT / "ansible" / "inventory" / "aws_ec2.yml"),
)
ANSIBLE_CFG = os.environ.get(
    "ANSIBLE_CFG",
    str(_REPO_ROOT / "ansible" / "ansible.cfg"),
)
TERRAFORM_TARGETS_DIR = os.environ.get(
    "TERRAFORM_TARGETS_DIR",
    str(_REPO_ROOT / "terraform" / "targets"),
)

# Subprocess timeout limits.  Must be generous to accommodate Ansible SSM
# command execution latency (Session Manager hop adds ~3 s per task).
# Every subprocess.run() below passes start_new_session=True, placing the child
# in its own process group. Gunicorn kills a worker that exceeds --timeout by
# SIGKILLing the worker pid; a child sharing that group would survive and keep
# running unattended. The dangerous case is a terraform apply that outlives the
# worker while still holding the DynamoDB state lock, because the respawned
# worker starts with an empty in-memory lease tracker and would admit a second
# remediation against the same target. Do not remove the flag.
_ANSIBLE_TIMEOUT_SECONDS   = 600   # 10 minutes
_TERRAFORM_TIMEOUT_SECONDS = 600   # 10 minutes (EC2 creation can take ~4 min)
_TERRAFORM_PLAN_TIMEOUT_SECONDS = 180  # plan is refresh + graph only, no creates
_TERRAFORM_INIT_TIMEOUT_SECONDS = 120

# Grace between SIGTERM and SIGKILL when a terraform command times out. Terraform
# releases its DynamoDB state lock only from its own shutdown path, which SIGTERM
# triggers ("Interrupt received. Stopping operation...") and SIGKILL cannot.
# Verified on the monitoring host with a throwaway local-backend config: SIGTERM
# and SIGINT both removed the lock, SIGKILL left it behind. These add to the
# worst-case dispatch time, so tests/unit/test_worker_timeout_budget.py counts them.
_TERRAFORM_GRACE_SECONDS       = 10
_TERRAFORM_APPLY_GRACE_SECONDS = 30   # an interrupted apply must unwind in-flight API calls

# The single resource address an instance_failure remediation is permitted to
# create.  Anything else in the plan is a scope violation.
_EXPECTED_ADDR_TEMPLATE = 'module.compute.aws_instance.targets["{target}"]'

# Plan actions that represent no mutation and are therefore ignored when
# counting the plan's real scope.
_INERT_PLAN_ACTIONS = ({"no-op"}, {"read"})

_AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-1")

# Post-instance_failure diagnostic-agent restore .
_DIAGNOSTIC_AGENTS_PLAYBOOK   = "diagnostic_agents.yml"
_SSM_ONLINE_POLL_INTERVAL     = 5     # seconds
_SSM_ONLINE_TIMEOUT_SECONDS   = 180   # matches instance_failure's validation_window


# ── Failure signalling ─────────────────────────────────────────────────────────

class UnsafePlanScopeError(RuntimeError):
    """
    Raised when the Terraform plan produced for an instance_failure
    remediation proposes anything other than creating exactly the one target
    instance that failed.

    This is deliberately NOT a retryable failure.  A plan whose scope is wrong
    is wrong for a structural reason — stale code on the monitoring host, an
    edited workspace, a variable default that removes resources — and running
    it again produces the same plan.  app.py maps this to
    escalation_reason="unsafe_plan_scope" rather than "retry_exhausted".
    """

    def __init__(self, message: str, *, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


# ── Public dispatch interface ──────────────────────────────────────────────────

def dispatch(actuator: str, action: str, alert: dict) -> dict:
    """
    Dispatch the remediation action to the appropriate actuator.

    Args:
      actuator — "ansible" or "terraform" (from routing.yaml)
      action   — playbook filename (Ansible) or resource address (Terraform)
      alert    — raw Alertmanager alert dict (provides target and metadata)

    Returns:
      An actuator report -- facts the actuator itself observed while acting,
      keyed by trial-record field name.  Empty for actuators that report
      nothing.  This is the Execute layer reporting its own outcome upward,
      not reading telemetry: no sensor or alert state is consulted here.

    Raises:
      RuntimeError if the actuator subprocess exits non-zero.
      ValueError   if `actuator` is not a recognised value.
    """
    labels  = alert.get("labels", {})
    target  = (
        labels.get("node_name")
        or labels.get("target")
        or labels.get("instance", "unknown").split(":")[0]
    )
    run_id  = str(uuid.uuid4())

    logger.info(
        "Dispatch | actuator=%s action=%s target=%s run_id=%s",
        actuator, action, target, run_id,
    )

    if actuator == "ansible":
        return _run_ansible(action, target=target, alert=alert, run_id=run_id)
    elif actuator == "terraform":
        _run_terraform(action, target=target, alert=alert, run_id=run_id)
        return {}
    else:
        raise ValueError(f"Unknown actuator: {actuator!r}")


# ── Ansible actuator ──────────────────────────────────────────────────────────

def _run_ansible(
    playbook: str,
    *,
    target: str,
    alert: dict,
    run_id: str,
) -> None:
    """
    Run an Ansible playbook over SSH against the target's sshd on port 22.

    The ANSIBLE_CONFIG environment variable is set to the checked-in
    ansible.cfg so the correct connection settings are always used
    regardless of the working directory.

    Extra vars exposed to the playbook:
      run_id      — unique dispatch identifier for syslog correlation
      trial_id    — injected by the experiment harness (annotation field)
      target_host — the --limit target (available as {{ target_host }})
    """
    playbook_path = ANSIBLE_PLAYBOOK_DIR / playbook
    if not playbook_path.exists():
        raise FileNotFoundError(f"Playbook not found: {playbook_path}")

    annotation = alert.get("annotations", {})
    trial_id   = annotation.get("trial_id", run_id)

    extra_vars = (
        f"run_id={run_id} "
        f"trial_id={trial_id} "
        f"target_host={target}"
    )

    # Connection type is inventory-driven (group_vars/target_nodes/vars.yml:
    # ansible_connection=ssh) rather than hardcoded here — see that file for
    # why this moved off the community.aws.aws_ssm connection plugin after a
    # confirmed AWS Session Manager plugin defect made SSH the reliable
    # transport for automated, multi-step remediation dispatch.
    cmd = [
        "ansible-playbook",
        str(playbook_path),
        "--limit", target,
        "--inventory", ANSIBLE_INVENTORY,
        "--extra-vars", extra_vars,
    ]

    env = dict(os.environ)
    env["ANSIBLE_CONFIG"] = ANSIBLE_CFG

    logger.info(
        "Ansible | playbook=%s target=%s run_id=%s cmd=%s",
        playbook, target, run_id, " ".join(cmd),
    )

    result = subprocess.run(
        cmd,
        cwd=str(ANSIBLE_PLAYBOOK_DIR.parent),
        capture_output=True,
        text=True,
        timeout=_ANSIBLE_TIMEOUT_SECONDS,
        env=env,
        start_new_session=True,
    )

    _log_subprocess_output(result, label=f"ansible:{playbook}", run_id=run_id)

    if result.returncode != 0:
        raise RuntimeError(
            f"ansible-playbook exited {result.returncode} | "
            f"playbook={playbook} target={target} run_id={run_id}"
        )

    logger.info(
        "Ansible OK | playbook=%s target=%s run_id=%s rc=0",
        playbook, target, run_id,
    )

    return _parse_actuator_report(result.stdout or "")


_FINAL_DISK_PCT_RE = re.compile(r"SELF_HEALING_FINAL_DISK_PCT=(\d{1,3})")


def _parse_actuator_report(stdout: str) -> dict:
    """
    Extract structured facts a playbook reported about its own execution.

    Only disk_cleanup.yml emits a marker today (final filesystem utilisation).
    A missing marker is not an error -- every other playbook returns {}.
    """
    report: dict = {}
    match = _FINAL_DISK_PCT_RE.search(stdout)
    if match:
        pct = int(match.group(1))
        if 0 <= pct <= 100:
            report["final_disk_pct"] = pct
        else:
            logger.warning("Ignoring out-of-range final_disk_pct=%s", pct)
    return report


# ── Terraform actuator ────────────────────────────────────────────────────────

def _run_terraform_cmd(
    cmd: list[str],
    *,
    cwd: str,
    timeout: int,
    grace: int,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    """
    Run one terraform command, escalating SIGTERM -> SIGKILL on timeout.

    The run() timeout path SIGKILLs the child, so Terraform never runs its
    deferred lock release and the DynamoDB state lock is orphaned until someone
    force-unlocks it (confirmed live 2026-10-02: a plan that hit its 180 s limit
    left lock d26f17fc blocking the next plan). Here the whole process group
    gets SIGTERM first and SIGKILL only if it is still alive after `grace`.

    Raises subprocess.TimeoutExpired on timeout, exactly as subprocess.run did,
    so callers and the retry policy are unchanged.
    """
    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.error(
            "%s exceeded %ds -- sending SIGTERM so terraform can release its "
            "state lock", " ".join(cmd[:2]), timeout,
        )
        _signal_group(proc, signal.SIGTERM)
        try:
            proc.communicate(timeout=grace)
        except subprocess.TimeoutExpired:
            logger.error(
                "%s ignored SIGTERM for %ds -- escalating to SIGKILL (the state "
                "lock may be orphaned and need terraform force-unlock)",
                " ".join(cmd[:2]), grace,
            )
            _signal_group(proc, signal.SIGKILL)
            proc.communicate()
        raise subprocess.TimeoutExpired(cmd, timeout) from None
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    """Signal the child's process group (start_new_session makes pgid == pid)."""
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass


def _run_terraform(
    action: str,
    *,
    target: str,
    alert: dict,
    run_id: str,
) -> None:
    """
    Plan, validate the plan's scope, then apply that plan, in the targets/
    workspace.

    No -target and no -replace flag is used — the workspace boundary enforces
    scope structurally, and assert_plan_scope() then verifies it explicitly.
    The faulted instance's for_each key ("target-1", etc.) is embedded in its
    Terraform resource address.  Because the instance has genuinely been
    terminated by the time remediation runs, the plan's own refresh finds that
    key absent from real infrastructure and plans a plain create for it.

    A plan proposing anything else — a delete, a replace, a second create, an
    update, or a create at a different address — raises UnsafePlanScopeError
    before anything is applied.  app.py maps that to
    escalation_reason="unsafe_plan_scope"; it is never retried.

    The plan round-trip falls inside T_remediate by design.  The safety check
    is part of the remediation, not a separate phase, so no second timing
    boundary is introduced around it:
        T_remediate = T_plan + T_scope_check + T_apply (+ retry overhead)

    For instance_failure, max_retries = 0 (routing.yaml).  A failed apply may
    leave the state backend partially locked.  The Execute layer must NOT retry
    the Terraform path.
    """
    logger.info(
        "Terraform | targets_dir=%s target=%s action=%s run_id=%s",
        TERRAFORM_TARGETS_DIR, target, action, run_id,
    )

    # Step 1: terraform init (idempotent — ensures providers/modules present)
    # -backend-config supplies the S3 backend's bucket/key/region/lock-table —
    # without it, `terraform init -reconfigure` interactively prompts for
    # "bucket" on stdin and a non-interactive subprocess gets immediate EOF.
    # -input=false makes that failure mode explicit instead of hanging.
    # Same backend-config file the self_healing Ansible role already syncs
    # to ../bootstrap/ and uses for its own pre-warm terraform init
    # (roles/self_healing/tasks/main.yml) — confirmed live: the orchestrator's
    # own terraform init had never actually succeeded in production without
    # this flag, so no instance_failure trial had ever reached terraform apply.
    init_result = _run_terraform_cmd(
        [
            "terraform", "init",
            "-reconfigure", "-no-color", "-input=false",
            "-backend-config=../bootstrap/outputs.backend.tfvars",
        ],
        cwd=TERRAFORM_TARGETS_DIR,
        timeout=_TERRAFORM_INIT_TIMEOUT_SECONDS,
        grace=_TERRAFORM_GRACE_SECONDS,
    )
    _log_subprocess_output(init_result, label="terraform:init", run_id=run_id)
    if init_result.returncode != 0:
        raise RuntimeError(
            f"terraform init exited {init_result.returncode} | run_id={run_id}"
        )

    # Step 2: terraform plan -out=<file>, then validate its scope, then apply
    # that exact saved plan.
    #
    # The plan is saved and re-used rather than re-planned at apply time so the
    # mutation that gets validated is the mutation that gets applied.  A
    # validate-then-bare-apply sequence leaves a window in which the second
    # plan differs from the first; applying the saved file closes it, and a
    # state change in between makes apply fail loudly ("saved plan is stale")
    # instead of silently applying something unreviewed.
    #
    # Note this is a NORMAL plan, and it answers a different question from the
    # Plan layer's Gate 4 (plan.py::drift_validation), which runs
    # `plan -refresh-only -json`.  Gate 4 asks "does state still reflect
    # reality?".  This asks "what is Terraform about to change?".  Neither
    # substitutes for the other; do not merge them.
    #
    # project must match the real project name used everywhere else
    # (module default, all resource tags/names: "self-healing-thesis") —
    # confirmed live: the previous "self-healing" value forced Terraform to
    # rename/replace every project-tagged resource (CloudWatch alarms for
    # ALL THREE targets, not just the faulted one) on every single
    # instance_failure trial, since var.project feeds directly into
    # alarm_name and tags.Project.
    #
    # Variables are supplied to `plan`, not to `apply`: Terraform refuses
    # -var when applying a saved plan, because the values are baked into it.
    plan_file = f".plan-{run_id}.tfplan"
    plan_path = os.path.join(TERRAFORM_TARGETS_DIR, plan_file)

    tf_env = {
        **os.environ,
        "TF_LOG": "ERROR",   # suppress verbose provider logs in subprocess
        "TF_IN_AUTOMATION": "1",
    }

    try:
        plan_result = _run_terraform_cmd(
            [
                "terraform", "plan",
                "-out", plan_file,
                "-no-color", "-input=false",
                "-var=project=self-healing-thesis",
                "-var=environment=experiment",
            ],
            cwd=TERRAFORM_TARGETS_DIR,
            timeout=_TERRAFORM_PLAN_TIMEOUT_SECONDS,
            grace=_TERRAFORM_GRACE_SECONDS,
            env=tf_env,
        )
        _log_subprocess_output(plan_result, label="terraform:plan", run_id=run_id)
        if plan_result.returncode != 0:
            raise RuntimeError(
                f"terraform plan exited {plan_result.returncode} | run_id={run_id}"
            )

        # `terraform show -json <planfile>` is the documented, versioned plan
        # representation (it carries its own format_version).  The
        # human-readable plan text carries no compatibility guarantee across
        # Terraform minor versions and must not be parsed instead.
        show_result = _run_terraform_cmd(
            ["terraform", "show", "-json", plan_file],
            cwd=TERRAFORM_TARGETS_DIR,
            timeout=_TERRAFORM_PLAN_TIMEOUT_SECONDS,
            grace=_TERRAFORM_GRACE_SECONDS,
            env=tf_env,
        )
        if show_result.returncode != 0:
            _log_subprocess_output(show_result, label="terraform:show", run_id=run_id)
            raise RuntimeError(
                f"terraform show -json exited {show_result.returncode} | run_id={run_id}"
            )

        try:
            plan_json = json.loads(show_result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"terraform show -json produced unparseable output | "
                f"run_id={run_id} error={exc}"
            ) from exc

        # Raises UnsafePlanScopeError — deliberately not retryable.
        assert_plan_scope(plan_json, target=target, run_id=run_id)

        # Step 3: apply the exact plan that was just validated.
        # No -auto-approve and no -var: a saved plan is already approved, and
        # Terraform rejects -var alongside one.
        apply_result = _run_terraform_cmd(
            [
                "terraform", "apply",
                "-no-color",
                "-compact-warnings",
                plan_file,
            ],
            cwd=TERRAFORM_TARGETS_DIR,
            timeout=_TERRAFORM_TIMEOUT_SECONDS,
            grace=_TERRAFORM_APPLY_GRACE_SECONDS,
            env=tf_env,
        )
        _log_subprocess_output(apply_result, label="terraform:apply", run_id=run_id)

        if apply_result.returncode != 0:
            raise RuntimeError(
                f"terraform apply exited {apply_result.returncode} | "
                f"target={target} run_id={run_id}"
            )
    finally:
        # The plan file embeds the full resource graph; never leave it behind.
        try:
            os.remove(plan_path)
        except OSError:
            pass

    logger.info(
        "Terraform OK | target=%s run_id=%s rc=0",
        target, run_id,
    )

# ── Plan-scope validation (instance_failure only) ─────────────────────────────

def _for_each_key(entry: dict) -> str:
    """
    The for_each key a plan entry belongs to, or "" when it cannot be
    determined.

    Mirrors plan.py::drift_validation()'s attribution deliberately: prefer the
    structured field, fall back to the ["..."] index expression at the end of
    the address.  Returning "" on an indeterminate entry makes the caller fail
    closed, since "" never equals a real target name.
    """
    idx = entry.get("index")
    if isinstance(idx, str):
        return idx
    m = re.search(r'\["([^"]+)"\]\s*$', entry.get("address", "") or "")
    return m.group(1) if m else ""


def assert_plan_scope(plan_json: dict, *, target: str, run_id: str = "") -> None:
    """
    Assert that a Terraform plan mutates the one failed target and nothing
    outside it.  Raises UnsafePlanScopeError otherwise.

    The invariant, stated positively: after discarding inert entries, the plan
    must contain exactly one create, at
    module.compute.aws_instance.targets["<target>"]; no deletes and no
    replacements, unconditionally; and any further change must be an update
    whose for_each key is that same target.

    Why updates keyed to the target are admitted rather than rejected.  The
    instance is not the only resource carrying its for_each key.  Both
    aws_cloudwatch_metric_alarm.cpu_high and .status_check_failed in
    modules/compute declare `dimensions = { InstanceId =
    aws_instance.targets[each.key].id }`, so recreating an instance
    necessarily makes its two alarms' dimensions "known after apply" and
    Terraform plans an update for each.  A genuine instance_failure
    remediation therefore always produces three active changes, not one.  An
    earlier revision of this function demanded exactly one and would have
    aborted every real recreation with unsafe_plan_scope before apply — the
    unit suite could not catch it, because the same wrong assumption was
    encoded in the tests.

    Attribution by for_each key, not by absolute address, is the rule
    plan.py::drift_validation() already applies at Gate 4, for exactly this
    reason (Thesis §3.5.4: "expected state shifts associated with instance
    termination (e.g., attached monitoring alarms)").  Both gates now share one
    attribution rule while continuing to answer different questions — Gate 4
    asks whether recorded state still matches reality; this asks what a normal
    apply would change.

    Why "create" and not "replace".  The orchestrator issues no -replace and
    no -target.  By the time remediation runs, the faulted instance has
    genuinely been terminated, so the plan's own refresh finds that for_each
    key absent from real infrastructure and plans a plain create for it.  A
    plan proposing ["delete", "create"] or ["create", "delete"] means the
    instance still exists and Terraform has decided to replace a live resource
    — a different, more destructive operation than the one this remediation is
    authorised to perform.

    Inert entries are dropped before counting: "no-op" changes and "read"
    (data-source) entries mutate nothing.

    An empty plan is rejected.  It means the for_each key is present and
    Terraform has nothing to do, so the alert that triggered this remediation
    cannot be resolved by a Terraform apply; applying nothing and then
    reporting success would record a trial whose T_remediate measured no
    remediation at all.

    This function is intentionally pure — it takes parsed plan JSON, not a
    workspace path — so the test matrix can exercise every branch without an
    AWS account or a terraform binary.
    """
    # `target` here is whatever the caller's own alert-derived identifier
    # looks like -- app.py passes labels["node_name"], which carries the
    # FULL Name-tag string (e.g. "self-healing-thesis-experiment-target-3"),
    # not Terraform's own for_each key ("target-3"). CONFIRMED LIVE: this
    # mismatch made `address != expected` fail on every single genuine
    # instance_failure recreation dispatched via the real Alertmanager
    # webhook path -- the plan was a correct, admissible three-change
    # recreation every time, but was rejected as "unsafe_plan_scope"
    # because the expected address was built from the full name while
    # Terraform's actual address always uses the short for_each key. Same
    # root cause as the identical fix in plan.py::drift_validation().
    # Extract the short "target-N" suffix before building the expected
    # address or comparing against `_for_each_key()`'s output.
    m_target = re.search(r'(target-\d+)$', target)
    target_key = m_target.group(1) if m_target else target

    expected = _EXPECTED_ADDR_TEMPLATE.format(target=target_key)

    changes = []
    for entry in plan_json.get("resource_changes", []) or []:
        actions = set(entry.get("change", {}).get("actions", []) or [])
        if actions in _INERT_PLAN_ACTIONS:
            continue
        changes.append(entry)

    def _fail(message: str) -> None:
        detail = "; ".join(
            f"{c.get('address')}={c.get('change', {}).get('actions')}"
            for c in changes
        ) or "<no mutating changes>"
        logger.error(
            "Plan scope violation | target=%s run_id=%s expected=%s plan=[%s]",
            target, run_id, expected, detail,
        )
        raise UnsafePlanScopeError(
            f"{message} | expected one create of {expected}, plus updates "
            f"keyed to {target!r} only", detail=detail
        )

    if not changes:
        _fail("Terraform plan proposes no changes")

    creates, updates, forbidden = [], [], []
    for entry in changes:
        actions = entry.get("change", {}).get("actions", []) or []
        if actions == ["create"]:
            creates.append(entry)
        elif actions == ["update"]:
            updates.append(entry)
        else:
            forbidden.append(entry)

    # Deletes and replacements are inadmissible whatever they are keyed to.
    if forbidden:
        first = forbidden[0]
        _fail(
            f"Terraform plan proposes "
            f"{first.get('change', {}).get('actions', [])!r} on "
            f"{first.get('address', '')!r}"
        )

    if len(creates) != 1:
        _fail(f"Terraform plan proposes {len(creates)} creates, not exactly one")

    address = creates[0].get("address", "")
    if address != expected:
        _fail(f"Terraform plan would create {address!r}")

    for entry in updates:
        key = _for_each_key(entry)
        if key != target_key:
            _fail(
                f"Terraform plan updates {entry.get('address', '')!r}, which is "
                f"keyed to {key or '<none>'!r}, not {target_key!r}"
            )

    logger.info(
        "Plan scope OK | one create of %s, %d update(s) keyed to it | "
        "target=%s run_id=%s",
        expected, len(updates), target, run_id,
    )




# ── Post-instance_failure diagnostic-agent restore ───────────────────────────
#
# Act-only (— "Do not read sensor data from the Execute
# layer"): this function performs the restore and returns the resolved
# instance ID. Verifying that Filebeat/OTel are actually forwarding data is
# a Knowledge-layer responsibility (observability_checker.py.ObservabilityChecker),
# called separately by app.py — not from here.

def restore_diagnostic_agents(target: str, alert: dict, run_id: str) -> str:
    """
    Re-install Filebeat + OTel Agent on the instance that Terraform just
 recreated for `target` .

    Args:
      target  — full node_name (e.g. "self-healing-thesis-experiment-target-1"),
                same identifier _run_ansible() already uses as --limit.
      alert   — raw Alertmanager alert dict (passed through to _run_ansible
                for trial_id correlation).
      run_id  — unique dispatch identifier for syslog correlation.

    Returns:
      The resolved EC2 instance ID of the recreated instance.

    Raises:
      RuntimeError if no running instance is found, SSM never reports
      PingStatus=Online within the timeout, or the Ansible playbook fails.
    """
    instance_id = _resolve_latest_instance_id(target)

    _refresh_prometheus_scrape_label(target, instance_id)

    logger.info(
        "Diagnostic restore | waiting for SSM online | target=%s instance_id=%s",
        target, instance_id,
    )
    deadline = time.monotonic() + _SSM_ONLINE_TIMEOUT_SECONDS
    online = False
    while time.monotonic() < deadline:
        if transport_validation(target, instance_id=instance_id):
            online = True
            break
        time.sleep(_SSM_ONLINE_POLL_INTERVAL)

    if not online:
        raise RuntimeError(
            f"SSM Agent never reported Online within "
            f"{_SSM_ONLINE_TIMEOUT_SECONDS}s | target={target} "
            f"instance_id={instance_id} run_id={run_id}"
        )

    logger.info(
        "Diagnostic restore | SSM online, running %s | target=%s run_id=%s",
        _DIAGNOSTIC_AGENTS_PLAYBOOK, target, run_id,
    )
    _run_ansible(_DIAGNOSTIC_AGENTS_PLAYBOOK, target=target, alert=alert, run_id=run_id)

    return instance_id


_PROMETHEUS_YML_PATH = "/opt/monitoring/prometheus/prometheus.yml"
_MONITORING_COMPOSE_DIR = "/opt/monitoring"


def _refresh_prometheus_scrape_label(target: str, new_instance_id: str) -> None:
    """
    Best-effort refresh of this target's static ec2_instance_id scrape label
    in Prometheus's own config, immediately after instance_failure recreates
    the underlying instance.

    CONFIRMED LIVE: Prometheus's node_exporter scrape config
    (ansible/roles/monitoring_stack/templates/prometheus.yml.j2) is
    static -- rendered once at deploy time, with no mechanism to notice a
    target's instance ID changed afterward. Left unpatched, a recreated
    target's ec2_instance_id label stays pointed at the terminated instance
    forever, which breaks Gate 3 (SSM transport_validation, plan.py) for
    every subsequent Ansible-actuated fault class dispatched against that
    target -- confirmed live to silently corrupt three separate S1 campaign
    runs in one night: real faults were genuinely injected and then sat
    completely undetected/unremediated because every dispatch attempt
    aborted at Gate 3 before ever reaching the playbook, with no error
    pointing at the real cause.

    Deliberately best-effort and non-raising: a failure here must never
    fail the instance_failure trial that's already in progress, since this
    trial's own T_MTTR is already correctly measured by the point this
    runs -- a refresh failure only risks corrupting a *future* trial on
    this target, not this one.
    """
    try:
        with open(_PROMETHEUS_YML_PATH, "r") as f:
            content = f.read()
    except OSError as exc:
        logger.warning(
            "Prometheus label refresh skipped: could not read %s (%s)",
            _PROMETHEUS_YML_PATH, exc,
        )
        return

    # ec2_instance_id and node_name are adjacent lines within the same
    # static_configs entry (see the template) -- match the ec2_instance_id
    # value immediately preceding this target's own node_name line, and
    # replace only that one occurrence. \s+ already spans the real newline
    # between the two lines. Confirmed live against the actual deployed
    # file for all three targets before this code was written.
    pattern = re.compile(
        r'(ec2_instance_id:\s*")[^"]*("\s+node_name:\s*"' + re.escape(target) + r'")'
    )
    new_content, count = pattern.subn(
        lambda m: m.group(1) + new_instance_id + m.group(2), content,
    )
    if count != 1:
        logger.warning(
            "Prometheus label refresh skipped: expected exactly 1 match for "
            "target=%s in %s, found %d",
            target, _PROMETHEUS_YML_PATH, count,
        )
        return

    try:
        with open(_PROMETHEUS_YML_PATH, "w") as f:
            f.write(new_content)
    except OSError as exc:
        logger.warning(
            "Prometheus label refresh: failed to write %s (%s)",
            _PROMETHEUS_YML_PATH, exc,
        )
        return

    # `/-/reload` is confirmed live to be unreliable (returns 200 but has
    # been observed to silently no-op); a full container restart is used
    # instead for certainty. `ubuntu` (this service's own User=, per
    # orchestrator.service) is a member of the local `docker` group, so
    # this requires no sudo.
    try:
        result = subprocess.run(
            ["docker", "compose", "restart", "prometheus"],
            cwd=_MONITORING_COMPOSE_DIR,
            capture_output=True, text=True, timeout=30,
            start_new_session=True,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        # Non-raising by contract (see docstring): a missing docker binary or a
        # hung restart must not fail the instance_failure trial in progress.
        logger.warning(
            "Prometheus label refresh: container restart failed for "
            "target=%s: %s",
            target, exc,
        )
        return
    if result.returncode != 0:
        logger.warning(
            "Prometheus label refresh: container restart failed for "
            "target=%s: %s",
            target, result.stderr[:300],
        )
        return

    logger.info(
        "Prometheus label refreshed | target=%s new_instance_id=%s",
        target, new_instance_id,
    )


def _resolve_latest_instance_id(target: str) -> str:
    """
    Resolve `target` (full node_name) to its current EC2 instance ID.

    Filters on the `Name` tag (which already holds the full node_name —
    terraform/modules/compute/main.tf: Name = "${name_prefix}-${each.key}"),
    then picks the instance with the most recent LaunchTime among any
    matches. The LaunchTime tiebreak guards against a race where the OLD
    (still-terminating) instance briefly still satisfies the same
    running-state filter alongside the newly-launched replacement.
    """
    ec2 = boto3.client("ec2", region_name=_AWS_REGION)
    resp = ec2.describe_instances(
        Filters=[
            {"Name": "tag:Name",            "Values": [target]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ]
    )
    instances = [
        inst
        for reservation in resp.get("Reservations", [])
        for inst in reservation["Instances"]
    ]
    if not instances:
        raise RuntimeError(f"No running instance found with tag Name={target!r}")

    latest = max(instances, key=lambda inst: inst["LaunchTime"])
    logger.info(
        "Resolved %s -> %s (LaunchTime=%s, %d candidate(s))",
        target, latest["InstanceId"], latest["LaunchTime"], len(instances),
    )
    return latest["InstanceId"]


# ── Utilities ─────────────────────────────────────────────────────────────────

def _log_subprocess_output(
    result: subprocess.CompletedProcess,
    *,
    label: str,
    run_id: str,
) -> None:
    """Forward captured stdout/stderr to the structured logger."""
    if result.stdout:
        for line in result.stdout.splitlines():
            if line.strip():
                logger.info("[%s] [%s] %s", label, run_id, line)
    if result.stderr:
        log_fn = logger.error if result.returncode != 0 else logger.debug
        for line in result.stderr.splitlines():
            if line.strip():
                log_fn("[%s] [%s] STDERR: %s", label, run_id, line)
