import os
from pathlib import Path


ADAPTER_DIR = Path(__file__).resolve().parent
SOLIDPILOT_ROOT = ADAPTER_DIR.parents[1]
WORKSPACE_ROOT = SOLIDPILOT_ROOT.parent
CLAUDE_ADAPTER_DIR = ADAPTER_DIR.parent / "claude"

STATE_ROOT = Path(os.getenv("DSH_SOLIDWORKS_STATE_ROOT", WORKSPACE_ROOT / "state"))
JOB_ROOT = STATE_ROOT / "jobs"
BACKUP_ROOT = Path(os.getenv("DSH_SOLIDWORKS_BACKUP_ROOT", WORKSPACE_ROOT / "backups"))

# read_drawing_region output: one folder per source drawing (gitignored, under artifacts/).
INTAKE_ROOT = Path(os.getenv("DSH_SOLIDWORKS_INTAKE_ROOT", WORKSPACE_ROOT / "artifacts" / "intake"))

RECIPE_PATH = SOLIDPILOT_ROOT / "cad-planner" / "recipe-usage.md"
FEATURE_GRAPH_SCHEMA_PATH = (
    SOLIDPILOT_ROOT / "cad-planner" / "contracts" / "feature-graph.schema.json"
)

ACTION_TTL_SECONDS = int(os.getenv("DSH_SOLIDWORKS_ACTION_TTL_SECONDS", "1800"))

# Cheap identity probe (GET /api/tool/identity, served by ToolController.Identity). Read-only,
# no state_version check — the same budget family as /health and /state.
EXECUTION_BASE_URL = os.getenv("EXECUTION_BASE_URL", "http://localhost:5000")
IDENTITY_ENDPOINT = f"{EXECUTION_BASE_URL}/api/tool/identity"
IDENTITY_TIMEOUT_SECONDS = float(os.getenv("HTTP_TIMEOUT", "30"))
BATCH_NODE_THRESHOLD = int(os.getenv("DSH_SOLIDWORKS_BATCH_NODE_THRESHOLD", "10"))
BATCH_EDIT_THRESHOLD = int(os.getenv("DSH_SOLIDWORKS_BATCH_EDIT_THRESHOLD", "5"))
