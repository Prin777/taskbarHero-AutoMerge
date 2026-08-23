from __future__ import annotations

import ctypes
import os
from ctypes import wintypes


if os.name != "nt":
    raise RuntimeError("此模块仅支持 Windows")


user32 = ctypes.WinDLL("user32", use_last_error=True)

# Windows SDK 中的 DPI_AWARENESS_CONTEXT 是带符号的伪句柄。
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
DPI_AWARENESS_UNAWARE = 0
DPI_AWARENESS_SYSTEM_AWARE = 1
DPI_AWARENESS_PER_MONITOR_AWARE = 2
ERROR_ACCESS_DENIED = 5


def _current_awareness() -> int:
    try:
        get_context = user32.GetThreadDpiAwarenessContext
        get_context.argtypes = []
        get_context.restype = wintypes.HANDLE
        get_awareness = user32.GetAwarenessFromDpiAwarenessContext
        get_awareness.argtypes = [wintypes.HANDLE]
        get_awareness.restype = ctypes.c_int
        return int(get_awareness(get_context()))
    except AttributeError:
        return DPI_AWARENESS_UNAWARE


def enable_per_monitor_dpi_awareness() -> int:
    """在创建任何窗口前关闭 DPI 虚拟化，并返回最终感知级别。"""
    try:
        setter = user32.SetProcessDpiAwarenessContext
        setter.argtypes = [wintypes.HANDLE]
        setter.restype = wintypes.BOOL
        if not setter(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2):
            error = ctypes.get_last_error()
            # 已由可执行文件清单或更早的调用设置时，Windows 返回拒绝访问。
            if error != ERROR_ACCESS_DENIED:
                raise ctypes.WinError(error)
    except AttributeError:
        # Windows 8.1 的兼容回退；本项目面向 Windows 11，通常不会走到这里。
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        setter = shcore.SetProcessDpiAwareness
        setter.argtypes = [ctypes.c_int]
        setter.restype = wintypes.HRESULT
        result = int(setter(DPI_AWARENESS_PER_MONITOR_AWARE))
        if result not in (0, -2147024891):  # S_OK 或 E_ACCESSDENIED
            raise OSError(f"无法启用 DPI 感知（HRESULT 0x{result & 0xFFFFFFFF:08X}）")

    awareness = _current_awareness()
    if awareness == DPI_AWARENESS_UNAWARE:
        raise RuntimeError("无法关闭 Windows DPI 虚拟化，程序已停止以避免错误截图和点击")
    return awareness


def awareness_name(awareness: int) -> str:
    return {
        DPI_AWARENESS_UNAWARE: "未感知",
        DPI_AWARENESS_SYSTEM_AWARE: "系统 DPI 感知",
        DPI_AWARENESS_PER_MONITOR_AWARE: "每显示器 DPI 感知",
    }.get(awareness, f"未知（{awareness}）")
