"""Unit tests for `benchmarks/evaluate_benchmark.py` and `benchmarks/manifest.json`."""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parents[1]
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

import evaluate_benchmark as eb  # noqa: E402


class BenchmarkSuiteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.manifest_path = BENCH_DIR / "manifest.json"
        cls.manifest = eb.load_json(cls.manifest_path)

    def test_manifest_validates_cleanly(self) -> None:
        errors = eb.validate_manifest(self.manifest)
        self.assertEqual(errors, [])

    def test_manifest_rejects_missing_category_or_freeform_exclusion(self) -> None:
        broken = copy.deepcopy(self.manifest)
        broken["category_taxonomy"].pop("sheet_metal")
        broken["excluded_categories"] = []
        errors = eb.validate_manifest(broken)
        self.assertTrue(any("sheet_metal" in e for e in errors))
        self.assertTrue(any("freeform_surface" in e for e in errors))

    def test_manifest_rejects_identical_editability_values(self) -> None:
        broken = copy.deepcopy(self.manifest)
        broken["cases"][0]["editability_checks"][0]["to_mm"] = broken["cases"][0]["editability_checks"][0]["from_mm"]
        errors = eb.validate_manifest(broken)
        self.assertTrue(any("from_mm and to_mm must differ" in e for e in errors))

    def test_bbox_and_volume_tolerance_rules(self) -> None:
        # Axis-order invariant & within max(1mm, 2%)
        self.assertTrue(eb.check_bbox_match([37.0, 12.0, 12.0], [12.0, 37.5, 12.4]))
        self.assertFalse(eb.check_bbox_match([37.0, 12.0, 12.0], [12.0, 40.0, 12.0]))
        self.assertIsNone(eb.check_bbox_match([37.0, 12.0, 12.0], None))

        self.assertTrue(eb.check_volume_match(1934.63, 1945.0, tol_ratio=0.01))
        self.assertFalse(eb.check_volume_match(1934.63, 2000.0, tol_ratio=0.01))

    def test_batch_build_json_and_log_ingestion(self) -> None:
        case_map = {c["id"]: c for c in self.manifest["cases"]}
        log_path = BENCH_DIR / "fixtures" / "sample_batch_build.log"
        log_runs = eb.parse_batch_build_log(log_path.read_text(encoding="utf-8"), case_map)
        self.assertEqual(len(log_runs), 2)
        self.assertEqual(log_runs[0]["case_id"], "housing_valve_ht200")
        self.assertIn("MATERIAL_NOT_FOUND", log_runs[0]["error_codes"])
        self.assertEqual(log_runs[1]["case_id"], "shaft_stepped_m5")

        raw_batch_json = [
            {
                "file": "D:/dsh-solidwork/artifacts/batch/shaft_stepped_m5_2.SLDPRT",
                "ok": True,
                "timing": {"begin": 2.0, "build": 10.0, "save": 0.6, "finish": 0.4},
                "verification": "passed",
                "trust": "verified",
                "warnings": [],
            }
        ]
        _, norm = eb.normalize_runs_payload(raw_batch_json, case_map)
        self.assertEqual(len(norm), 1)
        self.assertEqual(norm[0]["case_id"], "shaft_stepped_m5")
        self.assertAlmostEqual(norm[0]["t_ai_minutes"], round(13.0 / 60.0, 3))

    def test_north_star_and_editability_verdicts(self) -> None:
        runs = [
            {
                "case_id": "shaft_stepped_m5",
                "vision_verified": True,
                "dimensions_total": 7,
                "dimensions_correct": 7,
                "first_pass_build_ok": True,
                "final_delivery_ok": True,
                "measured_bbox_mm": [37.0, 12.0, 12.0],
                "measured_volume_mm3": 1934.63,
                "t_ai_minutes": 3.0,
                "t_edit_minutes": 1.0,
                "editability_passed": False,
            },
            {
                "case_id": "plate_notched_block",
                "vision_verified": True,
                "dimensions_total": 5,
                "dimensions_correct": 5,
                "first_pass_build_ok": True,
                "final_delivery_ok": True,
                "measured_bbox_mm": [30.0, 30.0, 42.0],
                "measured_volume_mm3": 23700.0,
                "t_ai_minutes": 7.0,
                "t_edit_minutes": 2.5,
                "editability_passed": True,
            },
        ]
        res = eb.evaluate_runs(self.manifest, runs)
        rows = {r["case_id"]: r for r in res["rows"]}
        # Fast & geometrically valid, but fragile tree -> FRAGILE_TREE
        self.assertTrue(rows["shaft_stepped_m5"]["verdict"].startswith("FRAGILE_TREE"))
        # 9.5 min vs 10.0 min human baseline -> ratio 0.95 > 0.80 -> NEGATIVE_ROI
        self.assertTrue(rows["plate_notched_block"]["verdict"].startswith("NEGATIVE_ROI"))

    def test_cli_self_check_returns_zero(self) -> None:
        rc = eb.main(["--self-check"])
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
