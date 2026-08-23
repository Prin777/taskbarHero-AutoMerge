from __future__ import annotations

import unittest

from tbh_automation.autodetect import (
    AUTO_FILL_MASK,
    find_auto_fill_anchor,
    layout_points_from_anchor,
)
from tbh_automation.model import Point


def gray_bgra(value: int) -> bytes:
    return bytes((value, value, value, 255))


class AutoDetectionTests(unittest.TestCase):
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

        with self.assertRaisesRegex(ValueError, "棕色"):
            find_auto_fill_anchor(
                bytes(image),
                width,
                height,
                preferred_scale=1.0,
                require_button_color=True,
            )

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
        self.assertEqual(points["grid_top_left"], Point(605, 275))
        self.assertEqual(points["grid_bottom_right"], Point(685, 355))
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
