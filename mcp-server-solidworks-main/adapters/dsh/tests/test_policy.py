import json

import pytest

from policy import (
    canonical_json,
    expected_output_paths,
    same_document,
    validate_drawing_workflow,
    validate_edits,
    validate_save_or_export_payload,
)


def test_edit_feature_action_and_rename_are_validated():
    with pytest.raises(ValueError, match="action must be one of"):
        validate_edits(
            [
                {
                    "tool": "edit_feature",
                    "params": {"feature_name": "Boss-Extrude1", "action": "drop"},
                }
            ]
        )

    with pytest.raises(ValueError, match="new_name"):
        validate_edits(
            [
                {
                    "tool": "edit_feature",
                    "params": {
                        "feature_name": "Boss-Extrude1",
                        "action": "rename",
                    },
                }
            ]
        )


def test_toolbox_and_exploded_view_edits_are_allowlisted_and_validated():
    edits = validate_edits(
        [
            {
                "tool": "insert_toolbox_component",
                "params": {
                    "standard": "gb",
                    "part_number": "GB/T 5780 M8x30",
                    "configuration": "M8x30",
                    "x": 0.01,
                    "y": 0.02,
                    "z": 0.03,
                    "fixed": False,
                },
            },
            {
                "tool": "create_exploded_view",
                "params": {"view_name": "ExplView1"},
            },
        ]
    )
    assert [edit["tool"] for edit in edits] == [
        "insert_toolbox_component",
        "create_exploded_view",
    ]

    with pytest.raises(ValueError, match="part_number"):
        validate_edits(
            [{"tool": "insert_toolbox_component", "params": {}}]
        )


@pytest.mark.parametrize(
    ("operation", "kwargs", "message"),
    [
        ("export", {"file_path": "", "format": "STEP"}, "requires file_path"),
        (
            "batch_export",
            {"file_path": "D:/out/part", "formats_json": "not-json"},
            "valid JSON",
        ),
        (
            "close",
            {"file_path": "D:/out/part.step"},
            "does not accept file_path",
        ),
        (
            "close",
            {"save_before_close": True, "close_all": True},
            "always discards",
        ),
    ],
)
def test_save_export_input_validation(operation, kwargs, message):
    payload = {
        "operation": operation,
        "file_path": "",
        "format": "",
        "formats_json": "[]",
        "save_before_close": False,
        "close_all": False,
    }
    payload.update(kwargs)
    with pytest.raises(ValueError, match=message):
        validate_save_or_export_payload(
            payload,
            {
                "activeDocument": "part.SLDPRT",
                "activeDocumentPath": "D:/parts/part.SLDPRT",
            },
        )


def test_first_save_requires_path_for_unsaved_document():
    with pytest.raises(ValueError, match="first save"):
        validate_save_or_export_payload(
            {
                "operation": "save",
                "file_path": "",
                "format": "",
                "formats_json": "[]",
                "save_before_close": False,
                "close_all": False,
            },
            {"activeDocument": "Part1", "activeDocumentPath": ""},
        )


def test_batch_formats_are_normalized_and_mapped_to_expected_paths():
    payload = validate_save_or_export_payload(
        {
            "operation": "batch_export",
            "file_path": "D:/out/part",
            "format": "",
            "formats_json": '["step", "IGES", "STL", "PDF", "DWG", "DXF"]',
            "save_before_close": False,
            "close_all": False,
        },
        {
            "activeDocumentPath": "D:/drawings/part.SLDDRW",
            "documentType": "DRAWING",
        },
    )
    assert json.loads(payload["formats_json"]) == [
        "STEP",
        "IGES",
        "STL",
        "PDF",
        "DWG",
        "DXF",
    ]
    assert expected_output_paths(payload, {}) == [
        "D:/out/part.step",
        "D:/out/part.igs",
        "D:/out/part.stl",
        "D:/out/part.pdf",
        "D:/out/part.dwg",
        "D:/out/part.dxf",
    ]


def test_drawing_literal_fields_are_validated():
    with pytest.raises(ValueError, match="view_type"):
        validate_drawing_workflow(
            {"views": [{"view_type": "perspective"}]}
        )


@pytest.mark.parametrize(
    ("workflow", "message"),
    [
        (
            {"flat_pattern": {"scale": 0}},
            "flat_pattern.scale",
        ),
        (
            {"flat_pattern": {"hide_bend_lines": "no"}},
            "hide_bend_lines",
        ),
        (
            {
                "section_views": [
                    {"edge_x": 0.1, "edge_y": 0.2, "px": 0.3}
                ]
            },
            "py is required",
        ),
        (
            {
                "section_views": [
                    {
                        "edge_x": 0.1,
                        "edge_y": 0.2,
                        "x1": 0.0,
                        "y1": 0.0,
                        "x2": 0.1,
                        "y2": 0.1,
                        "px": 0.3,
                        "py": 0.4,
                    }
                ]
            },
            "exactly one cut mode",
        ),
        (
            {
                "section_views": [
                    {
                        "x1": 0.1,
                        "y1": 0.2,
                        "x2": 0.1,
                        "y2": 0.2,
                        "px": 0.3,
                        "py": 0.4,
                    }
                ]
            },
            "endpoints must differ",
        ),
        (
            {"auto_dimensions": {"all_views": 1}},
            "auto_dimensions.all_views",
        ),
        (
            {"center_marks": {"unknown": True}},
            "unsupported fields",
        ),
    ],
)
def test_drawing_nested_fields_are_validated(workflow, message):
    with pytest.raises(ValueError, match=message):
        validate_drawing_workflow(workflow)


def test_valid_section_view_modes_are_accepted():
    edge = {
        "section_views": [
            {
                "edge_x": 0.1,
                "edge_y": 0.2,
                "px": 0.3,
                "py": 0.4,
                "label": "A",
                "flip": False,
            }
        ]
    }
    line = {
        "section_views": [
            {
                "x1": 0.1,
                "y1": 0.2,
                "x2": 0.3,
                "y2": 0.2,
                "px": 0.4,
                "py": 0.5,
                "scale": 0.75,
            }
        ]
    }
    assert validate_drawing_workflow(edge) == edge
    assert validate_drawing_workflow(line) == line


def test_extended_drawing_workflow_fields_are_validated():
    workflow = {
        "detail_views": [
            {
                "source_view": "Drawing View1",
                "center_x_ratio": 0.5,
                "center_y_ratio": 0.5,
                "radius_ratio": 0.2,
                "scale": 2.0,
                "label": "B",
            }
        ],
        "model_annotations": {
            "all_views": True,
            "eliminate_duplicates": True,
        },
        "bom": {
            "bom_type": "parts_only",
            "template_path": "D:/templates/bom.sldbomtbt",
        },
        "auto_balloons": {
            "view_name": "Drawing View1",
            "style": "hexagon",
        },
        "hole_table": {
            "view_name": "Drawing View1",
            "tag_style": "alphanumeric",
        },
        "standards_check": {
            "expected_view_count": 3,
            "require_dimensions": True,
            "require_resolved_references": True,
            "max_independent_scales": 1,
        },
    }
    assert validate_drawing_workflow(workflow) == workflow


@pytest.mark.parametrize(
    ("workflow", "message"),
    [
        (
            {"detail_views": [{"source_view": "", "radius_ratio": 0.2}]},
            "source_view",
        ),
        (
            {
                "detail_views": [
                    {"source_view": "View1", "center_x_ratio": 2}
                ]
            },
            "between 0 and 1",
        ),
        ({"bom": {"bom_type": "unknown"}}, "bom_type"),
        ({"auto_balloons": {"style": "cloud"}}, "style"),
        ({"hole_table": {"tag_style": "roman"}}, "tag_style"),
        (
            {"standards_check": {"expected_view_count": -1}},
            "non-negative integer",
        ),
    ],
)
def test_extended_drawing_workflow_rejects_invalid_values(
    workflow, message
):
    with pytest.raises(ValueError, match=message):
        validate_drawing_workflow(workflow)


def test_saved_document_identity_requires_the_path_to_remain_available():
    expected = {"title": "part.SLDPRT", "path": "D:/parts/part.SLDPRT"}
    assert not same_document(
        expected,
        {
            "activeDocument": "part.SLDPRT",
            "activeDocumentPath": "",
        },
    )


@pytest.mark.parametrize(
    ("operation", "kwargs", "message"),
    [
        (
            "save",
            {"file_path": "D:/out/part.sldasm"},
            "PART document",
        ),
        (
            "export",
            {"file_path": "D:/out/part.pdf", "format": "STEP"},
            "must end with",
        ),
        (
            "export",
            {"file_path": "D:/out/part.dxf", "format": "DXF"},
            "requires an active DRAWING",
        ),
        (
            "export",
            {"file_path": "D:/out/part.pdf", "format": "PDF"},
            "requires an active DRAWING",
        ),
        (
            "batch_export",
            {
                "file_path": "D:/out/part.step",
                "formats_json": '["STEP"]',
            },
            "extensionless",
        ),
        (
            "batch_export",
            {
                "file_path": "D:/out/part",
                "formats_json": '["STEP", "PDF"]',
            },
            "require an active DRAWING",
        ),
    ],
)
def test_output_paths_match_document_and_export_contract(
    operation, kwargs, message
):
    payload = {
        "operation": operation,
        "file_path": "",
        "format": "",
        "formats_json": "[]",
        "save_before_close": False,
        "close_all": False,
    }
    payload.update(kwargs)
    with pytest.raises(ValueError, match=message):
        validate_save_or_export_payload(
            payload,
            {
                "activeDocument": "part.SLDPRT",
                "activeDocumentPath": "D:/parts/part.SLDPRT",
                "documentType": "PART",
            },
        )


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_are_rejected(value):
    with pytest.raises(ValueError, match="finite number"):
        validate_edits(
            [
                {
                    "tool": "modify_dimension",
                    "params": {"name": "D1", "value": value},
                }
            ]
        )
    with pytest.raises(ValueError, match="finite number"):
        validate_edits(
            [
                {
                    "tool": "add_edge_feature",
                    "params": {
                        "feature_type": "fillet",
                        "radius_or_distance": value,
                        "ex": 0.0,
                        "ey": 0.0,
                        "ez": 0.0,
                    },
                }
            ]
        )


def test_huge_int_values_are_finite_and_never_overflow():
    # json.loads yields an int BEYOND float range for 10**400, and math.isfinite() raises
    # OverflowError on such an int. An int is always finite, so validation must accept it (or
    # reject it on its own merits) and never raise OverflowError.
    huge = 10 ** 400
    edits = validate_edits(
        [{"tool": "modify_dimension", "params": {"name": "D1", "value": huge}}]
    )
    assert edits[0]["params"]["value"] == huge
    # the positive-required path takes the same route (no OverflowError, value preserved)
    edits = validate_edits(
        [
            {
                "tool": "add_edge_feature",
                "params": {
                    "feature_type": "fillet",
                    "radius_or_distance": huge,
                    "ex": 0.0,
                    "ey": 0.0,
                    "ez": 0.0,
                },
            }
        ]
    )
    assert edits[0]["params"]["radius_or_distance"] == huge


def test_canonical_json_refuses_non_finite_numbers():
    assert canonical_json({"a": 1}) == '{"a":1}'
    with pytest.raises(ValueError):
        canonical_json({"a": float("nan")})
    with pytest.raises(ValueError):
        canonical_json({"a": float("inf")})


def test_edges_json_elements_must_be_numeric():
    with pytest.raises(ValueError, match=r"edges_json\[0\]\.ex"):
        validate_edits(
            [
                {
                    "tool": "add_edge_feature",
                    "params": {
                        "feature_type": "fillet",
                        "radius_or_distance": 0.001,
                        "edges_json": '[{"ex": "NOT_A_NUMBER", "ey": 0.0, "ez": 0.0}]',
                    },
                }
            ]
        )
    with pytest.raises(ValueError, match=r"edges_json\[1\]\.ez"):
        validate_edits(
            [
                {
                    "tool": "add_edge_feature",
                    "params": {
                        "feature_type": "fillet",
                        "radius_or_distance": 0.001,
                        "edges_json": '[{"ex": 0.0, "ey": 0.0, "ez": 0.0}, '
                        '{"ex": 0.0, "ey": 0.0, "ez": {}}]',
                    },
                }
            ]
        )


def test_edges_json_with_numeric_coordinates_is_accepted():
    edits = validate_edits(
        [
            {
                "tool": "add_edge_feature",
                "params": {
                    "feature_type": "fillet",
                    "radius_or_distance": 0.001,
                    "edges_json": '[{"ex": 0.0, "ey": 0.01, "ez": -0.02}]',
                },
            }
        ]
    )
    assert edits[0]["params"]["edges_json"].startswith("[")
