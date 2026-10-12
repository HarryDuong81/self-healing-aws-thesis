#!/usr/bin/env python3
"""
experiments/trials/run_calibration_driver.py -- host-side driver for the S2
alert-milestone calibration (run_manual_trial_v2.py --calibration).

Runs inside the tmux session the operator is attached to. For each scheduled
trial it:
  1. waits for the operator to press ENTER (the trial clock starts at injection,
     so the operator must be at the keyboard);
  2. waits for the quiet gate: no pending or firing alert other than Watchdog and
     all node_exporter targets up, for several consecutive polls;
  3. launches the harness with the operator's terminal attached, so the harness
     prompts reach the operator directly;
  4. logs the exit code, then imposes the cool-down.

Masking: the driver never prints the fault class. Every phase message is
identical for every trial. The harness command line (which contains the fault
class) is visible to `ps`; the operator is expected not to look (honour system,
recorded as a protocol note). The driver log records trial id, exit code and
times only.

Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from make_calibration_schedule import load_schedule  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_HARNESS = _REPO_ROOT / "experiments" / "trials" / "run_manual_trial_v2.py"
COOLDOWN_S = 120
GATE_CONSECUTIVE = 4
GATE_INTERVAL_S = 5
GATE_TIMEOUT_S = 1500


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_cmd(row: dict, python: str, harness: Path) -> list[str]:
    """Harness command for one schedule row (contains the fault class: never print it)."""
    return [python, str(harness), "--fault", row["fault"], "--target", row["target"],
            "--block", str(row["block"]), "--trial-id", row["trial_id"], "--calibration"]


def _query(url: str, expr: str) -> list:
    with urllib.request.urlopen(url + "/api/v1/query?" + urllib.parse.urlencode({"query": expr}), timeout=10) as r:
        return json.loads(r.read()).get("data", {}).get("result", [])


def gate_ok(query: Callable[[str], list]) -> bool:
    """True when nothing but Watchdog is pending/firing and all three node_exporter targets are up."""
    active = query('ALERTS{alertstate=~"pending|firing",alertname!="Watchdog"}')
    ups = query('up{job="node_exporter"}')
    return len(active) == 0 and len(ups) == 3 and all(float(s["value"][1]) == 1.0 for s in ups)


def wait_for_gate(query: Callable[[str], list], *, timeout_s: float = GATE_TIMEOUT_S,
                  consecutive: int = GATE_CONSECUTIVE, interval_s: float = GATE_INTERVAL_S,
                  sleep: Callable[[float], None] = time.sleep, now: Callable[[], float] = time.monotonic) -> bool:
    deadline = now() + timeout_s
    good = 0
    while now() < deadline:
        try:
            good = good + 1 if gate_ok(query) else 0
        except Exception:
            good = 0
        if good >= consecutive:
            return True
        sleep(interval_s)
    return False


def _orchestrator_active() -> bool:
    r = subprocess.run(["systemctl", "is-active", "orchestrator.service"], capture_output=True, text=True)
    return r.stdout.strip() == "active"


def _log(path: Path, rec: dict) -> None:
    with path.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Masked S2 calibration driver (never prints the fault class).")
    ap.add_argument("--schedule", required=True)
    ap.add_argument("--start", type=int, default=1)
    ap.add_argument("--end", type=int, default=None)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--harness", default=str(DEFAULT_HARNESS))
    ap.add_argument("--prometheus-url", default="http://localhost:9090")
    ap.add_argument("--log", default="/opt/calib/driver.log")
    ap.add_argument("--cooldown", type=int, default=COOLDOWN_S)
    ap.add_argument("--dry-run", action="store_true", help="no prompts, no gate, no harness: prints only the trial ids that would run")
    args = ap.parse_args(argv)

    rows = load_schedule(args.schedule)
    end = args.end if args.end is not None else len(rows)
    todo = [r for r in rows if args.start <= r["seq"] <= end]
    log = Path(args.log)

    if args.dry_run:
        for r in todo:
            print(f"would run {r['trial_id']}")
        return 0

    if _orchestrator_active():
        print("orchestrator.service is active. Stop it first: sudo systemctl stop orchestrator", file=sys.stderr)
        return 2

    env = dict(os.environ)
    env["PYTHONPATH"] = str(_REPO_ROOT)
    q = lambda expr: _query(args.prometheus_url, expr)  # noqa: E731
    print(f"S2 calibration driver: {len(todo)} trial(s) in this session, {len(rows)} in the schedule.")
    try:
        for r in todo:
            print(f"\n=== Trial {r['seq']:02d} of {len(rows)} ===")
            input("Press ENTER when you are at the keyboard and ready. The clock starts at injection, shortly after you press it. ")
            print("Checking that the system is quiet ...")
            t_gate = time.monotonic()
            if not wait_for_gate(q):
                print("The quiet gate did not clear within the time limit. Stopping; nothing was injected.")
                _log(log, {"trial_id": r["trial_id"], "event": "gate_failed", "at": _utcnow()})
                return 3
            print(f"Quiet after {time.monotonic() - t_gate:.0f}s. Starting the trial.\n")
            started = _utcnow()
            rc = subprocess.call(build_cmd(r, args.python, Path(args.harness)), env=env, cwd=str(_REPO_ROOT))
            _log(log, {"trial_id": r["trial_id"], "event": "finished", "returncode": rc, "started": started, "ended": _utcnow()})
            print(f"\nTrial {r['seq']:02d} finished (exit code {rc}).")
            if r is not todo[-1]:
                print(f"Cool-down {args.cooldown}s ...")
                time.sleep(args.cooldown)
    except KeyboardInterrupt:
        _log(log, {"event": "interrupted", "at": _utcnow()})
        print("\nInterrupted. Re-run with --start N to resume at the next unrun trial.")
        return 130
    print("\nAll scheduled trials in this session are done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
