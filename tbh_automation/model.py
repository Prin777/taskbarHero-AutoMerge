from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


APP_DIR_NAME = "TaskbarHeroAutoSynth"
LOG = logging.getLogger("tbh_automation.model")


class StaleCalibrationError(ValueError):
    """校准格式已经过期。"""


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
    calibration_version: int = 9

    REQUIRED_POINTS = {
        "auto_fill",
        "category_dropdown",
        "category_equipment",
        "category_material",
        "combine",
        "return_items",
    }

    def validate(self) -> None:
        if self.calibration_version != 9:
            raise StaleCalibrationError("旧版位置校准已失效，请重新自动识别或手动校准")
        missing = self.REQUIRED_POINTS.difference(self.points)
        if missing:
            raise ValueError(f"校准配置缺少位置：{', '.join(sorted(missing))}")
        if self.window.client_width <= 0 or self.window.client_height <= 0:
            raise ValueError("游戏窗口尺寸无效")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Calibration":
        version = int(value.get("calibration_version", 0))
        if version not in (8, 9):
            raise StaleCalibrationError("旧版位置校准已失效，请重新自动识别或手动校准")
        result = cls(
            window=WindowIdentity(**value["window"]),
            points={
                key: Point.from_dict(value["points"][key])
                for key in cls.REQUIRED_POINTS
                if key in value["points"]
            },
            # 0.3.x 的第 8 版校准已经包含所有按钮坐标，可直接迁移；旧的
            # 九宫格采样字段会被忽略，不要求用户重新校准。
            calibration_version=9,
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
    portal_enabled: bool = False
    version: int = 7

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":
        config_path = path or default_app_dir() / "config.json"
        if not config_path.exists():
            return cls()
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        calibration_raw = raw.pop("calibration", None)
        migrate = "portal_enabled" not in raw or raw.get("version",6) != 7
        config = cls(**raw)
        if not isinstance(config.portal_enabled,bool):
            raise ValueError("灵魂石识别配置必须为布尔值")
        config.version = 7
        if calibration_raw is not None:
            try:
                config.calibration = Calibration.from_dict(calibration_raw)
                if int(calibration_raw.get("calibration_version", 0)) == 8:
                    LOG.info("已迁移旧版校准：删除九宫格基准，仅保留按钮位置")
                    config.save(config_path)
            except StaleCalibrationError as error:
                # 版本升级属于正常迁移：只清除校准，保留间隔等其他设置，且
                # 立即写回，避免每次启动重复打印同一个异常堆栈。
                LOG.warning("已清除失效校准：%s", error)
                config.calibration = None
                config.save(config_path)
        if migrate:
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
