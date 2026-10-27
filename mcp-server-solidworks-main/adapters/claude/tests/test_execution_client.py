import sys
from pathlib import Path

import httpx
import pytest


ADAPTER_DIR = Path(__file__).resolve().parents[1]
if str(ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(ADAPTER_DIR))

import execution_client  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_shared_client():
    """The client is module-level and long-lived; each test must start from a clean pool.

    Without this, a FakeClient installed by one test would leak into the next through
    `execution_client._client` (the whole point of the shared client) and the next test
    would drive the wrong double.
    """
    execution_client._reset_client()
    _CountingClient.instances.clear()
    _CountingClient.handler = None
    yield
    execution_client._reset_client()
    _CountingClient.instances.clear()
    _CountingClient.handler = None


def test_get_health_autostarts_execution_server_after_connect_error(monkeypatch):
    request = httpx.Request("GET", execution_client.HEALTH_ENDPOINT)
    calls = {"get": 0, "start": 0}

    class FakeClient:
        def __init__(self, timeout, trust_env, limits=None):
            self.timeout = timeout
            assert trust_env is False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def close(self):
            return None

        def get(self, _url, timeout=None):
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
        def __init__(self, timeout, trust_env, limits=None):
            seen["timeout"] = timeout
            assert trust_env is False

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def close(self):
            return None

        def post(self, _url, json=None, timeout=None):
            seen["request_timeout"] = timeout
            raise httpx.ReadTimeout("too slow", request=request)

    monkeypatch.setattr(execution_client.httpx, "Client", FakeClient)
    monkeypatch.setattr(execution_client, "TOOL_EXEC_TIMEOUT", 123.0)

    with pytest.raises(execution_client.ExecutionLayerError) as excinfo:
        execution_client.call_tool("create_sketch", "op-1", 7, {"plane": "Top Plane"})

    # The long /execute budget is applied PER REQUEST (the shared client carries a short
    # default), so a rebuild-heavy tool still gets its own longer window.
    assert seen["request_timeout"] == 123.0
    message = str(excinfo.value)
    assert "timed out after 123.0s" in message
    assert "tool=create_sketch" in message
    assert "HTTP_TOOL_TIMEOUT" in message


# ------------------------------------------------------------------------------
# Shared HTTP client
#
# Each request used to build and close its own httpx.Client (~150 ms of setup/teardown per
# call). One module-level client is reused instead; these tests pin the reuse, the reset
# rules, and that behaviour is otherwise identical (timeouts, error mapping, autostart).
# ------------------------------------------------------------------------------
class _CountingClient:
    """Records construction, requests and closes so reuse/reset can be asserted."""

    instances = []
    # CLASS-level handler: a reset builds a NEW instance, and a test that installed a fake
    # response must keep driving it across that rebuild (that is what the reset tests probe).
    handler = None

    def __init__(self, timeout=None, trust_env=None, limits=None):
        self.timeout = timeout
        self.trust_env = trust_env
        self.limits = limits
        self.requests = []
        self.closed = False
        _CountingClient.instances.append(self)

    def _do(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        if _CountingClient.handler is not None:
            return _CountingClient.handler(method, url, **kwargs)
        return httpx.Response(200, json={"stateVersion": 3, "status": "COMPLETED"}, request=httpx.Request(method, url))

    def get(self, url, **kwargs):
        return self._do("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._do("POST", url, **kwargs)

    def close(self):
        self.closed = True


@pytest.fixture
def counting_client(monkeypatch):
    _CountingClient.instances = []
    monkeypatch.setattr(execution_client.httpx, "Client", _CountingClient)
    execution_client._reset_client()
    yield _CountingClient
    execution_client._reset_client()


def test_many_calls_construct_exactly_one_client(counting_client):
    for _ in range(20):
        execution_client.get_state()
    for _ in range(5):
        execution_client.get_health()
    assert len(counting_client.instances) == 1, "a client was rebuilt per request"
    assert len(counting_client.instances[0].requests) == 25
    assert counting_client.instances[0].trust_env is False


def test_one_client_is_shared_across_threads(counting_client):
    import threading

    errors = []

    def worker():
        try:
            for _ in range(5):
                execution_client.get_state()
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    assert len(counting_client.instances) == 1
    assert len(counting_client.instances[0].requests) == 20


def test_connect_error_discards_the_client_and_the_retry_gets_a_fresh_one(
    counting_client, monkeypatch
):
    request = httpx.Request("GET", execution_client.HEALTH_ENDPOINT)
    state = {"fail": True}

    def handler(method, url, **kwargs):
        if state["fail"]:
            state["fail"] = False
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200, json={"stateVersion": 9, "status": "COMPLETED"}, request=request)

    monkeypatch.setattr(execution_client, "_ensure_server_up", lambda: None)
    _CountingClient.handler = handler

    assert execution_client.get_state() == 9
    assert len(counting_client.instances) == 2, "the dead pool was reused"
    assert counting_client.instances[0].closed is True


def test_autostart_spawn_replaces_the_client_even_if_the_probe_succeeds(
    counting_client, monkeypatch
):
    # _ensure_server_up spawns a new process; the old pool's kept-alive sockets belong to the
    # dead one, so the client must be rebuilt even though the retry would have "worked".
    request = httpx.Request("GET", execution_client.HEALTH_ENDPOINT)
    state = {"fail": True}

    def handler(method, url, **kwargs):
        if state["fail"]:
            state["fail"] = False
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200, json={"stateVersion": 4, "status": "COMPLETED"}, request=request)

    spawned = {"n": 0}

    def fake_spawn():
        spawned["n"] += 1

    monkeypatch.setattr(execution_client, "_ensure_server_up", fake_spawn)
    _CountingClient.handler = handler

    assert execution_client.get_state() == 4
    assert spawned["n"] == 1
    assert len(counting_client.instances) == 2


def test_autostart_spawns_the_exe_and_the_retry_runs_on_a_fresh_client(
    counting_client, monkeypatch
):
    """End-to-end autostart with the REAL `_ensure_server_up`, only `Popen` mocked.

    The tests above stub `_ensure_server_up` out, which proves the client is dropped around
    the call but not that the spawn path still works. Here the actual probe/spawn/poll loop
    runs: /execute hits a dead socket, the exe is "spawned" (recorded, not executed), /health
    only starts answering once that happened, and the retry must go out on a client built
    after the respawn — never the pool whose sockets died with the old process.
    """
    request = httpx.Request("POST", execution_client.EXECUTE_ENDPOINT)
    spawned = []
    state = {"execute_fails": True}

    def handler(method, url, **kwargs):
        if url == execution_client.HEALTH_ENDPOINT:
            # The liveness probe fails until the exe has been spawned.
            if not spawned:
                raise httpx.ConnectError("nothing listening", request=request)
            return httpx.Response(200, json={"status": "UP"}, request=request)
        if state["execute_fails"]:
            state["execute_fails"] = False
            raise httpx.ConnectError("nothing listening", request=request)
        return httpx.Response(
            200, json={"stateVersion": 11, "status": "COMPLETED"}, request=request
        )

    def fake_popen(argv, **kwargs):
        spawned.append((argv, kwargs))
        return object()

    monkeypatch.setattr(execution_client.os.path, "isfile", lambda _p: True)
    monkeypatch.setattr(execution_client.subprocess, "Popen", fake_popen)
    _CountingClient.handler = handler

    body = execution_client.call_tool(
        "analyze_model", "op-autostart", 11, {"analysis_type": "mass_properties"}
    )

    assert body["status"] == "COMPLETED"
    assert len(spawned) == 1, "the exe was not spawned exactly once"
    assert spawned[0][0] == [execution_client.EXECUTION_EXE_PATH]
    # Detached, so the server outlives this process.
    assert spawned[0][1]["creationflags"] == 0x00000008 | 0x08000000
    # Old pool dropped and closed; the probe and the retry each got a fresh client.
    assert counting_client.instances[0].closed is True
    assert len(counting_client.instances) >= 2
    assert counting_client.instances[-1] is not counting_client.instances[0]


def test_transport_error_after_autostart_maps_to_execution_layer_error(
    counting_client, monkeypatch
):
    request = httpx.Request("GET", execution_client.HEALTH_ENDPOINT)

    def handler(method, url, **kwargs):
        raise httpx.ConnectError("still down", request=request)

    monkeypatch.setattr(execution_client, "_ensure_server_up", lambda: None)
    _CountingClient.handler = handler

    with pytest.raises(execution_client.ExecutionLayerError) as excinfo:
        execution_client.get_state()
    assert "even after auto-start" in str(excinfo.value)


def test_timeouts_are_per_request_not_per_client(counting_client):
    execution_client.get_health()
    execution_client.get_state()
    execution_client.call_tool("analyze_model", "op-1", 1, {"analysis_type": "mass_properties"})
    execution_client.ensure_ready()

    client = counting_client.instances[0]
    assert len(counting_client.instances) == 1
    # FIRST occurrence per URL: the 2.0 s liveness probe re-hits /health with its own short
    # timeout (by design), so a last-wins dict would read the probe instead of get_health.
    seen = {}
    for _method, url, kwargs in client.requests:
        seen.setdefault(url, kwargs.get("timeout"))
    assert seen[execution_client.HEALTH_ENDPOINT] == execution_client.HTTP_TIMEOUT
    assert seen[execution_client.STATE_ENDPOINT] == execution_client.HTTP_TIMEOUT
    assert seen[execution_client.EXECUTE_ENDPOINT] == execution_client.TOOL_EXEC_TIMEOUT
    assert seen[execution_client.ENSURE_ENDPOINT] == execution_client.ENSURE_TIMEOUT


def test_client_is_closed_on_reset_and_rebuilt_lazily(counting_client):
    client = execution_client._get_client()
    assert execution_client._client_count() == 1
    execution_client._reset_client()
    assert execution_client._client_count() == 0
    assert client.closed is True
    # nothing is built until the next request
    assert len(counting_client.instances) == 1
    execution_client.get_state()
    assert len(counting_client.instances) == 2
