from __future__ import annotations

import ctypes
import logging
import sys
import threading
from collections.abc import Callable
from pathlib import Path


LOG = logging.getLogger("tbh_automation")
ERROR_ALREADY_EXISTS = 183
INSTANCE_MUTEX_NAME = r"Local\TBH-AutoSynth-5A8D3D2F-970E-4A64-A830-746849BC7356"


def application_icon_path() -> Path:
    """Return the icon bundled beside the source or inside a PyInstaller app."""
    bundle_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return bundle_dir / "assets" / "taskbar-hero.ico"


def _create_named_mutex(name: str) -> tuple[int | None, int]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    create_mutex.restype = ctypes.c_void_p
    ctypes.set_last_error(0)
    handle = create_mutex(None, False, name)
    return (int(handle) if handle else None, ctypes.get_last_error())


def _close_handle(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_int
    close_handle(handle)


class SingleInstance:
    """Own a Windows named mutex for the lifetime of the process."""

    def __init__(self, name: str = INSTANCE_MUTEX_NAME) -> None:
        self.name = name
        self._handle: int | None = None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        if sys.platform != "win32":
            return True

        handle, error_code = _create_named_mutex(self.name)
        if handle is None:
            raise OSError(error_code, "无法创建程序单实例锁")
        if error_code == ERROR_ALREADY_EXISTS:
            _close_handle(handle)
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        _close_handle(self._handle)
        self._handle = None


def show_already_running_message() -> None:
    """Show a message without constructing a second Tk application."""
    if sys.platform != "win32":
        return
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    message_box = user32.MessageBoxW
    message_box.argtypes = (
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_uint,
    )
    message_box.restype = ctypes.c_int
    message_box(
        None,
        "TBH 自动合成已经在运行，请通过系统托盘打开现有程序。",
        "TBH 自动合成",
        0x00000040 | 0x00040000,
    )


class SystemTray:
    """Run a pystray icon and forward its actions to the Tk event queue."""

    def __init__(self, on_show: Callable[[], None], on_exit: Callable[[], None]) -> None:
        self._on_show = on_show
        self._on_exit = on_exit
        self._icon: object | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._icon is not None:
            return

        try:
            import pystray
            from PIL import Image
        except ImportError as error:
            raise RuntimeError("缺少系统托盘组件，请重新运行 build.cmd 构建程序") from error

        icon_path = application_icon_path()
        if not icon_path.is_file():
            raise RuntimeError(f"找不到系统托盘图标：{icon_path}")

        with Image.open(icon_path) as source_image:
            image = source_image.copy()
        menu = pystray.Menu(
            pystray.MenuItem("显示主窗口", lambda _icon, _item: self._on_show(), default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出后台", lambda _icon, _item: self._on_exit()),
        )
        icon = pystray.Icon("TBH-AutoSynth", image, "TBH 自动合成", menu)
        self._icon = icon
        self._thread = threading.Thread(
            target=self._run,
            args=(icon,),
            name="system-tray",
            daemon=True,
        )
        self._thread.start()

    def _run(self, icon: object) -> None:
        try:
            icon.run()  # type: ignore[attr-defined]
        except Exception:
            LOG.exception("系统托盘图标运行失败")

    def stop(self) -> None:
        icon = self._icon
        self._icon = None
        if icon is not None:
            try:
                icon.stop()  # type: ignore[attr-defined]
            except Exception:
                LOG.exception("停止系统托盘图标时出错")
        self._thread = None
