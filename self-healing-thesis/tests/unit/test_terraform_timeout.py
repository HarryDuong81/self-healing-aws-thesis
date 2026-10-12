"""
tests/unit/test_terraform_timeout.py — execute._run_terraform_cmd.

A terraform command that times out must be sent SIGTERM first so it can release
its DynamoDB state lock, and SIGKILLed only if it ignores that. subprocess.run
(timeout=...) SIGKILLs immediately, which orphaned the lock on every timeout.
These tests use real child processes (sh), not terraform.
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

_ORCHESTRATOR = Path(__file__).resolve().parent.parent.parent / "orchestrator"
sys.path.insert(0, str(_ORCHESTRATOR))

import execute


def _run(script: str, tmp_path, *, timeout=1, grace=2):
    return execute._run_terraform_cmd(
        ["sh", "-c", script], cwd=str(tmp_path), timeout=timeout, grace=grace,
    )


def test_normal_completion_returns_completed_process(tmp_path):
    result = _run("echo out; echo err >&2; exit 3", tmp_path, timeout=10)
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.returncode == 3
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"


def test_timeout_sends_sigterm_so_the_child_can_clean_up(tmp_path):
    marker = tmp_path / "released"
    script = f'trap "echo done > {marker}; exit 0" TERM; sleep 30 & wait'
    with pytest.raises(subprocess.TimeoutExpired):
        _run(script, tmp_path)
    assert marker.read_text().strip() == "done", "child never ran its SIGTERM handler"


def test_timeout_escalates_to_sigkill_when_sigterm_is_ignored(tmp_path):
    script = 'trap "" TERM; sleep 30 & wait'
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _run(script, tmp_path, timeout=1, grace=1)
    assert time.monotonic() - start < 10, "SIGKILL escalation did not happen"


def test_timeout_raises_timeoutexpired_with_the_original_command(tmp_path):
    with pytest.raises(subprocess.TimeoutExpired) as exc:
        _run("sleep 30", tmp_path)
    assert exc.value.timeout == 1
    assert exc.value.cmd[0] == "sh"


def test_terraform_calls_all_go_through_the_helper():
    src = (_ORCHESTRATOR / "execute.py").read_text(encoding="utf-8")
    body = src.split("def _run_terraform(", 1)[1].split("def _for_each_key", 1)[0]
    assert "subprocess.run(" not in body, (
        "a terraform command in _run_terraform bypasses _run_terraform_cmd and "
        "would SIGKILL terraform on timeout, orphaning the state lock"
    )
    assert body.count("_run_terraform_cmd(") == 4  # init, plan, show, apply
