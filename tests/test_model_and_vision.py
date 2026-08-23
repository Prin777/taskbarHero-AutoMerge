from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tbh_automation.model import AppConfig, Calibration, Point, WindowIdentity
from tbh_automation.windows import (
    Win32AutomationDriver,
    blue_highlight_ratio,
    count_filled_cells,
    crop_bgra,
    grid_centers,
    image_has_visual_content,
    mean_absolute_difference,
    salient_pixel_difference,
)


def pixel(blue: int, green: int, red: int) -> bytes:
    return bytes((blue, green, red, 255))


class VisionTests(unittest.TestCase):
    def test_grid_centers_are_interpolated_in_row_major_order(self) -> None:
        calibration = Calibration(
            window=WindowIdentity(client_width=800, client_height=700),
            points={
                **{key: Point(0, 0) for key in Calibration.REQUIRED_POINTS},
                "grid_top_left": Point(100, 200),
                "grid_bottom_right": Point(200, 300),
            },
            empty_grid_patches=[base64.b64encode(b"x").decode()] * 9,
        )
        self.assertEqual(
            grid_centers(calibration),
            [
                Point(100, 200), Point(150, 200), Point(200, 200),
                Point(100, 250), Point(150, 250), Point(200, 250),
                Point(100, 300), Point(150, 300), Point(200, 300),
            ],
        )

    def test_image_difference_ignores_alpha_channel(self) -> None:
        self.assertEqual(mean_absolute_difference(bytes((10, 20, 30, 0)), bytes((10, 20, 30, 255))), 0)
        self.assertEqual(mean_absolute_difference(pixel(0, 0, 0), pixel(30, 30, 30)), 30)

    def test_sparse_ordinary_item_details_are_detected_by_salient_pixels(self) -> None:
        empty = pixel(12, 12, 12) * 16
        ordinary_item = pixel(102, 102, 102) * 2 + pixel(12, 12, 12) * 14

        self.assertLess(mean_absolute_difference(empty, ordinary_item), 15)
        self.assertGreater(salient_pixel_difference(empty, ordinary_item), 15)

    def test_low_contrast_items_still_count_as_a_full_grid(self) -> None:
        threshold = Calibration(
            window=WindowIdentity(client_width=800, client_height=700),
            points={key: Point(10, 20) for key in Calibration.REQUIRED_POINTS},
            empty_grid_patches=[base64.b64encode(b"abcd").decode()] * 9,
        ).difference_threshold
        differences = [18.0, 21.0, 35.0, 19.0, 24.0, 31.0, 17.0, 28.0, 20.0]
        self.assertEqual(count_filled_cells(differences, threshold), 9)

    def test_blue_combine_button_is_distinguished_from_grey(self) -> None:
        blue_button = pixel(73, 60, 21) * 70 + pixel(50, 50, 50) * 30
        grey_button = pixel(58, 59, 60) * 100

        self.assertGreater(blue_highlight_ratio(blue_button), 0.25)
        self.assertEqual(blue_highlight_ratio(grey_button), 0.0)

    def test_client_image_crop_preserves_bgra_rows(self) -> None:
        image = b"".join(pixel(value, value, value) for value in range(12))

        cropped = crop_bgra(image, 4, 3, 1, 1, 2, 2)

        self.assertEqual(cropped, pixel(5, 5, 5) + pixel(6, 6, 6) + pixel(9, 9, 9) + pixel(10, 10, 10))

    def test_blank_window_capture_is_rejected(self) -> None:
        self.assertFalse(image_has_visual_content(pixel(0, 0, 0) * 100))
        self.assertFalse(
            image_has_visual_content(pixel(0, 0, 0) * 80 + pixel(80, 70, 60) * 20)
        )
        self.assertTrue(
            image_has_visual_content(pixel(0, 0, 0) * 50 + pixel(80, 70, 60) * 50)
        )


class FakeTimedGate:
    def __init__(self) -> None:
        self.now = 0.0

    def interruptible_wait(self, seconds: float, _generation: int) -> None:
        self.now += seconds


def timed_driver(observations, *, combination_wait: float = 2.5):
    driver = object.__new__(Win32AutomationDriver)
    gate = FakeTimedGate()
    driver.gate = gate
    driver.generation = 0
    driver.combination_wait = combination_wait
    driver.status = lambda _message: None
    iterator = iter(observations)
    driver._filled_count_once = lambda: next(iterator)
    return driver, gate


class TimedVisionFlowTests(unittest.TestCase):
    def test_brief_full_grid_during_fill_animation_is_not_accepted(self) -> None:
        # 9/9 持续不足 2.5 秒后又变为 8/9，应继续观察并接受稳定的 8/9。
        driver, gate = timed_driver([9] * 8 + [8] * 3)

        with patch("tbh_automation.windows.time.monotonic", side_effect=lambda: gate.now):
            result = driver.observe_filled_cells()

        self.assertEqual(result, 8)

    def test_full_grid_is_accepted_after_a_long_stable_window(self) -> None:
        observations = [9] * 20
        driver, gate = timed_driver(observations)

        with patch("tbh_automation.windows.time.monotonic", side_effect=lambda: gate.now):
            result = driver.observe_filled_cells()

        self.assertEqual(result, 9)
        self.assertGreaterEqual(gate.now, 3.15)

    def test_combination_wait_does_not_reinspect_the_grid(self) -> None:
        driver, gate = timed_driver([], combination_wait=2.5)

        driver.wait_for_combination()

        self.assertEqual(gate.now, 2.5)


class ConfigTests(unittest.TestCase):
    def test_default_interval_is_five_hours(self) -> None:
        self.assertEqual(AppConfig().interval_seconds, 18_000)

    def test_config_round_trip(self) -> None:
        calibration = Calibration(
            window=WindowIdentity(title="TBH", class_name="Game", process_name="game.exe", client_width=800, client_height=700),
            points={key: Point(10, 20) for key in Calibration.REQUIRED_POINTS},
            empty_grid_patches=[base64.b64encode(b"abcd").decode()] * 9,
        )
        config = AppConfig(calibration=calibration)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            config.save(path)
            loaded = AppConfig.load(path)
        self.assertIsNotNone(loaded.calibration)
        assert loaded.calibration is not None
        self.assertEqual(loaded.calibration.window.process_name, "game.exe")
        self.assertEqual(loaded.interval_seconds, 18_000)
        self.assertEqual(loaded.combination_wait_seconds, 2.5)

    def test_old_grid_baseline_is_rejected(self) -> None:
        old_calibration = {
            "window": {
                "title": "TBH",
                "class_name": "Game",
                "process_name": "game.exe",
                "client_width": 800,
                "client_height": 700,
            },
            "points": {key: {"x": 10, "y": 20} for key in Calibration.REQUIRED_POINTS},
            "empty_grid_patches": [base64.b64encode(b"abcd").decode()] * 9,
            "calibration_version": 6,
            "patch_size": 16,
        }
        with self.assertRaisesRegex(ValueError, "重新"):
            Calibration.from_dict(old_calibration)

    def test_greyscale_terminal_baseline_is_rejected(self) -> None:
        greyscale_patch = pixel(12, 12, 12) * (32 * 32)
        calibration = Calibration(
            window=WindowIdentity(client_width=800, client_height=700),
            points={key: Point(10, 20) for key in Calibration.REQUIRED_POINTS},
            empty_grid_patches=[base64.b64encode(greyscale_patch).decode()] * 9,
        )

        with self.assertRaisesRegex(ValueError, "终端"):
            calibration.validate()

    def test_stale_calibration_is_migrated_once_without_losing_settings(self) -> None:
        stale_config = {
            "calibration": {
                "window": {
                    "title": "TBH",
                    "class_name": "Game",
                    "process_name": "game.exe",
                    "client_width": 800,
                    "client_height": 700,
                },
                "points": {
                    key: {"x": 10, "y": 20}
                    for key in Calibration.REQUIRED_POINTS
                },
                "empty_grid_patches": [base64.b64encode(b"abcd").decode()] * 9,
                "calibration_version": 6,
                "patch_size": 32,
                "difference_threshold": 15.0,
            },
            "interval_seconds": 12345,
            "max_combinations_per_category": 100,
            "click_delay_seconds": 0.35,
            "action_timeout_seconds": 12.0,
            "combination_wait_seconds": 2.5,
            "version": 5,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(stale_config), encoding="utf-8")

            loaded = AppConfig.load(path)
            rewritten = json.loads(path.read_text(encoding="utf-8"))

        self.assertIsNone(loaded.calibration)
        self.assertEqual(loaded.interval_seconds, 12345)
        self.assertIsNone(rewritten["calibration"])



if __name__ == "__main__":
    unittest.main()
