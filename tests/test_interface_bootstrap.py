from __future__ import annotations

import unittest
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from tbh_automation.autodetect import (
    AmbiguousDetectionError, AutoDetectionResult, CubeIconDetectionResult, SynthesisLayoutError,
)
from tbh_automation.engine import RoundCancelled
from tbh_automation.model import Calibration, Point, WindowIdentity
from tbh_automation.windows import (
    GameActivationError,
    Win32AutomationDriver,
    open_synthesis_interface,
)


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

    def test_driver_keeps_waiting_when_verified_game_is_not_yet_activatable(self) -> None:
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
        driver.generation = 4
        driver.gate = Mock()
        driver.checkpoint = Mock()
        driver._foreground = Mock(return_value=123)
        driver.status = Mock()
        result = AutoDetectionResult(Point(100, 200), 0.1, 1.5)

        with patch(
            "tbh_automation.windows.open_synthesis_interface",
            side_effect=[
                GameActivationError("not ready"),
                (b"frame", 800, 700, result),
            ],
        ) as open_interface:
            driver.prepare_interface()

        self.assertEqual(open_interface.call_count, 2)
        driver.status.assert_called_once_with("已找到 TaskBarHero，正在等待游戏窗口可以激活")
        driver.gate.interruptible_wait.assert_called_once_with(0.5, 4)

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
                side_effect=[ValueError("hero menu closed"), result],
            ),
            patch(
                "tbh_automation.windows.find_cube_icon",
                side_effect=[ValueError("closed"), cube],
            ),
            patch("tbh_automation.windows.press_tab_on_game") as press_tab,
            patch("tbh_automation.windows.click_game_point") as click,
        ):
            open_synthesis_interface(123, wait=waits.append)

        press_tab.assert_called_once_with(123, wait=waits.append)
        click.assert_called_once_with(123, cube.center, "黄色魔方入口", wait=waits.append)
        self.assertEqual(waits, [1.0, 1.2])

    def test_open_panel_outside_the_cube_search_band_does_not_press_tab(self) -> None:
        result = AutoDetectionResult(Point(100, 200), 0.01, 1.0)
        with (
            patch("tbh_automation.windows.temporarily_uncover_window", return_value=nullcontext()),
            patch("tbh_automation.windows.capture_client_bgra", return_value=(b"frame", 800, 700)),
            patch("tbh_automation.windows.get_window_dpi", return_value=96),
            patch("tbh_automation.windows.find_cube_icon", side_effect=ValueError("outside band")),
            patch("tbh_automation.windows.find_auto_fill_anchor", return_value=result),
            patch("tbh_automation.windows.press_tab_on_game") as press,
            patch("tbh_automation.windows.click_game_point") as click,
        ):
            self.assertEqual(open_synthesis_interface(123)[3], result)
        press.assert_not_called()
        click.assert_not_called()

    def test_verified_open_panel_does_not_search_ambiguous_desktop_icons(self) -> None:
        result = AutoDetectionResult(Point(100, 200), 0.01, 2.0)
        with (
            patch("tbh_automation.windows.temporarily_uncover_window", return_value=nullcontext()),
            patch("tbh_automation.windows.capture_client_bgra", return_value=(b"frame", 800, 700)),
            patch("tbh_automation.windows.get_window_dpi", return_value=96),
            patch("tbh_automation.windows.find_cube_icon", side_effect=AmbiguousDetectionError("wallpaper")) as cube,
            patch("tbh_automation.windows.find_auto_fill_anchor", return_value=result) as detect,
            patch("tbh_automation.windows.press_tab_on_game") as press,
            patch("tbh_automation.windows.click_game_point") as click,
        ):
            self.assertEqual(open_synthesis_interface(123)[3], result)
        cube.assert_not_called()
        press.assert_not_called()
        click.assert_not_called()
        self.assertTrue(detect.call_args.kwargs["require_synthesis_layout"])
        self.assertTrue(detect.call_args.kwargs["require_button_color"])

    def test_wrong_mode_and_ambiguous_candidates_do_not_trigger_navigation(self) -> None:
        cube = CubeIconDetectionResult(Point(300, 500), (270, 470, 330, 530), 500)
        for cube_error, text_error in (
            (None, SynthesisLayoutError("wrong mode")),
            (None, AmbiguousDetectionError("two panels")),
            (AmbiguousDetectionError("two cubes"), ValueError("panel closed")),
        ):
            with (
                self.subTest(cube_error=cube_error, text_error=text_error),
                patch("tbh_automation.windows.temporarily_uncover_window", return_value=nullcontext()),
                patch("tbh_automation.windows.capture_client_bgra", return_value=(b"frame", 800, 700)),
                patch("tbh_automation.windows.get_window_dpi", return_value=96),
                patch("tbh_automation.windows.find_cube_icon", return_value=cube, side_effect=cube_error),
                patch("tbh_automation.windows.find_auto_fill_anchor", side_effect=text_error),
                patch("tbh_automation.windows.press_tab_on_game") as press,
                patch("tbh_automation.windows.click_game_point") as click,
            ):
                with self.assertRaises(ValueError):
                    open_synthesis_interface(123)
            press.assert_not_called()
            click.assert_not_called()

    def test_failed_cube_detection_saves_every_capture_stage(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            diagnostics_dir = Path(temporary_directory)
            with (
                patch("tbh_automation.windows.temporarily_uncover_window", return_value=nullcontext()),
                patch(
                    "tbh_automation.windows.capture_client_bgra",
                    side_effect=[(b"closed", 800, 700), (b"hero", 800, 700)],
                ),
                patch("tbh_automation.windows.get_window_dpi", return_value=120),
                patch(
                    "tbh_automation.windows.client_to_screen",
                    return_value=Point(10, 20),
                ),
                patch(
                    "tbh_automation.windows.find_cube_icon",
                    side_effect=[ValueError("closed"), ValueError("missing")],
                ),
                patch("tbh_automation.windows.press_tab_on_game"),
                patch("tbh_automation.windows.save_bgra_bmp") as save_bmp,
            ):
                with self.assertRaisesRegex(ValueError, "missing"):
                    open_synthesis_interface(
                        123,
                        wait=lambda _seconds: None,
                        diagnostics_dir=diagnostics_dir,
                    )

        saved_names = [call.args[0].name for call in save_bmp.call_args_list]
        self.assertEqual(len(saved_names), 2)
        self.assertTrue(saved_names[0].endswith("-initial.bmp"))
        self.assertTrue(saved_names[1].endswith("-after-tab.bmp"))

    def test_unrecognized_cube_page_is_not_clicked_again(self) -> None:
        cube = CubeIconDetectionResult(Point(300, 500), (270, 470, 330, 530), 500)
        click = Mock()
        with (
            patch("tbh_automation.windows.temporarily_uncover_window", return_value=nullcontext()),
            patch(
                "tbh_automation.windows.capture_client_bgra",
                side_effect=[
                    (b"other-mode", 800, 700),
                    (b"unrecognized", 800, 700),
                ],
            ),
            patch("tbh_automation.windows.get_window_dpi", return_value=144),
            patch(
                "tbh_automation.windows.find_auto_fill_anchor",
                side_effect=[ValueError("other"), ValueError("unrecognized")],
            ),
            patch("tbh_automation.windows.find_cube_icon", return_value=cube),
            patch("tbh_automation.windows.click_game_point", click),
        ):
            with self.assertRaisesRegex(ValueError, "unrecognized"):
                open_synthesis_interface(123, wait=lambda _seconds: None)

        click.assert_called_once()
        self.assertEqual(click.call_args.args, (123, cube.center, "黄色魔方入口"))

    def test_cancellation_after_detection_prevents_the_cube_click(self) -> None:
        cube = CubeIconDetectionResult(Point(300, 500), (270, 470, 330, 530), 500)
        cancelled = False

        def cancel_during_text_detection(*_args, **_kwargs):
            nonlocal cancelled
            cancelled = True
            raise ValueError("panel not open")

        def checkpoint() -> None:
            if cancelled:
                raise RoundCancelled()

        with (
            patch("tbh_automation.windows.temporarily_uncover_window", return_value=nullcontext()),
            patch("tbh_automation.windows.capture_client_bgra", return_value=(b"frame", 800, 700)),
            patch("tbh_automation.windows.get_window_dpi", return_value=144),
            patch("tbh_automation.windows.find_cube_icon", return_value=cube),
            patch(
                "tbh_automation.windows.find_auto_fill_anchor",
                side_effect=cancel_during_text_detection,
            ),
            patch("tbh_automation.windows.click_game_point") as click,
        ):
            with self.assertRaises(RoundCancelled):
                open_synthesis_interface(
                    123,
                    wait=lambda _seconds: None,
                    checkpoint=checkpoint,
                )

        click.assert_not_called()


if __name__ == "__main__":
    unittest.main()
