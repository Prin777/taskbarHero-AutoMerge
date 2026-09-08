from __future__ import annotations

import queue
import threading
import unittest
from unittest.mock import Mock, patch

from tbh_automation.app import AutoDetectionOutcome, TaskbarHeroApp
from tbh_automation.autodetect import AutoDetectionResult
from tbh_automation.model import Calibration, Point, WindowIdentity
from tbh_automation.windows import Win32AutomationDriver


class ScaleDisplayTests(unittest.TestCase):
    def test_queue_keeps_confirmed_scale_when_paused_and_ignores_replaced_calibration(self) -> None:
        app = object.__new__(TaskbarHeroApp)
        current_calibration = object()
        app.config = Mock(calibration=current_calibration)
        app.gate = Mock(shutdown_requested=True, enabled=False)
        app.scale_var = Mock()
        app.ui_queue = queue.Queue()
        app.ui_queue.put(("detected_scale", (current_calibration, 1.25)))
        app.ui_queue.put(("detected_scale", (object(), 3.0)))

        app._poll_ui_queue()

        app.scale_var.set.assert_called_once_with("当前识别模板缩放：×1.25")

    def test_auto_detection_only_publishes_scale_after_successful_save(self) -> None:
        for outcome in ("success", "save_error", "cancelled", "stale"):
            with self.subTest(outcome=outcome):
                app = object.__new__(TaskbarHeroApp)
                cancel_event = threading.Event()
                previous = object()
                app._auto_detection_cancel = cancel_event
                app._exiting = False
                app.config = Mock(calibration=previous)
                if outcome == "save_error":
                    app.config.save.side_effect = OSError("save failed")
                app.root = Mock()
                app._set_auto_detection_controls = Mock()
                app.show_main_window = Mock()
                app.toggle_button = Mock()
                app._set_status = Mock()
                app.scale_var = Mock()
                app.schedule = Mock()
                app.gate = Mock()
                app.run_now_event = Mock()
                app.diagnostics_dir = None
                payload = AutoDetectionOutcome(
                    cancel_event=threading.Event() if outcome == "stale" else cancel_event,
                    previous_calibration=previous,
                    calibration=object(),
                    detected_scale=2.0,
                    cancelled=outcome == "cancelled",
                )
                with patch("tbh_automation.app.messagebox"):
                    app._finish_auto_detection(payload)
                if outcome == "success":
                    app.scale_var.set.assert_called_once_with("当前识别模板缩放：×2")
                else:
                    app.scale_var.set.assert_not_called()
                    self.assertIs(app.config.calibration, previous)
                    app.gate.enable.assert_not_called()

    def test_detection_worker_returns_template_scale_without_touching_tk(self) -> None:
        app = object.__new__(TaskbarHeroApp)
        app.gate = Mock(shutdown_requested=False)
        app._automation_lock = threading.Lock()
        app.ui_queue = queue.Queue()
        app.scale_var = Mock()
        app.diagnostics_dir = None
        identity = WindowIdentity(process_name="TaskBarHero.exe", class_name="UnityWndClass", client_width=800, client_height=700)
        result = AutoDetectionResult(Point(100, 200), 0.01, 1.5)
        with (
            patch("tbh_automation.app.find_taskbar_hero_window", return_value=123),
            patch("tbh_automation.app.describe_window", return_value=identity),
            patch("tbh_automation.app.get_window_dpi", return_value=120),
            patch("tbh_automation.app.open_synthesis_interface", return_value=(b"frame", 800, 700, result)),
        ):
            app._perform_auto_detection(threading.Event(), None)
        kind, payload = list(app.ui_queue.queue)[-1]
        self.assertEqual(kind, "auto_detection_finished")
        self.assertEqual(payload.detected_scale, 1.5)
        self.assertIsInstance(payload.calibration, Calibration)
        app.scale_var.set.assert_not_called()

    def test_round_reports_scale_after_refreshing_coordinates(self) -> None:
        calibration = Calibration(
            window=WindowIdentity(process_name="TaskBarHero.exe", class_name="UnityWndClass", client_width=800, client_height=700),
            points={key: Point(0, 0) for key in Calibration.REQUIRED_POINTS},
        )
        received = []
        driver = Win32AutomationDriver(
            calibration, Mock(), 0, click_delay=0.35, combination_wait=2.5,
            on_scale_detected=lambda scale: received.append((scale, calibration.points["combine"])),
        )
        driver.checkpoint = Mock()
        driver._foreground = Mock(return_value=123)
        result = AutoDetectionResult(Point(100, 200), 0.01, 1.5)
        with patch("tbh_automation.windows.open_synthesis_interface", return_value=(b"frame", 800, 700, result)):
            driver.prepare_interface()
        self.assertEqual(received, [(1.5, Point(328, 210))])


if __name__ == "__main__":
    unittest.main()
