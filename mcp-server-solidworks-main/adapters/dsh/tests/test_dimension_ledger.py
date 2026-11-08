import unittest

from dimension_ledger import evaluate_graph_ledger, validate_graph_ledger


def graph_with(*dimensions):
    return {
        "units": "meters",
        "nodes": [{"id": "base", "type": "box", "width": .02, "depth": .02, "height": .02}],
        "dimension_ledger": list(dimensions),
    }


def dimension(dim_id, value, check, source="explicit", view="front", nodes=None, label=None):
    return {
        "id": dim_id,
        "label": label or dim_id,
        "value": value,
        "source_kind": source,
        "view": view,
        "node_ids": ["base"] if nodes is None else nodes,
        "check": check,
    }


def check_response(features):
    import json

    return {"status": "COMPLETED", "cadState": {"features": [json.dumps(features)]}}


def geometry_response(size):
    import json

    return {"status": "COMPLETED", "cadState": {"features": [
        "bodies=1", "faces=6", "edges=12", "vertices=8",
        "bbox_size=%s" % json.dumps(size),
    ]}}


class DimensionLedgerTests(unittest.TestCase):
    def test_optional_and_unknown_dimension_validate(self):
        graph = graph_with(dimension("D-unknown", None, None, source="unknown", nodes=[]))
        self.assertEqual(validate_graph_ledger(graph), [])
        self.assertEqual(evaluate_graph_ledger(graph, {}, None)[0]["code"], "LEDGER_UNMEASURED")

    def test_enforces_exact_seven_fields_and_graph_node_links(self):
        item = dimension("D-x", 20, {"kind": "bbox_extent", "axis": "x"})
        item["source_note"] = "not part of v1"
        self.assertTrue(any("fields must be exactly" in error for error in
                            validate_graph_ledger(graph_with(item))))
        item.pop("source_note")
        item["node_ids"] = ["missing"]
        self.assertTrue(any("node_ids" in error for error in validate_graph_ledger(graph_with(item))))

    def test_bbox_mismatch_is_warning(self):
        graph = graph_with(dimension("D-x", 30, {"kind": "bbox_extent", "axis": "x"}))
        verification = {"deep_checks": [geometry_response([0.02, 0.02, 0.02])]}
        warnings = evaluate_graph_ledger(graph, verification)
        self.assertEqual([w["code"] for w in warnings], ["LEDGER_MISMATCH"])
        self.assertEqual(warnings[0]["actual"], 20)

    def test_cylinder_uses_axis_line_and_diameter_from_face_readback(self):
        graph = graph_with(dimension("D-hole", 10, {
            "kind": "cylinder", "axis": [0, 0, 1], "through": [0.01, 0.01, 0],
        }))
        faces = check_response({"faces": [
            {"kind": "cylinder", "origin": [0.01, 0.01, -0.01],
             "axis": [0, 0, 1], "radius": 0.005},
        ]})
        self.assertEqual(evaluate_graph_ledger(graph, {}, faces), [])

    def test_coaxial_different_cylinders_are_unmeasured_not_guessed(self):
        graph = graph_with(dimension("D-hole", 10, {
            "kind": "cylinder", "axis": [0, 0, 1], "through": [0.01, 0.01, 0],
        }))
        faces = check_response({"faces": [
            {"kind": "cylinder", "origin": [0.01, 0.01, 0], "axis": [0, 0, 1], "radius": 0.005},
            {"kind": "cylinder", "origin": [0.01, 0.01, 0.01], "axis": [0, 0, -1], "radius": 0.008},
        ]})
        warnings = evaluate_graph_ledger(graph, {}, faces)
        self.assertEqual(warnings[0]["code"], "LEDGER_UNMEASURED")
        self.assertEqual(warnings[0]["reason"], "cylinder_candidate_count_2")

    def test_plane_distance_measures_parallel_face_offsets(self):
        graph = graph_with(dimension("D-thickness", 20, {
            "kind": "plane_distance", "normal": [1, 0, 0],
            "point_a": [0, 0, 0], "point_b": [0.02, 0, 0],
        }))
        faces = check_response({"faces": [
            {"planar": True, "normal": [1, 0, 0], "point": [0, 0.01, 0.01]},
            {"planar": True, "normal": [-1, 0, 0], "point": [0.02, 0.01, 0.01]},
        ]})
        self.assertEqual(evaluate_graph_ledger(graph, {}, faces), [])

    def test_derived_callout_is_metadata_not_an_expression_language(self):
        graph = graph_with(dimension("D-overall", 81, {"kind": "bbox_extent", "axis": "y"},
                                      source="derived", label="70+R11"))
        self.assertEqual(validate_graph_ledger(graph), [])
        graph["dimension_ledger"][0]["label"] = ""
        self.assertTrue(any("derived callout" in error for error in validate_graph_ledger(graph)))


if __name__ == "__main__":
    unittest.main()
