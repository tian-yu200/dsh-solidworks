import sys
from pathlib import Path

import httpx


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
