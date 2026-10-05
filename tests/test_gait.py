import unittest

from music_companion.gait import build_walking_guidance
from music_companion.ai_client import _merge_with_local
from music_companion.recommender import RecommendationError, recommend


class GaitGuidanceTests(unittest.TestCase):
    def test_measured_step_length_has_priority_and_explains_formula(self):
        guidance = build_walking_guidance(
            height_cm=170,
            leg_length_cm=80,
            measured_step_length_cm=62,
            target_speed_kmh=4,
        )
        self.assertEqual(guidance["method"], "measured_step_length_cm")
        self.assertAlmostEqual(guidance["step_length_cm"], 62)
        self.assertAlmostEqual(guidance["cadence"], 4 * 1000 / (60 * 0.62), places=2)
        self.assertTrue(guidance["achievable"])
        self.assertAlmostEqual(guidance["estimated_speed"], 4, places=5)
        self.assertTrue(any("每一步" in item for item in guidance["assumptions"]))

    def test_target_speed_is_clamped_without_claiming_target_achieved(self):
        guidance = build_walking_guidance(
            measured_step_length_cm=50,
            target_speed_kmh=7,
        )
        self.assertEqual(guidance["cadence"], 140)
        self.assertFalse(guidance["achievable"])
        self.assertLess(guidance["estimated_speed"], 7)
        self.assertIn("80–140", " ".join(guidance["limitations"]))

    def test_leg_length_then_height_heuristics_are_transparent(self):
        leg = build_walking_guidance(leg_length_cm=80, fallback_cadence=112)
        self.assertEqual(leg["method"], "leg_length_heuristic")
        self.assertAlmostEqual(leg["step_length_cm"], 56)
        self.assertIsNone(leg["achievable"])
        height = build_walking_guidance(height_cm=170, fallback_cadence=112)
        self.assertEqual(height["method"], "height_heuristic")
        self.assertAlmostEqual(height["step_length_cm"], 170 * 0.415)
        self.assertTrue(height["warnings"])

    def test_recommendation_exposes_walking_guidance_and_keeps_bpm_in_range(self):
        result = recommend(
            {
                "scene": "walking",
                "exercise": "快走",
                "target_speed_kmh": 7,
                "step_length_cm": 50,
            },
            ai_client=None,
        )
        guidance = result["recommendation"]["walking_guidance"]
        self.assertFalse(guidance["achievable"])
        self.assertGreaterEqual(result["recommendation"]["target_bpm"], result["recommendation"]["bpm_min"])
        self.assertLessEqual(result["recommendation"]["target_bpm"], result["recommendation"]["bpm_max"])
        self.assertEqual(result["profile"]["walking_guidance"], guidance)

    def test_target_speed_range_is_validated(self):
        with self.assertRaises(RecommendationError):
            recommend({"mood": "平静", "target_speed_kmh": 8}, ai_client=None)

    def test_ai_merge_keeps_gait_tempo_and_derived_interval(self):
        local = recommend(
            {"scene": "walking", "mood": "平静", "target_speed_kmh": 4, "step_length_cm": 60},
            ai_client=None,
        )
        merged = _merge_with_local(
            local,
            {"recommendation": {"bpm_min": 90, "bpm_max": 130, "target_bpm": 120, "genres": ["pop"]}},
        )
        expected = round(local["profile"]["walking_guidance"]["cadence"])
        self.assertEqual(merged["recommendation"]["target_bpm"], expected)
        self.assertEqual(merged["recommendation"]["beat_interval_ms"], round(60000 / expected))


if __name__ == "__main__":
    unittest.main()
