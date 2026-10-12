"""
tests/integration/conftest.py — pytest configuration for integration tests.

Adds the repo root to sys.path so experiments.* and orchestrator.* modules
are importable without package installation. Also registers the --integration
flag so tests can be enabled via CLI as an alternative to INTEGRATION_TESTS=1.
"""
import os
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent.parent
for _p in [str(_ROOT), str(_ROOT / "orchestrator")]:
    if _p not in sys.path:
        sys.path.insert(0, _p)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--integration",
        action="store_true",
        default=False,
        help="Enable integration tests (requires live AWS infrastructure).",
    )


def pytest_configure(config: pytest.Config) -> None:
    if config.getoption("--integration", default=False):
        os.environ["INTEGRATION_TESTS"] = "1"
