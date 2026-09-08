from __future__ import annotations

import unittest
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from tbh_automation.autodetect import (
    AutoDetectionResult,
    AmbiguousDetectionError,
    MAX_MISMATCH_RATIO,
    SynthesisLayoutError,
    combine_button_sample_size,
    find_auto_fill_anchor,
    find_cube_icon,
    layout_points_from_anchor,
)
from tbh_automation.engine import AutomationError, RoundCancelled
from tbh_automation.model import Calibration, Point, WindowIdentity
from tbh_automation.windows import Win32AutomationDriver, blue_highlight_ratio


FIXTURES = Path(__file__).parent / 'fixtures' / 'scaling'
SCALES = (1.0, 1.25, 1.5, 2.0, 3.0)


def scaled_image(name: str, scale: float) -> Image.Image:
    with Image.open(FIXTURES / name) as original:
        return original.convert('RGBA').resize(
            (round(original.width * scale), round(original.height * scale)),
            Image.Resampling.NEAREST,
        )


class ScaleSupportTests(unittest.TestCase):
    def test_equipment_and_material_controls_at_all_five_scales(self) -> None:
        for category in ('equipment', 'material'):
            for scale in SCALES:
                with self.subTest(category=category, scale=scale):
                    image = scaled_image(f'{category}-controls-x1.png', scale)
                    result = find_auto_fill_anchor(
                        image.tobytes('raw', 'BGRA'), *image.size,
                        preferred_scale=scale,
                        require_button_color=True, require_synthesis_layout=True,
                    )
                    self.assertEqual(result.scale, scale)
                    self.assertLessEqual(result.mismatch_ratio, MAX_MISMATCH_RATIO)
                    points = layout_points_from_anchor(result.anchor, result.scale, client_size=image.size)
                    self.assertLessEqual(abs(points['combine'].x - 196 * scale), 2)
                    self.assertLessEqual(abs(points['combine'].y - 65 * scale), 2)
                    self.assertLessEqual(abs(points['return_items'].x - 48 * scale), 2)
                    self.assertLessEqual(abs(points['return_items'].y - 13 * scale), 2)
                    size = combine_button_sample_size(result.scale)
                    self.assertEqual(size, (32, 40, 48, 64, 96)[SCALES.index(scale)])
                    center = points['combine']
                    left, top = center.x - size // 2, center.y - size // 2
                    button = image.crop((left, top, left + size, top + size))
                    self.assertGreater(blue_highlight_ratio(button.tobytes('raw', 'BGRA')), 0.60)
                    grey = button.convert('L').convert('RGBA')
                    self.assertEqual(blue_highlight_ratio(grey.tobytes('raw', 'BGRA')), 0.0)

    def test_triple_scale_does_not_follow_windows_dpi_preference(self) -> None:
        image = scaled_image('equipment-controls-x1.png', 3)
        result = find_auto_fill_anchor(
            image.tobytes('raw', 'BGRA'), *image.size,
            preferred_scale=1.25, require_button_color=True, require_synthesis_layout=True,
        )
        self.assertEqual(result.scale, 3)
        self.assertEqual(result.anchor, Point(132, 174))
        center = layout_points_from_anchor(result.anchor, result.scale)['combine']
        old_patch = image.crop((center.x - 16, center.y - 16, center.x + 16, center.y + 16))
        self.assertLess(blue_highlight_ratio(old_patch.tobytes('raw', 'BGRA')), 0.25)

    def test_navigation_row_at_all_five_scales(self) -> None:
        for scale in SCALES:
            with self.subTest(scale=scale):
                image = Image.new('RGBA', (round(420 * scale), round(360 * scale)), (20, 20, 20, 255))
                image.paste(scaled_image('navigation-x1.png', scale), (round(40 * scale), round(210 * scale)))
                result = find_cube_icon(image.tobytes('raw', 'BGRA'), *image.size)
                # 允许两个基准像素的中心偏差；放大时容差同样换为物理像素。
                self.assertLessEqual(abs(result.center.x - 242 * scale), 2 * scale)
                self.assertLessEqual(abs(result.center.y - 242 * scale), 2 * scale)

    def test_enlarged_isolated_yellow_shapes_need_navigation_evidence(self) -> None:
        for outline in (False, True):
            with self.subTest(outline=outline):
                image = Image.new('RGBA', (600, 500), (20, 20, 20, 255))
                draw = ImageDraw.Draw(image)
                draw.rectangle((150, 225, 430, 435), fill=(70, 30, 10, 255))
                if outline:
                    draw.rectangle((220, 260, 359, 399), outline=(220, 150, 20, 255), width=4)
                else:
                    draw.rectangle((245, 285, 334, 374), fill=(220, 150, 20, 255))
                with self.assertRaises(ValueError):
                    find_cube_icon(image.tobytes('raw', 'BGRA'), *image.size)

    def test_two_navigation_rows_are_rejected(self) -> None:
        image = Image.new('RGBA', (900, 360), (20, 20, 20, 255))
        row = scaled_image('navigation-x1.png', 1)
        image.paste(row, (40, 210))
        image.paste(row, (500, 210))
        with self.assertRaises(ValueError):
            find_cube_icon(image.tobytes('raw', 'BGRA'), *image.size)

    def test_two_panels_at_different_scales_are_rejected(self) -> None:
        image = Image.new('RGBA', (1100, 500), (20, 20, 20, 255))
        image.paste(scaled_image('equipment-controls-x1.png', 1), (0, 80))
        image.paste(scaled_image('material-controls-x1.png', 3), (320, 20))
        with self.assertRaises(AmbiguousDetectionError):
            find_auto_fill_anchor(
                image.tobytes('raw', 'BGRA'), *image.size,
                preferred_scale=1, require_button_color=True, require_synthesis_layout=True,
            )

    def test_brown_prefilter_preserves_odd_anchors_in_large_images(self) -> None:
        image = Image.new('RGBA', (1100, 750), (20, 20, 20, 255))
        image.paste(scaled_image('equipment-controls-x1.png', 3), (101, 113))
        result = find_auto_fill_anchor(
            image.tobytes('raw', 'BGRA'), *image.size,
            preferred_scale=1.25, require_button_color=True, require_synthesis_layout=True,
        )
        self.assertEqual(result.anchor, Point(233, 287))
        self.assertEqual(result.scale, 3)

    def test_nearby_nonoverlapping_text_at_different_scales_is_ambiguous(self) -> None:
        image = Image.new('RGBA', (400, 110), (100, 40, 10, 255))
        text = scaled_image('equipment-controls-x1.png', 1).crop((44, 58, 103, 71))
        image.paste(text, (20, 30))
        image.paste(text.resize((177, 39), Image.Resampling.NEAREST), (130, 30))
        with self.assertRaises(AmbiguousDetectionError):
            find_auto_fill_anchor(
                image.tobytes('raw', 'BGRA'), *image.size,
                preferred_scale=1, require_button_color=True,
            )

    def test_text_without_the_adjacent_category_menu_is_rejected(self) -> None:
        image = scaled_image('equipment-controls-x1.png', 1)
        ImageDraw.Draw(image).rectangle((118, 47, 140, 83), fill=(50, 50, 50, 255))
        with self.assertRaisesRegex(SynthesisLayoutError, '合成.*模式'):
            find_auto_fill_anchor(
                image.tobytes('raw', 'BGRA'), *image.size,
                preferred_scale=1, require_button_color=True, require_synthesis_layout=True,
            )

    def test_large_layout_and_sample_must_fit_inside_client(self) -> None:
        # All six centers fit, but the 96-pixel combine sampling square does not.
        with self.assertRaisesRegex(SynthesisLayoutError, '按钮显示不完整'):
            layout_points_from_anchor(Point(50, 200), 3, client_size=(520, 500))
        with self.assertRaisesRegex(SynthesisLayoutError, '控件超出'):
            layout_points_from_anchor(Point(50, 100), 3, client_size=(700, 600))
        for scale in (0, -1, 4, float('nan'), float('inf')):
            with self.subTest(scale=scale), self.assertRaises(ValueError):
                combine_button_sample_size(scale)

    def test_visual_scans_can_be_cancelled(self) -> None:
        image = Image.new('RGBA', (800, 500), (70, 30, 10, 255))
        for operation in (find_cube_icon, find_auto_fill_anchor):
            calls = 0

            def checkpoint() -> None:
                nonlocal calls
                calls += 1
                if calls == 5:
                    raise RoundCancelled()

            with self.subTest(operation=operation.__name__), self.assertRaises(RoundCancelled):
                operation(image.tobytes('raw', 'BGRA'), *image.size, checkpoint=checkpoint)


def driver_at_triple_scale() -> Win32AutomationDriver:
    calibration = Calibration(
        window=WindowIdentity(process_name='TaskBarHero.exe', class_name='UnityWndClass', client_width=800, client_height=700),
        points=layout_points_from_anchor(Point(100, 200), 3),
    )
    driver = Win32AutomationDriver(calibration, Mock(), 0, click_delay=0.35, combination_wait=2.5)
    driver._interface_scale = 3
    driver.checkpoint = Mock()
    driver._foreground = Mock(return_value=123)
    return driver


class ScaledDriverTests(unittest.TestCase):
    def test_final_resample_at_triple_scale_blocks_a_newly_grey_button(self) -> None:
        driver = driver_at_triple_scale()
        blue = bytes((73, 60, 21, 255)) * (96 * 96)
        grey = bytes((50, 50, 50, 255)) * (96 * 96)
        with (
            patch('tbh_automation.windows.temporarily_uncover_window', return_value=nullcontext()),
            patch('tbh_automation.windows.client_to_screen', side_effect=lambda _hwnd, point: point),
            patch('tbh_automation.windows.point_is_safe_on_active_game', return_value=True),
            patch('tbh_automation.windows.get_foreground_root', return_value=123),
            patch('tbh_automation.windows._root_window', side_effect=lambda hwnd: hwnd),
            patch('tbh_automation.windows.user32.SetCursorPos', return_value=True),
            patch('tbh_automation.windows.capture_client_patch', side_effect=[blue, blue, grey]) as capture,
            patch('tbh_automation.windows.user32.mouse_event') as mouse,
        ):
            self.assertTrue(driver.combine_button_is_enabled())
            with self.assertRaisesRegex(AutomationError, '灰色'):
                driver.click_combine()
        self.assertEqual([call.args[2] for call in capture.call_args_list], [96, 96, 96])
        mouse.assert_not_called()

    def test_scaled_diagnostic_has_matching_pixel_dimensions(self) -> None:
        driver = driver_at_triple_scale()
        with TemporaryDirectory() as temporary:
            driver.diagnostics_dir = Path(temporary)
            driver._save_button_diagnostic(bytes((73, 60, 21, 255)) * (96 * 96), 1.0, True)
            with Image.open(next(Path(temporary).glob('*.bmp'))) as image:
                self.assertEqual(image.size, (96, 96))

    def test_clipped_detection_does_not_replace_existing_calibration(self) -> None:
        driver = driver_at_triple_scale()
        previous = dict(driver.calibration.points)
        result = AutoDetectionResult(Point(700, 400), 0.01, 3)
        with patch('tbh_automation.windows.open_synthesis_interface', return_value=(b'frame', 800, 700, result)):
            with self.assertRaises(AutomationError):
                driver.prepare_interface()
        self.assertEqual(driver.calibration.points, previous)


if __name__ == '__main__':
    unittest.main()
