from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from tbh_automation import app as app_module
from tbh_automation.app import TaskbarHeroApp
from tbh_automation.desktop import ERROR_ALREADY_EXISTS, SingleInstance, application_icon_path


class SingleInstanceTests(unittest.TestCase):
    def test_first_instance_keeps_mutex_until_release(self) -> None:
        instance = SingleInstance()
        with (
            patch("tbh_automation.desktop.sys.platform", "win32"),
            patch("tbh_automation.desktop._create_named_mutex", return_value=(123, 0)),
            patch("tbh_automation.desktop._close_handle") as close_handle,
        ):
            self.assertTrue(instance.acquire())
            self.assertTrue(instance.acquire())
            close_handle.assert_not_called()
            instance.release()

        close_handle.assert_called_once_with(123)

    def test_second_instance_is_rejected_and_its_handle_is_closed(self) -> None:
        instance = SingleInstance()
        with (
            patch("tbh_automation.desktop.sys.platform", "win32"),
            patch(
                "tbh_automation.desktop._create_named_mutex",
                return_value=(456, ERROR_ALREADY_EXISTS),
            ),
            patch("tbh_automation.desktop._close_handle") as close_handle,
        ):
            self.assertFalse(instance.acquire())

        close_handle.assert_called_once_with(456)

    def test_source_icon_exists(self) -> None:
        self.assertTrue(application_icon_path().is_file())

    def test_duplicate_is_rejected_before_tk_window_is_created(self) -> None:
        instance = Mock()
        instance.acquire.return_value = False
        with (
            patch("tbh_automation.app.SingleInstance", return_value=instance),
            patch("tbh_automation.app.show_already_running_message") as show_message,
            patch("tbh_automation.app.tk.Tk") as create_window,
        ):
            app_module.main()

        show_message.assert_called_once_with()
        create_window.assert_not_called()


class WindowLifecycleTests(unittest.TestCase):
    def test_window_close_only_hides_main_window(self) -> None:
        app = object.__new__(TaskbarHeroApp)
        app.root = Mock()
        app._wizard = None

        app.hide_to_tray()

        app.root.withdraw.assert_called_once_with()

    def test_tray_exit_stops_all_background_components(self) -> None:
        app = object.__new__(TaskbarHeroApp)
        app._exiting = False
        app._auto_detection_cancel = Mock()
        app.gate = Mock()
        app.hotkeys = Mock()
        app.session_logging = Mock()
        app.tray = Mock()
        app.root = Mock()

        app.exit_application()
        app.exit_application()

        app._auto_detection_cancel.set.assert_called_once_with()
        app.gate.request_shutdown.assert_called_once_with()
        app.hotkeys.stop.assert_called_once_with()
        app.session_logging.disable.assert_called_once_with()
        app.tray.stop.assert_called_once_with()
        app.root.destroy.assert_called_once_with()

    def test_run_now_restarts_a_stale_waiting_round(self) -> None:
        app = object.__new__(TaskbarHeroApp)
        app._auto_detecting = False
        app.config = Mock(calibration=object())
        app.gate = Mock()
        app.run_now_event = Mock()
        app.schedule = Mock()
        app.toggle_button = Mock()
        app.root = Mock()
        app._set_status = Mock()

        with patch("tbh_automation.app.time.monotonic", return_value=321.0):
            app.run_now()

        app.gate.restart.assert_called_once_with()
        app.run_now_event.set.assert_called_once_with()
        app.schedule.run_now.assert_called_once_with(321.0)


if __name__ == "__main__":
    unittest.main()
