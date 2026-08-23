from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .dpi import awareness_name, enable_per_monitor_dpi_awareness

# 必须在导入 tkinter、创建任何窗口以及首次调用坐标 API 之前完成。否则
# ClientToScreen/GetCursorPos 会返回逻辑坐标，而桌面 BitBlt 使用物理像素。
DPI_AWARENESS = enable_per_monitor_dpi_awareness()

import tkinter as tk
from tkinter import messagebox, ttk

from .autodetect import layout_points_from_anchor
from .engine import AutomationError, ControlGate, RoundCancelled, RunSchedule, SynthesisEngine
from .model import AppConfig, Calibration, Point, WindowIdentity, default_app_dir
from .windows import (
    GlobalHotkeyListener,
    Win32AutomationDriver,
    cursor_capture,
    cursor_point_for_window,
    describe_window,
    get_window_dpi,
    find_taskbar_hero_window,
    open_synthesis_interface,
    save_bgra_bmp,
)


LOG = logging.getLogger("tbh_automation")


def scaled_pixels(value: int, scale: float) -> int:
    """把旧版 DPI 虚拟化下的界面像素换算为当前物理像素。"""
    return max(1, round(value * max(1.0, scale)))


def configure_logging() -> Path:
    app_dir = default_app_dir()
    app_dir.mkdir(parents=True, exist_ok=True)
    session_dir = app_dir / "diagnostics" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    session_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            RotatingFileHandler(
                app_dir / "automation.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            ),
            logging.FileHandler(session_dir / "session.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )
    LOG.info("本次诊断资料将自动保存到：%s", session_dir)
    return session_dir


@dataclass(frozen=True)
class CalibrationStep:
    key: str
    title: str
    instruction: str


CALIBRATION_STEPS = [
    CalibrationStep("auto_fill", "自动填充按钮", "把鼠标放在“自动填充”按钮中央，然后按 F7。"),
    CalibrationStep("category_dropdown", "类别下拉箭头", "把鼠标放在“自动填充”右侧的小箭头上，然后按 F7。"),
    CalibrationStep("category_equipment", "装备菜单项", "手动点开类别菜单，把鼠标放在“装备”这一项中央，然后按 F7。"),
    CalibrationStep("category_material", "材料菜单项", "再次手动点开类别菜单，把鼠标放在“材料”这一项中央，然后按 F7。"),
    CalibrationStep("combine", "合成按钮", "把鼠标放在右侧蓝色合成按钮中央，然后按 F7。"),
    CalibrationStep("return_items", "退回按钮", "把鼠标放在九宫格左侧、返回箭头形状的小按钮中央，然后按 F7。"),
]


def calibration_step_or_finish(index: int) -> tuple[str, CalibrationStep | None]:
    if index >= len(CALIBRATION_STEPS):
        return "finish", None
    return "capture", CALIBRATION_STEPS[index]


class TaskbarHeroApp:
    def __init__(self, root: tk.Tk, diagnostics_dir: Path | None = None) -> None:
        self.root = root
        self.diagnostics_dir = diagnostics_dir
        self.root.title("TBH 自动合成")
        self.ui_scale = max(1.0, float(self.root.winfo_fpixels("1i")) / 96.0)
        width = self._px(410)
        height = self._px(340)
        right_margin = self._px(20)
        top_margin = self._px(20)
        screen_width = self.root.winfo_screenwidth()
        self.root.geometry(
            f"{width}x{height}+{max(self._px(10), screen_width - width - right_margin)}"
            f"+{top_margin}"
        )
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self._config_load_error = ""
        try:
            self.config = AppConfig.load()
        except Exception as error:
            LOG.exception("配置文件无法读取，将要求重新校准")
            self.config = AppConfig()
            self._config_load_error = str(error)
        self.gate = ControlGate()
        self.ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.run_now_event = threading.Event()
        self.schedule = RunSchedule.starting_now(self.config.interval_seconds, time.monotonic())
        self.total_rounds = 0
        self.total_combinations = 0
        self._worker: threading.Thread | None = None
        self._auto_detecting = False

        self._wizard: tk.Toplevel | None = None
        self._calibration_index = 0
        self._calibration_points: dict[str, Point] = {}
        self._calibration_hwnd = 0
        self._calibration_identity: WindowIdentity | None = None

        self.status_var = tk.StringVar(value="正在启动")
        self.detail_var = tk.StringVar(value="")
        self.countdown_var = tk.StringVar(value="下一轮：准备立即执行")
        self.stats_var = tk.StringVar(value="已完成 0 轮，共合成 0 次")
        self.hotkey_warning_var = tk.StringVar(value="")
        self._build_ui()

        self.hotkeys = GlobalHotkeyListener(
            on_f7=lambda: self.root.after(0, self._capture_calibration_point),
            on_f8=lambda: self.root.after(0, self.toggle_enabled),
            on_f9=lambda: self.root.after(0, self.emergency_stop),
            on_error=lambda message: self.root.after(0, self._show_hotkey_warning, message),
        )
        self.hotkeys.start()
        self.root.after(100, self._poll_ui_queue)
        self.root.after(250, self._tick_countdown)
        self._start_worker()

        if self.config.calibration is None:
            detail = "首次使用请完成位置校准"
            if self._config_load_error:
                detail = f"原配置无法读取，请重新校准：{self._config_load_error}"
            self._set_status("需要校准", detail, "#b7791f")
            self.root.after(500, self.start_auto_detection)
        else:
            self._set_status("托管中", "启动后将立即执行一轮", "#137333")

    def _px(self, value: int) -> int:
        return scaled_pixels(value, self.ui_scale)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=self._px(14))
        outer.pack(fill="both", expand=True)

        self.status_label = tk.Label(
            outer,
            textvariable=self.status_var,
            bg="#137333",
            fg="white",
            font=("Microsoft YaHei UI", 15, "bold"),
            pady=self._px(9),
        )
        self.status_label.pack(fill="x")
        ttk.Label(
            outer,
            textvariable=self.detail_var,
            anchor="center",
            wraplength=self._px(370),
        ).pack(fill="x", pady=(self._px(8), self._px(2)))
        ttk.Label(outer, textvariable=self.countdown_var, anchor="center").pack(
            fill="x", pady=self._px(2)
        )
        ttk.Label(outer, textvariable=self.stats_var, anchor="center").pack(
            fill="x", pady=(self._px(2), self._px(10))
        )

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")
        self.toggle_button = ttk.Button(buttons, text="暂停托管 (F8)", command=self.toggle_enabled)
        self.toggle_button.pack(side="left", expand=True, fill="x", padx=(0, self._px(4)))
        ttk.Button(buttons, text="立即执行", command=self.run_now).pack(
            side="left", expand=True, fill="x", padx=(self._px(4), 0)
        )

        calibration_buttons = ttk.Frame(outer)
        calibration_buttons.pack(fill="x", pady=(self._px(7), 0))
        ttk.Button(
            calibration_buttons,
            text="自动识别位置",
            command=self.start_auto_detection,
        ).pack(side="left", expand=True, fill="x", padx=(0, self._px(4)))
        ttk.Button(calibration_buttons, text="手动校准", command=self.start_calibration).pack(
            side="left", expand=True, fill="x", padx=(self._px(4), 0)
        )

        ttk.Separator(outer).pack(fill="x", pady=self._px(12))
        ttk.Label(
            outer,
            text="F8 开启/暂停　　F9 紧急停止\n到期后会自动查找游戏并打开背包与魔方",
            anchor="center",
            justify="center",
        ).pack(fill="x")
        ttk.Label(
            outer,
            textvariable=self.hotkey_warning_var,
            foreground="#b3261e",
            anchor="center",
            wraplength=self._px(370),
        ).pack(fill="x", pady=(self._px(4), 0))

    def _show_hotkey_warning(self, message: str) -> None:
        current = self.hotkey_warning_var.get()
        self.hotkey_warning_var.set(f"{current}\n{message}".strip())
        LOG.warning(message)

    def _set_status(self, title: str, detail: str, color: str) -> None:
        self.status_var.set(title)
        self.detail_var.set(detail)
        self.status_label.configure(bg=color)

    def _post_status(self, detail: str) -> None:
        self.ui_queue.put(("status", detail))

    def _poll_ui_queue(self) -> None:
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "status":
                    detail = str(payload)
                    if detail.startswith("等待 TaskBarHero"):
                        self._set_status("等待游戏", detail, "#b7791f")
                    else:
                        self._set_status("托管中", detail, "#137333")
                elif kind == "round":
                    combinations = int(payload)
                    self.total_rounds += 1
                    self.total_combinations += combinations
                    self.stats_var.set(f"已完成 {self.total_rounds} 轮，共合成 {self.total_combinations} 次")
                elif kind == "error":
                    self._set_status("已安全暂停", str(payload), "#b3261e")
                    self.toggle_button.configure(text="开启托管 (F8)")
        except queue.Empty:
            pass
        if not self.gate.shutdown_requested:
            self.root.after(100, self._poll_ui_queue)

    def _tick_countdown(self) -> None:
        if self.config.calibration is None:
            self.countdown_var.set("完成校准后立即执行第一轮")
        elif not self.gate.enabled:
            self.countdown_var.set("托管已暂停")
        else:
            remaining = max(0, round(self.schedule.next_due - time.monotonic()))
            if remaining == 0:
                self.countdown_var.set("下一轮：已到时间，正在等待游戏启动")
            else:
                duration = str(timedelta(seconds=remaining))
                self.countdown_var.set(f"下一轮倒计时：{duration}")
        if not self.gate.shutdown_requested:
            self.root.after(1000, self._tick_countdown)

    def _start_worker(self) -> None:
        self._worker = threading.Thread(target=self._scheduler_loop, name="automation-worker", daemon=True)
        self._worker.start()

    def _scheduler_loop(self) -> None:
        while not self.gate.shutdown_requested:
            if self.config.calibration is None or not self.gate.enabled:
                time.sleep(0.2)
                continue
            if not self.run_now_event.is_set() and not self.schedule.is_due(time.monotonic()):
                time.sleep(0.25)
                continue
            self.run_now_event.clear()
            generation = self.gate.generation
            calibration = self.config.calibration
            if calibration is None:
                continue
            driver = Win32AutomationDriver(
                calibration,
                self.gate,
                generation,
                click_delay=self.config.click_delay_seconds,
                combination_wait=self.config.combination_wait_seconds,
                status=self._post_status,
                diagnostics_dir=self.diagnostics_dir,
            )
            engine = SynthesisEngine(
                driver,
                max_combinations_per_category=self.config.max_combinations_per_category,
                logger=LOG,
                status=self._post_status,
            )
            try:
                result = engine.run_round()
            except RoundCancelled:
                LOG.info("本轮已取消")
                continue
            except AutomationError as error:
                LOG.error("自动化安全暂停：%s", error)
                self.gate.pause(cancel_round=True)
                self.ui_queue.put(("error", str(error)))
                continue
            except Exception as error:  # 最后一道安全边界：任何未知异常都停止点击。
                LOG.exception("未处理异常，自动化已暂停")
                self.gate.pause(cancel_round=True)
                self.ui_queue.put(("error", f"发生异常：{error}"))
                continue
            self.ui_queue.put(("round", result.total))
            self.schedule.complete_round(time.monotonic())
            self._post_status("本轮完成，等待五小时后执行下一轮")

    def toggle_enabled(self) -> None:
        if self._auto_detecting or (self._wizard is not None and self._wizard.winfo_exists()):
            return
        enabled = self.gate.toggle()
        if enabled:
            self.toggle_button.configure(text="暂停托管 (F8)")
            self._set_status("托管中", "已开启；到期后将自动查找并激活游戏", "#137333")
        else:
            self.toggle_button.configure(text="开启托管 (F8)")
            self._set_status("已暂停", "不会执行任何鼠标操作", "#666666")

    def emergency_stop(self) -> None:
        if self._auto_detecting or (self._wizard is not None and self._wizard.winfo_exists()):
            return
        self.gate.pause(cancel_round=True)
        self.toggle_button.configure(text="开启托管 (F8)")
        self._set_status("紧急停止", "当前一轮已取消；按 F8 可重新开启", "#b3261e")

    def run_now(self) -> None:
        if self.config.calibration is None:
            messagebox.showinfo("尚未校准", "请先完成位置校准。")
            return
        self.gate.enable()
        self.run_now_event.set()
        self.schedule.run_now(time.monotonic())
        self.toggle_button.configure(text="暂停托管 (F8)")
        self._set_status("托管中", "将立即自动查找游戏并准备合成界面", "#137333")

    def start_auto_detection(self) -> None:
        if self._auto_detecting:
            return
        if self._wizard is not None and self._wizard.winfo_exists():
            self._wizard.lift()
            return
        proceed = messagebox.askokcancel(
            "自动识别位置",
            "请确保 TaskBarHero 游戏正在运行。\n\n"
            "点击“确定”后，本工具会自动找到游戏，必要时打开英雄背包和魔方合成窗口，"
            "然后识别并保存全部按钮位置。",
            parent=self.root,
        )
        if not proceed:
            if self.config.calibration is None:
                self.start_calibration()
            return
        self.gate.pause(cancel_round=True)
        self._auto_detecting = True
        self._set_status("正在自动识别", "正在查找游戏并准备背包与魔方窗口", "#b7791f")
        self.root.withdraw()
        self.root.after(500, self._perform_auto_detection)

    def _perform_auto_detection(self) -> None:
        previous_calibration = self.config.calibration
        image: bytes | None = None
        width = 0
        height = 0
        try:
            preferred_identity = (
                previous_calibration.window if previous_calibration is not None else None
            )
            hwnd = find_taskbar_hero_window(preferred_identity)
            if not hwnd:
                raise ValueError("未找到正在运行的 TaskBarHero.exe，请先启动游戏")
            identity = describe_window(hwnd)
            image, width, height, result = open_synthesis_interface(hwnd)
            window_dpi = get_window_dpi(hwnd)
            LOG.info(
                "自动识别截图：window=%r process=%r client=%dx%d dpi=%d scale=%.2f",
                identity.title,
                identity.process_name,
                width,
                height,
                window_dpi,
                window_dpi / 96,
            )
            calibration = Calibration(
                window=identity,
                points=layout_points_from_anchor(result.anchor, result.scale),
            )
            calibration.validate()
            self.config.calibration = calibration
            self.config.save()
            LOG.info(
                "自动识别成功：anchor=(%d,%d), scale=%.2f, confidence=%.1f%%",
                result.anchor.x,
                result.anchor.y,
                result.scale,
                result.confidence * 100,
            )
        except Exception as error:
            LOG.exception("自动识别位置失败")
            failure_path = None
            if image is not None and width > 0 and height > 0:
                try:
                    failure_path = default_app_dir() / "auto-detect-failure.bmp"
                    save_bgra_bmp(failure_path, image, width, height)
                    LOG.error("自动识别失败截图已保存：%s", failure_path)
                except Exception:
                    LOG.exception("保存自动识别失败截图时出错")
            self.config.calibration = previous_calibration
            self._auto_detecting = False
            self.root.deiconify()
            if previous_calibration is not None:
                self.gate.enable()
                self._set_status("自动识别失败", "已保留原校准配置", "#b3261e")
            else:
                self._set_status("需要校准", "自动识别失败，请使用手动校准", "#b3261e")
            messagebox.showerror(
                "自动识别失败",
                f"{error}\n\n已停止操作，可点击“手动校准”继续。"
                + (f"\n失败截图：{failure_path}" if failure_path else ""),
                parent=self.root,
            )
            return

        self._auto_detecting = False
        self.root.deiconify()
        self.schedule.run_now(time.monotonic())
        self.gate.enable()
        self.run_now_event.set()
        self.toggle_button.configure(text="暂停托管 (F8)")
        self._set_status("识别完成", "游戏与全部按钮位置已识别，将立即执行第一轮", "#137333")
        messagebox.showinfo("自动识别完成", "全部按钮位置已保存，将立即执行第一轮。", parent=self.root)

    def start_calibration(self) -> None:
        if self._wizard is not None and self._wizard.winfo_exists():
            self._wizard.lift()
            return
        self.gate.pause(cancel_round=True)
        self._calibration_index = 0
        self._calibration_points = {}
        self._calibration_hwnd = 0
        self._calibration_identity = None

        wizard = tk.Toplevel(self.root)
        self._wizard = wizard
        wizard.title("TBH 位置校准")
        wizard.geometry(
            f"{self._px(470)}x{self._px(230)}+{self._px(20)}+{self._px(20)}"
        )
        wizard.resizable(False, False)
        wizard.attributes("-topmost", True)
        wizard.protocol("WM_DELETE_WINDOW", self._cancel_calibration)
        frame = ttk.Frame(wizard, padding=self._px(16))
        frame.pack(fill="both", expand=True)
        self.calibration_progress_var = tk.StringVar()
        self.calibration_title_var = tk.StringVar()
        self.calibration_instruction_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.calibration_progress_var).pack(anchor="w")
        ttk.Label(frame, textvariable=self.calibration_title_var, font=("Microsoft YaHei UI", 14, "bold")).pack(
            anchor="w", pady=(self._px(10), self._px(6))
        )
        ttk.Label(
            frame,
            textvariable=self.calibration_instruction_var,
            wraplength=self._px(430),
            justify="left",
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text="按 F7 或点击下方按钮捕获。F8/F9 在校准期间不会启动操作。",
            foreground="#666666",
        ).pack(anchor="w", pady=(self._px(12), 0))
        wizard_buttons = ttk.Frame(frame)
        wizard_buttons.pack(side="bottom", fill="x")
        ttk.Button(wizard_buttons, text="3 秒后捕获鼠标位置", command=self._capture_after_delay).pack(side="left")
        ttk.Button(wizard_buttons, text="取消校准", command=self._cancel_calibration).pack(side="right")
        self._show_calibration_step()

    def _show_calibration_step(self) -> None:
        step = CALIBRATION_STEPS[self._calibration_index]
        self.calibration_progress_var.set(f"步骤 {self._calibration_index + 1} / {len(CALIBRATION_STEPS)}")
        self.calibration_title_var.set(step.title)
        self.calibration_instruction_var.set(step.instruction)

    def _capture_calibration_point(self) -> None:
        if self._wizard is None or not self._wizard.winfo_exists():
            return
        try:
            action, pending_step = calibration_step_or_finish(self._calibration_index)
            if action == "finish":
                self._finish_calibration()
                return
            assert pending_step is not None
            captured_hwnd, captured_point, identity = cursor_capture()
            if self._calibration_hwnd == 0:
                self._calibration_hwnd = captured_hwnd
                self._calibration_identity = identity
                if identity.client_width < 250 or identity.client_height < 250:
                    raise ValueError("捕获到的窗口太小，请把鼠标放在游戏魔方窗口内")
            else:
                # 下拉菜单有可能是独立弹窗；统一换算到首次捕获的游戏客户区。
                captured_point = cursor_point_for_window(self._calibration_hwnd)
                current_identity = describe_window(self._calibration_hwnd)
                if self._calibration_identity and (
                    current_identity.client_width != self._calibration_identity.client_width
                    or current_identity.client_height != self._calibration_identity.client_height
                ):
                    raise ValueError("校准过程中游戏窗口尺寸发生变化，请重新开始")

            step = pending_step
            self._calibration_points[step.key] = captured_point
            self._calibration_index += 1
            if self._calibration_index < len(CALIBRATION_STEPS):
                self._show_calibration_step()
                return
            self._finish_calibration()
        except Exception as error:
            messagebox.showerror("无法捕获", str(error), parent=self._wizard)

    def _capture_after_delay(self) -> None:
        if self._wizard is None or not self._wizard.winfo_exists():
            return
        self._wizard.withdraw()
        self.root.withdraw()

        def capture_and_restore() -> None:
            self._capture_calibration_point()
            if self._wizard is not None and self._wizard.winfo_exists():
                self.root.deiconify()
                self._wizard.deiconify()
                self._wizard.lift()

        self.root.after(3000, capture_and_restore)

    def _finish_calibration(self) -> None:
        if self._calibration_identity is None:
            raise ValueError("未捕获游戏窗口")
        assert self._wizard is not None
        self._wizard.withdraw()
        self.root.withdraw()
        self.root.update_idletasks()
        time.sleep(0.25)
        try:
            calibration = Calibration(
                window=self._calibration_identity,
                points=dict(self._calibration_points),
            )
            calibration.validate()
            self.config.calibration = calibration
            self.config.save()
        except Exception:
            LOG.exception("手动校准保存失败")
            self.root.deiconify()
            self._wizard.deiconify()
            raise
        self.root.deiconify()
        self._wizard.destroy()
        self._wizard = None
        self.schedule.run_now(time.monotonic())
        self.gate.enable()
        self.run_now_event.set()
        self.toggle_button.configure(text="暂停托管 (F8)")
        self._set_status("校准完成", "配置已保存，将立即自动准备游戏界面", "#137333")
        messagebox.showinfo("校准完成", "配置已保存，程序会自动找到游戏并执行第一轮。", parent=self.root)

    def _cancel_calibration(self) -> None:
        if self._wizard is not None:
            self._wizard.destroy()
            self._wizard = None
        if self.config.calibration is not None:
            self.gate.enable()
            self._set_status("托管中", "沿用上一次校准配置", "#137333")
        else:
            self._set_status("需要校准", "没有有效配置，不会执行鼠标操作", "#b7791f")

    def close(self) -> None:
        if not messagebox.askokcancel("退出", "确定退出 TBH 自动合成工具吗？"):
            return
        self.gate.request_shutdown()
        self.hotkeys.stop()
        self.root.destroy()


def main() -> None:
    diagnostics_dir = configure_logging()
    LOG.info("DPI 坐标模式：%s", awareness_name(DPI_AWARENESS))
    root = tk.Tk()
    TaskbarHeroApp(root, diagnostics_dir)
    root.mainloop()
