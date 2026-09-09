from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Callable

from .model import Point


# 录屏中“自动填充”文字的二值形状。只用文字区域，不依赖按钮图标或当前类别。
AUTO_FILL_MASK = (
    "...........................................................",
    "....#......................................................",
    "....#..............#.....#....#..#......##.................",
    ".########...#####..#.....#...###.........#.................",
    ".#......#........#####.####.#####...##########.............",
    ".#......#...#####.##.#...#..#####.....##..##...............",
    ".########....#....##.#...#..#...#....##....##..............",
    ".#......#....#.#..#..#...#..#...#....#########.............",
    ".########....#..#.#..#...#..######.....#..#................",
    ".#......#...#.###.#..#..##########.....#..#................",
    ".#......#...##..###..#..#....#.##.....##..#..#.............",
    ".########........#.###.....##...##..###...####.............",
    ".#.........................................................",
)

MASK_WIDTH = len(AUTO_FILL_MASK[0])
MASK_HEIGHT = len(AUTO_FILL_MASK)
GRAY_THRESHOLD = 65
MAX_MISMATCH_RATIO = 0.22
MAX_PREFILTER_MISMATCHES = 4
PREFILTER_SAMPLES_PER_STATE = 8
MIN_BUTTON_BROWN_RATIO = 0.15
GAME_UI_SCALES = (1.0, 1.25, 1.5, 2.0, 3.0)
SUPPORTED_SCALES = (*GAME_UI_SCALES, 1.75, 0.8, 0.75)
MIN_SUPPORTED_SCALE = min(SUPPORTED_SCALES)
MAX_SUPPORTED_SCALE = max(SUPPORTED_SCALES)
COMBINE_BUTTON_BASE_SAMPLE_SIZE = 32
CUBE_OUTLINE_MIN_PIXELS = round(200 * MIN_SUPPORTED_SCALE ** 2)
CUBE_OUTLINE_MIN_SIZE = round(40 * MIN_SUPPORTED_SCALE)
CUBE_SOLID_MIN_PIXELS = round(180 * MIN_SUPPORTED_SCALE ** 2)
CUBE_SOLID_MIN_SIZE = round(24 * MIN_SUPPORTED_SCALE)
CUBE_OUTLINE_MAX_SIZE = round(60 * MAX_SUPPORTED_SCALE)
CUBE_SOLID_MAX_SIZE = round(40 * MAX_SUPPORTED_SCALE)
CUBE_COMPONENT_LOG_MIN_PIXELS = 20


LOG = logging.getLogger("tbh_automation.autodetect")


class SynthesisLayoutError(ValueError):
    """文字存在，但不是可以安全操作的完整装备/材料合成布局。"""


class AmbiguousDetectionError(ValueError):
    """存在多个不同位置的有效候选，禁止用导航操作试探。"""


@dataclass(frozen=True)
class AutoDetectionResult:
    anchor: Point
    mismatch_ratio: float
    scale: float = 1.0

    @property
    def confidence(self) -> float:
        return 1.0 - self.mismatch_ratio


@dataclass(frozen=True)
class CubeIconDetectionResult:
    center: Point
    bounds: tuple[int, int, int, int]
    yellow_pixels: int


def _bgra_to_gray(image: bytes, checkpoint: Callable[[], None] | None = None) -> bytes:
    gray = bytearray(len(image) // 4)
    target = 0
    for source in range(0, len(image), 4):
        if checkpoint is not None and source % 262144 == 0:
            checkpoint()
        blue, green, red = image[source], image[source + 1], image[source + 2]
        gray[target] = (29 * blue + 150 * green + 77 * red) >> 8
        target += 1
    return bytes(gray)


def _scaled_samples(scale: float) -> tuple[int, int, list[tuple[int, int, bool]]]:
    scaled_width = round(MASK_WIDTH * scale)
    scaled_height = round(MASK_HEIGHT * scale)
    source_limit = 52
    samples = [
        (
            target_x,
            target_y,
            AUTO_FILL_MASK[
                min(int(target_y / scale), MASK_HEIGHT - 1)
            ][min(int(target_x / scale), MASK_WIDTH - 1)] == "#",
        )
        for target_y in range(scaled_height)
        for target_x in range(scaled_width)
        if min(int(target_x / scale), MASK_WIDTH - 1) < source_limit
    ]
    return scaled_width, scaled_height, samples


def _brown_anchor_mask(
    rows: tuple[int, ...],
    y: int,
    offsets: tuple[tuple[int, int], ...],
    valid_columns: int,
) -> int:
    """用每像素一个字节的位集合，筛出七个采样点中至少两个为棕色的锚点。

    与逐像素预筛选等价，包括奇数坐标及中心被文字覆盖的情况。大幅减少
    大截图上对纯色背景执行 Python 循环的次数，不降低文字匹配精度。
    """
    seen = twice = 0
    for dx, dy in offsets:
        if 0 <= y + dy < len(rows):
            shifted = rows[y + dy] >> (dx * 8)
            twice |= seen & shifted
            seen |= shifted
    return twice & valid_columns


def _best_match_at_scale(
    gray: bytes,
    width: int,
    height: int,
    scale: float,
    brown_mask: bytes | None = None,
    checkpoint: Callable[[], None] | None = None,
    matches: list[tuple[Point, float, float]] | None = None,
    bright_rows: tuple[int, ...] | None = None,
) -> tuple[Point | None, float]:
    mask_width, mask_height, samples = _scaled_samples(scale)
    if width < mask_width or height < mask_height:
        return None, 1.0
    foreground = [sample for sample in samples if sample[2]]
    background = [sample for sample in samples if not sample[2]]
    foreground_step = max(1, len(foreground) // PREFILTER_SAMPLES_PER_STATE)
    background_step = max(1, len(background) // PREFILTER_SAMPLES_PER_STATE)
    prefilter = (
        foreground[::foreground_step][:PREFILTER_SAMPLES_PER_STATE]
        + background[::background_step][:PREFILTER_SAMPLES_PER_STATE]
    )

    best_ratio = 1.0
    best_point: Point | None = None
    max_x = width - mask_width
    max_y = height - mask_height
    brown_rows = (
        tuple(int.from_bytes(brown_mask[y * width:(y + 1) * width], 'little') for y in range(height))
        if brown_mask is not None else None
    )
    brown_offsets = tuple(
        (round(29 * scale) + round(dx * scale), round(7 * scale) + round(dy * scale))
        for dx, dy in ((0, 0), (-12, -5), (12, -5), (-12, 5), (12, 5), (-22, 0), (22, 0))
    )
    # 每像素仍占一个字节，位 0 表示明暗；所有横坐标（含奇数）并行比较。
    if bright_rows is None:
        binary = gray.translate(bytes(int(value >= GRAY_THRESHOLD) for value in range(256)))
        bright_rows = tuple(
            int.from_bytes(binary[y * width:(y + 1) * width], "little")
            for y in range(height)
        )
    valid_columns = int.from_bytes(b"\x01" * (max_x + 1), "little")
    sample_rows = []
    for row_y in range(mask_height):
        row_samples = [sample for sample in samples if sample[1] == row_y]
        row_mask = sum(1 << (x * 8) for x, _, _ in row_samples)
        expected = sum(1 << (x * 8) for x, _, on in row_samples if on)
        sample_rows.append((row_y, row_mask, expected))
    for y in range(max_y + 1):
        if checkpoint is not None and y % 16 == 0:
            checkpoint()
        # states[k] 表示目前预筛选错配不超过 k 的所有横坐标，与原逐点判断等价。
        states = [valid_columns] * (MAX_PREFILTER_MISMATCHES + 1)
        for sample_x, sample_y, expected_on in prefilter:
            actual = bright_rows[y + sample_y] >> (sample_x * 8)
            bad = (~actual if expected_on else actual) & valid_columns
            for k in range(MAX_PREFILTER_MISMATCHES, 0, -1):
                states[k] = (states[k] & ~bad) | (states[k - 1] & bad)
            states[0] &= ~bad
        candidates = states[-1]
        if brown_rows is not None:
            candidates &= _brown_anchor_mask(brown_rows, y, brown_offsets, valid_columns)
        while candidates:
            lowest = candidates & -candidates
            x = (lowest.bit_length() - 1) // 8
            candidates ^= lowest
            mismatches = sum(
                (((bright_rows[y + row_y] >> (x * 8)) ^ expected) & row_mask).bit_count()
                for row_y, row_mask, expected in sample_rows
            )
            ratio = mismatches / len(samples)
            if matches is not None and ratio <= MAX_MISMATCH_RATIO:
                matches.append((Point(x, y), scale, ratio))
            if ratio < best_ratio:
                best_ratio = ratio
                best_point = Point(x, y)
    return best_point, best_ratio


def _button_brown_ratio(
    image_bgra: bytes,
    width: int,
    height: int,
    anchor: Point,
    scale: float,
) -> float:
    """核对文字周围确实是棕色自动填充按钮，排除终端白字等假匹配。"""
    center_x = anchor.x + round(29 * scale)
    center_y = anchor.y + round(7 * scale)
    half_width = max(12, round(20 * scale))
    half_height = max(7, round(10 * scale))
    left = max(0, center_x - half_width)
    right = min(width, center_x + half_width)
    top = max(0, center_y - half_height)
    bottom = min(height, center_y + half_height)
    total = max(0, right - left) * max(0, bottom - top)
    if total == 0:
        return 0.0
    brown = 0
    for y in range(top, bottom):
        for x in range(left, right):
            offset = (y * width + x) * 4
            blue, green, red = image_bgra[offset : offset + 3]
            if red >= 55 and red >= green + 12 and green >= blue + 4:
                brown += 1
    return brown / total


def _is_cube_yellow(blue: int, green: int, red: int) -> bool:
    return (
        red >= 150
        and green >= 85
        and blue <= 100
        and red >= green + 20
        and green >= blue + 25
    )


def _cube_button_brown_ratio(
    image_bgra: bytes,
    width: int,
    height: int,
    bounds: tuple[int, int, int, int],
) -> float:
    """计算黄色图案周围的深棕按钮底色比例。"""
    component_width = bounds[2] - bounds[0] + 1
    component_height = bounds[3] - bounds[1] + 1
    margin = round(max(component_width, component_height) * 0.5)
    left = max(0, bounds[0] - margin)
    top = max(0, bounds[1] - margin)
    right = min(width - 1, bounds[2] + margin)
    bottom = min(height - 1, bounds[3] + margin)
    brown = 0
    total = 0
    for y in range(top, bottom + 1):
        for x in range(left, right + 1):
            offset = (y * width + x) * 4
            blue, green, red = image_bgra[offset : offset + 3]
            if _is_cube_yellow(blue, green, red):
                continue
            total += 1
            if red >= 35 and red >= green + 12 and green >= blue + 3:
                brown += 1
    return brown / total if total else 0.0


def _infer_cube_from_navigation_row(
    image_bgra: bytes,
    width: int,
    height: int,
    candidates: list[CubeIconDetectionResult],
) -> CubeIconDetectionResult | None:
    """由底部前三个等距黄色功能图标推导第 4 个魔方入口。"""
    # 一个功能图标内的纹理也可能形成独立黄色区域。完全包含的区域不是
    # 另一个导航按钮，否则放大后会由同一个图标推导出偏移的第二组排列。
    distinct = [
        candidate for candidate in candidates
        if not any(
            other is not candidate
            and other.bounds != candidate.bounds
            and other.bounds[0] <= candidate.bounds[0]
            and other.bounds[1] <= candidate.bounds[1]
            and other.bounds[2] >= candidate.bounds[2]
            and other.bounds[3] >= candidate.bounds[3]
            for other in candidates
        )
    ]
    ordered = sorted(distinct, key=lambda candidate: candidate.center.x)
    hypotheses: list[tuple[float, CubeIconDetectionResult]] = []
    y_tolerance = max(10, round(height * 0.025))
    minimum_spacing = max(30, round(width * 0.05))
    maximum_spacing = max(minimum_spacing, round(width * 0.25))

    for first_index in range(len(ordered) - 2):
        for second_index in range(first_index + 1, len(ordered) - 1):
            for third_index in range(second_index + 1, len(ordered)):
                first = ordered[first_index]
                second = ordered[second_index]
                third = ordered[third_index]
                centers_y = (first.center.y, second.center.y, third.center.y)
                if max(centers_y) - min(centers_y) > y_tolerance:
                    continue
                first_spacing = second.center.x - first.center.x
                second_spacing = third.center.x - second.center.x
                average_spacing = (first_spacing + second_spacing) / 2
                if not minimum_spacing <= average_spacing <= maximum_spacing:
                    continue
                if abs(first_spacing - second_spacing) > average_spacing * 0.22:
                    continue

                predicted_x = third.center.x + round(average_spacing)
                predicted_y = round(sum(centers_y) / 3)
                radius = max(16, round(average_spacing * 0.32))
                if (
                    predicted_x - radius < 0
                    or predicted_x + radius >= width
                    or predicted_y - radius < 0
                    or predicted_y + radius >= height
                    or predicted_y > round(height * 0.88)
                ):
                    continue

                yellow_points: list[tuple[int, int]] = []
                for y in range(predicted_y - radius, predicted_y + radius + 1):
                    row_offset = y * width * 4
                    for x in range(predicted_x - radius, predicted_x + radius + 1):
                        offset = row_offset + x * 4
                        if _is_cube_yellow(
                            image_bgra[offset],
                            image_bgra[offset + 1],
                            image_bgra[offset + 2],
                        ):
                            yellow_points.append((x, y))
                if len(yellow_points) < 180:
                    continue
                left = min(point[0] for point in yellow_points)
                top = min(point[1] for point in yellow_points)
                right = max(point[0] for point in yellow_points)
                bottom = max(point[1] for point in yellow_points)
                bounds = (left, top, right, bottom)
                component_width = right - left + 1
                component_height = bottom - top + 1
                aspect = component_width / component_height
                yellow_center = Point((left + right) // 2, (top + bottom) // 2)
                if (
                    not 0.65 <= aspect <= 1.4
                    or abs(yellow_center.x - predicted_x) > radius * 0.30
                    or abs(yellow_center.y - predicted_y) > radius * 0.30
                    or _cube_button_brown_ratio(
                        image_bgra,
                        width,
                        height,
                        bounds,
                    ) < 0.25
                ):
                    continue
                regularity = abs(first_spacing - second_spacing) / average_spacing
                hypotheses.append(
                    (
                        regularity,
                        CubeIconDetectionResult(
                            center=yellow_center,
                            bounds=bounds,
                            yellow_pixels=len(yellow_points),
                        ),
                    )
                )

    if not hypotheses:
        return None
    hypotheses.sort(key=lambda hypothesis: hypothesis[0])
    best = hypotheses[0][1]
    if any(
        abs(candidate.center.x - best.center.x) > 5
        or abs(candidate.center.y - best.center.y) > 5
        for _score, candidate in hypotheses[1:]
    ):
        return None
    return best


def find_cube_icon(
    image_bgra: bytes,
    width: int,
    height: int,
    *,
    checkpoint: Callable[[], None] | None = None,
) -> CubeIconDetectionResult:
    """识别英雄背包底部唯一的大块黄色魔方图标。"""
    if len(image_bgra) != width * height * 4:
        raise ValueError("魔方入口截图尺寸不正确")
    if width < 250 or height < 250:
        raise ValueError("游戏窗口太小，无法识别魔方入口")

    # 魔方入口位于英雄菜单下半部分。先提取高饱和黄色像素，再用连通区域
    # 排除金币文字、细边框和其他较小图标。
    remaining: set[tuple[int, int]] = set()
    for y in range(round(height * 0.45), height):
        if checkpoint is not None and y % 16 == 0:
            checkpoint()
        row_offset = y * width * 4
        for x in range(width):
            offset = row_offset + x * 4
            if _is_cube_yellow(
                image_bgra[offset],
                image_bgra[offset + 1],
                image_bgra[offset + 2],
            ):
                remaining.add((x, y))

    candidates: list[CubeIconDetectionResult] = []
    neighbours = (
        (-1, -1), (0, -1), (1, -1),
        (-1, 0), (1, 0),
        (-1, 1), (0, 1), (1, 1),
    )
    while remaining:
        if checkpoint is not None:
            checkpoint()
        seed = remaining.pop()
        stack = [seed]
        count = 1
        left = right = seed[0]
        top = bottom = seed[1]
        while stack:
            if checkpoint is not None and count % 4096 == 0:
                checkpoint()
            x, y = stack.pop()
            for delta_x, delta_y in neighbours:
                adjacent = (x + delta_x, y + delta_y)
                if adjacent not in remaining:
                    continue
                remaining.remove(adjacent)
                stack.append(adjacent)
                count += 1
                left = min(left, adjacent[0])
                right = max(right, adjacent[0])
                top = min(top, adjacent[1])
                bottom = max(bottom, adjacent[1])

        component_width = right - left + 1
        component_height = bottom - top + 1
        aspect = component_width / component_height
        density = count / (component_width * component_height)
        outline_style = (
            count >= CUBE_OUTLINE_MIN_PIXELS
            and CUBE_OUTLINE_MIN_SIZE <= component_width <= CUBE_OUTLINE_MAX_SIZE
            and CUBE_OUTLINE_MIN_SIZE <= component_height <= CUBE_OUTLINE_MAX_SIZE
            and 0.65 <= aspect <= 1.4
            and density <= 0.24
        )
        solid_style = (
            count >= CUBE_SOLID_MIN_PIXELS
            and CUBE_SOLID_MIN_SIZE <= component_width <= CUBE_SOLID_MAX_SIZE
            and CUBE_SOLID_MIN_SIZE <= component_height <= CUBE_SOLID_MAX_SIZE
            and 0.75 <= aspect <= 1.3
            and density >= 0.30
        )
        bounds = (left, top, right, bottom)
        center_y = (top + bottom) // 2
        position_ok = center_y <= round(height * 0.88)
        brown_ratio = _cube_button_brown_ratio(
            image_bgra,
            width,
            height,
            bounds,
        )
        brown_ok = brown_ratio >= 0.25
        accepted = (outline_style or solid_style) and position_ok and brown_ok
        if count >= CUBE_COMPONENT_LOG_MIN_PIXELS:
            rejected_by: list[str] = []
            if not (outline_style or solid_style):
                rejected_by.append("尺寸/像素数/长宽比/密度不符合")
            if not position_ok:
                rejected_by.append("位置低于客户区88%")
            if not brown_ok:
                rejected_by.append("棕色按钮底座不足")
            LOG.info(
                "黄色区域：bounds=%s pixels=%d size=%dx%d aspect=%.3f "
                "density=%.3f brown=%.3f result=%s",
                bounds,
                count,
                component_width,
                component_height,
                aspect,
                density,
                brown_ratio,
                "候选" if accepted else "淘汰（" + "；".join(rejected_by) + "）",
            )
        if accepted:
            candidates.append(
                CubeIconDetectionResult(
                    center=Point((left + right) // 2, (top + bottom) // 2),
                    bounds=bounds,
                    yellow_pixels=count,
                )
            )

    LOG.info("黄色魔方候选汇总：accepted=%d", len(candidates))
    if len(candidates) == 1:
        candidate = candidates[0]
        size = max(candidate.bounds[2] - candidate.bounds[0] + 1,
                   candidate.bounds[3] - candidate.bounds[1] + 1)
        density = candidate.yellow_pixels / (
            (candidate.bounds[2] - candidate.bounds[0] + 1)
            * (candidate.bounds[3] - candidate.bounds[1] + 1)
        )
        # 扩大的尺寸范围只用于导航排列交叉验证，不放行孤立的大黄色块。
        if size <= (120 if density <= 0.24 else 80):
            return candidate
    inferred = _infer_cube_from_navigation_row(
        image_bgra,
        width,
        height,
        candidates,
    )
    if inferred is not None:
        return inferred
    if not candidates:
        raise ValueError("未识别到英雄背包底部的黄色魔方入口")
    if len(candidates) == 1:
        raise ValueError("黄色区域较大且缺少导航排列证据，无法安全确定魔方入口")
    raise AmbiguousDetectionError("识别到多个相似的黄色图标，无法安全确定魔方入口")


def find_auto_fill_anchor(
    image_bgra: bytes,
    width: int,
    height: int,
    preferred_scale: float | None = None,
    require_button_color: bool = False,
    *,
    require_synthesis_layout: bool = False,
    checkpoint: Callable[[], None] | None = None,
    progress: Callable[[str], None] | None = None,
) -> AutoDetectionResult:
    """在前台窗口截图中以常见界面缩放比例寻找“自动填充”文字。"""
    if len(image_bgra) != width * height * 4:
        raise ValueError("自动识别截图尺寸不正确")
    if width < round(MASK_WIDTH * min(SUPPORTED_SCALES)) or height < round(MASK_HEIGHT * min(SUPPORTED_SCALES)):
        raise ValueError("前台窗口太小，未找到魔方面板")

    if checkpoint is not None:
        checkpoint()
    if progress is not None:
        progress("正在分析截图，准备识别模板比例")
    gray = _bgra_to_gray(image_bgra, checkpoint)
    binary = gray.translate(bytes(int(value >= GRAY_THRESHOLD) for value in range(256)))
    bright_rows = tuple(int.from_bytes(binary[y * width:(y + 1) * width], "little") for y in range(height))
    brown_mask: bytes | None = None
    if require_button_color:
        mask = bytearray(width * height)
        target = 0
        for source in range(0, len(image_bgra), 4):
            if checkpoint is not None and source % 262144 == 0:
                checkpoint()
            blue, green, red = image_bgra[source : source + 3]
            mask[target] = int(
                red >= 55 and red >= green + 12 and green >= blue + 4
            )
            target += 1
        brown_mask = bytes(mask)
    best_point: Point | None = None
    best_ratio = 1.0
    best_scale = 1.0
    matches: list[tuple[Point, float, float]] = []
    scales = SUPPORTED_SCALES
    if preferred_scale is not None:
        # DPI 只决定搜索顺序。截图中的游戏比例才决定坐标和采样尺寸。
        scales = tuple(sorted(SUPPORTED_SCALES, key=lambda value: abs(value - preferred_scale)))
    for index, scale in enumerate(scales, 1):
        if progress is not None:
            progress(f"正在核对模板 ×{scale:g}（{index}/{len(scales)}），F9 可取消")
        point, ratio = _best_match_at_scale(
            gray,
            width,
            height,
            scale,
            brown_mask,
            checkpoint,
            matches,
            bright_rows,
        )
        if point is not None and ratio < best_ratio:
            best_point = point
            best_ratio = ratio
            best_scale = scale

    if best_point is None or best_ratio > MAX_MISMATCH_RATIO:
        raise ValueError("未能可靠识别“自动填充”按钮，请使用手动校准")
    if (
        require_button_color
        and _button_brown_ratio(image_bgra, width, height, best_point, best_scale)
        < MIN_BUTTON_BROWN_RATIO
    ):
        raise ValueError("识别到相似文字，但未检测到棕色“自动填充”按钮，请使用手动校准")
    for point, scale, _ratio in matches:
        # 同一文字在相邻像素/比例上的匹配属于一个候选；相互分离的文字
        # 不能按分数或搜索顺序任选其一。跨比例同样检查，避免 DPI 提前放行。
        same_text = (
            point.x < best_point.x + round(MASK_WIDTH * best_scale)
            and best_point.x < point.x + round(MASK_WIDTH * scale)
            and point.y < best_point.y + round(MASK_HEIGHT * best_scale)
            and best_point.y < point.y + round(MASK_HEIGHT * scale)
        )
        if not same_text and (
            not require_button_color
            or _button_brown_ratio(image_bgra, width, height, point, scale) >= MIN_BUTTON_BROWN_RATIO
        ):
            raise AmbiguousDetectionError("识别到多个自动填充按钮，无法安全确定合成面板")
    result = AutoDetectionResult(anchor=best_point, mismatch_ratio=best_ratio, scale=best_scale)
    if require_synthesis_layout:
        points = layout_points_from_anchor(best_point, best_scale, client_size=(width, height))
        dropdown = points["category_dropdown"]
        brown = 0
        for dx, dy in ((-6, -6), (6, -6), (-6, 6), (6, 6)):
            x, y = dropdown.x + round(dx * best_scale), dropdown.y + round(dy * best_scale)
            if 0 <= x < width and 0 <= y < height:
                offset = (y * width + x) * 4
                blue, green, red = image_bgra[offset:offset + 3]
                brown += red >= 55 and red >= green + 12 and green >= blue + 4
        if brown < 2:
            raise SynthesisLayoutError("未找到自动填充右侧的类别菜单，请在魔方面板顶部选择“合成”模式")
    if checkpoint is not None:
        checkpoint()
    return result


def combine_button_sample_size(scale: float) -> int:
    if not math.isfinite(scale) or not MIN_SUPPORTED_SCALE <= scale <= MAX_SUPPORTED_SCALE:
        raise ValueError("界面比例超出支持范围，请重新识别位置")
    return round(COMBINE_BUTTON_BASE_SAMPLE_SIZE * scale)


def layout_points_from_anchor(
    anchor: Point,
    scale: float = 1.0,
    *,
    client_size: tuple[int, int] | None = None,
) -> dict[str, Point]:
    """从文字锚点推导当前固定魔方面板中的所有操作位置。"""
    offsets = {
        "auto_fill": (29, 7),
        "category_dropdown": (84, 7),
        "category_equipment": (28, 41),
        "category_material": (28, 69),
        "combine": (152, 7),
        "return_items": (4, -45),
    }
    sample_size = combine_button_sample_size(scale)
    points = {
        key: Point(anchor.x + round(offset_x * scale), anchor.y + round(offset_y * scale))
        for key, (offset_x, offset_y) in offsets.items()
    }
    if client_size is not None:
        width, height = client_size
        if any(not (0 <= point.x < width and 0 <= point.y < height) for point in points.values()):
            raise SynthesisLayoutError("合成控件超出游戏客户区，请减小游戏界面比例后重新识别")
        center = points["combine"]
        left, top = center.x - sample_size // 2, center.y - sample_size // 2
        if left < 0 or top < 0 or left + sample_size > width or top + sample_size > height:
            raise SynthesisLayoutError("合成按钮显示不完整，请减小游戏界面比例后重新识别")
    return points
