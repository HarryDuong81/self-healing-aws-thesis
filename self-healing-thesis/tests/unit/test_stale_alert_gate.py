"""
tests/unit/test_stale_alert_gate.py — Stale-alert gate in app.handle_alert and
health_checker.alert_still_firing.

A webhook queued behind a long remediation can be handled after the alert it
announces has cleared. The gate drops it (still logged for RQ4) instead of
opening a redundant MAPE-K cycle; it fails open if Prometheus is unreachable.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

_ORCHESTRATOR = Path(__file__).resolve().parent.parent.parent / "orchestrator"
sys.path.insert(0, str(_ORCHESTRATOR))

import app as app_module
import health_checker as hc_module


def _prom_response(result: list) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {"data": {"result": result}}
    return resp


class TestAlertStillFiring:

    def test_returns_true_when_alert_series_present(self):
        with patch.object(hc_module.requests, "get",
                          return_value=_prom_response([{"metric": {}}])):
            assert hc_module.alert_still_firing("instance_failure", "target-3") is True

    def test_returns_false_when_alert_series_empty(self):
        with patch.object(hc_module.requests, "get",
                          return_value=_prom_response([])):
            assert hc_module.alert_still_firing("instance_failure", "target-3") is False

    def test_returns_none_when_prometheus_unreachable(self):
        with patch.object(hc_module.requests, "get",
                          side_effect=requests.ConnectionError("down")):
            assert hc_module.alert_still_firing("instance_failure", "target-3") is None

    def test_queries_firing_state_for_fault_and_target(self):
        with patch.object(hc_module.requests, "get",
                          return_value=_prom_response([])) as get:
            hc_module.alert_still_firing("instance_failure", "target-3")
        query = get.call_args.kwargs["params"]["query"]
        assert 'fault_type="instance_failure"' in query
        assert 'node_name="target-3"' in query
        assert 'alertstate="firing"' in query


def _payload() -> dict:
    return {"alerts": [{
        "status": "firing",
        "labels": {
            "fault_type": "instance_failure",
            "node_name": "target-3",
            "instance": "10.0.2.12",
            "ec2_instance_id": "i-0d1cbf9fa5f67cf85",
        },
    }]}


@pytest.fixture
def client():
    app_module._suppressor.clear()
    with app_module.app.test_client() as c:
        yield c
    app_module._suppressor.clear()


class TestHandleAlertGate:

    def _post(self, client, firing):
        with patch.object(app_module, "alert_still_firing", return_value=firing), \
             patch.object(app_module, "_run_mape_k") as run, \
             patch.object(app_module, "record_alert_receipt") as record:
            resp = client.post("/alert", json=_payload())
        return resp, run, record

    def test_stale_alert_is_not_dispatched_but_is_logged(self, client):
        resp, run, record = self._post(client, False)
        assert resp.get_json() == {"status": "received", "processed": 0, "suppressed": 1}
        run.assert_not_called()
        record.assert_called_once()
        assert record.call_args.kwargs["suppressed"] is True
        assert record.call_args.kwargs["ec2_instance_id"] == "i-0d1cbf9fa5f67cf85"

    def test_still_firing_alert_is_dispatched(self, client):
        resp, run, record = self._post(client, True)
        assert resp.get_json()["processed"] == 1
        run.assert_called_once()
        assert record.call_args.kwargs["suppressed"] is False

    def test_unknown_freshness_fails_open(self, client):
        resp, run, _ = self._post(client, None)
        assert resp.get_json()["processed"] == 1
        run.assert_called_once()

    def test_stale_drop_does_not_open_suppression_window(self, client):
        self._post(client, False)
        resp, run, _ = self._post(client, True)
        assert resp.get_json()["processed"] == 1
        run.assert_called_once()
