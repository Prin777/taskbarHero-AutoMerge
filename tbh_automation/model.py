from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


APP_DIR_NAME = "TaskbarHeroAutoSynth"
LOG = logging.getLogger("tbh_automation.model")


class StaleCalibrationError(ValueError):
    """校准格式过期或基准明显被其他窗口污染。"""


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
    calibration_version: int = 8
    patch_size: int = 32
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
        if self.calibration_version != 8:
            raise StaleCalibrationError("旧版截图基准已失效，请重新自动识别或手动校准")
        missing = self.REQUIRED_POINTS.difference(self.points)
        if missing:
            raise ValueError(f"校准配置缺少位置：{', '.join(sorted(missing))}")
        if len(self.empty_grid_patches) != 9:
            raise ValueError("空九宫格基准必须恰好包含 9 个采样块")
        decoded_patches = [base64_decode(value) for value in self.empty_grid_patches]
        expected_patch_bytes = self.patch_size * self.patch_size * 4
        if all(len(patch) == expected_patch_bytes for patch in decoded_patches):
            colorful_pixels = sum(
                max(patch[index : index + 3]) - min(patch[index : index + 3]) >= 4
                for patch in decoded_patches
                for index in range(0, len(patch), 4)
            )
            if colorful_pixels == 0:
                raise StaleCalibrationError(
                    "空九宫格基准为纯灰阶画面，疑似采到了终端窗口，请重新校准"
                )
        if self.window.client_width <= 0 or self.window.client_height <= 0:
            raise ValueError("游戏窗口尺寸无效")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Calibration":
        result = cls(
            window=WindowIdentity(**value["window"]),
            points={key: Point.from_dict(point) for key, point in value["points"].items()},
            empty_grid_patches=list(value["empty_grid_patches"]),
            calibration_version=int(value.get("calibration_version", 0)),
            patch_size=int(value.get("patch_size", 32)),
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
    combination_wait_seconds: float = 2.5
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
            try:
                config.calibration = Calibration.from_dict(calibration_raw)
            except StaleCalibrationError as error:
                # 版本升级属于正常迁移：只清除校准，保留间隔等其他设置，且
                # 立即写回，避免每次启动重复打印同一个异常堆栈。
                LOG.warning("已清除失效校准：%s", error)
                config.calibration = None
                config.save(config_path)
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


def base64_decode(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError("空九宫格基准编码损坏") from error
