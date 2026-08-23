from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tbh_automation.model import AppConfig, Calibration, Point, WindowIdentity
from tbh_automation.windows import (
    Win32AutomationDriver,
    blue_highlight_ratio,
    crop_bgra,
    image_has_visual_content,
    is_taskbar_hero_identity,
    point_is_safe_on_active_game,
)


def pixel(blue: int, green: int, red: int) -> bytes:
    return bytes((blue, green, red, 255))


class VisionTests(unittest.TestCase):
    def test_transparent_game_can_report_the_normal_window_underneath(self) -> None:
        with (
            patch("tbh_automation.windows.point_is_visible_on_window", return_value=False),
            patch("tbh_automation.windows.get_foreground_root", return_value=100),
            patch("tbh_automation.windows._root_window", side_effect=lambda hwnd: hwnd),
            patch("tbh_automation.windows._window_at_screen_point", return_value=200),
            patch("tbh_automation.windows._screen_point_is_inside_client", return_value=True),
            patch(
                "tbh_automation.windows._window_is_topmost",
                side_effect=lambda hwnd: hwnd == 100,
            ),
            patch("tbh_automation.windows.get_process_name", return_value="explorer.exe"),
            patch("tbh_automation.windows.get_window_title", return_value="Explorer"),
            patch("tbh_automation.windows.get_class_name", return_value="CabinetWClass"),
        ):
            self.assertTrue(point_is_safe_on_active_game(100, Point(500, 700)))

    def test_a_different_topmost_window_still_blocks_the_click(self) -> None:
        with (
            patch("tbh_automation.windows.point_is_visible_on_window", return_value=False),
            patch("tbh_automation.windows.get_foreground_root", return_value=100),
            patch("tbh_automation.windows._root_window", side_effect=lambda hwnd: hwnd),
            patch("tbh_automation.windows._window_at_screen_point", return_value=200),
            patch("tbh_automation.windows._screen_point_is_inside_client", return_value=True),
            patch("tbh_automation.windows._window_is_topmost", return_value=True),
            patch("tbh_automation.windows.get_process_name", return_value="overlay.exe"),
        ):
            self.assertFalse(point_is_safe_on_active_game(100, Point(500, 700)))

    def test_game_identity_requires_exact_process_and_unity_window_class(self) -> None:
        self.assertTrue(
            is_taskbar_hero_identity(
                WindowIdentity(
                    process_name="TaskBarHero.exe",
                    class_name="UnityWndClass",
                    client_width=1455,
                    client_height=1338,
                )
            )
        )
        self.assertFalse(
            is_taskbar_hero_identity(
                WindowIdentity(
                    process_name="Terminal.exe",
                    class_name="UnityWndClass",
                    client_width=1455,
                    client_height=1338,
                )
            )
        )

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


def timed_driver(button_ratios=(), *, combination_wait: float = 2.5):
    driver = object.__new__(Win32AutomationDriver)
    gate = FakeTimedGate()
    driver.gate = gate
    driver.generation = 0
    driver.combination_wait = combination_wait
    driver.status = lambda _message: None
    driver.diagnostics_dir = None
    driver._diagnostic_index = 0
    iterator = iter(button_ratios)
    sample = pixel(50, 50, 50) * (32 * 32)
    driver._combine_button_ratio_once = lambda: (next(iterator), sample)
    return driver, gate


class TimedVisionFlowTests(unittest.TestCase):
    def test_grey_button_can_become_blue_during_fill_animation(self) -> None:
        driver, gate = timed_driver([0.0, 0.30, 0.32])

        with patch("tbh_automation.windows.time.monotonic", side_effect=lambda: gate.now):
            result = driver.combine_button_is_enabled()

        self.assertTrue(result)
        self.assertGreaterEqual(gate.now, 1.05)

    def test_permanently_grey_button_times_out_as_unavailable(self) -> None:
        driver, gate = timed_driver([0.0] * 20)

        with patch("tbh_automation.windows.time.monotonic", side_effect=lambda: gate.now):
            result = driver.combine_button_is_enabled()

        self.assertFalse(result)
        self.assertGreaterEqual(gate.now, 3.65)

    def test_blue_button_requires_two_consecutive_confirmations(self) -> None:
        driver, gate = timed_driver([0.30, 0.0, 0.31, 0.32])

        with patch("tbh_automation.windows.time.monotonic", side_effect=lambda: gate.now):
            result = driver.combine_button_is_enabled()

        self.assertTrue(result)
        self.assertGreaterEqual(gate.now, 1.25)

    def test_combination_wait_only_waits_for_the_animation(self) -> None:
        driver, gate = timed_driver(combination_wait=2.5)

        driver.wait_for_combination()

        self.assertEqual(gate.now, 2.5)


class ConfigTests(unittest.TestCase):
    def test_default_interval_is_five_hours(self) -> None:
        self.assertEqual(AppConfig().interval_seconds, 18_000)

    def test_config_round_trip(self) -> None:
        calibration = Calibration(
            window=WindowIdentity(title="TBH", class_name="Game", process_name="game.exe", client_width=800, client_height=700),
            points={key: Point(10, 20) for key in Calibration.REQUIRED_POINTS},
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

    def test_version_eight_calibration_migrates_without_grid_baseline(self) -> None:
        previous_calibration = {
            "window": {
                "title": "TBH",
                "class_name": "Game",
                "process_name": "game.exe",
                "client_width": 800,
                "client_height": 700,
            },
            "points": {key: {"x": 10, "y": 20} for key in Calibration.REQUIRED_POINTS},
            "empty_grid_patches": ["obsolete"] * 9,
            "calibration_version": 8,
            "patch_size": 32,
        }
        calibration = Calibration.from_dict(previous_calibration)

        self.assertEqual(calibration.calibration_version, 9)
        self.assertFalse(hasattr(calibration, "empty_grid_patches"))

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
                "empty_grid_patches": ["obsolete"] * 9,
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
        self.assertEqual(rewritten["version"], 6)



if __name__ == "__main__":
    unittest.main()
