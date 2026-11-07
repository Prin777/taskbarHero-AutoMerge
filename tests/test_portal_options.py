import unittest
import threading
import queue
import time
from unittest.mock import Mock,patch
from tempfile import TemporaryDirectory
from pathlib import Path
import json
from tbh_automation.model import AppConfig
from tbh_automation.app import TaskbarHeroApp
from tbh_automation.engine import ControlGate, RunSchedule

class PortalOptionTests(unittest.TestCase):
    def app(self):
        app=object.__new__(TaskbarHeroApp)
        app.config=AppConfig();app.config.save=Mock()
        app.portal_enabled_var=Mock();app.portal_enabled_var.get.return_value=True
        app.portal_check_event=threading.Event();app.round_active_event=threading.Event()
        app._post_status=Mock();app.root=Mock()
        return app
    def test_idle_enable_requests_once_and_disable_clears(self):
        app=self.app();app._change_portal_option()
        self.assertTrue(app.portal_check_event.is_set())
        self.assertTrue(app.config.portal_enabled)
        app.portal_enabled_var.get.return_value=False
        app._change_portal_option()
        self.assertFalse(app.portal_check_event.is_set())
    def test_busy_enable_does_not_queue_extra_run(self):
        app=self.app();app.round_active_event.set();app._change_portal_option()
        self.assertFalse(app.portal_check_event.is_set())
    def test_save_failure_rolls_back_without_request(self):
        app=self.app();app.config.save.side_effect=OSError('failed')
        with patch('tbh_automation.app.messagebox.showerror'):app._change_portal_option()
        self.assertFalse(app.config.portal_enabled)
        app.portal_enabled_var.set.assert_called_with(False)
        self.assertFalse(app.portal_check_event.is_set())
    def test_standalone_check_does_not_advance_schedule_or_count_round(self):
        app=self.app();app.config.portal_enabled=True;app.config.calibration=Mock()
        app.portal_check_event.set();app.run_now_event=threading.Event()
        app.gate=ControlGate();app.schedule=RunSchedule.starting_now(3600,time.monotonic()+3600)
        due=app.schedule.next_due;app._automation_lock=threading.Lock()
        app.diagnostics_dir=None;app.ui_queue=queue.Queue()
        driver=Mock();driver.check_boss_portal.side_effect=app.gate.request_shutdown
        with patch('tbh_automation.app.Win32AutomationDriver',return_value=driver):app._scheduler_loop()
        driver.check_boss_portal.assert_called_once()
        driver.prepare_interface.assert_not_called()
        self.assertEqual(app.schedule.next_due,due)
        self.assertIsNone(app.schedule.last_completed_at)
        self.assertTrue(app.ui_queue.empty())
    def test_old_configuration_migrates_and_option_roundtrips(self):
        with TemporaryDirectory() as temp:
            path=Path(temp)/'config.json'
            path.write_text(json.dumps({'version':6,'interval_seconds':7200}),encoding='utf-8')
            config=AppConfig.load(path)
            self.assertFalse(config.portal_enabled)
            self.assertEqual(config.interval_seconds,7200)
            self.assertEqual(json.loads(path.read_text())['version'],7)
            config.portal_enabled=True;config.save(path)
            self.assertTrue(AppConfig.load(path).portal_enabled)


class ChestKeyboardTests(unittest.TestCase):
    def test_space_is_only_sent_to_active_game_and_released(self):
        from contextlib import nullcontext
        from tbh_automation.windows import Win32AutomationDriver,KEYEVENTF_KEYUP
        from tbh_automation.engine import AutomationError
        for foreground in (123,456):
            driver=object.__new__(Win32AutomationDriver)
            driver.checkpoint=Mock();driver._foreground=Mock(return_value=123)
            driver.gate=Mock();driver.generation=0
            api=Mock()
            with patch('tbh_automation.windows.user32',api), patch('tbh_automation.windows.temporarily_uncover_window',return_value=nullcontext()), patch('tbh_automation.windows.get_foreground_root',return_value=foreground), patch('tbh_automation.windows._root_window',return_value=123):
                if foreground==123:
                    driver.open_chests()
                    self.assertEqual([call.args for call in api.keybd_event.call_args_list],[(0x20,0,0,0),(0x20,0,KEYEVENTF_KEYUP,0)])
                else:
                    with self.assertRaises(AutomationError):driver.open_chests()
                    api.keybd_event.assert_not_called()
