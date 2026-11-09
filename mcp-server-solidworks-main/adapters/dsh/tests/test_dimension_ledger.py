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


def edge_response(edges):
    return check_response({"edges": edges})


def square_edges(x):
    points = [[x, 0, 0], [x, .02, 0], [x, .02, .02], [x, 0, .02]]
    return [{"start": points[i], "end": points[(i + 1) % 4],
             "mid": [(points[i][j] + points[(i + 1) % 4][j]) / 2 for j in range(3)]}
            for i in range(4)]


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
            {"planar": True, "normal": [1, 0, 0], "point": [0, 0.01, 0.01], "area": .0004},
            {"planar": True, "normal": [-1, 0, 0], "point": [0.02, 0.01, 0.01], "area": .0004},
        ]})
        edges = edge_response(square_edges(0) + square_edges(.02))
        self.assertEqual(evaluate_graph_ledger(graph, {}, faces, edges), [])

    def test_plane_selector_must_land_in_face_or_within_half_mm_of_boundary(self):
        graph = graph_with(dimension("D-thickness", 20, {
            "kind": "plane_distance", "normal": [1, 0, 0],
            "point_a": [0, -.0004, .01], "point_b": [.02, -.0004, .01],
        }))
        faces = check_response({"faces": [
            {"planar": True, "normal": [1, 0, 0], "point": [0, .01, .01], "area": .0004},
            {"planar": True, "normal": [-1, 0, 0], "point": [.02, .01, .01], "area": .0004},
        ]})
        edges = edge_response(square_edges(0) + square_edges(.02))
        self.assertEqual(evaluate_graph_ledger(graph, {}, faces, edges), [])

        for point_y in (-.0006, .025):
            graph["dimension_ledger"][0]["check"]["point_a"][1] = point_y
            warnings = evaluate_graph_ledger(graph, {}, faces, edges)
            self.assertEqual(warnings[0]["code"], "LEDGER_UNMEASURED")
            self.assertEqual(warnings[0]["reason"], "plane_point_not_on_face")

    def test_plane_selector_inside_a_circular_hole_is_unmeasured(self):
        graph = graph_with(dimension("D-thickness", 20, {
            "kind": "plane_distance", "normal": [1, 0, 0],
            "point_a": [0, .01, .01], "point_b": [.02, .01, .01],
        }))
        faces = check_response({"faces": [
            {"planar": True, "normal": [1, 0, 0], "point": [0, .005, .005],
             "area": .0004 - 3.141592653589793 * .004 ** 2},
            {"planar": True, "normal": [-1, 0, 0], "point": [.02, .005, .005],
             "area": .0004 - 3.141592653589793 * .004 ** 2},
        ]})
        edges = square_edges(0) + square_edges(.02) + [
            {"kind": "circle", "center": [0, .01, .01], "normal": [1, 0, 0], "radius": .004},
            {"kind": "circle", "center": [.02, .01, .01], "normal": [-1, 0, 0], "radius": .004},
        ]

        warnings = evaluate_graph_ledger(graph, {}, faces, edge_response(edges))

        self.assertEqual(warnings[0]["code"], "LEDGER_UNMEASURED")
        self.assertEqual(warnings[0]["reason"], "plane_point_not_on_face")

    def test_plane_area_validation_handles_a_concentric_annular_face(self):
        outer, inner = .01, .004
        area = 3.141592653589793 * (outer ** 2 - inner ** 2)
        graph = graph_with(dimension("D-thickness", 20, {
            "kind": "plane_distance", "normal": [0, 0, 1],
            "point_a": [.007, 0, 0], "point_b": [.007, 0, .02],
        }))
        faces = check_response({"faces": [
            {"planar": True, "normal": [0, 0, 1], "point": [0, 0, 0], "area": area},
            {"planar": True, "normal": [0, 0, -1], "point": [0, 0, .02], "area": area},
        ]})
        edges = edge_response([
            {"kind": "circle", "center": [0, 0, 0], "normal": [0, 0, 1], "radius": outer},
            {"kind": "circle", "center": [0, 0, 0], "normal": [0, 0, 1], "radius": inner},
            {"kind": "circle", "center": [0, 0, .02], "normal": [0, 0, -1], "radius": outer},
            {"kind": "circle", "center": [0, 0, .02], "normal": [0, 0, -1], "radius": inner},
        ])

        self.assertEqual(evaluate_graph_ledger(graph, {}, faces, edges), [])

    def test_plane_region_accepts_coplanar_faces_with_a_shared_seam(self):
        graph = graph_with(dimension("D-thickness", 20, {
            "kind": "plane_distance", "normal": [1, 0, 0],
            "point_a": [0, .005, .01], "point_b": [.02, .005, .01],
        }))
        faces = check_response({"faces": [
            {"planar": True, "normal": [1, 0, 0], "point": [0, .005, .01], "area": .0002},
            {"planar": True, "normal": [1, 0, 0], "point": [0, .015, .01], "area": .0002},
            {"planar": True, "normal": [-1, 0, 0], "point": [.02, .005, .01], "area": .0002},
            {"planar": True, "normal": [-1, 0, 0], "point": [.02, .015, .01], "area": .0002},
        ]})
        edges = []
        for x in (0, .02):
            corners = [[x, 0, 0], [x, .01, 0], [x, .02, 0],
                       [x, .02, .02], [x, .01, .02], [x, 0, .02]]
            for start, end in ((0, 1), (1, 2), (2, 3), (3, 4), (4, 5), (5, 0), (1, 4)):
                a, b = corners[start], corners[end]
                edges.append({"start": a, "end": b,
                              "mid": [(a[i] + b[i]) / 2 for i in range(3)]})

        self.assertEqual(evaluate_graph_ledger(graph, {}, faces, edge_response(edges)), [])

    def test_plane_region_ignores_open_edges_but_keeps_area_guard(self):
        graph = graph_with(dimension("D-thickness", 20, {
            "kind": "plane_distance", "normal": [0, 1, 0],
            "point_a": [.01, 0, .01], "point_b": [.034, .02, .025],
        }))
        faces = check_response({"faces": [
            {"planar": True, "normal": [0, -1, 0], "point": [.01, 0, .01], "area": .0004},
            {"planar": True, "normal": [0, 1, 0], "point": [.034, .02, .025], "area": .00016},
        ]})
        edges = []
        for points in (
            [[0, 0, 0], [.02, 0, 0], [.02, 0, .02], [0, 0, .02]],
            [[.026, .02, .02], [.042, .02, .02], [.042, .02, .03], [.026, .02, .03]],
        ):
            edges.extend({"start": points[i], "end": points[(i + 1) % 4],
                          "mid": [(points[i][j] + points[(i + 1) % 4][j]) / 2 for j in range(3)]}
                         for i in range(4))
        # This open, disconnected edge is present in the SolidWorks y=.020 mm plane projection.
        edges.append({"start": [.007, .02, .01], "end": [.007, .02, 0],
                      "mid": [.007, .02, .005]})
        # Also retain the closed region when a multi-edge open branch meets its boundary once.
        edges.extend([
            {"start": [0, 0, 0], "end": [-.01, 0, 0], "mid": [-.005, 0, 0]},
            {"start": [-.01, 0, 0], "end": [-.02, 0, 0], "mid": [-.015, 0, 0]},
        ])

        self.assertEqual(evaluate_graph_ledger(graph, {}, faces, edge_response(edges)), [])

        graph["dimension_ledger"][0]["check"]["point_b"] = [.0256, .02, .025]
        self.assertEqual(evaluate_graph_ledger(graph, {}, faces, edge_response(edges)), [])
        graph["dimension_ledger"][0]["check"]["point_b"] = [.0255, .02, .025]
        self.assertEqual(evaluate_graph_ledger(graph, {}, faces, edge_response(edges)), [])
        graph["dimension_ledger"][0]["check"]["point_b"] = [.025499999, .02, .025]
        warnings = evaluate_graph_ledger(graph, {}, faces, edge_response(edges))
        self.assertEqual(warnings[0]["reason"], "plane_point_not_on_face")
        graph["dimension_ledger"][0]["check"]["point_b"] = [.034, .02, .025]

        faces = check_response({"faces": [
            {"planar": True, "normal": [0, -1, 0], "point": [.01, 0, .01], "area": .0004},
            {"planar": True, "normal": [0, 1, 0], "point": [.034, .02, .025], "area": .00015},
        ]})
        warnings = evaluate_graph_ledger(graph, {}, faces, edge_response(edges))
        self.assertEqual(warnings[0]["code"], "LEDGER_UNMEASURED")
        self.assertEqual(warnings[0]["reason"], "plane_boundary_unavailable")

    def test_plane_region_uses_open_arc_midpoints_for_rounded_boundaries(self):
        radius, area = .01, 3.141592653589793 * .01 ** 2 / 2
        graph = graph_with(dimension("D-thickness", 20, {
            "kind": "plane_distance", "normal": [0, 0, 1],
            "point_a": [0, .003, 0], "point_b": [0, .003, .02],
        }))
        faces = check_response({"faces": [
            {"planar": True, "normal": [0, 0, 1], "point": [0, .005, 0], "area": area},
            {"planar": True, "normal": [0, 0, -1], "point": [0, .005, .02], "area": area},
        ]})
        edges = []
        for z in (0, .02):
            edges.extend([
                {"start": [-radius, 0, z], "end": [radius, 0, z], "mid": [0, 0, z]},
                {"start": [radius, 0, z], "end": [-radius, 0, z], "mid": [0, radius, z]},
            ])

        self.assertEqual(evaluate_graph_ledger(graph, {}, faces, edge_response(edges)), [])

        graph["dimension_ledger"][0]["check"]["point_a"][1] = -.003
        warnings = evaluate_graph_ledger(graph, {}, faces, edge_response(edges))
        self.assertEqual(warnings[0]["code"], "LEDGER_UNMEASURED")
        self.assertEqual(warnings[0]["reason"], "plane_point_not_on_face")

    def test_plane_distance_is_unmeasured_when_boundary_readback_is_missing(self):
        graph = graph_with(dimension("D-thickness", 20, {
            "kind": "plane_distance", "normal": [1, 0, 0],
            "point_a": [0, .01, .01], "point_b": [.02, .01, .01],
        }))
        faces = check_response({"faces": [
            {"planar": True, "normal": [1, 0, 0], "point": [0, .01, .01], "area": .0004},
            {"planar": True, "normal": [-1, 0, 0], "point": [.02, .01, .01], "area": .0004},
        ]})

        warnings = evaluate_graph_ledger(graph, {}, faces)

        self.assertEqual(warnings[0]["code"], "LEDGER_UNMEASURED")
        self.assertEqual(warnings[0]["reason"], "edges_unavailable")

    def test_derived_callout_is_metadata_not_an_expression_language(self):
        graph = graph_with(dimension("D-overall", 81, {"kind": "bbox_extent", "axis": "y"},
                                      source="derived", label="70+R11"))
        self.assertEqual(validate_graph_ledger(graph), [])
        graph["dimension_ledger"][0]["label"] = ""
        self.assertTrue(any("derived callout" in error for error in validate_graph_ledger(graph)))


if __name__ == "__main__":
    unittest.main()
