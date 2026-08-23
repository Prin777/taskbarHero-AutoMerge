from __future__ import annotations

import unittest

from tbh_automation.app import (
    CALIBRATION_STEPS,
    calibration_step_or_finish,
    scaled_pixels,
)


class CalibrationFlowTests(unittest.TestCase):
    def test_ui_pixels_restore_previous_size_at_150_percent(self) -> None:
        self.assertEqual(scaled_pixels(410, 1.5), 615)
        self.assertEqual(scaled_pixels(340, 1.5), 510)

    def test_retry_after_final_validation_failure_retries_finish_without_indexing_step_nine(self) -> None:
        action, step = calibration_step_or_finish(len(CALIBRATION_STEPS))
        self.assertEqual(action, "finish")
        self.assertIsNone(step)

    def test_manual_calibration_only_requests_button_positions(self) -> None:
        keys = {step.key for step in CALIBRATION_STEPS}
        self.assertNotIn("grid_top_left", keys)
        self.assertNotIn("grid_bottom_right", keys)
        self.assertEqual(len(CALIBRATION_STEPS), 6)


if __name__ == "__main__":
    unittest.main()
