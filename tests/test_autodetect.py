from __future__ import annotations

import unittest

from tbh_automation.autodetect import (
    AUTO_FILL_MASK,
    find_cube_icon,
    find_auto_fill_anchor,
    layout_points_from_anchor,
)
from tbh_automation.model import Point


def gray_bgra(value: int) -> bytes:
    return bytes((value, value, value, 255))


def paint_rectangle(
    image: bytearray,
    width: int,
    left: int,
    top: int,
    right: int,
    bottom: int,
    color: bytes,
) -> None:
    for y in range(top, bottom + 1):
        for x in range(left, right + 1):
            offset = (y * width + x) * 4
            image[offset:offset + 4] = color


class AutoDetectionTests(unittest.TestCase):
    def test_finds_single_large_yellow_cube_icon_in_lower_half(self) -> None:
        width, height = 400, 320
        image = bytearray(gray_bgra(20) * (width * height))
        paint_rectangle(image, width, 220, 200, 335, 315, bytes((10, 30, 70, 255)))
        for y in range(230, 286):
            for x in range(251, 307):
                if x not in (251, 252, 305, 306) and y not in (230, 231, 284, 285):
                    continue
                offset = (y * width + x) * 4
                image[offset:offset + 4] = bytes((20, 150, 220, 255))

        result = find_cube_icon(bytes(image), width, height)

        self.assertEqual(result.center, Point(278, 257))
        self.assertEqual(result.bounds, (251, 230, 306, 285))

    def test_multiple_cube_like_icons_are_rejected(self) -> None:
        width, height = 400, 320
        image = bytearray(gray_bgra(20) * (width * height))
        for left in (80, 250):
            paint_rectangle(
                image,
                width,
                left - 25,
                205,
                left + 74,
                304,
                bytes((10, 30, 70, 255)),
            )
            for y in range(230, 280):
                for x in range(left, left + 50):
                    if x not in (left, left + 1, left + 48, left + 49) and y not in (230, 231, 278, 279):
                        continue
                    offset = (y * width + x) * 4
                    image[offset:offset + 4] = bytes((20, 150, 220, 255))

        with self.assertRaisesRegex(ValueError, "多个"):
            find_cube_icon(bytes(image), width, height)

    def test_finds_solid_cube_from_current_runtime_screenshot_style(self) -> None:
        width, height = 400, 320
        image = bytearray(gray_bgra(32) * (width * height))
        paint_rectangle(image, width, 230, 195, 330, 305, bytes((10, 30, 70, 255)))
        paint_rectangle(image, width, 261, 231, 299, 269, bytes((20, 150, 220, 255)))

        result = find_cube_icon(bytes(image), width, height)

        self.assertEqual(result.center, Point(280, 250))
        self.assertEqual(result.bounds, (261, 231, 299, 269))

    def test_uses_navigation_row_to_distinguish_cube_from_other_gold_icons(self) -> None:
        width, height = 600, 400
        image = bytearray(gray_bgra(32) * (width * height))
        brown = bytes((10, 30, 70, 255))
        yellow = bytes((20, 150, 220, 255))
        for center_x in (100, 190, 280, 370):
            paint_rectangle(
                image,
                width,
                center_x - 45,
                255,
                center_x + 45,
                345,
                brown,
            )
        for center_x in (100, 190, 280):
            paint_rectangle(
                image,
                width,
                center_x - 15,
                285,
                center_x + 15,
                315,
                yellow,
            )
        # 魔方内部的深色线条会把黄色图案切成三块，单块都不满足候选尺寸。
        paint_rectangle(image, width, 355, 282, 385, 293, yellow)
        paint_rectangle(image, width, 354, 297, 367, 318, yellow)
        paint_rectangle(image, width, 373, 297, 386, 318, yellow)

        result = find_cube_icon(bytes(image), width, height)

        self.assertEqual(result.center, Point(370, 300))
        self.assertEqual(result.bounds, (354, 282, 386, 318))

    def test_finds_fragmented_cube_when_navigation_icons_are_small(self) -> None:
        """复现 125% 显示器截图中较小的真实功能栏比例。"""
        width, height = 616, 612
        image = bytearray(gray_bgra(32) * (width * height))
        brown = bytes((10, 30, 70, 255))
        yellow = bytes((20, 150, 220, 255))
        centers = (124, 170, 216, 262)
        center_y = 390
        for center_x in centers:
            paint_rectangle(
                image,
                width,
                center_x - 19,
                center_y - 19,
                center_x + 19,
                center_y + 19,
                brown,
            )

        # 前三个功能图标约为 21×18 像素；旧规则要求宽高均至少 24 像素。
        paint_rectangle(image, width, 114, 381, 134, 398, yellow)
        paint_rectangle(image, width, 160, 381, 180, 399, yellow)
        paint_rectangle(image, width, 206, 380, 226, 400, yellow)
        # 魔方被深色线条切成三块，必须由前三个等距图标推导后再整体复核。
        paint_rectangle(image, width, 253, 379, 270, 386, yellow)
        paint_rectangle(image, width, 252, 389, 260, 402, yellow)
        paint_rectangle(image, width, 264, 389, 272, 402, yellow)

        result = find_cube_icon(bytes(image), width, height)

        self.assertEqual(result.center, Point(262, 390))
        self.assertEqual(result.bounds, (252, 379, 272, 402))

    def test_large_bottom_scene_icon_from_runtime_log_is_rejected(self) -> None:
        width, height = 1455, 1338
        image = bytearray(gray_bgra(20) * (width * height))
        for y in range(1189, 1300):
            for x in range(227, 306):
                if (x + y) % 3:
                    continue
                offset = (y * width + x) * 4
                image[offset:offset + 4] = bytes((20, 150, 220, 255))

        with self.assertRaisesRegex(ValueError, "未识别"):
            find_cube_icon(bytes(image), width, height)

    def test_finds_auto_fill_text_inside_larger_image(self) -> None:
        width, height = 180, 80
        anchor = Point(73, 31)
        image = bytearray(gray_bgra(25) * (width * height))
        for y, row in enumerate(AUTO_FILL_MASK):
            for x, character in enumerate(row):
                value = 105 if character == "#" else 25
                offset = ((anchor.y + y) * width + anchor.x + x) * 4
                image[offset:offset + 4] = gray_bgra(value)

        result = find_auto_fill_anchor(bytes(image), width, height)

        self.assertEqual(result.anchor, anchor)
        self.assertEqual(result.mismatch_ratio, 0.0)

    def test_finds_auto_fill_text_when_interface_is_scaled_to_125_percent(self) -> None:
        width, height = 240, 110
        anchor = Point(41, 23)
        scale = 1.25
        image = bytearray(gray_bgra(25) * (width * height))
        scaled_width = round(len(AUTO_FILL_MASK[0]) * scale)
        scaled_height = round(len(AUTO_FILL_MASK) * scale)
        for target_y in range(scaled_height):
            source_y = min(int(target_y / scale), len(AUTO_FILL_MASK) - 1)
            for target_x in range(scaled_width):
                source_x = min(int(target_x / scale), len(AUTO_FILL_MASK[0]) - 1)
                value = 105 if AUTO_FILL_MASK[source_y][source_x] == "#" else 25
                offset = ((anchor.y + target_y) * width + anchor.x + target_x) * 4
                image[offset:offset + 4] = gray_bgra(value)

        result = find_auto_fill_anchor(bytes(image), width, height)

        self.assertEqual(result.anchor, anchor)
        self.assertAlmostEqual(result.scale, scale)

    def test_preferred_150_percent_scale_finds_physical_pixel_capture(self) -> None:
        width, height = 360, 180
        anchor = Point(211, 97)
        scale = 1.5
        image = bytearray(gray_bgra(25) * (width * height))
        scaled_width = round(len(AUTO_FILL_MASK[0]) * scale)
        scaled_height = round(len(AUTO_FILL_MASK) * scale)
        for target_y in range(scaled_height):
            source_y = min(int(target_y / scale), len(AUTO_FILL_MASK) - 1)
            for target_x in range(scaled_width):
                source_x = min(int(target_x / scale), len(AUTO_FILL_MASK[0]) - 1)
                value = 105 if AUTO_FILL_MASK[source_y][source_x] == "#" else 25
                offset = ((anchor.y + target_y) * width + anchor.x + target_x) * 4
                image[offset:offset + 4] = gray_bgra(value)

        result = find_auto_fill_anchor(
            bytes(image),
            width,
            height,
            preferred_scale=1.5,
        )

        self.assertEqual(result.anchor, anchor)
        self.assertEqual(result.scale, scale)

    def test_button_color_check_rejects_terminal_like_white_text(self) -> None:
        width, height = 180, 80
        anchor = Point(73, 31)
        image = bytearray(gray_bgra(25) * (width * height))
        for y, row in enumerate(AUTO_FILL_MASK):
            for x, character in enumerate(row):
                value = 105 if character == "#" else 25
                offset = ((anchor.y + y) * width + anchor.x + x) * 4
                image[offset:offset + 4] = gray_bgra(value)

        with self.assertRaises(ValueError):
            find_auto_fill_anchor(
                bytes(image),
                width,
                height,
                preferred_scale=1.0,
                require_button_color=True,
            )

    def test_button_color_prefilter_allows_text_over_the_center_pixel(self) -> None:
        width, height = 180, 80
        anchor = Point(73, 31)
        center = Point(anchor.x + 29, anchor.y + 7)
        image = bytearray(gray_bgra(25) * (width * height))
        paint_rectangle(
            image,
            width,
            center.x - 24,
            center.y - 12,
            center.x + 24,
            center.y + 12,
            bytes((10, 30, 70, 255)),
        )
        for y, row in enumerate(AUTO_FILL_MASK):
            for x, character in enumerate(row):
                if character != "#":
                    continue
                offset = ((anchor.y + y) * width + anchor.x + x) * 4
                image[offset:offset + 4] = gray_bgra(105)
        # 真实截图中按钮中心落在文字边缘，RGB=(67,56,45)，红绿差仅 11。
        center_offset = (center.y * width + center.x) * 4
        image[center_offset:center_offset + 4] = bytes((45, 56, 67, 255))

        result = find_auto_fill_anchor(
            bytes(image),
            width,
            height,
            preferred_scale=1.0,
            require_button_color=True,
        )

        self.assertEqual(result.anchor, anchor)
        self.assertEqual(result.scale, 1.0)

    def test_large_capture_does_not_skip_an_odd_pixel_anchor(self) -> None:
        width, height = 671, 643
        anchor = Point(431, 317)
        image = bytearray(gray_bgra(25) * (width * height))
        for y, row in enumerate(AUTO_FILL_MASK):
            for x, character in enumerate(row):
                value = 105 if character == "#" else 25
                offset = ((anchor.y + y) * width + anchor.x + x) * 4
                image[offset:offset + 4] = gray_bgra(value)

        result = find_auto_fill_anchor(bytes(image), width, height)

        self.assertEqual(result.anchor, anchor)

    def test_layout_points_match_recorded_cube_geometry(self) -> None:
        points = layout_points_from_anchor(Point(566, 407))
        self.assertEqual(points["auto_fill"], Point(595, 414))
        self.assertEqual(points["combine"], Point(718, 414))
        self.assertEqual(points["return_items"], Point(570, 362))
        self.assertEqual(points["category_equipment"], Point(594, 448))
        self.assertEqual(points["category_material"], Point(594, 476))

    def test_layout_offsets_scale_with_detected_interface(self) -> None:
        points = layout_points_from_anchor(Point(100, 200), 1.25)
        self.assertEqual(points["auto_fill"], Point(136, 209))
        self.assertEqual(points["combine"], Point(290, 209))
        self.assertEqual(points["return_items"], Point(105, 144))


if __name__ == "__main__":
    unittest.main()
