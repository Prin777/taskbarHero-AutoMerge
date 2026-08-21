from __future__ import annotations

import base64
import ctypes
import logging
import os
import struct
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable

from .engine import AutomationError, Category, ControlGate, RoundCancelled
from .model import Calibration, Point, WindowIdentity


LOG = logging.getLogger("tbh_automation.windows")


if os.name != "nt":
    raise RuntimeError("此模块仅支持 Windows")


user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GA_ROOT = 2
SW_RESTORE = 9
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


def capture_screen_bgra(x: int, y: int, width: int, height: int) -> bytes:
    if width <= 0 or height <= 0:
        raise ValueError("截图区域必须大于零")
    screen_dc = user32.GetDC(0)
    memory_dc = gdi32.CreateCompatibleDC(screen_dc)
    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
    old_object = gdi32.SelectObject(memory_dc, bitmap)
    try:
        if not gdi32.BitBlt(memory_dc, 0, 0, width, height, screen_dc, x, y, SRCCOPY):
            raise ctypes.WinError(ctypes.get_last_error())
        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # top-down
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB
        buffer = (ctypes.c_ubyte * (width * height * 4))()
        lines = gdi32.GetDIBits(memory_dc, bitmap, 0, height, buffer, ctypes.byref(info), DIB_RGB_COLORS)
        if lines != height:
            raise ctypes.WinError(ctypes.get_last_error())
        return bytes(buffer)
    finally:
        gdi32.SelectObject(memory_dc, old_object)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(0, screen_dc)


def capture_client_patch(hwnd: int, center: Point, size: int) -> bytes:
    screen = client_to_screen(hwnd, center)
    half = size // 2
    return capture_screen_bgra(screen.x - half, screen.y - half, size, size)


def capture_client_bgra(hwnd: int) -> tuple[bytes, int, int]:
    width, height = get_client_size(hwnd)
    origin = client_to_screen(hwnd, Point(0, 0))
    return capture_screen_bgra(origin.x, origin.y, width, height), width, height


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
    for center in grid_centers(calibration):
        screen = client_to_screen(hwnd, center)
        if not point_is_visible_on_window(hwnd, screen):
            raise ValueError("校准时九宫格被其他窗口遮挡，请移开遮挡后重试")
        patch = capture_client_patch(hwnd, center, calibration.patch_size)
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


def count_filled_cells(differences: list[float], threshold: float) -> int:
    return sum(difference >= threshold for difference in differences)


class Win32AutomationDriver:
    def __init__(
        self,
        calibration: Calibration,
        gate: ControlGate,
        generation: int,
        *,
        click_delay: float,
        action_timeout: float,
        status: Callable[[str], None] | None = None,
    ) -> None:
        calibration.validate()
        self.calibration = calibration
        self.gate = gate
        self.generation = generation
        self.click_delay = click_delay
        self.action_timeout = action_timeout
        self.status = status or (lambda _message: None)
        self._empty_patches = [base64.b64decode(value) for value in calibration.empty_grid_patches]

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
        screen = client_to_screen(hwnd, self.calibration.points[key])
        if not point_is_visible_on_window(hwnd, screen):
            raise AutomationError("游戏操作区域被其他窗口遮挡，请移开遮挡窗口后重新开启")
        user32.SetCursorPos(screen.x, screen.y)
        # 移动后再复核，防止切窗瞬间误点其他程序。
        self.checkpoint()
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
        self.gate.interruptible_wait(self.click_delay, self.generation)

    def _filled_count_once(self) -> int:
        self.checkpoint()
        hwnd = self._foreground()
        differences: list[float] = []
        for center, empty in zip(grid_centers(self.calibration), self._empty_patches, strict=True):
            screen = client_to_screen(hwnd, center)
            if not point_is_visible_on_window(hwnd, screen):
                raise AutomationError("九宫格被其他窗口遮挡，已暂停以避免误判")
            current = capture_client_patch(hwnd, center, self.calibration.patch_size)
            difference = mean_absolute_difference(current, empty)
            differences.append(difference)
        count = count_filled_cells(differences, self.calibration.difference_threshold)
        LOG.info(
            "九宫格识别：%d/9，阈值 %.1f，各格差异 %s",
            count,
            self.calibration.difference_threshold,
            ", ".join(f"{difference:.1f}" for difference in differences),
        )
        return count

    def select_category(self, category: Category) -> None:
        self._click("category_dropdown")
        key = "category_equipment" if category is Category.EQUIPMENT else "category_material"
        self._click(key)

    def click_auto_fill(self) -> None:
        self._click("auto_fill")

    def observe_filled_cells(self) -> int:
        self.gate.interruptible_wait(0.65, self.generation)
        deadline = time.monotonic() + 3.0
        previous: int | None = None
        stable = 0
        latest = 0
        while time.monotonic() < deadline:
            latest = self._filled_count_once()
            self.status(f"正在识别合成格：{latest}/9")
            if latest == previous:
                stable += 1
            else:
                stable = 1
                previous = latest
            if stable >= 2:
                return latest
            self.gate.interruptible_wait(0.25, self.generation)
        return latest

    def click_combine(self) -> None:
        # 上层只有在稳定识别为 9/9 时才会调用。
        self._click("combine")

    def wait_for_combination(self) -> None:
        self.gate.interruptible_wait(0.5, self.generation)
        deadline = time.monotonic() + self.action_timeout
        non_full_samples = 0
        while time.monotonic() < deadline:
            count = self._filled_count_once()
            if count < 9:
                non_full_samples += 1
                if non_full_samples >= 2:
                    self.gate.interruptible_wait(0.55, self.generation)
                    return
            else:
                non_full_samples = 0
            self.gate.interruptible_wait(0.25, self.generation)
        raise AutomationError("合成动画超时，无法确认合成完成，已暂停")

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
