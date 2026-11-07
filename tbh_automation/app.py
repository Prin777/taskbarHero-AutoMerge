from __future__ import annotations

import logging
import platform
import queue
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import __version__
from .dpi import awareness_name, enable_per_monitor_dpi_awareness

# 必须在导入 tkinter、创建任何窗口以及首次调用坐标 API 之前完成。否则
# ClientToScreen/GetCursorPos 会返回逻辑坐标，而桌面 BitBlt 使用物理像素。
DPI_AWARENESS = enable_per_monitor_dpi_awareness()

import tkinter as tk
from tkinter import messagebox, ttk

from .autodetect import layout_points_from_anchor
from .desktop import (
    SingleInstance,
    SystemTray,
    application_icon_path,
    show_already_running_message,
)
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
LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
INTERVAL_OPTIONS = {
    "1h": 1 * 60 * 60,
    "2h": 2 * 60 * 60,
    "3h": 3 * 60 * 60,
    "5h": 5 * 60 * 60,
}


def interval_option_for_seconds(seconds: int | float) -> str:
    for option, option_seconds in INTERVAL_OPTIONS.items():
        if seconds == option_seconds:
            return option
    return f"{seconds / 3600:g}h"


def interval_description(seconds: int | float) -> str:
    hours = seconds / 3600
    return f"{hours:g} 小时"


def format_interval_remaining(seconds: int | float) -> str:
    remaining = max(0, round(seconds))
    hours, remainder = divmod(remaining, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def scaled_pixels(value: int, scale: float) -> int:
    """把旧版 DPI 虚拟化下的界面像素换算为当前物理像素。"""
    return max(1, round(value * max(1.0, scale)))


def runtime_output_dir() -> Path:
    """源码运行时返回脚本目录，打包运行时返回 EXE 所在目录。"""
    launcher = sys.argv[0] if sys.argv and sys.argv[0] else sys.executable
    return Path(launcher).resolve().parent


def configure_logging() -> None:
    """默认只输出到控制台；用户勾选后再启用会话文件日志。"""
    logging.basicConfig(
        level=logging.INFO,
        format=LOG_FORMAT,
        handlers=[logging.StreamHandler()],
    )


class SessionLogging:
    """按需管理单次会话日志及其诊断目录。"""

    def __init__(self) -> None:
        self.session_dir: Path | None = None
        self._handler: logging.FileHandler | None = None

    def enable(self) -> Path:
        if self.session_dir is not None and self._handler is not None:
            return self.session_dir

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        candidates = [runtime_output_dir(), default_app_dir()]
        last_error: OSError | None = None
        for output_dir in candidates:
            try:
                session_dir = output_dir / "diagnostics" / timestamp
                session_dir.mkdir(parents=True, exist_ok=True)
                handler = logging.FileHandler(
                    session_dir / "session.log",
                    encoding="utf-8",
                )
                handler.setLevel(logging.INFO)
                handler.setFormatter(logging.Formatter(LOG_FORMAT))
            except OSError as error:
                last_error = error
                continue

            logging.getLogger().addHandler(handler)
            self.session_dir = session_dir
            self._handler = handler
            LOG.info("本次会话日志与诊断截图将保存到：%s", session_dir)
            return session_dir

        if last_error is not None:
            raise last_error
        raise OSError("无法创建会话日志目录")

    def disable(self) -> None:
        handler = self._handler
        if handler is None:
            self.session_dir = None
            return
        LOG.info("已停止保存本次会话日志与诊断截图")
        logging.getLogger().removeHandler(handler)
        handler.close()
        self._handler = None
        self.session_dir = None


def log_runtime_environment() -> None:
    LOG.info(
        "运行环境：version=%s executable=%s python=%s os=%s",
        __version__,
        sys.executable,
        platform.python_version(),
        platform.platform(),
    )
    LOG.info("DPI 坐标模式：%s", awareness_name(DPI_AWARENESS))


@dataclass(frozen=True)
class CalibrationStep:
    key: str
    title: str
    instruction: str


@dataclass(frozen=True)
class AutoDetectionOutcome:
    cancel_event: threading.Event
    previous_calibration: Calibration | None
    calibration: Calibration | None = None
    error: str = ""
    failure_path: Path | None = None
    cancelled: bool = False
    detected_scale: float | None = None


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
    def __init__(self, root: tk.Tk, session_logging: SessionLogging | None = None) -> None:
        self.root = root
        self.session_logging = session_logging or SessionLogging()
        self.diagnostics_dir: Path | None = None
        self.root.title("TBH 自动合成")
        try:
            self.root.iconbitmap(default=str(application_icon_path()))
        except tk.TclError:
            LOG.warning("无法设置主窗口图标", exc_info=True)
        self.ui_scale = max(1.0, float(self.root.winfo_fpixels("1i")) / 96.0)
        width = self._px(410)
        height = self._px(475)
        right_margin = self._px(20)
        top_margin = self._px(20)
        screen_width = self.root.winfo_screenwidth()
        self.root.geometry(
            f"{width}x{height}+{max(self._px(10), screen_width - width - right_margin)}"
            f"+{top_margin}"
        )
        self.root.resizable(False, False)
        self.root.attributes("-topmost", True)
        self.root.protocol("WM_DELETE_WINDOW", self.hide_to_tray)

        self._config_load_error = ""
        try:
            self.config = AppConfig.load()
        except Exception as error:
            LOG.exception("配置文件无法读取，将要求重新校准")
            self.config = AppConfig()
            self._config_load_error = str(error)
        # Optional automation must be explicitly enabled for this session.
        self.config.portal_enabled = False
        self.gate = ControlGate()
        self.ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.run_now_event = threading.Event()
        self.portal_check_event = threading.Event()
        self.round_active_event = threading.Event()
        self._automation_lock = threading.Lock()
        self.schedule = RunSchedule.starting_now(self.config.interval_seconds, time.monotonic())
        self.total_rounds = 0
        self.total_combinations = 0
        self._worker: threading.Thread | None = None
        self._auto_detecting = False
        self._auto_detection_thread: threading.Thread | None = None
        self._auto_detection_cancel: threading.Event | None = None
        self._exiting = False

        self._wizard: tk.Toplevel | None = None
        self._calibration_index = 0
        self._calibration_points: dict[str, Point] = {}
        self._calibration_hwnd = 0
        self._calibration_identity: WindowIdentity | None = None

        self.status_var = tk.StringVar(value="正在启动")
        self.detail_var = tk.StringVar(value="")
        self.scale_var = tk.StringVar(value="当前识别模板缩放：未识别")
        self.stats_var = tk.StringVar(value="已完成 0 轮，共合成 0 次")
        self.interval_var = tk.StringVar(
            value=interval_option_for_seconds(self.config.interval_seconds)
        )
        self.hotkey_warning_var = tk.StringVar(value="")
        self.portal_enabled_var = tk.BooleanVar(value=self.config.portal_enabled)
        self.save_logs_var = tk.BooleanVar(value=False)
        self._build_ui()
        self._toggle_session_logging()

        self.tray = SystemTray(
            on_show=lambda: self.ui_queue.put(("tray_show", None)),
            on_exit=lambda: self.ui_queue.put(("tray_exit", None)),
        )
        self.tray.start()

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
        self.footer_status_label = tk.Label(
            self.root,
            textvariable=self.detail_var,
            anchor="w",
            bg="#f0f0f0",
            fg="#333333",
            relief="sunken",
            bd=1,
            padx=self._px(7),
            pady=self._px(4),
        )
        self.footer_status_label.pack(side="bottom", fill="x")

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
        self.scale_label = ttk.Label(
            outer,
            textvariable=self.scale_var,
            anchor="center",
            foreground="#555555",
            font=("Microsoft YaHei UI", 10),
        )
        self.scale_label.pack(fill="x", pady=(self._px(5), 0))
        self.stats_label = ttk.Label(
            outer,
            textvariable=self.stats_var,
            anchor="center",
            font=("Microsoft YaHei UI", 12, "bold"),
        )
        self.stats_label.pack(
            fill="x",
            pady=(self._px(3), self._px(8)),
        )

        ttk.Checkbutton(outer,text="识别灵魂石入口（空闲时勾选立即检查）",
                        variable=self.portal_enabled_var,command=self._change_portal_option).pack(fill="x",pady=(0,self._px(6)))
        interval_row = ttk.Frame(outer)
        interval_row.pack(fill="x", pady=(0, self._px(8)))
        ttk.Label(interval_row, text="下一轮间隔：").pack(side="left")
        interval_picker = ttk.Combobox(
            interval_row,
            textvariable=self.interval_var,
            values=tuple(INTERVAL_OPTIONS),
            state="readonly",
            width=7,
        )
        interval_picker.pack(side="left")
        interval_picker.bind("<<ComboboxSelected>>", self._change_interval)
        ttk.Label(interval_row, text="选择后自动保存", foreground="#666666").pack(
            side="left", padx=(self._px(8), 0)
        )

        buttons = ttk.Frame(outer)
        buttons.pack(fill="x")
        self.toggle_button = ttk.Button(buttons, text="暂停托管 (F8)", command=self.toggle_enabled)
        self.toggle_button.pack(side="left", expand=True, fill="x", padx=(0, self._px(4)))
        self.run_now_button = ttk.Button(buttons, text="立即执行", command=self.run_now)
        self.run_now_button.pack(
            side="left", expand=True, fill="x", padx=(self._px(4), 0)
        )

        calibration_buttons = ttk.Frame(outer)
        calibration_buttons.pack(fill="x", pady=(self._px(7), 0))
        self.auto_detection_button = ttk.Button(
            calibration_buttons,
            text="自动识别位置",
            command=self.start_auto_detection,
        )
        self.auto_detection_button.pack(
            side="left", expand=True, fill="x", padx=(0, self._px(4))
        )
        self.manual_calibration_button = ttk.Button(
            calibration_buttons,
            text="手动校准",
            command=self.start_calibration,
        )
        self.manual_calibration_button.pack(
            side="left", expand=True, fill="x", padx=(self._px(4), 0)
        )

        ttk.Checkbutton(
            outer,
            text="保存本次会话日志和诊断截图",
            variable=self.save_logs_var,
            command=self._toggle_session_logging,
        ).pack(anchor="w", pady=(self._px(10), 0))

        self.exit_button = ttk.Button(
            outer,
            text="关闭自动化程序",
            command=self.exit_application,
        )
        self.exit_button.pack(fill="x", pady=(self._px(9), 0))

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

    def _toggle_session_logging(self) -> None:
        if not self.save_logs_var.get():
            self.session_logging.disable()
            self.diagnostics_dir = None
            return
        try:
            self.diagnostics_dir = self.session_logging.enable()
            log_runtime_environment()
        except OSError as error:
            self.save_logs_var.set(False)
            self.diagnostics_dir = None
            messagebox.showerror(
                "无法保存日志",
                f"无法创建本次会话日志目录：{error}",
                parent=self.root,
            )

    def _change_portal_option(self):
        previous=self.config.portal_enabled
        enabled=self.portal_enabled_var.get()
        self.config.portal_enabled=enabled
        try:
            self.config.save()
        except OSError as error:
            self.config.portal_enabled=previous
            self.portal_enabled_var.set(previous)
            messagebox.showerror("保存失败",str(error),parent=self.root)
            return
        LOG.info("灵魂石选项已保存：enabled=%s，工作中=%s",enabled,self.round_active_event.is_set())
        if not enabled:
            self.portal_check_event.clear()
        elif not previous and not self.round_active_event.is_set():
            self.portal_check_event.set()
            self._post_status("灵魂石检查已排队；暂停时将在恢复托管后执行")

    def _change_interval(self, _event: object | None = None) -> None:
        option = self.interval_var.get()
        if option not in INTERVAL_OPTIONS:
            return
        interval_seconds = INTERVAL_OPTIONS[option]
        previous_seconds = self.config.interval_seconds
        if interval_seconds == previous_seconds:
            return

        self.config.interval_seconds = interval_seconds
        try:
            self.config.save()
        except OSError as error:
            self.config.interval_seconds = previous_seconds
            self.interval_var.set(interval_option_for_seconds(previous_seconds))
            messagebox.showerror(
                "无法保存设置",
                f"无法保存下一轮执行间隔：{error}",
                parent=self.root,
            )
            return

        self.schedule.change_interval(interval_seconds)
        LOG.info("下一轮执行间隔已调整为：%s", interval_description(interval_seconds))
        self._update_countdown_text(time.monotonic())

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

    def _set_detected_scale(self, scale: float | None) -> None:
        value = "未识别" if scale is None else f"×{scale:g}"
        self.scale_var.set(f"当前识别模板缩放：{value}")

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
                elif kind == "detected_scale":
                    calibration, scale = payload
                    # 允许暂停后显示本轮已确认的比例；已被新校准替换的旧轮次
                    # 结果则丢弃。后台只投递数据，不直接修改 Tk 控件。
                    if calibration is self.config.calibration:
                        self._set_detected_scale(scale)
                elif kind == "tray_show":
                    self.show_main_window()
                elif kind == "tray_exit":
                    self.exit_application()
                elif kind == "auto_detection_finished":
                    self._finish_auto_detection(payload)
                elif kind == "auto_detection_status":
                    if (
                        isinstance(payload, tuple)
                        and len(payload) == 2
                        and payload[0] is self._auto_detection_cancel
                        and not payload[0].is_set()
                    ):
                        self._set_status("正在自动识别", str(payload[1]), "#b7791f")
        except queue.Empty:
            pass
        if not self.gate.shutdown_requested:
            self.root.after(100, self._poll_ui_queue)

    def _tick_countdown(self) -> None:
        self._update_countdown_text(time.monotonic())
        if not self.gate.shutdown_requested:
            self.root.after(1000, self._tick_countdown)

    def _update_countdown_text(self, now: float) -> None:
        if (
            self._auto_detecting
            or self.config.calibration is None
            or not self.gate.enabled
            or self.round_active_event.is_set()
        ):
            return
        remaining = max(0, round(self.schedule.next_due - now))
        if remaining == 0:
            return
        duration = format_interval_remaining(remaining)
        self._set_status(
            f"循环间隔剩余 {duration}",
            self.detail_var.get(),
            "#1a73e8",
        )

    def _start_worker(self) -> None:
        self._worker = threading.Thread(target=self._scheduler_loop, name="automation-worker", daemon=True)
        self._worker.start()

    def _scheduler_loop(self) -> None:
        while not self.gate.shutdown_requested:
            if self.config.calibration is None or not self.gate.enabled:
                time.sleep(0.2)
                continue
            due=self.run_now_event.is_set() or self.schedule.is_due(time.monotonic())
            portal_only=not due and self.portal_check_event.is_set() and self.config.portal_enabled
            if not due and not portal_only:
                time.sleep(0.25)
                continue
            self.run_now_event.clear()
            self.portal_check_event.clear()
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
                on_scale_detected=lambda scale, calibration=calibration: self.ui_queue.put(
                    ("detected_scale", (calibration, scale))
                ),
            )
            driver.diagnostics_enabled = lambda: self.diagnostics_dir is not None and self.diagnostics_dir == driver.diagnostics_dir
            driver.portal_option_enabled = lambda:self.config.portal_enabled
            engine = SynthesisEngine(
                driver,
                portal_enabled=lambda:self.config.portal_enabled,
                max_combinations_per_category=self.config.max_combinations_per_category,
                logger=LOG,
                status=self._post_status,
            )
            LOG.info("开始%s，灵魂石选项=%s", "独立灵魂石检查" if portal_only else "合成轮次",self.config.portal_enabled)
            self.round_active_event.set()
            try:
                with self._automation_lock:
                    if portal_only:
                        if self.config.portal_enabled:
                            driver.check_boss_portal()
                    else:
                        self.portal_check_event.clear()
                        result = engine.run_round()
            except RoundCancelled:
                LOG.info("本轮已取消")
                continue
            except AutomationError as error:
                driver.record_problem(f"自动化安全暂停：{error}", desktop=True)
                LOG.error("自动化安全暂停：%s", error)
                self.gate.pause(cancel_round=True)
                self.ui_queue.put(("error", str(error)))
                continue
            except Exception as error:  # 最后一道安全边界：任何未知异常都停止点击。
                driver.record_problem(f"未处理异常：{error}", desktop=True)
                LOG.exception("未处理异常，自动化已暂停")
                self.gate.pause(cancel_round=True)
                self.ui_queue.put(("error", f"发生异常：{error}"))
                continue
            finally:
                self.round_active_event.clear()
            if portal_only:
                self._post_status("灵魂石检查完成，继续等待原定合成时间")
                continue
            self.ui_queue.put(("round", result.total))
            self.schedule.complete_round(time.monotonic())
            self._post_status(
                f"本轮完成，等待 {interval_description(self.schedule.interval_seconds)}后执行下一轮"
            )

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
        if getattr(self,'portal_check_event',None) is not None:
            self.portal_check_event.clear()
        if self._auto_detecting:
            cancel_event = self._auto_detection_cancel
            if cancel_event is not None:
                cancel_event.set()
            self._set_status("正在停止", "正在安全取消自动识别，请稍候", "#b3261e")
            return
        if self._wizard is not None and self._wizard.winfo_exists():
            return
        self.gate.pause(cancel_round=True)
        self.toggle_button.configure(text="开启托管 (F8)")
        self._set_status("紧急停止", "当前一轮已取消；按 F8 可重新开启", "#b3261e")

    def run_now(self) -> None:
        if self._auto_detecting:
            self._set_status("正在自动识别", "识别完成后会立即执行；按 F9 可取消识别", "#b7791f")
            return
        if self.config.calibration is None:
            messagebox.showinfo("尚未校准", "请先完成位置校准。", parent=self.root)
            return
        # 取消可能卡在“等待游戏”的旧轮次，让这次点击确实启动一轮新的查找。
        self.gate.restart()
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
        cancel_event = threading.Event()
        self._auto_detection_cancel = cancel_event
        self._set_auto_detection_controls(True)
        self._set_status("正在自动识别", "正在查找游戏并准备背包与魔方窗口", "#b7791f")
        # 不再隐藏整个界面；临时取消置顶，让游戏可以接管前台，同时保留进度与退出按钮。
        self.root.attributes("-topmost", False)
        self.root.after(250, self._start_auto_detection_worker, cancel_event)

    def _set_auto_detection_controls(self, busy: bool) -> None:
        state = ["disabled"] if busy else ["!disabled"]
        for button in (
            self.toggle_button,
            self.run_now_button,
            self.auto_detection_button,
            self.manual_calibration_button,
        ):
            button.state(state)

    def _start_auto_detection_worker(self, cancel_event: threading.Event) -> None:
        if (
            self._exiting
            or not self._auto_detecting
            or cancel_event is not self._auto_detection_cancel
        ):
            return
        previous_calibration = self.config.calibration
        self._auto_detection_thread = threading.Thread(
            target=self._perform_auto_detection,
            args=(cancel_event, previous_calibration),
            name="auto-detection",
            daemon=True,
        )
        self._auto_detection_thread.start()

    def _perform_auto_detection(
        self,
        cancel_event: threading.Event,
        previous_calibration: Calibration | None,
    ) -> None:
        image: bytes | None = None
        width = 0
        height = 0

        def checkpoint() -> None:
            if cancel_event.is_set() or self.gate.shutdown_requested:
                raise RoundCancelled()

        def wait(seconds: float) -> None:
            deadline = time.monotonic() + seconds
            while True:
                checkpoint()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                cancel_event.wait(min(0.10, remaining))

        lock_acquired = False
        try:
            self.ui_queue.put(
                (
                    "auto_detection_status",
                    (cancel_event, "正在等待当前自动化操作安全停止"),
                )
            )
            while not self._automation_lock.acquire(timeout=0.10):
                checkpoint()
            lock_acquired = True
            checkpoint()
            self.ui_queue.put(
                (
                    "auto_detection_status",
                    (cancel_event, "正在查找 TaskBarHero 游戏窗口"),
                )
            )
            preferred_identity = (
                previous_calibration.window if previous_calibration is not None else None
            )
            hwnd = find_taskbar_hero_window(preferred_identity)
            if not hwnd:
                raise ValueError("未找到正在运行的 TaskBarHero.exe，请先启动游戏")
            checkpoint()
            identity = describe_window(hwnd)
            window_dpi = get_window_dpi(hwnd)
            self.ui_queue.put(
                (
                    "auto_detection_status",
                    (cancel_event, "已找到游戏，正在准备背包与魔方窗口"),
                )
            )
            LOG.info(
                "自动识别目标：window=%r process=%r class=%r client=%dx%d "
                "dpi=%d scale=%.2f diagnostics=%s",
                identity.title,
                identity.process_name,
                identity.class_name,
                identity.client_width,
                identity.client_height,
                window_dpi,
                window_dpi / 96,
                self.diagnostics_dir,
            )
            image, width, height, result = open_synthesis_interface(
                hwnd,
                wait=wait,
                checkpoint=checkpoint,
                diagnostics_dir=self.diagnostics_dir,
                progress=lambda detail: self.ui_queue.put(
                    ("auto_detection_status", (cancel_event, detail))
                ),
            )
            checkpoint()
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
                points=layout_points_from_anchor(
                    result.anchor, result.scale, client_size=(width, height)
                ),
            )
            calibration.validate()
            checkpoint()
            LOG.info(
                "自动识别成功：anchor=(%d,%d), scale=%.2f, confidence=%.1f%%",
                result.anchor.x,
                result.anchor.y,
                result.scale,
                result.confidence * 100,
            )
        except RoundCancelled:
            LOG.info("自动识别已取消")
            outcome = AutoDetectionOutcome(
                cancel_event=cancel_event,
                previous_calibration=previous_calibration,
                cancelled=True,
            )
        except Exception as error:
            LOG.exception("自动识别位置失败")
            from .diagnostics import record_event
            record_event(self.diagnostics_dir,f"自动识别位置失败：{error}",
                         frame=(image,width,height) if image is not None and width>0 and height>0 else None,
                         desktop=True)
            failure_path = None
            if (
                self.diagnostics_dir is not None
                and image is not None
                and width > 0
                and height > 0
            ):
                try:
                    failure_path = self.diagnostics_dir / "auto-detect-failure.bmp"
                    save_bgra_bmp(failure_path, image, width, height)
                    LOG.error("自动识别失败截图已保存：%s", failure_path)
                except Exception:
                    LOG.exception("保存自动识别失败截图时出错")
            outcome = AutoDetectionOutcome(
                cancel_event=cancel_event,
                previous_calibration=previous_calibration,
                error=str(error),
                failure_path=failure_path,
            )
        else:
            outcome = AutoDetectionOutcome(
                cancel_event=cancel_event,
                previous_calibration=previous_calibration,
                calibration=calibration,
                detected_scale=result.scale,
            )
        finally:
            if lock_acquired:
                self._automation_lock.release()
        self.ui_queue.put(("auto_detection_finished", outcome))

    def _finish_auto_detection(self, payload: object) -> None:
        if not isinstance(payload, AutoDetectionOutcome):
            return
        if payload.cancel_event is not self._auto_detection_cancel or self._exiting:
            return

        self._auto_detecting = False
        self._auto_detection_thread = None
        self._auto_detection_cancel = None
        self._set_auto_detection_controls(False)
        self.root.attributes("-topmost", True)
        self.show_main_window()

        if payload.cancelled or payload.cancel_event.is_set():
            self.config.calibration = payload.previous_calibration
            self.toggle_button.configure(text="开启托管 (F8)")
            self._set_status("已取消识别", "没有执行后续鼠标操作", "#666666")
            return

        if payload.calibration is not None:
            self.config.calibration = payload.calibration
            try:
                self.config.save()
            except Exception as error:
                LOG.exception("自动识别配置保存失败")
                self.config.calibration = payload.previous_calibration
                payload = AutoDetectionOutcome(
                    cancel_event=payload.cancel_event,
                    previous_calibration=payload.previous_calibration,
                    error=f"无法保存识别结果：{error}",
                )
            else:
                self._set_detected_scale(payload.detected_scale)
                self.toggle_button.configure(text="暂停托管 (F8)")
                self._set_status(
                    "识别完成",
                    "游戏与全部按钮位置已识别，将立即执行第一轮",
                    "#137333",
                )
                messagebox.showinfo(
                    "自动识别完成",
                    "全部按钮位置已保存，将立即执行第一轮。",
                    parent=self.root,
                )
                if self._exiting:
                    return
                self.schedule.run_now(time.monotonic())
                self.gate.enable()
                self.run_now_event.set()
                return

        self.config.calibration = payload.previous_calibration
        self.toggle_button.configure(text="开启托管 (F8)")
        if payload.previous_calibration is not None:
            self._set_status("自动识别失败", "已保留原校准配置，托管保持暂停", "#b3261e")
        else:
            self._set_status("需要校准", "自动识别失败，请使用手动校准", "#b3261e")
        messagebox.showerror(
            "自动识别失败",
            f"{payload.error}\n\n已停止操作，可点击“手动校准”继续。"
            + (f"\n失败截图：{payload.failure_path}" if payload.failure_path else "")
            + (f"\n诊断目录：{self.diagnostics_dir}" if self.diagnostics_dir else ""),
            parent=self.root,
        )

    def start_calibration(self) -> None:
        if self._auto_detecting:
            return
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
        self._set_detected_scale(None)
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

    def hide_to_tray(self) -> None:
        """Window close keeps automation alive and hides only the UI."""
        if self._wizard is not None and self._wizard.winfo_exists():
            self._wizard.withdraw()
        self.root.withdraw()

    def show_main_window(self) -> None:
        if self._exiting:
            return
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.focus_force()
        if self._wizard is not None and self._wizard.winfo_exists():
            self._wizard.deiconify()
            self._wizard.lift()

    def exit_application(self) -> None:
        """Fully stop the app from either the main window or system tray."""
        if self._exiting:
            return
        self._exiting = True
        cancel_event = getattr(self, "_auto_detection_cancel", None)
        if cancel_event is not None:
            cancel_event.set()
        self.gate.request_shutdown()
        self.hotkeys.stop()
        self.session_logging.disable()
        self.tray.stop()
        self.root.destroy()


def main() -> None:
    configure_logging()
    log_runtime_environment()
    instance = SingleInstance()
    if not instance.acquire():
        show_already_running_message()
        return
    try:
        root = tk.Tk()
        TaskbarHeroApp(root)
        root.mainloop()
    finally:
        instance.release()
