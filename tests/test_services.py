from __future__ import annotations

import unittest

from backend.services.llm_decision import ALLOWED_ACTIONS, build_decision_cards
from backend.services.mechanical_calc import (
    discharge_trend,
    gate_backflow_check,
    release_trend,
    water_level_risk,
)
from backend.services.simulation_engine import run_simulation
from backend.services.validation_engine import compare_predictions_to_events


class MechanicalCalcTests(unittest.TestCase):
    def test_water_level_missing_reference_is_not_available(self) -> None:
        self.assertEqual(water_level_risk(None, 1)["status"], "not_available")
        self.assertEqual(water_level_risk(1, 0)["status"], "not_available")

    def test_water_level_boundaries(self) -> None:
        self.assertEqual(water_level_risk(69, 100)["status"], "normal")
        self.assertEqual(water_level_risk(70, 100)["status"], "watch")
        self.assertEqual(water_level_risk(90, 100)["status"], "danger")
        self.assertEqual(water_level_risk(100, 100)["status"], "critical")

    def test_trends_do_not_infer_causality(self) -> None:
        self.assertEqual(discharge_trend(2, 1)["status"], "increasing")
        self.assertEqual(release_trend(2, 1)["interpretation"], "downstream_effect_review_required")
        self.assertEqual(gate_backflow_check(2, 1)["recommended_action"], "keep_or_check_closed")


class StrictSimulationTests(unittest.TestCase):
    def test_unsourced_actual_rainfall_is_excluded(self) -> None:
        result = run_simulation(
            {
                "scenario_id": "strict_check",
                "mode": "observed",
                "rainfall_observations": [
                    {
                        "value": 300,
                        "unit": "mm",
                        "observed_at": "2023-07-15T00:00:00+09:00",
                        "source_name": "",
                        "source_url": "",
                        "source_type": "",
                    }
                ],
                "hydraulic_assets": [],
            },
            strict_data_mode=True,
        )
        self.assertIn("usable_rainfall_observations_missing", result["data_gaps"])
        self.assertEqual(result["rainfall_summary"]["count"], 0)

    def test_no_assets_means_no_map_predictions(self) -> None:
        result = run_simulation({"scenario_id": "empty", "mode": "observed"}, strict_data_mode=True)
        self.assertEqual(result["overall_risk"], "unknown")
        self.assertIn("hydraulic_assets_missing", result["data_gaps"])

    def test_decision_cards_only_use_allowed_actions(self) -> None:
        result = run_simulation({"scenario_id": "empty", "mode": "observed"}, strict_data_mode=True)
        cards = build_decision_cards(result)
        for action in cards["actions"]:
            self.assertIn(action["action"], ALLOWED_ACTIONS)
            self.assertTrue(action["source_basis"])

    def test_validation_rejects_unsourced_labels(self) -> None:
        comparison = compare_predictions_to_events(
            {"predictions": []},
            {"validation_events": [{"event_id": "label_without_source"}]},
        )
        self.assertEqual(comparison["valid_event_count"], 0)
        self.assertEqual(comparison["rejected_event_count"], 1)


if __name__ == "__main__":
    unittest.main()

