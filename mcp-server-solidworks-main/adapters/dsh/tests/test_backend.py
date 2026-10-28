"""Unit tests for SolidWorksBackend.identity() — GET /api/tool/identity with fallback.

The orchestrator's identity-only guards depend on two promises:
  1. a healthy endpoint yields the identity subset of verify_state, in the SAME response
     shape, with cadState carrying ONLY the fields the C# service reports (stateVersion,
     activeDocument, activeDocumentPath, documentType, isDirty);
  2. ANY endpoint failure (missing route on an older service build, transport error,
     non-200, missing fields) degrades to the FULL verify_state — safety never weakens.

The GET goes through execution_client.pooled_get (SPEED #1: one shared httpx client, not a
new one per probe), which is the seam patched here.
"""

import backend as backend_module
from backend import SolidWorksBackend


class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


_IDENTITY_BODY = {
    "stateVersion": 7,
    "activeDocument": "part.SLDPRT",
    "activeDocumentPath": "D:/parts/part.SLDPRT",
    "documentType": "PART",
    "isDirty": True,
}


def test_identity_returns_the_identity_subset_in_the_verify_state_shape(monkeypatch):
    b = SolidWorksBackend()
    requested = []

    def fake_get(url, timeout=None):
        requested.append(url)
        return _Response(200, dict(_IDENTITY_BODY))

    monkeypatch.setattr(backend_module, "pooled_get", fake_get)

    response = b.identity()

    assert requested == [backend_module.IDENTITY_ENDPOINT]
    assert response["status"] == "COMPLETED"
    assert response["error"] is None
    assert response["stateVersion"] == 7
    # cadState carries ONLY the identity fields the endpoint reports — a caller that reads
    # CAD content off an identity response must fail loudly in tests, not silently.
    assert set(response["cadState"]) == {
        "stateVersion",
        "activeDocument",
        "activeDocumentPath",
        "documentType",
        "isDirty",
    }
    assert response["cadState"]["isDirty"] is True
    # the cheap probe keeps the adapter's own counter fresh for subsequent tool calls
    assert b._state_version == 7


def test_identity_falls_back_to_full_verify_state_when_the_endpoint_is_missing(
    monkeypatch,
):
    b = SolidWorksBackend()

    def missing(*_args, **_kwargs):
        raise RuntimeError("HTTP 404 Not Found")

    monkeypatch.setattr(backend_module, "pooled_get", missing)
    fallback = {
        "status": "COMPLETED",
        "stateVersion": 3,
        "cadState": {"features": ["Boss-Extrude1"], "activeDocument": "part.SLDPRT"},
        "error": None,
    }
    monkeypatch.setattr(b, "verify_state", lambda: fallback)
    assert b.identity() is fallback


def test_identity_falls_back_on_server_error_or_missing_fields(monkeypatch):
    b = SolidWorksBackend()
    fallback = {
        "status": "FAILED",
        "stateVersion": 3,
        "cadState": None,
        "error": {"code": "NO_ACTIVE_DOCUMENT", "message": "No active document."},
    }
    monkeypatch.setattr(b, "verify_state", lambda: fallback)

    # 503 (COM attach failed / no active document) falls through to verify_state, which
    # reports the same condition authoritatively.
    monkeypatch.setattr(
        backend_module,
        "pooled_get",
        lambda *_a, **_k: _Response(503, {"error": "NO_ACTIVE_DOCUMENT: ..."}),
    )
    assert b.identity() is fallback

    # a 200 without the identity contract is treated as a failure, not trusted
    monkeypatch.setattr(
        backend_module,
        "pooled_get",
        lambda *_a, **_k: _Response(200, {"activeDocument": "part.SLDPRT"}),
    )
    assert b.identity() is fallback
