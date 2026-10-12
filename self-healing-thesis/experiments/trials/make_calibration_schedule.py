#!/usr/bin/env python3
"""
experiments/trials/make_calibration_schedule.py -- masked schedule for the S2
alert-milestone calibration (run_manual_trial_v2.py --calibration).

18 trials: each of the six fault classes three times, once on each target
(a Latin arrangement), in a shuffled order. The shuffle is rejected until no
target is used again within the two trials that follow an instance_failure
recreation of it.

The output file is the masking secret. This script therefore never prints the
fault classes, targets or order; it reports only how many rows it wrote and
whether the constraints hold. Trial ids are opaque (calib-01 .. calib-18).

The first line of the file is a {"_meta": ...} record holding the seed, so the
schedule can be regenerated and audited afterwards; readers must skip it.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import random
import sys
from datetime import datetime, timezone

CLASSES = ["cpu_overload", "memory_exhaustion", "disk_saturation",
           "service_crash", "configuration_drift", "instance_failure"]
TARGETS = ["target-1", "target-2", "target-3"]
REPS = 3


def _units() -> list[tuple[str, str]]:
    return [(c, TARGETS[(k + j) % 3]) for k, c in enumerate(CLASSES) for j in range(REPS)]


def check(rows: list[dict]) -> list[str]:
    """Return a list of constraint violations (empty list = schedule is valid)."""
    problems: list[str] = []
    if len(rows) != len(CLASSES) * REPS:
        problems.append(f"expected {len(CLASSES) * REPS} rows, got {len(rows)}")
    for c in CLASSES:
        ts = sorted(r["target"] for r in rows if r["fault"] == c)
        if ts != sorted(TARGETS):
            problems.append(f"class {c} does not visit each target exactly once")
    for i, r in enumerate(rows):
        if r["fault"] == "instance_failure":
            for nxt in rows[i + 1:i + 3]:
                if nxt["target"] == r["target"]:
                    problems.append(f"target reused within two trials after a recreation (seq {r['seq']})")
    ids = [r["trial_id"] for r in rows]
    if ids != [f"calib-{i + 1:02d}" for i in range(len(rows))]:
        problems.append("trial ids are not the opaque sequence calib-01..")
    return problems


def build(seed: int) -> list[dict]:
    rng = random.Random(seed)
    units = _units()
    while True:
        order = units[:]
        rng.shuffle(order)
        rows = [{"seq": i + 1, "trial_id": f"calib-{i + 1:02d}", "fault": c, "target": t, "block": -1}
                for i, (c, t) in enumerate(order)]
        if not check(rows):
            return rows


def load_schedule(path: str) -> list[dict]:
    """Read a schedule file, skipping the _meta line."""
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if "_meta" in rec:
                continue
            rows.append(rec)
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Generate the masked S2 calibration schedule (prints nothing about its contents).")
    ap.add_argument("--out", required=True)
    ap.add_argument("--seed", type=int, default=None, help="default: a fresh random seed (recorded in the file)")
    args = ap.parse_args(argv)
    if os.path.exists(args.out):
        print("refusing to overwrite an existing schedule file", file=sys.stderr)
        return 2
    seed = args.seed if args.seed is not None else secrets.randbits(62)
    rows = build(seed)
    problems = check(rows)
    if problems:
        print("schedule failed its own checks", file=sys.stderr)
        return 1
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"_meta": {"seed": seed, "created": datetime.now(timezone.utc).isoformat(),
                                       "generator": "make_calibration_schedule.py"}}) + "\n")
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    print(f"wrote {len(rows)} rows to {args.out}; constraints OK; contents not shown")
    return 0


if __name__ == "__main__":
    sys.exit(main())
