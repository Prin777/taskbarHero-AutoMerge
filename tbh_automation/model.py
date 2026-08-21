from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


APP_DIR_NAME = "TaskbarHeroAutoSynth"


def default_app_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / APP_DIR_NAME


@dataclass(frozen=True)
class Point:
    x: int
    y: int

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Point":
        return cls(x=int(value["x"]), y=int(value["y"]))


@dataclass
class WindowIdentity:
    title: str = ""
    class_name: str = ""
    process_name: str = ""
    client_width: int = 0
    client_height: int = 0


@dataclass
class Calibration:
    window: WindowIdentity
    points: dict[str, Point]
    empty_grid_patches: list[str]
    calibration_version: int = 3
    patch_size: int = 16
    difference_threshold: float = 15.0

    REQUIRED_POINTS = {
        "grid_top_left",
        "grid_bottom_right",
        "auto_fill",
        "category_dropdown",
        "category_equipment",
        "category_material",
        "combine",
        "return_items",
    }

    def validate(self) -> None:
        if self.calibration_version != 3:
            raise ValueError("旧版九宫格基准已失效，请重新自动识别或手动校准")
        missing = self.REQUIRED_POINTS.difference(self.points)
        if missing:
            raise ValueError(f"校准配置缺少位置：{', '.join(sorted(missing))}")
        if len(self.empty_grid_patches) != 9:
            raise ValueError("空九宫格基准必须恰好包含 9 个采样块")
        if self.window.client_width <= 0 or self.window.client_height <= 0:
            raise ValueError("游戏窗口尺寸无效")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Calibration":
        result = cls(
            window=WindowIdentity(**value["window"]),
            points={key: Point.from_dict(point) for key, point in value["points"].items()},
            empty_grid_patches=list(value["empty_grid_patches"]),
            calibration_version=int(value.get("calibration_version", 0)),
            patch_size=int(value.get("patch_size", 16)),
            difference_threshold=float(value.get("difference_threshold", 15.0)),
        )
        result.validate()
        return result


@dataclass
class AppConfig:
    calibration: Calibration | None = None
    interval_seconds: int = 5 * 60 * 60
    max_combinations_per_category: int = 100
    click_delay_seconds: float = 0.35
    action_timeout_seconds: float = 12.0
    version: int = 5

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        config_path = path or default_app_dir() / "config.json"
        if not config_path.exists():
            return cls()
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        calibration_raw = raw.pop("calibration", None)
        config = cls(**raw)
        if calibration_raw is not None:
            config.calibration = Calibration.from_dict(calibration_raw)
        return config

    def save(self, path: Path | None = None) -> Path:
        config_path = path or default_app_dir() / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = config_path.with_suffix(".tmp")
        temp_path.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(config_path)
        return config_path
