from __future__ import annotations

import unittest

import base64

from tbh_automation.app import (
    CALIBRATION_STEPS,
    calibration_step_or_finish,
    manual_grid_baseline_is_acceptable,
)


def uniform_patch(gray: int) -> str:
    pixels = bytes((gray, gray, gray, 255)) * (16 * 16)
    return base64.b64encode(pixels).decode("ascii")


class CalibrationFlowTests(unittest.TestCase):
    def test_retry_after_final_validation_failure_retries_finish_without_indexing_step_nine(self) -> None:
        action, step = calibration_step_or_finish(len(CALIBRATION_STEPS))
        self.assertEqual(action, "finish")
        self.assertIsNone(step)

    def test_manual_empty_baseline_accepts_normal_background_variation(self) -> None:
        patches = [uniform_patch(gray) for gray in (20, 25, 30, 35, 40, 45, 50, 55, 60)]
        self.assertTrue(manual_grid_baseline_is_acceptable(patches))


if __name__ == "__main__":
    unittest.main()
