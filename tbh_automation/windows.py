from __future__ import annotations

import base64
import ctypes
import logging
import math
import os
import struct
import threading
import time
from contextlib import contextmanager
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

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

# 自动填充过程中九格可能短暂全部出现，随后继续播放逐格动画。先等待画面
# 稳定，再以合成按钮高亮作为最终点击条件。
FULL_GRID_STABLE_SECONDS = 2.5
OTHER_GRID_STABLE_SECONDS = 0.5
GRID_OBSERVATION_TIMEOUT_SECONDS = 10.0
CURSOR_SETTLE_SECONDS = 0.10
SALIENT_PIXEL_FRACTION = 0.20
MAX_DIAGNOSTIC_SCREENSHOTS = 200
COMBINE_BUTTON_SAMPLE_SIZE = 32
COMBINE_BUTTON_BLUE_RATIO_THRESHOLD = 0.25
COMBINE_BUTTON_CONFIRMATIONS = 2
COMBINE_BUTTON_CONFIRM_INTERVAL_SECONDS = 0.20


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


user32.GetForegroundWindow.restype = wintypes.HWND
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


def point_is_visible_on_window(hwnd: int, screen_point: Point) -> bool:
    covering = user32.WindowFromPoint(wintypes.POINT(screen_point.x, screen_point.y))
    return bool(covering) and _root_window(int(covering)) == _root_window(hwnd)


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
def temporarily_uncover_window(hwnd: int, *, report_obstruction: bool = False) -> Iterator[None]:
    """截图或点击前真正激活并置顶游戏，避免置顶终端污染桌面取图。"""
    was_topmost = bool(user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST)
    if report_obstruction and not client_area_is_uncovered(hwnd):
        LOG.warning("检测到游戏窗口被遮挡，采集期间将激活并临时置于最上层")
    activate_flags = SWP_NOMOVE | SWP_NOSIZE
    restore_flags = activate_flags | SWP_NOACTIVATE
    if not user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, activate_flags):
        raise ctypes.WinError(ctypes.get_last_error())
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    # 给 DWM 和 Unity 两个刷新周期，确保桌面画面已经切换到游戏。
    time.sleep(0.15)
    lowered_windows: list[int] = []
    try:
        if get_foreground_root() != _root_window(hwnd):
            message = "无法把游戏窗口激活到前台，请先手动点击游戏后重试"
            if report_obstruction:
                raise ValueError(message)
            raise AutomationError(message)
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
            time.sleep(0.08)
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


def grid_centers(calibration: Calibration) -> list[Point]:
    top_left = calibration.points["grid_top_left"]
    bottom_right = calibration.points["grid_bottom_right"]
    centers: list[Point] = []
    for row in range(3):
        for column in range(3):
            x = round(top_left.x + (bottom_right.x - top_left.x) * column / 2)
            y = round(top_left.y + (bottom_right.y - top_left.y) * row / 2)
            centers.append(Point(x, y))
    return centers


def encode_empty_grid(hwnd: int, calibration: Calibration) -> list[str]:
    patches: list[str] = []
    image, width, height = capture_client_bgra(hwnd)
    for center in grid_centers(calibration):
        screen = client_to_screen(hwnd, center)
        if not point_is_visible_on_window(hwnd, screen):
            raise ValueError("校准时九宫格被其他窗口遮挡，请移开遮挡后重试")
        half = calibration.patch_size // 2
        patch = crop_bgra(
            image,
            width,
            height,
            center.x - half,
            center.y - half,
            calibration.patch_size,
            calibration.patch_size,
        )
        patches.append(base64.b64encode(patch).decode("ascii"))
    return patches


def encoded_grid_looks_empty(encoded_patches: list[str], maximum_average_difference: float = 15.0) -> bool:
    if len(encoded_patches) != 9:
        return False
    patches = [base64.b64decode(value) for value in encoded_patches]
    differences = [
        mean_absolute_difference(patches[left], patches[right])
        for left in range(9)
        for right in range(left)
    ]
    return bool(differences) and sum(differences) / len(differences) <= maximum_average_difference


def mean_absolute_difference(left: bytes, right: bytes) -> float:
    if len(left) != len(right) or not left:
        raise ValueError("图像采样块尺寸不一致")
    # 忽略恒为 0/255 的 alpha 通道。
    total = 0
    channels = 0
    for index in range(0, len(left), 4):
        total += abs(left[index] - right[index])
        total += abs(left[index + 1] - right[index + 1])
        total += abs(left[index + 2] - right[index + 2])
        channels += 3
    return total / channels


def salient_pixel_difference(
    left: bytes,
    right: bytes,
    fraction: float = SALIENT_PIXEL_FRACTION,
) -> float:
    """返回变化最明显的一部分像素的平均 RGB 差异。

    普通品质图标可能只有边缘少量像素区别于空格背景；全图平均会把这些变化
    稀释掉。取显著像素仍能识别图标，同时忽略 alpha 通道。
    """
    if len(left) != len(right) or not left or len(left) % 4:
        raise ValueError("图像采样块尺寸不一致")
    if not 0 < fraction <= 1:
        raise ValueError("显著像素比例必须在 0 到 1 之间")
    pixel_differences = [
        (
            abs(left[index] - right[index])
            + abs(left[index + 1] - right[index + 1])
            + abs(left[index + 2] - right[index + 2])
        )
        / 3
        for index in range(0, len(left), 4)
    ]
    selected_count = max(1, math.ceil(len(pixel_differences) * fraction))
    pixel_differences.sort(reverse=True)
    return sum(pixel_differences[:selected_count]) / selected_count


def count_filled_cells(differences: list[float], threshold: float) -> int:
    return sum(difference >= threshold for difference in differences)


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
    ) -> None:
        calibration.validate()
        self.calibration = calibration
        self.gate = gate
        self.generation = generation
        self.click_delay = click_delay
        self.combination_wait = combination_wait
        self.status = status or (lambda _message: None)
        self.diagnostics_dir = diagnostics_dir
        self._diagnostic_index = (
            sum(1 for _path in diagnostics_dir.glob("*.bmp"))
            if diagnostics_dir is not None
            else 0
        )
        self._diagnostic_limit_logged = False
        self._empty_patches = [base64.b64decode(value) for value in calibration.empty_grid_patches]
        self._last_mean_differences: list[float] = []
        self._last_salient_differences: list[float] = []

    def _foreground(self) -> int:
        return get_foreground_root()

    def checkpoint(self) -> None:
        if self.gate.shutdown_requested or self.generation != self.gate.generation or not self.gate.enabled:
            raise RoundCancelled()
        while True:
            hwnd = self._foreground()
            if window_matches(hwnd, self.calibration.window):
                return
            self.status("等待游戏窗口位于前台")
            self.gate.wait_enabled(self.generation, 0.25)

    def _click(self, key: str) -> None:
        self.checkpoint()
        hwnd = self._foreground()
        with temporarily_uncover_window(hwnd):
            screen = client_to_screen(hwnd, self.calibration.points[key])
            if not point_is_visible_on_window(hwnd, screen):
                raise AutomationError("游戏操作区域被其他窗口遮挡，请移开遮挡窗口后重新开启")
            if not user32.SetCursorPos(screen.x, screen.y):
                raise AutomationError("无法移动鼠标到游戏操作位置，已暂停")
            # Unity 需要处理一次鼠标移动后，按钮悬停与可点击状态才会更新。
            self.gate.interruptible_wait(CURSOR_SETTLE_SECONDS, self.generation)
            # 移动后再复核，防止切窗瞬间误点其他程序。
            self.checkpoint()
            hwnd = self._foreground()
            screen = client_to_screen(hwnd, self.calibration.points[key])
            if not point_is_visible_on_window(hwnd, screen):
                raise AutomationError("游戏操作区域被其他窗口遮挡，请移开遮挡窗口后重新开启")
            if not user32.SetCursorPos(screen.x, screen.y):
                raise AutomationError("无法移动鼠标到游戏操作位置，已暂停")
            if key == "combine":
                # 最后一次检查紧贴鼠标按下，避免前两次确认后按钮状态发生变化。
                button_image = capture_client_patch(
                    hwnd,
                    self.calibration.points["combine"],
                    COMBINE_BUTTON_SAMPLE_SIZE,
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

    def _filled_count_once(self) -> int:
        self.checkpoint()
        hwnd = self._foreground()
        mean_differences: list[float] = []
        salient_differences: list[float] = []
        with temporarily_uncover_window(hwnd):
            image, width, height = capture_client_bgra(hwnd)
            for center, empty in zip(grid_centers(self.calibration), self._empty_patches, strict=True):
                screen = client_to_screen(hwnd, center)
                if not point_is_visible_on_window(hwnd, screen):
                    raise AutomationError("九宫格被其他窗口遮挡，已暂停以避免误判")
                half = self.calibration.patch_size // 2
                current = crop_bgra(
                    image,
                    width,
                    height,
                    center.x - half,
                    center.y - half,
                    self.calibration.patch_size,
                    self.calibration.patch_size,
                )
                mean_differences.append(mean_absolute_difference(current, empty))
                salient_differences.append(salient_pixel_difference(current, empty))
        self._last_mean_differences = mean_differences
        self._last_salient_differences = salient_differences
        count = count_filled_cells(salient_differences, self.calibration.difference_threshold)
        LOG.info(
            "九宫格识别：%d/9，显著像素阈值 %.1f，显著差异 %s，整体平均 %s",
            count,
            self.calibration.difference_threshold,
            ", ".join(f"{difference:.1f}" for difference in salient_differences),
            ", ".join(f"{difference:.1f}" for difference in mean_differences),
        )
        return count

    def _save_grid_diagnostic(self, label: str, count: int | None) -> None:
        diagnostics_dir = getattr(self, "diagnostics_dir", None)
        if diagnostics_dir is None:
            return
        if self._diagnostic_index >= MAX_DIAGNOSTIC_SCREENSHOTS:
            if not self._diagnostic_limit_logged:
                LOG.warning(
                    "本次会话诊断截图已达到 %d 张上限，后续只记录文字日志",
                    MAX_DIAGNOSTIC_SCREENSHOTS,
                )
                self._diagnostic_limit_logged = True
            return
        try:
            self._diagnostic_index += 1
            hwnd = self._foreground()
            if not window_matches(hwnd, self.calibration.window):
                LOG.info("诊断截图已跳过：游戏窗口当前不在前台")
                return
            with temporarily_uncover_window(hwnd):
                centers = grid_centers(self.calibration)
                half = self.calibration.patch_size // 2
                padding = 8
                client_width, client_height = get_client_size(hwnd)
                left = max(0, min(point.x for point in centers) - half - padding)
                top = max(0, min(point.y for point in centers) - half - padding)
                right = min(client_width, max(point.x for point in centers) + half + padding)
                bottom = min(client_height, max(point.y for point in centers) + half + padding)
                client_image, captured_width, captured_height = capture_client_bgra(hwnd)
                image = crop_bgra(
                    client_image,
                    captured_width,
                    captured_height,
                    left,
                    top,
                    right - left,
                    bottom - top,
                )
            timestamp = datetime.now().strftime("%H%M%S-%f")
            count_suffix = "" if count is None else f"-{count}of9"
            filename = f"{timestamp}-{self._diagnostic_index:04d}-{label}{count_suffix}.bmp"
            path = save_bgra_bmp(diagnostics_dir / filename, image, right - left, bottom - top)
            LOG.info(
                "九宫格诊断截图：%s；显著差异=%s；整体平均=%s",
                path,
                ", ".join(f"{value:.1f}" for value in self._last_salient_differences),
                ", ".join(f"{value:.1f}" for value in self._last_mean_differences),
            )
        except Exception:
            LOG.exception("保存九宫格诊断截图失败，但自动化将继续运行")

    def select_category(self, category: Category) -> None:
        self._click("category_dropdown")
        key = "category_equipment" if category is Category.EQUIPMENT else "category_material"
        self._click(key)

    def click_auto_fill(self) -> None:
        self._click("auto_fill")

    def observe_filled_cells(self) -> int:
        self.gate.interruptible_wait(0.65, self.generation)
        deadline = time.monotonic() + GRID_OBSERVATION_TIMEOUT_SECONDS
        previous: int | None = None
        stable_since: float | None = None
        latest = 0
        while time.monotonic() < deadline:
            latest = self._filled_count_once()
            observed_at = time.monotonic()
            self.status(f"正在识别合成格：{latest}/9")
            if latest == previous:
                if stable_since is None:
                    stable_since = observed_at
            else:
                previous = latest
                stable_since = observed_at
            required_stability = (
                FULL_GRID_STABLE_SECONDS if latest == 9 else OTHER_GRID_STABLE_SECONDS
            )
            assert stable_since is not None
            if observed_at - stable_since >= required_stability:
                self._save_grid_diagnostic("fill-stable", latest)
                return latest
            self.gate.interruptible_wait(0.25, self.generation)
        if latest == 9:
            self._save_grid_diagnostic("fill-unstable", latest)
            raise AutomationError("九宫格虽已填满但画面一直未稳定，未点击合成并已暂停")
        self._save_grid_diagnostic("fill-timeout", latest)
        return latest

    def _combine_button_ratio_once(self) -> tuple[float, bytes]:
        self.checkpoint()
        hwnd = self._foreground()
        with temporarily_uncover_window(hwnd):
            center = self.calibration.points["combine"]
            screen = client_to_screen(hwnd, center)
            if not point_is_visible_on_window(hwnd, screen):
                raise AutomationError("合成按钮被其他窗口遮挡，已禁止点击")
            image = capture_client_patch(hwnd, center, COMBINE_BUTTON_SAMPLE_SIZE)
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
                COMBINE_BUTTON_SAMPLE_SIZE,
                COMBINE_BUTTON_SAMPLE_SIZE,
            )
            LOG.info("合成按钮诊断截图：%s", path)
        except Exception:
            LOG.exception("保存合成按钮诊断截图失败，但自动化将继续运行")

    def combine_button_is_enabled(self) -> bool:
        """连续确认按钮为蓝色高亮；任一采样不满足时均禁止合成。"""
        latest_image = b""
        ratios: list[float] = []
        self.gate.interruptible_wait(0.15, self.generation)
        for sample_index in range(COMBINE_BUTTON_CONFIRMATIONS):
            ratio, latest_image = self._combine_button_ratio_once()
            ratios.append(ratio)
            if ratio < COMBINE_BUTTON_BLUE_RATIO_THRESHOLD:
                LOG.info(
                    "合成按钮识别：灰色不可用，蓝色像素比例 %.1f%%（阈值 %.1f%%）",
                    ratio * 100,
                    COMBINE_BUTTON_BLUE_RATIO_THRESHOLD * 100,
                )
                self._save_button_diagnostic(latest_image, ratio, False)
                return False
            if sample_index + 1 < COMBINE_BUTTON_CONFIRMATIONS:
                self.gate.interruptible_wait(
                    COMBINE_BUTTON_CONFIRM_INTERVAL_SECONDS,
                    self.generation,
                )
        LOG.info(
            "合成按钮识别：蓝色可用，连续采样比例 %s（阈值 %.1f%%）",
            ", ".join(f"{ratio * 100:.1f}%" for ratio in ratios),
            COMBINE_BUTTON_BLUE_RATIO_THRESHOLD * 100,
        )
        self._save_button_diagnostic(latest_image, ratios[-1], True)
        return True

    def click_combine(self) -> None:
        # 上层只有在连续确认合成按钮为蓝色高亮时才会调用。
        self._click("combine")

    def wait_for_combination(self) -> None:
        self.status("等待合成动画完成，随后将再次自动填充")
        self.gate.interruptible_wait(self.combination_wait, self.generation)
        self._save_grid_diagnostic("combine-wait-finished", None)

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
