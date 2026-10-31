"""Prompt-contract regressions, not a substitute for a real model/drawing evaluation."""
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[4]
SKILL = ROOT / ".dsh/skills/solidworks-cad"


@pytest.mark.parametrize("name", ["SKILL.md", "references/drawing-intake.md", "references/reconstruction-reasoning.md"])
def test_source_classes_and_no_pixel_fallback(name):
    text = (SKILL / name).read_text(encoding="utf-8")
    for token in ["EXPLICIT", "DERIVED", "USER", "UNKNOWN", "pixel", "contradiction"]:
        assert token in text
    for token in ["formula", "接受/继续"]:
        assert token in text
    for obsolete in ["reset_budget=true", "REGION_ALREADY_READ", "INTAKE_BATCH_REQUIRED", "Round 0", "proportion ONLY"]:
        assert obsolete not in text


@pytest.mark.parametrize("scenario,evidence", [
    ("dimension-chain-derived-no-question", "DERIVED = 90−2×10"),
    ("concentric-center-derived-no-question", "concentric hole needs no separate center"),
    ("tangent-radius-center-derived-no-question", "R10 tangent to two resolved perpendicular lines"),
    ("missing-slot-offset-requires-answer", "UNKNOWN: ask for its offset"),
    ("small-fillet-must-not-be-omitted", "Never drop it"),
    ("generic-approval-is-not-a-number", "not an absent numeric value"),
])
def test_intake_examples_protect_question_boundary(scenario, evidence):
    text = (SKILL / "references/drawing-intake.md").read_text(encoding="utf-8")
    assert evidence in text
    assert "ONE ask_user_question" in text
    assert "no hard limit" in text


def test_model_crop_schema_has_no_reset_or_task_identity_override():
    import ast
    source = (Path(__file__).resolve().parents[1] / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "read_drawing_region")
    names = {arg.arg for arg in fn.args.args}
    assert names == {"image_path", "region", "zoom", "regions"}
    doc = ast.get_docstring(fn) or ""
    assert "Optional" in doc and "reset" in doc and "no-progress" in doc
    text_fn = next(node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "read_drawing_text")
    assert "Optional" in (ast.get_docstring(text_fn) or "")
