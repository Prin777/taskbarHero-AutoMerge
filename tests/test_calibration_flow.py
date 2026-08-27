from __future__ import annotations

import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from tbh_automation.app import (
    CALIBRATION_STEPS,
    INTERVAL_OPTIONS,
    SessionLogging,
    TaskbarHeroApp,
    calibration_step_or_finish,
    interval_option_for_seconds,
    runtime_output_dir,
    scaled_pixels,
)


class CalibrationFlowTests(unittest.TestCase):
    def test_runtime_output_uses_launcher_directory(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            launcher = Path(temporary_directory) / "taskbar_hero.py"
            with patch("tbh_automation.app.sys.argv", [str(launcher)]):
                self.assertEqual(runtime_output_dir(), Path(temporary_directory))

    def test_ui_pixels_restore_previous_size_at_150_percent(self) -> None:
        self.assertEqual(scaled_pixels(410, 1.5), 615)
        self.assertEqual(scaled_pixels(400, 1.5), 600)

    def test_next_round_interval_options_are_one_two_three_and_five_hours(self) -> None:
        self.assertEqual(
            INTERVAL_OPTIONS,
            {"1h": 3_600, "2h": 7_200, "3h": 10_800, "5h": 18_000},
        )
        self.assertEqual(interval_option_for_seconds(10_800), "3h")
        self.assertEqual(interval_option_for_seconds(12_345), "3.42917h")

    def test_changing_interval_saves_config_and_updates_schedule(self) -> None:
        app = object.__new__(TaskbarHeroApp)
        app.interval_var = Mock()
        app.interval_var.get.return_value = "2h"
        app.config = Mock(interval_seconds=18_000)
        app.schedule = Mock()
        app.root = Mock()
        app._update_countdown_text = Mock()

        with patch("tbh_automation.app.time.monotonic", return_value=123.0):
            app._change_interval()

        self.assertEqual(app.config.interval_seconds, 7_200)
        app.config.save.assert_called_once_with()
        app.schedule.change_interval.assert_called_once_with(7_200)
        app._update_countdown_text.assert_called_once_with(123.0)

    def test_failed_interval_save_restores_previous_selection(self) -> None:
        app = object.__new__(TaskbarHeroApp)
        app.interval_var = Mock()
        app.interval_var.get.return_value = "1h"
        app.config = Mock(interval_seconds=18_000)
        app.config.save.side_effect = OSError("read-only")
        app.schedule = Mock()
        app.root = Mock()

        with patch("tbh_automation.app.messagebox.showerror") as show_error:
            app._change_interval()

        self.assertEqual(app.config.interval_seconds, 18_000)
        app.interval_var.set.assert_called_once_with("5h")
        app.schedule.change_interval.assert_not_called()
        show_error.assert_called_once()

    def test_session_logging_only_creates_one_log_when_enabled(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            output_dir = Path(temporary_directory)
            session_logging = SessionLogging()
            self.assertEqual(list(output_dir.rglob("*.log")), [])

            with patch("tbh_automation.app.runtime_output_dir", return_value=output_dir):
                session_dir = session_logging.enable()
                logging.getLogger("tbh_automation").warning("session-only-test")
                session_logging.disable()

            self.assertEqual(session_dir.parent, output_dir / "diagnostics")
            self.assertEqual(
                [path.relative_to(output_dir) for path in output_dir.rglob("*.log")],
                [session_dir.relative_to(output_dir) / "session.log"],
            )
            self.assertFalse((output_dir / "automation.log").exists())
            self.assertIn("session-only-test", (session_dir / "session.log").read_text("utf-8"))

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
