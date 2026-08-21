from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path

from tbh_automation.model import AppConfig, Calibration, Point, WindowIdentity
from tbh_automation.windows import count_filled_cells, grid_centers, mean_absolute_difference


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

    def test_low_contrast_items_still_count_as_a_full_grid(self) -> None:
        threshold = Calibration(
            window=WindowIdentity(client_width=800, client_height=700),
            points={key: Point(10, 20) for key in Calibration.REQUIRED_POINTS},
            empty_grid_patches=[base64.b64encode(b"abcd").decode()] * 9,
        ).difference_threshold
        differences = [18.0, 21.0, 35.0, 19.0, 24.0, 31.0, 17.0, 28.0, 20.0]
        self.assertEqual(count_filled_cells(differences, threshold), 9)

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
        }
        with self.assertRaisesRegex(ValueError, "重新"):
            Calibration.from_dict(old_calibration)



if __name__ == "__main__":
    unittest.main()
