import sys
from pathlib import Path

import httpx
import pytest


ADAPTER_DIR = Path(__file__).resolve().parents[1]
if str(ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(ADAPTER_DIR))

import execution_client  # noqa: E402


def test_get_health_autostarts_execution_server_after_connect_error(monkeypatch):
    request = httpx.Request("GET", execution_client.HEALTH_ENDPOINT)
    calls = {"get": 0, "start": 0}

    class FakeClient:
        def __init__(self, timeout, trust_env):
            self.timeout = timeout
            assert trust_env is False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def get(self, _url):
            calls["get"] += 1
            if calls["get"] == 1:
                raise httpx.ConnectError("execution server is down", request=request)
            return httpx.Response(
                200,
                json={"status": "UP", "comAttached": False, "stateVersion": 0},
                request=request,
            )

    def fake_start():
        calls["start"] += 1

    monkeypatch.setattr(execution_client.httpx, "Client", FakeClient)
    monkeypatch.setattr(execution_client, "_ensure_server_up", fake_start)

    result = execution_client.get_health()

    assert result["status"] == "UP"
    assert calls == {"get": 2, "start": 1}


def test_call_tool_posts_with_tool_exec_timeout_and_reports_tool_on_timeout(monkeypatch):
    # Tool executions run COM against SolidWorks (rebuild-heavy, sometimes minutes) so they
    # use TOOL_EXEC_TIMEOUT, not the fast /health budget — and a timeout names the tool/op
    # and the knob to turn instead of a bare "timed out after 30.0s".
    request = httpx.Request("POST", execution_client.EXECUTE_ENDPOINT)
    seen = {}

    class FakeClient:
        def __init__(self, timeout, trust_env):
            seen["timeout"] = timeout
            assert trust_env is False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def post(self, _url, json=None):
            raise httpx.ReadTimeout("too slow", request=request)

    monkeypatch.setattr(execution_client.httpx, "Client", FakeClient)
    monkeypatch.setattr(execution_client, "TOOL_EXEC_TIMEOUT", 123.0)

    with pytest.raises(execution_client.ExecutionLayerError) as excinfo:
        execution_client.call_tool("create_sketch", "op-1", 7, {"plane": "Top Plane"})

    assert seen["timeout"] == 123.0
    message = str(excinfo.value)
    assert "timed out after 123.0s" in message
    assert "tool=create_sketch" in message
    assert "HTTP_TOOL_TIMEOUT" in message
