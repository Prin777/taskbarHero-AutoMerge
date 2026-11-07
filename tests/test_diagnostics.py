import unittest
import json
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from tbh_automation.diagnostics import record_event

class DiagnosticEventTests(unittest.TestCase):
    def test_correlates_frame_desktop_metadata_and_sequence(self):
        with TemporaryDirectory() as temp:
            image=Image.new('RGB',(8,8),'blue')
            with patch('tbh_automation.diagnostics.ImageGrab.grab',return_value=image) as grab:
                first=record_event(temp,'lost foreground',frame=(image.tobytes('raw','BGRX'),8,8),metadata={'foreground_process':'example.exe'},desktop=True)
                second=record_event(temp,'next')
            self.assertEqual((first,second),('event-0001','event-0002'))
            info=json.loads((Path(temp)/(first+'.json')).read_text(encoding='utf-8'))
            self.assertEqual(len(info['files']),2)
            for file in info['files']:self.assertTrue((Path(temp)/file).exists())
            self.assertEqual(info['metadata']['foreground_process'],'example.exe')
            grab.assert_called_once_with(all_screens=True)
    def test_capture_failure_is_recorded_and_disabled_never_captures(self):
        with TemporaryDirectory() as temp:
            with patch('tbh_automation.diagnostics.ImageGrab.grab',side_effect=OSError('unavailable')) as grab:
                self.assertIsNone(record_event(None,'disabled',desktop=True))
                grab.assert_not_called()
                event=record_event(temp,'failed capture',desktop=True)
            info=json.loads((Path(temp)/(event+'.json')).read_text(encoding='utf-8'))
            self.assertTrue(info['capture_errors'])
    def test_events_are_bounded(self):
        with TemporaryDirectory() as temp:
            with patch('tbh_automation.diagnostics.MAX_EVENTS',1):
                self.assertEqual(record_event(temp,'first'),'event-0001')
                self.assertIsNone(record_event(temp,'second'))
