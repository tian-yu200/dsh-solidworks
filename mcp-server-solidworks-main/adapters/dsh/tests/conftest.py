import copy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ADAPTER_DIR = Path(__file__).resolve().parents[1]
if str(ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(ADAPTER_DIR))

import orchestrator as orchestrator_module  # noqa: E402
from job_store import JobStore  # noqa: E402


class FakeBackend:
    def __init__(self, state):
        self.state = dict(state)
        self.calls = []
        self.events = []
        self.call_handler = None
        self.feature_graph_handler = None
        self._document_stack = []
        self._undo_state = None

    def health(self):
        return {"comAttached": True}

    def ensure_ready(self):
        return {"comAttached": True}

    def verify_state(self):
        self.events.append(("verify_state", {}))
        if self.state is None:
            return {
                "status": "FAILED",
                "stateVersion": 1,
                "cadState": None,
                "error": {
                    "code": "NO_ACTIVE_DOCUMENT",
                    "message": "No active document.",
                },
            }
        return {
            "status": "COMPLETED",
            "stateVersion": self.state.get("stateVersion", 1),
            "cadState": copy.deepcopy(self.state),
            "error": None,
        }

    def call(self, tool, params):
        self.calls.append((tool, copy.deepcopy(params)))
        self.events.append((tool, copy.deepcopy(params)))
        if self.call_handler:
            response = self.call_handler(tool, params)
            if response is not None:
                return response

        if tool in {"open_new_part", "open_new_assembly", "create_drawing"}:
            self._document_stack.append(copy.deepcopy(self.state))
            document_type = {
                "open_new_part": "PART",
                "open_new_assembly": "ASSEMBLY",
                "create_drawing": "DRAWING",
            }[tool]
            title = {
                "open_new_part": "Part1",
                "open_new_assembly": "Assembly1",
                "create_drawing": "Drawing1",
            }[tool]
            self.state = {
                "stateVersion": (
                    (self.state or {}).get("stateVersion", 1) + 1
                ),
                "activeDocument": title,
                "activeDocumentPath": "",
                "documentType": document_type,
                "activeSketch": None,
                "features": [],
                "dimensions": [],
                "isDirty": False,
            }
        elif tool == "close_document":
            expected_path = params.get("expected_path", "")
            expected_title = params.get("expected_title", "")
            expected_type = params.get("expected_type", "")
            active_path = (self.state or {}).get("activeDocumentPath", "")
            active_title = (self.state or {}).get("activeDocument", "")
            active_type = (self.state or {}).get("documentType", "")
            identity_mismatch = (
                (expected_path and expected_path.lower() != active_path.lower())
                or (
                    not expected_path
                    and expected_title
                    and expected_title.casefold() != active_title.casefold()
                )
                or (
                    expected_type
                    and expected_type.casefold() != active_type.casefold()
                )
            )
            if identity_mismatch:
                return {
                    "status": "FAILED",
                    "stateVersion": (self.state or {}).get(
                        "stateVersion", 1
                    ),
                    "cadState": copy.deepcopy(self.state),
                    "error": {
                        "code": "ACTIVE_DOCUMENT_CHANGED",
                        "message": "Refusing to close a different document.",
                    },
                }
            self.state = (
                self._document_stack.pop()
                if self._document_stack
                else None
            )
        elif tool == "save_document" and self.state is not None:
            file_path = params.get("file_path") or self.state.get(
                "activeDocumentPath", ""
            )
            if file_path:
                Path(file_path).parent.mkdir(parents=True, exist_ok=True)
                Path(file_path).write_bytes(b"saved-solidworks-document")
                self.state["activeDocumentPath"] = str(Path(file_path))
                self.state["activeDocument"] = Path(file_path).name

        return {
            "status": "COMPLETED",
            "stateVersion": (self.state or {}).get("stateVersion", 1),
            "cadState": copy.deepcopy(self.state),
            "error": None,
        }

    def begin_transaction(self, name):
        response = self.call("begin_undo_scope", {"name": name})
        if response.get("status") == "COMPLETED":
            self._undo_state = copy.deepcopy(self.state)
        return response

    def commit_transaction(self, name="DSH SolidWorks operation"):
        response = self.call("commit_undo_scope", {"name": name})
        if response.get("status") == "COMPLETED":
            self._undo_state = None
        return response

    def rollback_transaction(self):
        response = self.call("rollback_undo_scope", {})
        if response.get("status") == "COMPLETED" and self._undo_state is not None:
            self.state = copy.deepcopy(self._undo_state)
            self._undo_state = None
        return response

    def run_feature_graph(self, graph, fresh_document):
        self.events.append(
            (
                "run_feature_graph",
                {"graph": copy.deepcopy(graph), "fresh_document": fresh_document},
            )
        )
        if self.feature_graph_handler:
            return self.feature_graph_handler(graph, fresh_document)
        return {
            "status": "COMPLETED",
            "stateVersion": self.state.get("stateVersion", 1),
            "cadState": copy.deepcopy(self.state),
            "compiler": {
                "nodes_completed": len(graph.get("nodes") or []),
                "node_log": [
                    {"id": node.get("id"), "status": "COMPLETED"}
                    for node in graph.get("nodes") or []
                ],
            },
            "error": None,
        }


@pytest.fixture
def cad_env(tmp_path, monkeypatch):
    source = tmp_path / "part.SLDPRT"
    source.write_bytes(b"solidworks-test-document")
    state = {
        "stateVersion": 1,
        "activeDocument": source.name,
        "activeDocumentPath": str(source),
        "documentType": "PART",
        "activeSketch": None,
        "features": [],
        "dimensions": [],
        "isDirty": False,
    }
    fake_backend = FakeBackend(state)
    store = JobStore(tmp_path / "jobs")
    backup_root = tmp_path / "backups"

    monkeypatch.setattr(orchestrator_module, "backend", fake_backend)
    monkeypatch.setattr(orchestrator_module, "job_store", store)
    monkeypatch.setattr(orchestrator_module, "BACKUP_ROOT", backup_root)

    orchestrator = orchestrator_module.CadOrchestrator()
    job, token = store.create(
        "job-1", "test CAD job", orchestrator._identity(state)
    )
    job["last_state"] = dict(state)
    store.save(job)
    return SimpleNamespace(
        orchestrator=orchestrator,
        backend=fake_backend,
        store=store,
        job=job,
        token=token,
        source=source,
        backup_root=backup_root,
        module=orchestrator_module,
    )


@pytest.fixture
def unbound_env(tmp_path, monkeypatch):
    """A job started while NO document is active: expected_document is empty."""
    fake_backend = FakeBackend({})
    fake_backend.state = None
    store = JobStore(tmp_path / "jobs")
    backup_root = tmp_path / "backups"

    monkeypatch.setattr(orchestrator_module, "backend", fake_backend)
    monkeypatch.setattr(orchestrator_module, "job_store", store)
    monkeypatch.setattr(orchestrator_module, "BACKUP_ROOT", backup_root)

    orchestrator = orchestrator_module.CadOrchestrator()
    started = orchestrator.begin_job("test CAD job with no active document")
    job = store.load(started["job_id"], started["job_token"])
    return SimpleNamespace(
        orchestrator=orchestrator,
        backend=fake_backend,
        store=store,
        job=job,
        job_id=started["job_id"],
        token=started["job_token"],
        backup_root=backup_root,
        module=orchestrator_module,
    )
