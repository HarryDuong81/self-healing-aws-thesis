"""
tests/unit/conftest.py — pytest configuration for orchestrator unit tests.

Adds orchestrator/ to sys.path and sets ROUTING_YAML so classifier.py
loads the checked-in routing.yaml without needing package installation.
"""
import os
import sys
from pathlib import Path

_ROOT        = Path(__file__).resolve().parent.parent.parent
_ORCHESTRATOR = _ROOT / "orchestrator"

# Allow bare `import classifier`, `import plan`, etc.
if str(_ORCHESTRATOR) not in sys.path:
    sys.path.insert(0, str(_ORCHESTRATOR))

# Point classifier.py to the real routing.yaml
os.environ.setdefault("ROUTING_YAML", str(_ORCHESTRATOR / "routing.yaml"))

# Redirect results output to /tmp during tests (overridden per-test where needed)
os.environ.setdefault("RESULTS_DIR", "/tmp/self-healing-test-results")
