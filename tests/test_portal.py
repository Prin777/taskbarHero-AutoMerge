import unittest
from PIL import Image
from tbh_automation.portal import find_ready_portal
from tbh_automation.portal_templates import LABEL, READY
from tbh_automation.model import Point

class PortalTests(unittest.TestCase):
    def frame(self, count=True, duplicate=False):
        self._has_count = count
        im=Image.new('RGB',(280,200),'black')
        for ox in ([10,150] if duplicate else [10]):
            for rows,x,y in [(LABEL,ox,10)]+([(READY,ox+15,96)] if count else []):
                for iy,row in enumerate(rows):
                    for ix,v in enumerate(row):
                        if v=='1': im.putpixel((x+ix,y+iy),(255,255,255))
        return im
    def detect(self,im):
        from unittest.mock import patch
        with patch('tbh_automation.portal.read_count',return_value=6 if self._has_count else None):
            return find_ready_portal(im.tobytes('raw','BGRX'),*im.size)
    def test_requires_both_label_and_count(self):
        self.assertEqual(self.detect(self.frame()),Point(30,60))
        self.assertIsNone(self.detect(self.frame(False)))
    def test_rejects_ambiguous_shortcuts(self):
        with self.assertRaises(ValueError): self.detect(self.frame(duplicate=True))
    def test_blank_and_solid_frames_never_click(self):
        for color in ('white','black','red'):
            self._has_count = False
            self.assertIsNone(self.detect(Image.new('RGB',(280,200),color)))
    def test_unexpected_image_size_rejected(self):
        with self.assertRaises(ValueError): find_ready_portal(b'',280,200)

class PortalClickSafetyTests(unittest.TestCase):
    def test_changed_count_during_mouse_settle_aborts(self):
        from unittest.mock import Mock, patch
        from tbh_automation.windows import Win32AutomationDriver
        from tbh_automation.model import Calibration, WindowIdentity
        from tbh_automation.engine import AutomationError
        driver=object.__new__(Win32AutomationDriver)
        driver.calibration=Calibration(WindowIdentity(client_width=280,client_height=200),{})
        driver.checkpoint=Mock()
        driver._foreground=Mock(return_value=123)
        from tbh_automation.engine import ControlGate
        driver.gate=ControlGate()
        driver.generation=0
        driver.status=Mock()
        def click(hwnd, point, label, *, wait, before_press):
            wait(0.06)
            before_press()
        with patch('tbh_automation.windows.temporarily_uncover_window',return_value=__import__('contextlib').nullcontext()), patch('tbh_automation.windows.get_foreground_root',return_value=123), patch('tbh_automation.windows._root_window',return_value=123), patch('tbh_automation.windows.capture_client_bgra',return_value=(b'frame',280,200)), patch('tbh_automation.portal.find_ready_portal',side_effect=[Point(30,60),Point(30,60),None]), patch('tbh_automation.windows.click_game_point',side_effect=click):
            with self.assertRaises(AutomationError): driver._check_boss_portal_active()
        self.assertGreaterEqual(driver.checkpoint.call_count,3)



class SevenPortalTests(unittest.TestCase):
    def test_seven_reference_is_accepted(self):
        from tbh_automation.portal_templates import LABEL_7, READY_7
        im=Image.new('RGB',(140,160),'black')
        for rows,x,y in ((LABEL_7,10,10),(READY_7,25,97)):
            for iy,row in enumerate(rows):
                for ix,v in enumerate(row):
                    if v=='1': im.putpixel((x+ix,y+iy),(255,255,255))
        from unittest.mock import patch
        with patch('tbh_automation.portal.read_count',return_value=7):
            self.assertEqual(find_ready_portal(im.tobytes('raw','BGRX'),*im.size),Point(30,60))


class QuantityParsingTests(unittest.TestCase):
    def test_generic_counts_and_invalid_text(self):
        from tbh_automation.portal_ocr import parse_count
        for count in (0,1,3,4,5,6,7,8,9,10,12,99,1234):
            self.assertEqual(parse_count(f" {count} / 1 "),count)
        for text in ('5/2','5','S/1','5/l','-5/1','05/1','5/1 7','5/1\n6/1','9999999/1'):
            self.assertIsNone(parse_count(text))

    def test_only_counts_greater_than_five_are_ready(self):
        from unittest.mock import patch
        im=PortalTests().frame()
        for count in (None,0,1,4,5,6,8,12,100):
            with patch('tbh_automation.portal.read_count',return_value=count):
                point=find_ready_portal(im.tobytes('raw','BGRX'),*im.size)
                self.assertEqual(point is not None,count is not None and count>5)


class WindowsOCRTests(unittest.TestCase):
    def test_generated_counts_use_the_real_ocr_engine(self):
        from PIL import ImageDraw, ImageFont
        from tbh_automation.portal_ocr import read_count
        import os
        font=ImageFont.truetype(os.path.join(os.environ.get('WINDIR','C:/Windows'),'Fonts','arial.ttf'),48)
        for count in (4,6,8,9,12,100):
            image=Image.new('RGB',(300,110),'white')
            ImageDraw.Draw(image).text((20,20),f'{count}/1',font=font,fill='black')
            self.assertEqual(read_count(image),count)

    def test_cancellation_kills_ocr_process(self):
        from unittest.mock import Mock, patch
        from tbh_automation.portal_ocr import read_count
        from tbh_automation.engine import RoundCancelled
        process=Mock()
        process.poll.return_value=None
        checkpoint=Mock(side_effect=[None,RoundCancelled])
        with patch('tbh_automation.portal_ocr.subprocess.Popen',return_value=process):
            with self.assertRaises(RoundCancelled):
                read_count(Image.new('RGB',(100,50),'white'),checkpoint=checkpoint)
        process.kill.assert_called_once()


class ActualRegressionTests(unittest.TestCase):
    def test_actual_thirteen_counter(self):
        from pathlib import Path
        im=Image.open(Path(__file__).parent/'fixtures/portal/thirteen.png').convert('RGB')
        self.assertEqual(find_ready_portal(im.tobytes('raw','BGRX'),*im.size),Point(55,66))

    def test_known_server_dialog_requires_message_and_confirm(self):
        from pathlib import Path
        from tbh_automation.server_error import find_server_error
        from PIL import ImageDraw
        im=Image.open(Path(__file__).parent/'fixtures/portal/server-5001.png').convert('RGB')
        self.assertEqual(find_server_error(im.tobytes('raw','BGRX'),*im.size),Point(281,402))
        ImageDraw.Draw(im).rectangle((250,385,315,425),fill='black')
        self.assertIsNone(find_server_error(im.tobytes('raw','BGRX'),*im.size))

    def test_server_dialog_blank_and_multiple_candidates_fail_safe(self):
        from pathlib import Path
        from tbh_automation.server_error import find_server_error
        self.assertIsNone(find_server_error(bytes(200*200*4),200,200))
        im=Image.open(Path(__file__).parent/'fixtures/portal/server-5001.png').convert('RGB')
        double=Image.new('RGB',(im.width*2,im.height),'black')
        double.paste(im,(0,0));double.paste(im,(im.width,0))
        with self.assertRaises(ValueError):find_server_error(double.tobytes('raw','BGRX'),*double.size)


class ServerDialogScaleTests(unittest.TestCase):
    def test_dialog_across_five_game_sizes(self):
        from pathlib import Path
        from tbh_automation.server_error import find_server_error
        im=Image.open(Path(__file__).parent/'fixtures/portal/server-5001.png').convert('RGB')
        for scale in (.5,.625,.75,1,1.5):
            a=im.resize((round(im.width*scale),round(im.height*scale)),Image.Resampling.NEAREST)
            point=find_server_error(a.tobytes('raw','BGRX'),*a.size)
            self.assertIsNotNone(point,scale)
            self.assertLessEqual(abs(point.x-281*scale),2)
            self.assertLessEqual(abs(point.y-402*scale),2)


class InventoryErrorRegressionTests(unittest.TestCase):
    def test_actual_twenty_two_counter(self):
        from pathlib import Path
        im=Image.open(Path(__file__).parent/'fixtures/portal/twenty-two.png').convert('RGB')
        point=find_ready_portal(im.tobytes('raw','BGRX'),*im.size)
        self.assertIsNotNone(point)
        self.assertLessEqual(abs(point.x-43),2)
        self.assertLessEqual(abs(point.y-63),2)

    def test_server_error_with_wrapped_inventory_message(self):
        from pathlib import Path
        from tbh_automation.server_error import find_server_error
        from PIL import ImageDraw
        im=Image.open(Path(__file__).parent/'fixtures/portal/server-inventory.png').convert('RGB')
        self.assertIsNotNone(find_server_error(im.tobytes('raw','BGRX'),*im.size))
        ImageDraw.Draw(im).rectangle((200,30,345,58),fill='black')
        self.assertIsNone(find_server_error(im.tobytes('raw','BGRX'),*im.size))


class OCRLocatorTests(unittest.TestCase):
    def test_battle_numbers_do_not_require_an_exact_label_template(self):
        from pathlib import Path
        from unittest.mock import patch
        im=Image.open(Path(__file__).parent/'fixtures/portal/twenty-six-battle.png').convert('RGB')
        with patch('tbh_automation.portal._find_template_portal',return_value=None):
            point=find_ready_portal(im.tobytes('raw','BGRX'),*im.size)
        self.assertIsNotNone(point)
        self.assertLessEqual(abs(point.x-91),3)
        self.assertLessEqual(abs(point.y-200),3)

    def test_counter_token_grouping(self):
        from tbh_automation.portal import _word_counter_candidates
        words=[dict(text=text,x=i*20,y=10,width=18,height=15) for i,text in enumerate(('26','/','1'))]
        self.assertEqual(list(_word_counter_candidates(words))[0][0],26)
        words[-1]['text']='2'
        self.assertEqual(list(_word_counter_candidates(words)),[])


class ForegroundOwnershipTests(unittest.TestCase):
    def test_portal_and_server_recovery_activate_before_capture_and_restore_on_failure(self):
        from contextlib import contextmanager
        from unittest.mock import Mock, patch
        from tbh_automation.windows import Win32AutomationDriver
        from tbh_automation.engine import ControlGate, AutomationError
        for method in ('check_boss_portal','recover_server_error'):
            events=[]
            driver=object.__new__(Win32AutomationDriver)
            driver.checkpoint=Mock()
            driver._foreground=Mock(return_value=123)
            driver.gate=ControlGate();driver.generation=0
            @contextmanager
            def active(hwnd,*,wait):
                events.append('activate')
                try:yield
                finally:events.append('restore')
            def work():
                self.assertEqual(events,['activate'])
                events.append('capture')
                raise AutomationError('changed')
            setattr(driver,'_'+method+'_active',work)
            with patch('tbh_automation.windows.temporarily_uncover_window',side_effect=active):
                with self.assertRaises(AutomationError):getattr(driver,method)()
            self.assertEqual(events,['activate','capture','restore'])


class PortalFastPathTests(unittest.TestCase):
    def test_successful_ocr_skips_expensive_template_scan(self):
        from unittest.mock import patch
        image=Image.new('RGB',(20,20),'black');image.putpixel((1,1),(255,255,255))
        with patch('tbh_automation.portal._find_ocr_portal',return_value=Point(5,8)), patch('tbh_automation.portal._find_template_portal') as template:
            self.assertEqual(find_ready_portal(image.tobytes('raw','BGRX'),*image.size,prefer_ocr=True),Point(5,8))
            template.assert_not_called()
    def test_ocr_failure_preserves_template_fallback(self):
        from unittest.mock import patch
        image=Image.new('RGB',(20,20),'black');image.putpixel((1,1),(255,255,255))
        with patch('tbh_automation.portal._find_ocr_portal',return_value=None), patch('tbh_automation.portal._find_template_portal',return_value=Point(5,8)) as template:
            self.assertEqual(find_ready_portal(image.tobytes('raw','BGRX'),*image.size,prefer_ocr=True),Point(5,8))
            template.assert_called_once()


class BeforePressTests(unittest.TestCase):
    def test_recheck_runs_once_after_settle_and_before_mouse_down(self):
        from contextlib import nullcontext
        from unittest.mock import Mock,patch
        from tbh_automation.windows import click_game_point
        events=[]
        api=Mock()
        api.SetCursorPos.side_effect=lambda *args: events.append('move') or True
        api.mouse_event.side_effect=lambda *args: events.append('mouse')
        with patch('tbh_automation.windows.user32',api), patch('tbh_automation.windows.temporarily_uncover_window',return_value=nullcontext()), patch('tbh_automation.windows.client_to_screen',return_value=Point(5,8)), patch('tbh_automation.windows.point_is_safe_on_active_game',return_value=True), patch('tbh_automation.windows.get_foreground_root',return_value=123), patch('tbh_automation.windows._root_window',return_value=123):
            click_game_point(123,Point(5,8),'test',wait=lambda seconds:events.append('settle'),before_press=lambda:events.append('verify'))
        self.assertEqual(events,['move','settle','verify','mouse','mouse'])


class OverlappingPortalTests(unittest.TestCase):
    def test_same_label_box_merges_even_with_five_pixel_target_jitter(self):
        from tbh_automation.portal import _add_ocr_candidate
        found=[]
        _add_ocr_candidate(found,Point(652,1605),15,(630,1540,674,1556))
        _add_ocr_candidate(found,Point(652,1610),15,(630,1541,674,1557))
        self.assertEqual(len(found),1)
        self.assertEqual(found[0][0],Point(652,1605))
        with self.assertRaises(ValueError):
            _add_ocr_candidate(found,Point(652,1610),16,(630,1541,674,1557))
    def test_distinct_label_boxes_are_never_merged_by_target_distance(self):
        from tbh_automation.portal import _add_ocr_candidate
        found=[]
        _add_ocr_candidate(found,Point(20,30),15,(0,0,40,15))
        _add_ocr_candidate(found,Point(21,31),15,(0,20,40,35))
        self.assertEqual(len(found),2)
    def test_actual_overlapping_tile_regression(self):
        from pathlib import Path
        from tbh_automation.portal import _find_ocr_portal
        im=Image.open(Path(__file__).parent/'fixtures/portal/overlap-fifteen.png').convert('RGB')
        point=_find_ocr_portal(im,lambda:None)
        self.assertIsNotNone(point)
        self.assertLessEqual(abs(point.x-652),4)
        self.assertLessEqual(abs(point.y-1605),4)
    def test_white_bits_preserve_all_pixels_and_odd_row_widths(self):
        import random
        from tbh_automation.vision_bits import white_pixel_rows
        rng=random.Random(9)
        for width in (1,7,8,9,31,40):
            pixels=[tuple(rng.randrange(256) for _ in range(3)) for _ in range(width*7)]
            im=Image.new('RGB',(width,7));im.putdata(pixels)
            expected=[sum((min(pixel)>=175)<<x for x,pixel in enumerate(pixels[y*width:(y+1)*width])) for y in range(7)]
            self.assertEqual(white_pixel_rows(im),expected)


class BatchOCRTests(unittest.TestCase):
    def test_batch_preserves_separate_image_results_and_empty_images(self):
        from tbh_automation.portal_ocr import read_words_batch,read_words
        from pathlib import Path
        blank=Image.new('RGB',(120,80),'white')
        image=Image.open(Path(__file__).parent/'fixtures/portal/thirteen.png').convert('RGB')
        result=read_words_batch([blank,image])
        self.assertEqual(result[0],[])
        self.assertEqual(result[1],read_words(image))
        self.assertEqual(read_words_batch([]),[])
    def test_incomplete_batch_is_rejected(self):
        from unittest.mock import patch
        from tbh_automation.portal_ocr import read_words_batch
        with patch('tbh_automation.portal_ocr._ocr_result',return_value=[[]]):
            with self.assertRaises(RuntimeError):read_words_batch([Image.new('RGB',(10,10)),Image.new('RGB',(10,10))])
    def test_cancel_before_encoding_never_launches_process(self):
        from unittest.mock import Mock,patch
        from tbh_automation.portal_ocr import read_words_batch
        from tbh_automation.engine import RoundCancelled
        with patch('tbh_automation.portal_ocr.subprocess.Popen') as process:
            with self.assertRaises(RoundCancelled):
                read_words_batch([Image.new('RGB',(10,10))],checkpoint=Mock(side_effect=RoundCancelled))
            process.assert_not_called()


class ExactPrefixPrefilterTests(unittest.TestCase):
    def test_prefilter_retains_every_exact_prefix_at_odd_and_even_positions(self):
        from tbh_automation.server_error import _prefix_probes
        import random
        rng=random.Random(42)
        for width in (9,27,55,110):
            template=tuple(rng.getrandbits(width) for _ in range(15))
            probes=_prefix_probes(template,width)
            for x in (0,1,7,13):
                rows=[rng.getrandbits(width*3) for _ in template]
                mask=((1<<width)-1)<<x
                rows=[(row&~mask)|(expected<<x) for row,expected in zip(rows,template)]
                for y,j,white in probes:
                    self.assertEqual(bool(rows[y]&(1<<(x+j))),white)


class HoverPortalTests(unittest.TestCase):
    def test_actual_hover_requires_prior_target_and_fresh_counter(self):
        from pathlib import Path
        im=Image.open(Path(__file__).parent/'fixtures/portal/hover-seven.png').convert('RGB')
        data=im.tobytes('raw','BGRX')
        self.assertIsNone(find_ready_portal(data,*im.size,prefer_ocr=True,allow_template_fallback=False))
        self.assertEqual(find_ready_portal(data,*im.size,prefer_ocr=True,hover_target=Point(652,1605),allow_template_fallback=False),Point(652,1605))
        self.assertIsNone(find_ready_portal(data,*im.size,prefer_ocr=True,hover_target=Point(700,1605),allow_template_fallback=False))
    def test_tooltip_text_is_exact_and_bounds_are_required(self):
        from unittest.mock import patch
        from tbh_automation.portal import _confirm_hover_tooltip
        im=Image.new('RGB',(200,200),'black')
        for words in ([],[dict(text='其他',x=80,y=80,width=80,height=80)]):
            with patch('tbh_automation.portal.read_words_batch',return_value=[words]):
                self.assertIsNone(_confirm_hover_tooltip(im,Point(100,100),10,lambda:None))
        self.assertIsNone(_confirm_hover_tooltip(im,Point(0,0),10,lambda:None))


class PortalRetryTests(unittest.TestCase):
    def driver(self):
        from unittest.mock import Mock
        from tbh_automation.windows import Win32AutomationDriver
        from tbh_automation.model import Calibration,WindowIdentity
        from tbh_automation.engine import ControlGate
        driver=object.__new__(Win32AutomationDriver)
        driver.calibration=Calibration(WindowIdentity(client_width=800,client_height=600),{})
        driver.gate=ControlGate();driver.generation=0
        driver.gate.interruptible_wait=Mock()
        driver.checkpoint=Mock();driver._foreground=Mock(return_value=123)
        driver.record_problem=Mock();driver.status=Mock()
        return driver
    def run_case(self,outcomes):
        from unittest.mock import Mock,patch
        from contextlib import nullcontext
        driver=self.driver();driver._check_boss_portal_active=Mock(side_effect=outcomes)
        api=Mock();api.SetCursorPos.return_value=True
        with patch('tbh_automation.windows.user32',api),patch('tbh_automation.windows.temporarily_uncover_window',return_value=nullcontext()),patch('tbh_automation.windows.get_foreground_root',return_value=123),patch('tbh_automation.windows._root_window',return_value=123),patch('tbh_automation.windows.client_to_screen',return_value=Point(400,300)):
            result=driver.check_boss_portal()
        return result,driver,api
    def test_transient_frame_restarts_complete_confirmation(self):
        from tbh_automation.windows import PortalFrameUnconfirmed
        result,driver,api=self.run_case([PortalFrameUnconfirmed('occluded'),True])
        self.assertTrue(result)
        self.assertEqual(driver._check_boss_portal_active.call_count,2)
        api.SetCursorPos.assert_called_once_with(400,300)
        api.mouse_event.assert_not_called()
    def test_three_unconfirmed_frames_skip_without_pause_or_click(self):
        from tbh_automation.windows import PortalFrameUnconfirmed
        result,driver,api=self.run_case([PortalFrameUnconfirmed('uncertain')]*3)
        self.assertFalse(result)
        self.assertTrue(driver.gate.enabled)
        self.assertEqual(driver._check_boss_portal_active.call_count,3)
        self.assertEqual(api.SetCursorPos.call_count,2)
        api.mouse_event.assert_not_called()
    def test_unknown_errors_still_propagate(self):
        from tbh_automation.engine import AutomationError
        with self.assertRaises(AutomationError):self.run_case([AutomationError('window changed')])
        with self.assertRaises(ValueError):self.run_case([ValueError('ambiguous')])


class SmallStageLabelTests(unittest.TestCase):
    def test_actual_eight_frame_label_survives_resizing_retry(self):
        from pathlib import Path
        im=Image.open(Path(__file__).parent/'fixtures/portal/label-eight-small.png').convert('RGB')
        self.assertEqual(find_ready_portal(im.tobytes('raw','BGRX'),*im.size,prefer_ocr=True,allow_template_fallback=False),Point(652,1605))
