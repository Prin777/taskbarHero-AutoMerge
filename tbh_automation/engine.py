from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol


class Category(str, Enum):
    EQUIPMENT = "装备"
    MATERIAL = "材料"


class RoundCancelled(Exception):
    """本轮被暂停、紧急停止或程序退出。"""


class AutomationError(RuntimeError):
    """为避免误操作而主动停止的自动化错误。"""


@dataclass
class RunSchedule:
    """第一轮立即到期；成功完成后按固定间隔重新计时。"""

    interval_seconds: float
    next_due: float
    last_completed_at: float | None = None

    @classmethod
    def starting_now(cls, interval_seconds: float, now: float) -> "RunSchedule":
        return cls(interval_seconds=interval_seconds, next_due=now)

    def is_due(self, now: float) -> bool:
        return now >= self.next_due

    def complete_round(self, now: float) -> None:
        self.last_completed_at = now
        self.next_due = now + self.interval_seconds

    def run_now(self, now: float) -> None:
        self.next_due = now

    def change_interval(self, interval_seconds: float) -> None:
        """Apply a new interval without postponing the initial immediate run."""
        self.interval_seconds = interval_seconds
        if self.last_completed_at is not None:
            self.next_due = self.last_completed_at + interval_seconds


class AutomationDriver(Protocol):
    def recover_server_error(self) -> bool: ...

    def check_boss_portal(self) -> bool: ...

    def prepare_interface(self) -> None: ...

    def open_chests(self) -> None: ...

    def checkpoint(self) -> None: ...

    def select_category(self, category: Category) -> None: ...

    def click_auto_fill(self) -> None: ...

    def combine_button_is_enabled(self) -> bool: ...

    def click_combine(self) -> None: ...

    def wait_for_combination(self) -> None: ...

    def click_return_items(self) -> None: ...


@dataclass(frozen=True)
class RoundResult:
    equipment_combinations: int = 0
    material_combinations: int = 0

    @property
    def total(self) -> int:
        return self.equipment_combinations + self.material_combinations


class SynthesisEngine:
    """与界面实现无关的自动合成状态机。"""

    def __init__(
        self,
        driver: AutomationDriver,
        *,
        max_combinations_per_category: int = 100,
        logger: logging.Logger | None = None,
        status: Callable[[str], None] | None = None,
        portal_enabled: Callable[[], bool] | None = None,
    ) -> None:
        self.portal_enabled = portal_enabled or (lambda:False)
        self.driver = driver
        self.max_combinations = max_combinations_per_category
        self.log = logger or logging.getLogger(__name__)
        self.status = status or (lambda _message: None)

    def run_round(self) -> RoundResult:
        self.status("正在查找游戏并准备合成界面")
        self.driver.prepare_interface()
        self.driver.checkpoint()
        self.status("模板比例已确认，按空格打开箱子")
        self.driver.open_chests()
        if self.portal_enabled():
            self.status("合成前检查 3-10 入口与灵魂石数量（需大于 5）")
            self.driver.checkpoint()
            if self.driver.check_boss_portal():
                self.driver.prepare_interface()
            self.driver.checkpoint()
        # 清理上次暂停或异常时可能留在合成区域中的物品。
        self.driver.checkpoint()
        self.driver.click_return_items()
        counts: dict[Category, int] = {}
        for category in (Category.EQUIPMENT, Category.MATERIAL):
            counts[category] = self._run_category(category)
        result = RoundResult(
            equipment_combinations=counts[Category.EQUIPMENT],
            material_combinations=counts[Category.MATERIAL],
        )
        self.log.info("本轮完成：装备 %d 次，材料 %d 次", result.equipment_combinations, result.material_combinations)
        return result

    def _run_category(self, category: Category) -> int:
        self.status(f"正在选择{category.value}类别")
        self.driver.checkpoint()
        self.driver.select_category(category)
        combinations = 0
        server_errors = 0
        attempts = 0

        while attempts < self.max_combinations:
            self.driver.checkpoint()
            self.status(f"{category.value}：正在自动填充")
            self.driver.click_auto_fill()
            combine_ready = self.driver.combine_button_is_enabled()
            self.log.info(
                "%s首次填充：合成按钮=%s",
                category.value,
                "蓝色可用" if combine_ready else "灰色不可用",
            )

            # 是否合成只服从游戏自身的按钮状态，不再读取九宫格画面。
            if combine_ready:
                attempts += 1
                if self._combine(category, combinations + 1):
                    combinations += 1
                else:
                    server_errors += 1
                    if server_errors >= 3:
                        raise AutomationError("本类别服务器错误累计达到 3 次，已安全暂停")
                continue

            # 第一次按钮一直未激活：全部退回后再次填充，以排除界面瞬态或残留。
            self.status(f"{category.value}：合成按钮不可用，退回后复核")
            self.driver.click_return_items()
            self.driver.checkpoint()
            self.driver.click_auto_fill()
            retry_ready = self.driver.combine_button_is_enabled()
            self.log.info(
                "%s复核填充：合成按钮=%s",
                category.value,
                "蓝色可用" if retry_ready else "灰色不可用",
            )

            if retry_ready:
                attempts += 1
                if self._combine(category, combinations + 1):
                    combinations += 1
                else:
                    server_errors += 1
                    if server_errors >= 3:
                        raise AutomationError("本类别服务器错误累计达到 3 次，已安全暂停")
                continue

            # 第二次按钮仍未激活时也退回，避免残留物品影响下一类别。
            self.driver.click_return_items()
            self.status(f"{category.value}已全部处理")
            return combinations

        raise AutomationError(
            f"{category.value}单轮已达到安全上限 {self.max_combinations} 次，已暂停以防止死循环"
        )

    def _combine(self, category: Category, sequence: int) -> bool:
        self.driver.checkpoint()
        self.status(f"{category.value}：合成按钮已高亮，正在合成（第 {sequence} 次）")
        self.driver.click_combine()
        # 等待合成动画后直接回到循环；下一次自动填充只检查按钮是否激活。
        self.driver.wait_for_combination()
        self.driver.checkpoint()
        if self.driver.recover_server_error():
            self.status("服务器错误已确认，正在退回物品并重新填充")
            self.driver.click_return_items()
            return False
        return True


class ControlGate:
    """跨线程控制暂停、退出和紧急取消。"""

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._enabled = True
        self._shutdown = False
        self._generation = 0

    @property
    def enabled(self) -> bool:
        with self._condition:
            return self._enabled

    @property
    def shutdown_requested(self) -> bool:
        with self._condition:
            return self._shutdown

    @property
    def generation(self) -> int:
        with self._condition:
            return self._generation

    def toggle(self) -> bool:
        with self._condition:
            self._enabled = not self._enabled
            if not self._enabled:
                self._generation += 1
            self._condition.notify_all()
            return self._enabled

    def enable(self) -> None:
        with self._condition:
            self._enabled = True
            self._condition.notify_all()

    def restart(self) -> None:
        """Cancel any in-flight round and immediately allow a fresh one."""
        with self._condition:
            self._enabled = True
            self._generation += 1
            self._condition.notify_all()

    def pause(self, *, cancel_round: bool = False) -> None:
        with self._condition:
            self._enabled = False
            if cancel_round:
                self._generation += 1
            self._condition.notify_all()

    def request_shutdown(self) -> None:
        with self._condition:
            self._shutdown = True
            self._generation += 1
            self._condition.notify_all()

    def wait_enabled(self, generation: int, timeout: float = 0.25) -> None:
        with self._condition:
            if self._shutdown or generation != self._generation:
                raise RoundCancelled()
            if not self._enabled:
                raise RoundCancelled()
            self._condition.wait(timeout=timeout)
            if self._shutdown or generation != self._generation or not self._enabled:
                raise RoundCancelled()

    def interruptible_wait(self, seconds: float, generation: int) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            self.wait_enabled(generation, min(0.25, max(0.0, remaining)))
