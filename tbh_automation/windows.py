from __future__ import annotations

import ctypes
import logging
import os
import struct
import threading
import time
from contextlib import contextmanager
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

from .autodetect import (
    AutoDetectionResult,
    AmbiguousDetectionError,
    COMBINE_BUTTON_BASE_SAMPLE_SIZE,
    SynthesisLayoutError,
    combine_button_sample_size,
    find_auto_fill_anchor,
    find_cube_icon,
    layout_points_from_anchor,
)
from .engine import AutomationError, Category, ControlGate, RoundCancelled
from .model import Calibration, Point, WindowIdentity


LOG = logging.getLogger("tbh_automation.windows")


if os.name != "nt":
    raise RuntimeError("此模块仅支持 Windows")


user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GA_ROOT = 2
GW_HWNDPREV = 3
SW_RESTORE = 9
GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
HWND_TOPMOST = wintypes.HWND(-1)
HWND_NOTOPMOST = wintypes.HWND(-2)
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
BI_RGB = 0
DIB_RGB_COLORS = 0
SRCCOPY = 0x00CC0020
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
VK_F7 = 0x76
VK_F8 = 0x77
VK_F9 = 0x78
VK_TAB = 0x09
KEYEVENTF_KEYUP = 0x0002
TASKBAR_HERO_PROCESS = "taskbarhero.exe"
TASKBAR_HERO_CLASS = "UnityWndClass"
TASKBAR_HERO_TITLE = "TaskBarHero"

CURSOR_SETTLE_SECONDS = 0.10
MAX_DIAGNOSTIC_SCREENSHOTS = 200
COMBINE_BUTTON_SAMPLE_SIZE = COMBINE_BUTTON_BASE_SAMPLE_SIZE
COMBINE_BUTTON_BLUE_RATIO_THRESHOLD = 0.25
COMBINE_BUTTON_CONFIRMATIONS = 2
COMBINE_BUTTON_CONFIRM_INTERVAL_SECONDS = 0.20
COMBINE_BUTTON_INITIAL_WAIT_SECONDS = 0.65
COMBINE_BUTTON_ACTIVATION_TIMEOUT_SECONDS = 3.0
GAME_ACTIVATION_TIMEOUT_SECONDS = 3.0
GAME_ACTIVATION_RETRY_SECONDS = 0.20


class GameActivationError(AutomationError):
    """The verified game window exists but is not ready to become foreground."""


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class RGBQUAD(ctypes.Structure):
    _fields_ = [
        ("rgbBlue", wintypes.BYTE),
        ("rgbGreen", wintypes.BYTE),
        ("rgbRed", wintypes.BYTE),
        ("rgbReserved", wintypes.BYTE),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", RGBQUAD * 1)]


ENUM_WINDOWS_CALLBACK = ctypes.WINFUNCTYPE(
    wintypes.BOOL,
    wintypes.HWND,
    wintypes.LPARAM,
)


user32.GetForegroundWindow.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.EnumWindows.argtypes = [ENUM_WINDOWS_CALLBACK, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetAncestor.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, wintypes.WPARAM]
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.GetDpiForWindow.argtypes = [wintypes.HWND]
user32.GetDpiForWindow.restype = wintypes.UINT
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = wintypes.HDC
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, wintypes.LPVOID, ctypes.POINTER(BITMAPINFO), wintypes.UINT]
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wintypes.HDC]
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]


def _root_window(hwnd: int) -> int:
    return int(user32.GetAncestor(hwnd, GA_ROOT) or hwnd)


def get_foreground_root() -> int:
    hwnd = user32.GetForegroundWindow()
    return _root_window(int(hwnd)) if hwnd else 0


def get_client_size(hwnd: int) -> tuple[int, int]:
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError(ctypes.get_last_error())
    return rect.right - rect.left, rect.bottom - rect.top


def get_window_dpi(hwnd: int) -> int:
    """返回窗口实际 DPI；Windows 11 上 100% 为 96，150% 为 144。"""
    dpi = int(user32.GetDpiForWindow(hwnd))
    return dpi if dpi > 0 else 96


def get_window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


def get_class_name(hwnd: int) -> str:
    buffer = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buffer, len(buffer))
    return buffer.value


def get_process_name(hwnd: int) -> str:
    process_id = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, process_id.value)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return ""
        return Path(buffer.value).name
    finally:
        kernel32.CloseHandle(handle)


def describe_window(hwnd: int) -> WindowIdentity:
    width, height = get_client_size(hwnd)
    return WindowIdentity(
        title=get_window_title(hwnd),
        class_name=get_class_name(hwnd),
        process_name=get_process_name(hwnd),
        client_width=width,
        client_height=height,
    )


def window_matches(hwnd: int, identity: WindowIdentity) -> bool:
    if not hwnd:
        return False
    try:
        width, height = get_client_size(hwnd)
    except OSError:
        return False
    # 像素模板要求尺寸保持一致。位置可以任意移动。
    if (width, height) != (identity.client_width, identity.client_height):
        return False
    process_name = get_process_name(hwnd)
    if identity.process_name and process_name:
        return process_name.casefold() == identity.process_name.casefold()
    title = get_window_title(hwnd)
    class_name = get_class_name(hwnd)
    if identity.title and title != identity.title:
        return False
    return not identity.class_name or class_name == identity.class_name


def is_taskbar_hero_identity(identity: WindowIdentity) -> bool:
    return (
        identity.process_name.casefold() == TASKBAR_HERO_PROCESS
        and identity.class_name == TASKBAR_HERO_CLASS
        and identity.client_width >= 300
        and identity.client_height >= 300
    )


def find_taskbar_hero_window(preferred: WindowIdentity | None = None) -> int:
    """从所有顶层窗口中安全定位游戏，不依赖用户先把游戏切到前台。"""
    candidates: list[tuple[int, WindowIdentity]] = []
    known = int(user32.FindWindowW(TASKBAR_HERO_CLASS, TASKBAR_HERO_TITLE) or 0)
    if known and user32.IsWindowVisible(known):
        try:
            identity = describe_window(known)
            if is_taskbar_hero_identity(identity):
                candidates.append((known, identity))
        except OSError:
            pass

    def collect(hwnd: int, _parameter: int) -> bool:
        try:
            value = int(hwnd)
            if not user32.IsWindowVisible(value) or value == known:
                return True
            identity = describe_window(value)
            if is_taskbar_hero_identity(identity):
                candidates.append((value, identity))
        except (OSError, ValueError):
            pass
        return True

    callback = ENUM_WINDOWS_CALLBACK(collect)
    user32.EnumWindows(callback, 0)
    if not candidates:
        foreground = get_foreground_root()
        if foreground:
            try:
                identity = describe_window(foreground)
                if is_taskbar_hero_identity(identity):
                    candidates.append((foreground, identity))
            except OSError:
                pass
    if not candidates:
        return 0

    def candidate_score(candidate: tuple[int, WindowIdentity]) -> tuple[int, int]:
        hwnd, identity = candidate
        preferred_match = int(preferred is not None and window_matches(hwnd, preferred))
        return preferred_match, identity.client_width * identity.client_height

    return max(candidates, key=candidate_score)[0]


def cursor_capture() -> tuple[int, Point, WindowIdentity]:
    screen_point = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(screen_point)):
        raise ctypes.WinError(ctypes.get_last_error())
    hwnd = _root_window(int(user32.WindowFromPoint(screen_point)))
    client_point = wintypes.POINT(screen_point.x, screen_point.y)
    if not user32.ScreenToClient(hwnd, ctypes.byref(client_point)):
        raise ctypes.WinError(ctypes.get_last_error())
    return hwnd, Point(client_point.x, client_point.y), describe_window(hwnd)


def cursor_point_for_window(hwnd: int) -> Point:
    screen_point = wintypes.POINT()
    if not user32.GetCursorPos(ctypes.byref(screen_point)):
        raise ctypes.WinError(ctypes.get_last_error())
    if not user32.ScreenToClient(hwnd, ctypes.byref(screen_point)):
        raise ctypes.WinError(ctypes.get_last_error())
    return Point(screen_point.x, screen_point.y)


def client_to_screen(hwnd: int, point: Point) -> Point:
    value = wintypes.POINT(point.x, point.y)
    if not user32.ClientToScreen(hwnd, ctypes.byref(value)):
        raise ctypes.WinError(ctypes.get_last_error())
    return Point(value.x, value.y)


def _window_at_screen_point(screen_point: Point) -> int:
    return int(
        user32.WindowFromPoint(wintypes.POINT(screen_point.x, screen_point.y)) or 0
    )


def point_is_visible_on_window(hwnd: int, screen_point: Point) -> bool:
    covering = _window_at_screen_point(screen_point)
    return bool(covering) and _root_window(covering) == _root_window(hwnd)


def _screen_point_is_inside_client(hwnd: int, screen_point: Point) -> bool:
    client_point = wintypes.POINT(screen_point.x, screen_point.y)
    if not user32.ScreenToClient(hwnd, ctypes.byref(client_point)):
        return False
    width, height = get_client_size(hwnd)
    return 0 <= client_point.x < width and 0 <= client_point.y < height


def _window_is_topmost(hwnd: int) -> bool:
    return bool(hwnd and user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)


def point_is_safe_on_active_game(hwnd: int, screen_point: Point) -> bool:
    """兼容透明 Unity 覆盖窗口，同时拒绝真正的置顶遮挡。"""
    if point_is_visible_on_window(hwnd, screen_point):
        return True
    game_root = _root_window(hwnd)
    if get_foreground_root() != game_root or not _window_is_topmost(game_root):
        return False
    covering = _window_at_screen_point(screen_point)
    if not covering or not _screen_point_is_inside_client(hwnd, screen_point):
        return False
    covering_root = _root_window(covering)
    if get_process_name(covering_root).casefold() == TASKBAR_HERO_PROCESS:
        return True
    # 游戏已经处于最上层时，透明区域的 WindowFromPoint 仍可能返回其下方的
    # 资源管理器等普通窗口；这不是视觉遮挡。另一个置顶窗口则仍然拒绝。
    if _window_is_topmost(covering_root):
        return False
    LOG.info(
        "透明游戏窗口命中下层窗口，已按前台和置顶状态继续：title=%r class=%r process=%r",
        get_window_title(covering_root),
        get_class_name(covering_root),
        get_process_name(covering_root),
    )
    return True


def client_area_is_uncovered(hwnd: int) -> bool:
    width, height = get_client_size(hwnd)
    sample_points = [
        Point(round(width * x_fraction), round(height * y_fraction))
        for y_fraction in (0.15, 0.35, 0.55, 0.75, 0.9)
        for x_fraction in (0.15, 0.35, 0.55, 0.75, 0.9)
    ]
    return all(
        point_is_visible_on_window(hwnd, client_to_screen(hwnd, point))
        for point in sample_points
    )


def _rectangles_intersect(left: wintypes.RECT, right: wintypes.RECT) -> bool:
    return not (
        left.right <= right.left
        or left.left >= right.right
        or left.bottom <= right.top
        or left.top >= right.bottom
    )


def _automation_focus_rect(hwnd: int) -> wintypes.RECT:
    width, height = get_client_size(hwnd)
    origin = client_to_screen(hwnd, Point(0, 0))
    # 背包与魔方操作区位于客户区右侧中段；避开任务栏等无关系统窗口。
    return wintypes.RECT(
        origin.x + round(width * 0.48),
        origin.y + round(height * 0.28),
        origin.x + width,
        origin.y + round(height * 0.78),
    )


def _topmost_obstructions_above(hwnd: int) -> list[int]:
    focus_rect = _automation_focus_rect(hwnd)
    result: list[int] = []
    candidate = int(user32.GetWindow(hwnd, GW_HWNDPREV) or 0)
    while candidate:
        if (
            _root_window(candidate) != _root_window(hwnd)
            and user32.IsWindowVisible(candidate)
            and bool(user32.GetWindowLongW(candidate, GWL_EXSTYLE) & WS_EX_TOPMOST)
            and get_class_name(candidate) not in {"Shell_TrayWnd", "Shell_SecondaryTrayWnd"}
        ):
            rect = wintypes.RECT()
            if user32.GetWindowRect(candidate, ctypes.byref(rect)) and _rectangles_intersect(
                rect,
                focus_rect,
            ):
                result.append(candidate)
        candidate = int(user32.GetWindow(candidate, GW_HWNDPREV) or 0)
    return result


@contextmanager
def temporarily_uncover_window(
    hwnd: int,
    *,
    report_obstruction: bool = False,
    wait: Callable[[float], None] | None = None,
) -> Iterator[None]:
    """截图或点击前真正激活并置顶游戏，避免置顶终端污染桌面取图。"""
    pause = wait or time.sleep
    was_topmost = bool(user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)
    lowered_windows: list[int] = []
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        pause(0.15)
        LOG.info("自动界面准备：已从最小化状态恢复 TaskBarHero")
    if report_obstruction and not client_area_is_uncovered(hwnd):
        LOG.warning("检测到游戏窗口被遮挡，采集期间将激活并临时置于最上层")
    activate_flags = SWP_NOMOVE | SWP_NOSIZE
    restore_flags = activate_flags | SWP_NOACTIVATE
    try:
        activation_deadline = time.monotonic() + GAME_ACTIVATION_TIMEOUT_SECONDS
        activation_attempt = 0
        while True:
            activation_attempt += 1
            if not user32.SetWindowPos(
                hwnd,
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                activate_flags,
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            # Unity 冷启动时窗口可能已枚举出来，但还要数个刷新周期才能接管前台。
            pause(0.15 if activation_attempt == 1 else GAME_ACTIVATION_RETRY_SECONDS)
            if get_foreground_root() == _root_window(hwnd):
                break
            if time.monotonic() >= activation_deadline:
                raise GameActivationError(
                    "游戏窗口已经找到，但暂时无法激活到前台，请先手动点击游戏"
                )
            LOG.info("游戏窗口尚未接管前台，正在重试激活（第 %d 次）", activation_attempt + 1)
        # 某些置顶终端即使不处于前台，也会继续覆盖透明的 Unity 游戏窗口。
        # 在截图/点击期间临时取消其置顶，退出上下文时恢复原状态。
        for _attempt in range(2):
            obstructions = [
                candidate
                for candidate in _topmost_obstructions_above(hwnd)
                if candidate not in lowered_windows
            ]
            if not obstructions:
                break
            for candidate in obstructions:
                LOG.warning(
                    "临时降低覆盖窗口：title=%r class=%r process=%r",
                    get_window_title(candidate),
                    get_class_name(candidate),
                    get_process_name(candidate),
                )
                if user32.SetWindowPos(
                    candidate,
                    HWND_NOTOPMOST,
                    0,
                    0,
                    0,
                    0,
                    restore_flags,
                ):
                    lowered_windows.append(candidate)
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, activate_flags)
            user32.BringWindowToTop(hwnd)
            pause(0.08)
        remaining = _topmost_obstructions_above(hwnd)
        if remaining:
            covering = remaining[0]
            message = (
                "置顶窗口持续覆盖游戏操作区："
                f"{get_window_title(covering) or get_process_name(covering) or get_class_name(covering)}"
            )
            if report_obstruction:
                raise ValueError(message)
            raise AutomationError(message)
        yield
    finally:
        if not was_topmost:
            user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, restore_flags)
        for candidate in reversed(lowered_windows):
            if user32.IsWindow(candidate):
                user32.SetWindowPos(candidate, HWND_TOPMOST, 0, 0, 0, 0, restore_flags)


def press_tab_on_game(
    hwnd: int,
    *,
    wait: Callable[[float], None] | None = None,
) -> None:
    """激活已经严格识别的游戏窗口，并发送一次英雄菜单快捷键。"""
    with temporarily_uncover_window(
        hwnd,
        report_obstruction=True,
        wait=wait,
    ):
        if get_foreground_root() != _root_window(hwnd):
            raise ValueError("无法激活游戏，未发送 Tab")
        user32.keybd_event(VK_TAB, 0, 0, 0)
        user32.keybd_event(VK_TAB, 0, KEYEVENTF_KEYUP, 0)
        LOG.info("自动界面准备：已向 TaskBarHero 发送 Tab，打开英雄背包")


def click_game_point(
    hwnd: int,
    point: Point,
    label: str,
    *,
    wait: Callable[[float], None] | None = None,
) -> None:
    """校准前只允许在已识别游戏客户区的可见位置执行一次安全点击。"""
    pause = wait or time.sleep
    with temporarily_uncover_window(
        hwnd,
        report_obstruction=True,
        wait=pause,
    ):
        screen = client_to_screen(hwnd, point)
        if not point_is_safe_on_active_game(hwnd, screen):
            raise ValueError(f"{label}被其他窗口遮挡，未执行点击")
        if not user32.SetCursorPos(screen.x, screen.y):
            raise ValueError(f"无法移动鼠标到{label}")
        pause(CURSOR_SETTLE_SECONDS)
        if get_foreground_root() != _root_window(hwnd) or not point_is_safe_on_active_game(
            hwnd,
            screen,
        ):
            raise ValueError(f"点击{label}前游戏失去前台，已取消")
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        LOG.info(
            "自动界面准备：点击%s，客户区坐标=(%d,%d)，屏幕坐标=(%d,%d)",
            label,
            point.x,
            point.y,
            screen.x,
            screen.y,
        )


def capture_client_patch(hwnd: int, center: Point, size: int) -> bytes:
    image, width, height = capture_client_bgra(hwnd)
    half = size // 2
    return crop_bgra(image, width, height, center.x - half, center.y - half, size, size)


def capture_client_bgra(hwnd: int) -> tuple[bytes, int, int]:
    """从已经激活并置顶的游戏客户区在桌面上取实时 DirectX 画面。"""
    width, height = get_client_size(hwnd)
    origin = client_to_screen(hwnd, Point(0, 0))
    screen_dc = user32.GetDC(0)
    if not screen_dc:
        raise AutomationError("无法取得桌面画面，已暂停以避免误判")
    memory_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
    old_object = gdi32.SelectObject(memory_dc, bitmap)
    try:
        if not gdi32.BitBlt(
            memory_dc,
            0,
            0,
            width,
            height,
            screen_dc,
            origin.x,
            origin.y,
            SRCCOPY,
        ):
            raise AutomationError("无法截取游戏客户区画面，已暂停以避免误判")
        image = bitmap_bgra(memory_dc, bitmap, width, height)
        if not image_has_visual_content(image):
            raise AutomationError("游戏客户区截图为空白或不完整，已暂停以避免误判")
        return image, width, height
    finally:
        gdi32.SelectObject(memory_dc, old_object)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(0, screen_dc)


def open_synthesis_interface(
    hwnd: int,
    *,
    wait: Callable[[float], None] | None = None,
    checkpoint: Callable[[], None] | None = None,
    diagnostics_dir: Path | None = None,
    progress: Callable[[str], None] | None = None,
) -> tuple[bytes, int, int, AutoDetectionResult]:
    """必要时打开英雄背包和魔方，并返回自动填充文字的识别结果。"""
    pause = wait or time.sleep
    ensure_active = checkpoint or (lambda: None)

    def capture(stage: str) -> tuple[bytes, int, int]:
        ensure_active()
        with temporarily_uncover_window(
            hwnd,
            report_obstruction=True,
            wait=pause,
        ):
            image, width, height = capture_client_bgra(hwnd)
        ensure_active()
        if diagnostics_dir is not None:
            try:
                dpi = get_window_dpi(hwnd)
                origin = client_to_screen(hwnd, Point(0, 0))
                LOG.info(
                    "自动识别环境：stage=%s hwnd=%d client=%dx%d origin=(%d,%d) "
                    "primary=%dx%d virtual=(%d,%d,%dx%d) dpi=%d scale=%.2f",
                    stage,
                    hwnd,
                    width,
                    height,
                    origin.x,
                    origin.y,
                    user32.GetSystemMetrics(0),
                    user32.GetSystemMetrics(1),
                    user32.GetSystemMetrics(76),
                    user32.GetSystemMetrics(77),
                    user32.GetSystemMetrics(78),
                    user32.GetSystemMetrics(79),
                    dpi,
                    dpi / 96,
                )
            except Exception:
                LOG.exception("记录自动识别显示环境失败，但识别将继续")
            try:
                timestamp = datetime.now().strftime("%H%M%S-%f")
                path = save_bgra_bmp(
                    diagnostics_dir / f"{timestamp}-auto-detect-{stage}.bmp",
                    image,
                    width,
                    height,
                )
                LOG.info("自动识别阶段截图：stage=%s path=%s", stage, path)
            except Exception:
                LOG.exception("保存自动识别阶段截图失败，但识别将继续")
        return image, width, height

    def detect(
        image: bytes,
        width: int,
        height: int,
    ) -> AutoDetectionResult:
        ensure_active()
        dpi = get_window_dpi(hwnd)
        result = find_auto_fill_anchor(
            image,
            width,
            height,
            preferred_scale=dpi / 96,
            require_button_color=True,
            require_synthesis_layout=True,
            checkpoint=ensure_active,
            progress=progress,
        )
        ensure_active()
        return result

    image, width, height = capture("initial")
    # 已打开的合成面板通过完整文字、类别菜单、边界及多候选校验后，
    # 不需要任何入口导航。透明客户区中的桌面黄色图案不应阻断此路径。
    try:
        return image, width, height, detect(image, width, height)
    except (SynthesisLayoutError, AmbiguousDetectionError):
        raise
    except ValueError:
        LOG.info("当前未确认合成面板，开始检查英雄背包入口")

    ensure_active()
    try:
        cube = find_cube_icon(image, width, height, checkpoint=ensure_active)
        ensure_active()
        LOG.info("英雄背包已打开：识别到魔方入口 %s", cube)
    except AmbiguousDetectionError:
        # 需要导航时仍禁止从多个入口候选中猜选。
        raise
    except ValueError:
        LOG.info("当前未显示英雄背包，开始自动准备界面")
        ensure_active()
        press_tab_on_game(hwnd, wait=pause)
        pause(1.0)
        image, width, height = capture("after-tab")
        ensure_active()
        cube = find_cube_icon(image, width, height, checkpoint=ensure_active)
        ensure_active()
        LOG.info("按 Tab 后识别到魔方入口：%s", cube)

    ensure_active()
    click_game_point(hwnd, cube.center, "黄色魔方入口", wait=pause)
    pause(1.2)
    image, width, height = capture("after-cube-click")
    # 无法识别自动填充时直接安全失败。再次点击同一入口可能把已经打开的
    # 合成面板关闭，不能仅凭文字识别失败推断当前页面状态。
    return image, width, height, detect(image, width, height)


def bitmap_bgra(memory_dc: int, bitmap: int, width: int, height: int) -> bytes:
    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = -height
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = BI_RGB
    buffer = (ctypes.c_ubyte * (width * height * 4))()
    lines = gdi32.GetDIBits(
        memory_dc,
        bitmap,
        0,
        height,
        buffer,
        ctypes.byref(info),
        DIB_RGB_COLORS,
    )
    if lines != height:
        raise ctypes.WinError(ctypes.get_last_error())
    return bytes(buffer)


def image_has_visual_content(image_bgra: bytes) -> bool:
    """拒绝窗口 DC 常见的全黑、纯色或只绘制了局部的失败画面。"""
    if not image_bgra or len(image_bgra) % 4:
        return False
    pixel_count = len(image_bgra) // 4
    stride_pixels = max(1, pixel_count // 4096)
    minimum = 255
    maximum = 0
    visible_samples = 0
    total_samples = 0
    for pixel_index in range(0, pixel_count, stride_pixels):
        offset = pixel_index * 4
        channels = image_bgra[offset : offset + 3]
        total_samples += 1
        if max(channels) >= 8:
            visible_samples += 1
        for channel in channels:
            minimum = min(minimum, channel)
            maximum = max(maximum, channel)
    visible_ratio = visible_samples / total_samples
    return maximum - minimum >= 8 and visible_ratio >= 0.40


def crop_bgra(
    image_bgra: bytes,
    image_width: int,
    image_height: int,
    left: int,
    top: int,
    width: int,
    height: int,
) -> bytes:
    if (
        image_width <= 0
        or image_height <= 0
        or len(image_bgra) != image_width * image_height * 4
        or left < 0
        or top < 0
        or width <= 0
        or height <= 0
        or left + width > image_width
        or top + height > image_height
    ):
        raise ValueError("客户区图像裁剪范围无效")
    row_bytes = width * 4
    rows: list[bytes] = []
    for row in range(top, top + height):
        start = (row * image_width + left) * 4
        rows.append(image_bgra[start : start + row_bytes])
    return b"".join(rows)


def save_bgra_bmp(path: Path, image_bgra: bytes, width: int, height: int) -> Path:
    if width <= 0 or height <= 0 or len(image_bgra) != width * height * 4:
        raise ValueError("截图尺寸不正确，无法保存诊断图片")
    path.parent.mkdir(parents=True, exist_ok=True)
    pixel_offset = 14 + 40
    file_size = pixel_offset + len(image_bgra)
    file_header = struct.pack("<2sIHHI", b"BM", file_size, 0, 0, pixel_offset)
    dib_header = struct.pack(
        "<IiiHHIIiiII",
        40,
        width,
        -height,
        1,
        32,
        BI_RGB,
        len(image_bgra),
        0,
        0,
        0,
        0,
    )
    path.write_bytes(file_header + dib_header + image_bgra)
    return path


def blue_highlight_ratio(image_bgra: bytes) -> float:
    """计算按钮采样块中蓝/青色高亮像素的比例。"""
    if not image_bgra or len(image_bgra) % 4:
        raise ValueError("按钮图像采样块尺寸不正确")
    highlighted = 0
    pixels = len(image_bgra) // 4
    for index in range(0, len(image_bgra), 4):
        blue = image_bgra[index]
        green = image_bgra[index + 1]
        red = image_bgra[index + 2]
        # 可合成按钮是深蓝/青色；不可合成按钮的 RGB 三通道接近灰色。
        if blue >= 55 and blue - red >= 25 and green - red >= 8:
            highlighted += 1
    return highlighted / pixels


class Win32AutomationDriver:
    def __init__(
        self,
        calibration: Calibration,
        gate: ControlGate,
        generation: int,
        *,
        click_delay: float,
        combination_wait: float,
        status: Callable[[str], None] | None = None,
        diagnostics_dir: Path | None = None,
        on_scale_detected: Callable[[float], None] | None = None,
    ) -> None:
        calibration.validate()
        self.calibration = calibration
        self.gate = gate
        self.generation = generation
        self.click_delay = click_delay
        self.combination_wait = combination_wait
        self.status = status or (lambda _message: None)
        self.diagnostics_dir = diagnostics_dir
        self._interface_scale = 1.0
        self.on_scale_detected = on_scale_detected
        self._diagnostic_index = (
            sum(1 for _path in diagnostics_dir.glob("*.bmp"))
            if diagnostics_dir is not None
            else 0
        )

    def _foreground(self) -> int:
        return find_taskbar_hero_window(self.calibration.window)

    def checkpoint(self) -> None:
        if self.gate.shutdown_requested or self.generation != self.gate.generation or not self.gate.enabled:
            raise RoundCancelled()
        while True:
            hwnd = self._foreground()
            if window_matches(hwnd, self.calibration.window):
                return
            if hwnd:
                raise AutomationError("游戏窗口尺寸或显示缩放已改变，请重新自动识别位置")
            self.status("等待 TaskBarHero 游戏启动")
            self.gate.wait_enabled(self.generation, 0.25)

    def prepare_interface(self) -> None:
        while True:
            self.checkpoint()
            hwnd = self._foreground()
            try:
                _image, width, height, result = open_synthesis_interface(
                    hwnd,
                    wait=lambda seconds: self.gate.interruptible_wait(
                        seconds,
                        self.generation,
                    ),
                    checkpoint=self.checkpoint,
                    diagnostics_dir=getattr(self, "diagnostics_dir", None),
                    progress=getattr(self, "status", None),
                )
                break
            except GameActivationError:
                self.status("已找到 TaskBarHero，正在等待游戏窗口可以激活")
                self.gate.interruptible_wait(0.5, self.generation)
            except ValueError as error:
                raise AutomationError(f"自动准备游戏界面失败：{error}") from error
        if (width, height) != (
            self.calibration.window.client_width,
            self.calibration.window.client_height,
        ):
            raise AutomationError("游戏窗口尺寸在界面准备期间发生变化，请重新自动识别")
        try:
            points = layout_points_from_anchor(
                result.anchor, result.scale, client_size=(width, height)
            )
        except ValueError as error:
            raise AutomationError(str(error)) from error
        self.calibration.points.update(points)
        self._interface_scale = result.scale
        if callback := getattr(self, "on_scale_detected", None):
            callback(result.scale)
        LOG.info(
            "合成界面准备完成：anchor=(%d,%d) scale=%.2f，全部按钮位置已刷新",
            result.anchor.x,
            result.anchor.y,
            result.scale,
        )

    def _button_sample_size(self) -> int:
        return combine_button_sample_size(getattr(self, "_interface_scale", 1.0))

    def _click(self, key: str) -> None:
        self.checkpoint()
        hwnd = self._foreground()
        with temporarily_uncover_window(
            hwnd,
            wait=lambda seconds: self.gate.interruptible_wait(
                seconds,
                self.generation,
            ),
        ):
            screen = client_to_screen(hwnd, self.calibration.points[key])
            if not point_is_safe_on_active_game(hwnd, screen):
                raise AutomationError("游戏操作区域被其他窗口遮挡，请移开遮挡窗口后重新开启")
            if not user32.SetCursorPos(screen.x, screen.y):
                raise AutomationError("无法移动鼠标到游戏操作位置，已暂停")
            # Unity 需要处理一次鼠标移动后，按钮悬停与可点击状态才会更新。
            self.gate.interruptible_wait(CURSOR_SETTLE_SECONDS, self.generation)
            # 移动后再复核，防止切窗瞬间误点其他程序。
            self.checkpoint()
            hwnd = self._foreground()
            screen = client_to_screen(hwnd, self.calibration.points[key])
            if get_foreground_root() != _root_window(hwnd):
                raise AutomationError("点击前游戏失去前台，已取消操作")
            if not point_is_safe_on_active_game(hwnd, screen):
                raise AutomationError("游戏操作区域被其他窗口遮挡，请移开遮挡窗口后重新开启")
            if not user32.SetCursorPos(screen.x, screen.y):
                raise AutomationError("无法移动鼠标到游戏操作位置，已暂停")
            if key == "combine":
                # 最后一次检查紧贴鼠标按下，避免前两次确认后按钮状态发生变化。
                button_image = capture_client_patch(
                    hwnd,
                    self.calibration.points["combine"],
                    self._button_sample_size(),
                )
                button_ratio = blue_highlight_ratio(button_image)
                if button_ratio < COMBINE_BUTTON_BLUE_RATIO_THRESHOLD:
                    self._save_button_diagnostic(button_image, button_ratio, False)
                    raise AutomationError("合成按钮在点击前已变为灰色，已取消点击并暂停")
                LOG.info("合成按钮点击前复核：蓝色像素比例 %.1f%%", button_ratio * 100)
            LOG.info(
                "点击操作：%s，客户区坐标=(%d,%d)，屏幕坐标=(%d,%d)",
                key,
                self.calibration.points[key].x,
                self.calibration.points[key].y,
                screen.x,
                screen.y,
            )
            user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            self.gate.interruptible_wait(self.click_delay, self.generation)

    def select_category(self, category: Category) -> None:
        self._click("category_dropdown")
        key = "category_equipment" if category is Category.EQUIPMENT else "category_material"
        self._click(key)

    def click_auto_fill(self) -> None:
        self._click("auto_fill")

    def _combine_button_ratio_once(self) -> tuple[float, bytes]:
        self.checkpoint()
        hwnd = self._foreground()
        with temporarily_uncover_window(
            hwnd,
            wait=lambda seconds: self.gate.interruptible_wait(
                seconds,
                self.generation,
            ),
        ):
            center = self.calibration.points["combine"]
            screen = client_to_screen(hwnd, center)
            if not point_is_safe_on_active_game(hwnd, screen):
                raise AutomationError("合成按钮被其他窗口遮挡，已禁止点击")
            image = capture_client_patch(hwnd, center, self._button_sample_size())
        return blue_highlight_ratio(image), image

    def _save_button_diagnostic(self, image: bytes, ratio: float, enabled: bool) -> None:
        if self.diagnostics_dir is None or self._diagnostic_index >= MAX_DIAGNOSTIC_SCREENSHOTS:
            return
        try:
            self._diagnostic_index += 1
            timestamp = datetime.now().strftime("%H%M%S-%f")
            state = "enabled" if enabled else "disabled"
            filename = (
                f"{timestamp}-{self._diagnostic_index:04d}-combine-{state}"
                f"-{ratio:.3f}.bmp"
            )
            path = save_bgra_bmp(
                self.diagnostics_dir / filename,
                image,
                self._button_sample_size(),
                self._button_sample_size(),
            )
            LOG.info("合成按钮诊断截图：%s", path)
        except Exception:
            LOG.exception("保存合成按钮诊断截图失败，但自动化将继续运行")

    def combine_button_is_enabled(self) -> bool:
        """等待自动填充完成，并连续确认按钮为蓝色高亮。"""
        latest_image = b""
        consecutive_blue: list[float] = []
        self.status("正在等待合成按钮激活")
        self.gate.interruptible_wait(
            COMBINE_BUTTON_INITIAL_WAIT_SECONDS,
            self.generation,
        )
        deadline = time.monotonic() + COMBINE_BUTTON_ACTIVATION_TIMEOUT_SECONDS
        while True:
            ratio, latest_image = self._combine_button_ratio_once()
            if ratio >= COMBINE_BUTTON_BLUE_RATIO_THRESHOLD:
                consecutive_blue.append(ratio)
                if len(consecutive_blue) >= COMBINE_BUTTON_CONFIRMATIONS:
                    LOG.info(
                        "合成按钮识别：蓝色可用，连续采样比例 %s（阈值 %.1f%%）",
                        ", ".join(
                            f"{value * 100:.1f}%"
                            for value in consecutive_blue[-COMBINE_BUTTON_CONFIRMATIONS:]
                        ),
                        COMBINE_BUTTON_BLUE_RATIO_THRESHOLD * 100,
                    )
                    self._save_button_diagnostic(latest_image, ratio, True)
                    return True
            else:
                consecutive_blue.clear()

            if time.monotonic() >= deadline:
                LOG.info(
                    "合成按钮识别：等待 %.1f 秒后仍为灰色不可用，最后蓝色像素比例 "
                    "%.1f%%（阈值 %.1f%%）",
                    COMBINE_BUTTON_ACTIVATION_TIMEOUT_SECONDS,
                    ratio * 100,
                    COMBINE_BUTTON_BLUE_RATIO_THRESHOLD * 100,
                )
                self._save_button_diagnostic(latest_image, ratio, False)
                return False
            self.gate.interruptible_wait(
                COMBINE_BUTTON_CONFIRM_INTERVAL_SECONDS,
                self.generation,
            )

    def click_combine(self) -> None:
        # 上层只有在连续确认合成按钮为蓝色高亮时才会调用。
        self._click("combine")

    def wait_for_combination(self) -> None:
        self.status("等待合成动画完成，随后将再次自动填充")
        self.gate.interruptible_wait(self.combination_wait, self.generation)

    def click_return_items(self) -> None:
        self._click("return_items")


class GlobalHotkeyListener:
    """F7 校准捕获、F8 开关、F9 紧急停止。"""

    def __init__(
        self,
        *,
        on_f7: Callable[[], None],
        on_f8: Callable[[], None],
        on_f9: Callable[[], None],
        on_error: Callable[[str], None] | None = None,
    ) -> None:
        self.callbacks = {1: on_f7, 2: on_f8, 3: on_f9}
        self.on_error = on_error or (lambda _message: None)
        self._thread: threading.Thread | None = None
        self._thread_id = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="global-hotkeys", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    def _run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        registered: list[int] = []
        try:
            for hotkey_id, key in ((1, VK_F7), (2, VK_F8), (3, VK_F9)):
                if user32.RegisterHotKey(None, hotkey_id, 0, key):
                    registered.append(hotkey_id)
                else:
                    name = {1: "F7（校准捕获）", 2: "F8（开关托管）", 3: "F9（紧急停止）"}[hotkey_id]
                    self.on_error(f"{name}注册失败，可能已被其他程序占用")
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == WM_HOTKEY:
                    callback = self.callbacks.get(int(message.wParam))
                    if callback:
                        callback()
        finally:
            for hotkey_id in registered:
                user32.UnregisterHotKey(None, hotkey_id)
