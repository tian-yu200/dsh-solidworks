import os
from pathlib import Path


ADAPTER_DIR = Path(__file__).resolve().parent
SOLIDPILOT_ROOT = ADAPTER_DIR.parents[1]
WORKSPACE_ROOT = SOLIDPILOT_ROOT.parent
CLAUDE_ADAPTER_DIR = ADAPTER_DIR.parent / "claude"

STATE_ROOT = Path(os.getenv("DSH_SOLIDWORKS_STATE_ROOT", WORKSPACE_ROOT / "state"))
JOB_ROOT = STATE_ROOT / "jobs"
BACKUP_ROOT = Path(os.getenv("DSH_SOLIDWORKS_BACKUP_ROOT", WORKSPACE_ROOT / "backups"))

RECIPE_PATH = SOLIDPILOT_ROOT / "cad-planner" / "recipe-usage.md"
FEATURE_GRAPH_SCHEMA_PATH = (
    SOLIDPILOT_ROOT / "cad-planner" / "contracts" / "feature-graph.schema.json"
)

ACTION_TTL_SECONDS = int(os.getenv("DSH_SOLIDWORKS_ACTION_TTL_SECONDS", "1800"))
BATCH_NODE_THRESHOLD = int(os.getenv("DSH_SOLIDWORKS_BATCH_NODE_THRESHOLD", "10"))
BATCH_EDIT_THRESHOLD = int(os.getenv("DSH_SOLIDWORKS_BATCH_EDIT_THRESHOLD", "5"))
