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

DEFAULT_MATERIAL_LIBRARY = "SolidWorks Materials"   # the execution layer's own default
KNOWN_MATERIALS = frozenset(
    {"1060 Alloy", "AISI 1020", "ABS", "Nylon 6/10", "Gray Cast Iron", "Refused Material"}
)
# Present in the library but refused by the setter: MATERIAL_NOT_APPLIED, which stays fatal.
UNAPPLICABLE_MATERIALS = frozenset({"Refused Material"})


class FakeBackend:
    def __init__(self, state):
        self.state = dict(state)
        self.calls = []
        self.events = []
        self.call_handler = None
        self.feature_graph_handler = None
        self.cheap_probes = []
        # The materials the fake library contains, what the setter refuses even though it is
        # present, and what the part already carries. Mirrors the live SetPartMaterial: a name that
        # is positively absent fails MATERIAL_NOT_FOUND, a present-but-refused one fails
        # MATERIAL_NOT_APPLIED, and every response reports the material actually on the part.
        self.materials = set(KNOWN_MATERIALS)
        self.unapplicable = set(UNAPPLICABLE_MATERIALS)
        self.material_on_part = None
        self.material_library_on_part = None
        # When True, the /api/tool/identity endpoint is treated as missing/failed and
        # identity() exercises the same verify_state fallback the real backend uses.
        self.identity_endpoint_down = False
        self._document_stack = []
        self._undo_state = None

    def _material_failure_code(self, name, library):
        """None when the fake applies it, else the code the live layer would answer with."""
        if not name or name not in self.materials:
            return "MATERIAL_NOT_FOUND"
        if name in self.unapplicable:
            return "MATERIAL_NOT_APPLIED"
        self.material_on_part = name
        self.material_library_on_part = library or DEFAULT_MATERIAL_LIBRARY
        return None

    def _material_features(self):
        if not self.material_on_part:
            return ["material=<none>", "library=<none>"]
        return ["material=%s" % self.material_on_part,
                "library=%s" % self.material_library_on_part]

    def _material_rejection(self, params):
        """The FAILED response for a material the fake cannot apply, else None.

        The state_version does NOT move on this failure and nothing is applied — exactly like
        SetPartMaterial — and cadState.features carries the material still on the part."""
        name = params.get("material_name")
        library = params.get("library") or DEFAULT_MATERIAL_LIBRARY
        code = self._material_failure_code(name, library)
        if code is None:
            return None
        reason = ("not present in the material database"
                  if code == "MATERIAL_NOT_FOUND"
                  else "present in the database but not applied by the setter")
        return {
            "status": "FAILED",
            "stateVersion": (self.state or {}).get("stateVersion", 1),
            "cadState": copy.deepcopy(self.state),
            "error": {
                "code": code,
                "message": "Material '%s' was not applied: %s ('%s', library '%s'). The part now "
                           "carries '%s'." % (name, reason, name, library,
                                              self.material_on_part or "<none>"),
            },
        }

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

    def current_state_version(self):
        """GET /api/tool/state stand-in: the counter only, no CAD state (SPEED task C)."""
        self.events.append(("current_state_version", {}))
        self.cheap_probes.append("current_state_version")
        return (self.state or {}).get("stateVersion", 1)

    def probe_identity(self):
        """GET /health stand-in: attach state + active TITLE + counter (no path/type)."""
        self.events.append(("probe_identity", {}))
        self.cheap_probes.append("probe_identity")
        return {
            "comAttached": True,
            "stateVersion": (self.state or {}).get("stateVersion", 1),
            "activeDocument": (self.state or {}).get("activeDocument"),
        }

    def identity(self):
        """GET /api/tool/identity stand-in: the identity subset of verify_state, no walk.

        Same response SHAPE as verify_state, but cadState carries ONLY the identity fields
        the C# endpoint reports (stateVersion, activeDocument, activeDocumentPath,
        documentType, isDirty) — so a test that passes while reading CAD content from an
        identity response would be lying. Mirrors the real backend's fallback: when
        identity_endpoint_down is set, the full verify_state runs instead.
        """
        self.events.append(("identity", {}))
        if self.identity_endpoint_down:
            self.cheap_probes.append("identity_fallback_verify_state")
            return self.verify_state()
        self.cheap_probes.append("identity")
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
        state_version = self.state.get("stateVersion", 1)
        return {
            "status": "COMPLETED",
            "stateVersion": state_version,
            "cadState": {
                "stateVersion": state_version,
                "activeDocument": self.state.get("activeDocument"),
                "activeDocumentPath": self.state.get("activeDocumentPath"),
                "documentType": self.state.get("documentType"),
                "isDirty": self.state.get("isDirty"),
            },
            "error": None,
        }

    def call(self, tool, params):
        self.calls.append((tool, copy.deepcopy(params)))
        self.events.append((tool, copy.deepcopy(params)))
        if self.call_handler:
            response = self.call_handler(tool, params)
            if response is not None:
                return response

        if tool == "set_part_material":
            rejected = self._material_rejection(params)
            if rejected is not None:
                return rejected

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
        nodes = graph.get("nodes") or []
        material = graph.get("material")
        warning = None
        fatal = None
        node_log = [{"id": node.get("id"), "status": "COMPLETED"} for node in nodes]
        if material:
            name = material.get("name")
            library = material.get("library") or DEFAULT_MATERIAL_LIBRARY
            rejected = self._material_rejection({"material_name": name, "library": library})
            code = rejected["error"]["code"] if rejected is not None else None
            if code is None:
                node_log.append({"id": "_material", "type": "material", "status": "COMPLETED"})
            elif code == "MATERIAL_NOT_FOUND":
                # The one non-fatal case: geometry ran, the material did not, and the warning says
                # what is ACTUALLY on the part (which may be a pre-existing material).
                warning = {
                    "code": "MATERIAL_NOT_FOUND",
                    "step": "set_part_material",
                    "material": name,
                    "library": library,
                    "material_on_part": self.material_on_part,
                    "library_on_part": self.material_library_on_part,
                    "message": rejected["error"]["message"],
                    "hint": "apply an exact library name or add the material to the library",
                }
                node_log.append({"id": "_material", "type": "material", "status": "WARNING",
                                 "warning": warning})
            else:
                # MATERIAL_NOT_APPLIED is fatal: the whole run reports FAILED, so the caller rolls
                # back exactly as it does for any other node failure.
                fatal = {
                    "code": code,
                    "message": rejected["error"]["message"],
                    "node_id": "_material",
                    "node_type": "material",
                    "step": "set_part_material",
                }
                node_log.append({"id": "_material", "type": "material", "status": "FAILED"})
        if fatal is not None:
            return {
                "status": "FAILED",
                "stateVersion": self.state.get("stateVersion", 1),
                "cadState": copy.deepcopy(self.state),
                "compiler": {
                    "nodes_completed": len(nodes),
                    "node_log": node_log,
                    "warnings": [],
                },
                "error": fatal,
            }
        return {
            "status": "COMPLETED",
            "stateVersion": self.state.get("stateVersion", 1),
            "cadState": copy.deepcopy(self.state),
            "compiler": {
                "nodes_completed": len(nodes),
                "node_log": node_log,
                "warnings": [warning] if warning else [],
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
    job["document_write_allowed"] = True  # fixture models an explicitly targeted document
    job["explicit_target_document"] = True
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
