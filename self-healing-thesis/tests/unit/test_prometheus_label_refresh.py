"""
tests/unit/test_prometheus_label_refresh.py — execute._refresh_prometheus_scrape_label.

After an instance_failure recreation the target has a new EC2 instance ID, but
Prometheus's static `ec2_instance_id` scrape label (rendered once from
prometheus.yml.j2) still names the terminated one, which breaks the Plan
layer's transport-readiness gate for later Ansible-actuated trials on that
target. The refresh rewrites that one label in place and restarts Prometheus.

It is best-effort by contract: whatever goes wrong, it must log and return,
never raise, because it runs inside an instance_failure remediation whose own
timings are already recorded.
"""
from __future__ import annotations

import builtins
import logging
import subprocess
import sys
from pathlib import Path

import pytest

_ORCHESTRATOR = Path(__file__).resolve().parent.parent.parent / "orchestrator"
sys.path.insert(0, str(_ORCHESTRATOR))

import execute

_PREFIX = "self-healing-thesis-experiment-"
_RENDERED = """\
scrape_configs:
  - job_name: node_exporter
    static_configs:
      - targets: ["10.0.2.10:9100"]
        labels:
          ec2_instance_id: "i-0000000000000001"
          node_name: "self-healing-thesis-experiment-target-1"
      - targets: ["10.0.2.11:9100"]
        labels:
          ec2_instance_id: "i-0000000000000002"
          node_name: "self-healing-thesis-experiment-target-2"
      - targets: ["10.0.2.12:9100"]
        labels:
          ec2_instance_id: "i-0000000000000003"
          node_name: "self-healing-thesis-experiment-target-3"
"""


@pytest.fixture
def prom(tmp_path, monkeypatch):
    path = tmp_path / "prometheus.yml"
    path.write_text(_RENDERED)
    monkeypatch.setattr(execute, "_PROMETHEUS_YML_PATH", str(path))
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(execute.subprocess, "run", fake_run)
    return path, calls


def test_rewrites_only_the_named_targets_label_and_restarts_prometheus(prom):
    path, calls = prom
    execute._refresh_prometheus_scrape_label(_PREFIX + "target-2", "i-0abc")

    expected = _RENDERED.replace("i-0000000000000002", "i-0abc")
    assert path.read_text() == expected          # one label changed, nothing else
    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd == ["docker", "compose", "restart", "prometheus"]
    assert kwargs["cwd"] == execute._MONITORING_COMPOSE_DIR
    assert kwargs["start_new_session"] is True


def test_unknown_target_changes_nothing_and_does_not_restart(prom, caplog):
    path, calls = prom
    with caplog.at_level(logging.WARNING):
        execute._refresh_prometheus_scrape_label(_PREFIX + "target-9", "i-0abc")
    assert path.read_text() == _RENDERED
    assert calls == []
    assert "expected exactly 1 match" in caplog.text


def test_duplicate_node_name_is_ambiguous_and_skipped(prom, caplog):
    path, calls = prom
    path.write_text(_RENDERED + _RENDERED.split("static_configs:\n", 1)[1])
    before = path.read_text()
    with caplog.at_level(logging.WARNING):
        execute._refresh_prometheus_scrape_label(_PREFIX + "target-1", "i-0abc")
    assert path.read_text() == before
    assert calls == []
    assert "found 2" in caplog.text


def test_unreadable_config_is_logged_not_raised(prom, monkeypatch, caplog):
    _, calls = prom
    monkeypatch.setattr(execute, "_PROMETHEUS_YML_PATH", "/nonexistent/prometheus.yml")
    with caplog.at_level(logging.WARNING):
        execute._refresh_prometheus_scrape_label(_PREFIX + "target-1", "i-0abc")
    assert calls == []
    assert "could not read" in caplog.text


def test_write_failure_is_logged_not_raised_and_skips_restart(prom, monkeypatch, caplog):
    path, calls = prom
    real_open = builtins.open

    def guarded_open(file, mode="r", *a, **k):
        if str(file) == str(path) and "w" in mode:
            raise PermissionError("read-only")
        return real_open(file, mode, *a, **k)

    monkeypatch.setattr(builtins, "open", guarded_open)
    with caplog.at_level(logging.WARNING):
        execute._refresh_prometheus_scrape_label(_PREFIX + "target-1", "i-0abc")
    assert calls == []
    assert "failed to write" in caplog.text


def test_restart_nonzero_exit_is_logged_not_raised(prom, monkeypatch, caplog):
    monkeypatch.setattr(
        execute.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "no such service"),
    )
    with caplog.at_level(logging.WARNING):
        execute._refresh_prometheus_scrape_label(_PREFIX + "target-1", "i-0abc")
    assert "container restart failed" in caplog.text


@pytest.mark.parametrize("exc", [
    subprocess.TimeoutExpired(cmd="docker", timeout=30),
    FileNotFoundError("docker"),
])
def test_restart_that_raises_is_logged_not_raised(prom, monkeypatch, caplog, exc):
    """The documented contract is non-raising: this runs inside an
    instance_failure remediation whose timings are already recorded."""
    def boom(cmd, **kw):
        raise exc

    monkeypatch.setattr(execute.subprocess, "run", boom)
    with caplog.at_level(logging.WARNING):
        execute._refresh_prometheus_scrape_label(_PREFIX + "target-1", "i-0abc")
    assert "container restart failed" in caplog.text
