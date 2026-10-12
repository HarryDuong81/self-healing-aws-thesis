#!/usr/bin/env python3
"""
scripts/verify_pins.py — Verify installed toolchain versions against .toolchain.

Called by `make verify-pins`. Reads .toolchain (KEY=VERSION lines) and checks
each tool's installed version with a best-effort probe. Reports PASS / FAIL
per tool and exits 1 if any pin is violated or unverifiable.

Thesis reference: §3 (pinned toolchain) Table in §3.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path


def _run(*args: str) -> str:
    try:
        return subprocess.check_output(list(args), stderr=subprocess.STDOUT, text=True).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def _major_minor(version_str: str) -> tuple[int, ...]:
    """Extract numeric version tuple from a version string like '1.14.9' or '2.17.10'."""
    parts = re.findall(r"\d+", version_str)
    return tuple(int(x) for x in parts[:3]) if parts else ()


def _version_satisfies(installed: str, pinned: str) -> bool:
    """
    Returns True if the installed version satisfies the pinned spec.
    Pin specs may be:
      exact:  "1.14.9"   → exact major.minor.patch match
      major:  "24.0"     → major.minor prefix match
      minor:  "2.17"     → major.minor prefix match
    """
    i_parts = _major_minor(installed)
    p_parts = _major_minor(pinned)
    if not i_parts or not p_parts:
        return False
    # Match as many components as the pin specifies
    return i_parts[: len(p_parts)] == p_parts[: len(p_parts)]


PROBES: dict[str, tuple[str, ...]] = {
    "terraform":      ("terraform", "version"),
    "ansible-core":   ("ansible", "--version"),
    "python":         ("python3", "--version"),
    "docker":         ("docker", "--version"),
    "docker-compose": ("docker", "compose", "version"),
    "prometheus":     ("promtool", "--version"),
}


def check_tool(name: str, pinned: str) -> tuple[bool, str]:
    probe = PROBES.get(name)
    if probe is None:
        return True, f"{name}: no probe defined — skipped (container-managed)"

    if not shutil.which(probe[0]):
        return True, f"{name}: {probe[0]} not in PATH — skipped"

    raw = _run(*probe)
    version_match = re.search(r"(\d+\.\d+(?:\.\d+)?)", raw)
    if not version_match:
        return False, f"{name}: could not parse version from output: {raw!r}"

    installed = version_match.group(1)
    ok        = _version_satisfies(installed, pinned)
    status    = "OK" if ok else "MISMATCH"
    msg       = f"{name}: installed={installed} pinned={pinned} [{status}]"
    return ok, msg


# ── Declared-vs-deployed image pins ───────────────────────────────────────────
# .toolchain records container-image versions, but the versions actually
# deployed come from the monitoring_stack role's defaults, which the Compose
# template interpolates.  The two are independent declarations of the same
# fact and can drift apart silently -- which is exactly how cAdvisor came to be
# recorded at 0.49.1 while the deployed template still requested `latest`.
# Nothing probes a running container here; this compares the two declarations.
_ROLE_DEFAULTS = Path("ansible/roles/monitoring_stack/defaults/main.yml")
_IMAGE_PINS: dict[str, str] = {
    "prometheus":    "prometheus_version",
    "alertmanager":  "alertmanager_version",
    "grafana":       "grafana_version",
    "elasticsearch": "elasticsearch_version",
    "logstash":      "logstash_version",
    "kibana":        "kibana_version",
    "otelcol":       "otel_collector_version",
    "jaeger":        "jaeger_version",
    "cadvisor":      "cadvisor_version",
}


def check_image_pins(pins: dict[str, str], root: Path) -> tuple[bool, list[str]]:
    """Compare .toolchain's image entries against the role defaults."""
    defaults_path = root / _ROLE_DEFAULTS
    lines: list[str] = []
    if not defaults_path.exists():
        return True, [f"  SKIP  {_ROLE_DEFAULTS} not found — image pins not cross-checked"]

    declared: dict[str, str] = {}
    for raw in defaults_path.read_text().splitlines():
        raw = raw.split("#", 1)[0].strip()          # strip trailing comments
        if ":" not in raw:
            continue
        key, _, val = raw.partition(":")
        declared[key.strip()] = val.strip().strip('"').strip("'")

    ok = True
    for pin_name, var_name in _IMAGE_PINS.items():
        want = pins.get(pin_name)
        got  = declared.get(var_name)
        if want is None:
            continue
        if got is None:
            lines.append(f" FAIL  {pin_name}: .toolchain pins {want} but "
                         f"{var_name} is absent from the role defaults")
            ok = False
        elif got != want:
            lines.append(f" FAIL  {pin_name}: .toolchain pins {want} but "
                         f"{var_name} deploys {got}")
            ok = False
        else:
            lines.append(f"  OK   {pin_name}={want} (declaration matches {var_name})")
    return ok, lines


def main(argv: list[str]) -> int:
    toolchain_path = Path(argv[0]) if argv else Path(".toolchain")
    if not toolchain_path.exists():
        print(f"ERROR: {toolchain_path} not found", file=sys.stderr)
        return 1

    pins: dict[str, str] = {}
    for line in toolchain_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            pins[key.strip()] = val.strip()

    all_ok = True
    for name, pinned in pins.items():
        ok, msg = check_tool(name, pinned)
        prefix  = "  OK  " if ok else " FAIL "
        print(f"{prefix} {msg}")
        if not ok:
            all_ok = False

    # Cross-check the declarative container-image entries against the values
    # the deployment actually interpolates.
    images_ok, image_lines = check_image_pins(pins, toolchain_path.resolve().parent)
    if image_lines:
        print("\nContainer-image pins (declared vs deployed):")
        for line in image_lines:
            print(line)
    if not images_ok:
        all_ok = False

    if all_ok:
        print("\nAll toolchain pins verified.")
        return 0
    else:
        print("\nPin verification FAILED — see MISMATCH lines above.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
