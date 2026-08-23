from __future__ import annotations

import unittest
from contextlib import nullcontext
from unittest.mock import Mock, patch

from tbh_automation.autodetect import AutoDetectionResult, CubeIconDetectionResult
from tbh_automation.model import Calibration, Point, WindowIdentity
from tbh_automation.windows import Win32AutomationDriver, open_synthesis_interface


class InterfaceBootstrapTests(unittest.TestCase):
    def test_driver_refreshes_every_control_from_detected_anchor(self) -> None:
        driver = object.__new__(Win32AutomationDriver)
        driver.calibration = Calibration(
            window=WindowIdentity(
                process_name="TaskBarHero.exe",
                class_name="UnityWndClass",
                client_width=800,
                client_height=700,
            ),
            points={key: Point(0, 0) for key in Calibration.REQUIRED_POINTS},
        )
        driver.generation = 0
        driver.gate = Mock()
        driver.checkpoint = Mock()
        driver._foreground = Mock(return_value=123)
        result = AutoDetectionResult(Point(100, 200), 0.1, 1.5)

        with patch(
            "tbh_automation.windows.open_synthesis_interface",
            return_value=(b"frame", 800, 700, result),
        ):
            driver.prepare_interface()

        self.assertEqual(driver.calibration.points["auto_fill"], Point(144, 210))
        self.assertEqual(driver.calibration.points["combine"], Point(328, 210))
        self.assertEqual(set(driver.calibration.points), Calibration.REQUIRED_POINTS)

    def test_already_open_synthesis_does_not_press_or_click(self) -> None:
        result = AutoDetectionResult(Point(100, 200), 0.1, 1.5)
        cube = CubeIconDetectionResult(Point(300, 500), (270, 470, 330, 530), 500)
        with (
            patch("tbh_automation.windows.temporarily_uncover_window", return_value=nullcontext()),
            patch("tbh_automation.windows.capture_client_bgra", return_value=(b"frame", 800, 700)),
            patch("tbh_automation.windows.get_window_dpi", return_value=144),
            patch("tbh_automation.windows.find_cube_icon", return_value=cube),
            patch("tbh_automation.windows.find_auto_fill_anchor", return_value=result),
            patch("tbh_automation.windows.press_tab_on_game") as press_tab,
            patch("tbh_automation.windows.click_game_point") as click,
        ):
            _image, _width, _height, detected = open_synthesis_interface(123)

        self.assertEqual(detected, result)
        press_tab.assert_not_called()
        click.assert_not_called()

    def test_closed_hero_menu_is_opened_before_cube_is_clicked(self) -> None:
        result = AutoDetectionResult(Point(100, 200), 0.1, 1.5)
        cube = CubeIconDetectionResult(Point(300, 500), (270, 470, 330, 530), 500)
        waits: list[float] = []
        with (
            patch("tbh_automation.windows.temporarily_uncover_window", return_value=nullcontext()),
            patch(
                "tbh_automation.windows.capture_client_bgra",
                side_effect=[(b"closed", 800, 700), (b"hero", 800, 700), (b"cube", 800, 700)],
            ),
            patch("tbh_automation.windows.get_window_dpi", return_value=144),
            patch(
                "tbh_automation.windows.find_auto_fill_anchor",
                return_value=result,
            ),
            patch(
                "tbh_automation.windows.find_cube_icon",
                side_effect=[ValueError("closed"), cube],
            ),
            patch("tbh_automation.windows.press_tab_on_game") as press_tab,
            patch("tbh_automation.windows.click_game_point") as click,
        ):
            open_synthesis_interface(123, wait=waits.append)

        press_tab.assert_called_once_with(123)
        click.assert_called_once_with(123, cube.center, "黄色魔方入口")
        self.assertEqual(waits, [1.0, 1.2])

    def test_cube_in_other_mode_is_reopened_once(self) -> None:
        result = AutoDetectionResult(Point(100, 200), 0.1, 1.5)
        cube = CubeIconDetectionResult(Point(300, 500), (270, 470, 330, 530), 500)
        click = Mock()
        with (
            patch("tbh_automation.windows.temporarily_uncover_window", return_value=nullcontext()),
            patch(
                "tbh_automation.windows.capture_client_bgra",
                side_effect=[
                    (b"other-mode", 800, 700),
                    (b"closed", 800, 700),
                    (b"synthesis", 800, 700),
                ],
            ),
            patch("tbh_automation.windows.get_window_dpi", return_value=144),
            patch(
                "tbh_automation.windows.find_auto_fill_anchor",
                side_effect=[ValueError("other"), ValueError("closed"), result],
            ),
            patch("tbh_automation.windows.find_cube_icon", return_value=cube),
            patch("tbh_automation.windows.click_game_point", click),
        ):
            open_synthesis_interface(123, wait=lambda _seconds: None)

        self.assertEqual(click.call_count, 2)
        self.assertEqual(click.call_args_list[1].args[2], "黄色魔方入口（重新打开合成）")


if __name__ == "__main__":
    unittest.main()
