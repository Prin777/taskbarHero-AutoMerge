from pathlib import Path
import random
import unittest
from PIL import Image
from tbh_automation.autodetect import (
    SUPPORTED_SCALES, GRAY_THRESHOLD, MAX_MISMATCH_RATIO,
    _scaled_samples, _best_match_at_scale, find_auto_fill_anchor,
    layout_points_from_anchor,
)
from tbh_automation.model import Point
from tbh_automation.windows import blue_highlight_ratio


def scalar_reference(gray, width, height, scale, brown):
    """Exhaustive pixel oracle: original prefilter and complete mismatch count."""
    mw, mh, samples = _scaled_samples(scale)
    foreground = [s for s in samples if s[2]]
    background = [s for s in samples if not s[2]]
    prefilter = foreground[::max(1, len(foreground)//8)][:8] + background[::max(1, len(background)//8)][:8]
    offsets = [(round(29*scale)+round(dx*scale), round(7*scale)+round(dy*scale))
               for dx, dy in ((0,0),(-12,-5),(12,-5),(-12,5),(12,5),(-22,0),(22,0))]
    best, best_ratio, matches = None, 1.0, []
    for y in range(height-mh+1):
        for x in range(width-mw+1):
            if brown is not None and sum(brown[(y+dy)*width+x+dx] for dx,dy in offsets if 0 <= y+dy < height) < 2:
                continue
            if sum((gray[(y+dy)*width+x+dx] >= GRAY_THRESHOLD) != on for dx,dy,on in prefilter) > 4:
                continue
            ratio = sum((gray[(y+dy)*width+x+dx] >= GRAY_THRESHOLD) != on for dx,dy,on in samples) / len(samples)
            if ratio <= MAX_MISMATCH_RATIO:
                matches.append((Point(x,y),scale,ratio))
            if ratio < best_ratio:
                best, best_ratio = Point(x,y), ratio
    return (best, best_ratio), matches


class DetectionBatchTests(unittest.TestCase):
    def test_batch_matches_exhaustive_pixel_oracle_including_all_candidates(self):
        rng = random.Random(8162)
        for scale in SUPPORTED_SCALES:
            mw,mh,samples = _scaled_samples(scale)
            width,height = mw+10,mh+10
            gray = bytearray(rng.choice((0,64,65,255)) for _ in range(width*height))
            for x,y,on in samples:
                gray[(y+5)*width+x+3] = 65 if on else 64
            for brown in (None, bytes(rng.randrange(2) for _ in gray)):
                with self.subTest(scale=scale, brown=brown is not None):
                    expected, matches = scalar_reference(gray,width,height,scale,brown)
                    actual_matches = []
                    actual = _best_match_at_scale(gray,width,height,scale,brown,matches=actual_matches)
                    self.assertEqual(actual, expected)
                    self.assertEqual(actual_matches, matches)
                    self.assertTrue(any(point == Point(3,5) for point,_,_ in matches))

    def test_updated_real_double_scale_and_progress(self):
        path = Path(__file__).parent/'fixtures/scaling/updated-controls-x2.png'
        with Image.open(path) as source:
            image = source.convert('RGBA')
        progress = []
        result = find_auto_fill_anchor(image.tobytes('raw','BGRA'), *image.size,
            preferred_scale=1.25, require_button_color=True, require_synthesis_layout=True,
            progress=progress.append)
        self.assertEqual(result.scale,2)
        self.assertEqual(result.anchor,Point(89,110))
        self.assertEqual(len(progress), 1+len(SUPPORTED_SCALES))
        for i,message in enumerate(progress[1:],1):
            self.assertIn(f'（{i}/8）',message)
        points = layout_points_from_anchor(result.anchor,result.scale,client_size=image.size)
        self.assertEqual(points['category_dropdown'],Point(257,124))
        self.assertEqual(points['combine'],Point(393,124))
        patch = image.crop((361,92,425,156))
        self.assertEqual(blue_highlight_ratio(patch.tobytes('raw','BGRA')),0)

    def test_progress_does_not_prevent_cancellation(self):
        from tbh_automation.engine import RoundCancelled
        messages = []
        def checkpoint():
            if any('（2/8）' in message for message in messages):
                raise RoundCancelled()
        with self.assertRaises(RoundCancelled):
            find_auto_fill_anchor(bytes(250*140*4),250,140,
                checkpoint=checkpoint,progress=messages.append)
        self.assertFalse(any('（3/8）' in message for message in messages))
