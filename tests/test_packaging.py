from __future__ import annotations

import struct
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PackagingTests(unittest.TestCase):
    def test_embedded_icon_asset_contains_all_expected_sizes(self) -> None:
        icon_path = PROJECT_ROOT / "assets" / "taskbar-hero.ico"
        data = icon_path.read_bytes()
        reserved, icon_type, count = struct.unpack_from("<HHH", data)
        self.assertEqual((reserved, icon_type), (0, 1))
        self.assertEqual(count, 9)

        sizes = []
        for index in range(count):
            width, height = struct.unpack_from("<BB", data, 6 + index * 16)
            sizes.append((width or 256, height or 256))
        self.assertEqual(
            sizes,
            [
                (16, 16),
                (24, 24),
                (32, 32),
                (48, 48),
                (64, 64),
                (96, 96),
                (128, 128),
                (192, 192),
                (256, 256),
            ],
        )

    def test_build_cmd_embeds_and_verifies_icon_without_build_ps1(self) -> None:
        script = (PROJECT_ROOT / "build.cmd").read_text("utf-8")
        self.assertIn('--icon "%ICON_PATH%"', script)
        self.assertIn('--add-data "%ICON_PATH%;assets"', script)
        self.assertIn("-r requirements.txt", script)
        self.assertIn("Embedded icon mismatch", script)
        self.assertIn("The previous EXE was kept", script)
        self.assertIn("assert tcltk_info.available", script)
        self.assertIn('findstr /c:"missing module named tkinter"', script)
        self.assertNotIn("build.ps1", script.lower())
        self.assertFalse((PROJECT_ROOT / "build.ps1").exists())

    def test_runtime_dependencies_are_declared(self) -> None:
        requirements = (PROJECT_ROOT / "requirements.txt").read_text("utf-8")
        self.assertIn("Pillow", requirements)
        self.assertIn("pystray", requirements)


if __name__ == "__main__":
    unittest.main()
