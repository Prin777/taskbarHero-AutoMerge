from __future__ import annotations

import logging
from dataclasses import dataclass

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
STRONG_MATCH_RATIO = 0.08
PREFERRED_SCALE_EARLY_ACCEPT_RATIO = 0.205
MIN_BUTTON_BROWN_RATIO = 0.15
SUPPORTED_SCALES = (1.0, 1.25, 1.5, 1.75, 2.0, 0.8, 0.75)
MIN_SUPPORTED_SCALE = min(SUPPORTED_SCALES)
CUBE_OUTLINE_MIN_PIXELS = round(200 * MIN_SUPPORTED_SCALE ** 2)
CUBE_OUTLINE_MIN_SIZE = round(40 * MIN_SUPPORTED_SCALE)
CUBE_SOLID_MIN_PIXELS = round(180 * MIN_SUPPORTED_SCALE ** 2)
CUBE_SOLID_MIN_SIZE = round(24 * MIN_SUPPORTED_SCALE)
CUBE_COMPONENT_LOG_MIN_PIXELS = 20


LOG = logging.getLogger("tbh_automation.autodetect")


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


def _bgra_to_gray(image: bytes) -> bytes:
    gray = bytearray(len(image) // 4)
    target = 0
    for source in range(0, len(image), 4):
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


def _best_match_at_scale(
    gray: bytes,
    width: int,
    height: int,
    scale: float,
    brown_mask: bytes | None = None,
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
    for y in range(max_y + 1):
        for x in range(max_x + 1):
            if brown_mask is not None:
                center_x = x + round(29 * scale)
                center_y = y + round(7 * scale)
                if not (0 <= center_x < width and 0 <= center_y < height):
                    continue
                brown_samples = 0
                for delta_x, delta_y in (
                    (0, 0),
                    (-12, -5), (12, -5),
                    (-12, 5), (12, 5),
                    (-22, 0), (22, 0),
                ):
                    sample_x = center_x + round(delta_x * scale)
                    sample_y = center_y + round(delta_y * scale)
                    if (
                        0 <= sample_x < width
                        and 0 <= sample_y < height
                        and brown_mask[sample_y * width + sample_x]
                    ):
                        brown_samples += 1
                if brown_samples < 2:
                    continue
            mismatches = 0
            for sample_x, sample_y, expected_on in prefilter:
                actual_on = gray[(y + sample_y) * width + x + sample_x] >= GRAY_THRESHOLD
                mismatches += actual_on != expected_on
                if mismatches > MAX_PREFILTER_MISMATCHES:
                    break
            if mismatches > MAX_PREFILTER_MISMATCHES:
                continue

            mismatches = 0
            for sample_x, sample_y, expected_on in samples:
                actual_on = gray[(y + sample_y) * width + x + sample_x] >= GRAY_THRESHOLD
                mismatches += actual_on != expected_on
            ratio = mismatches / len(samples)
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
    ordered = sorted(candidates, key=lambda candidate: candidate.center.x)
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


def find_cube_icon(image_bgra: bytes, width: int, height: int) -> CubeIconDetectionResult:
    """识别英雄背包底部唯一的大块黄色魔方图标。"""
    if len(image_bgra) != width * height * 4:
        raise ValueError("魔方入口截图尺寸不正确")
    if width < 250 or height < 250:
        raise ValueError("游戏窗口太小，无法识别魔方入口")

    # 魔方入口位于英雄菜单下半部分。先提取高饱和黄色像素，再用连通区域
    # 排除金币文字、细边框和其他较小图标。
    remaining: set[tuple[int, int]] = set()
    for y in range(round(height * 0.45), height):
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
        seed = remaining.pop()
        stack = [seed]
        count = 1
        left = right = seed[0]
        top = bottom = seed[1]
        while stack:
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
            and CUBE_OUTLINE_MIN_SIZE <= component_width <= 120
            and CUBE_OUTLINE_MIN_SIZE <= component_height <= 120
            and 0.65 <= aspect <= 1.4
            and density <= 0.24
        )
        solid_style = (
            count >= CUBE_SOLID_MIN_PIXELS
            and CUBE_SOLID_MIN_SIZE <= component_width <= 80
            and CUBE_SOLID_MIN_SIZE <= component_height <= 80
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
        return candidates[0]
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
    raise ValueError("识别到多个相似的黄色图标，无法安全确定魔方入口")


def find_auto_fill_anchor(
    image_bgra: bytes,
    width: int,
    height: int,
    preferred_scale: float | None = None,
    require_button_color: bool = False,
) -> AutoDetectionResult:
    """在前台窗口截图中以常见界面缩放比例寻找“自动填充”文字。"""
    if len(image_bgra) != width * height * 4:
        raise ValueError("自动识别截图尺寸不正确")
    if width < round(MASK_WIDTH * min(SUPPORTED_SCALES)) or height < round(MASK_HEIGHT * min(SUPPORTED_SCALES)):
        raise ValueError("前台窗口太小，未找到魔方面板")

    gray = _bgra_to_gray(image_bgra)
    brown_mask: bytes | None = None
    if require_button_color:
        mask = bytearray(width * height)
        target = 0
        for source in range(0, len(image_bgra), 4):
            blue, green, red = image_bgra[source : source + 3]
            mask[target] = int(
                red >= 55 and red >= green + 12 and green >= blue + 4
            )
            target += 1
        brown_mask = bytes(mask)
    best_point: Point | None = None
    best_ratio = 1.0
    best_scale = 1.0
    scales = SUPPORTED_SCALES
    if preferred_scale is not None:
        # DPI 感知后截图为物理像素，优先搜索当前显示器的缩放比例。匹配足够
        # 强时可立即结束；其他比例仍保留为游戏自身 UI 缩放的回退。
        scales = tuple(sorted(SUPPORTED_SCALES, key=lambda value: abs(value - preferred_scale)))
    for index, scale in enumerate(scales):
        point, ratio = _best_match_at_scale(
            gray,
            width,
            height,
            scale,
            brown_mask,
        )
        if point is not None and ratio < best_ratio:
            best_point = point
            best_ratio = ratio
            best_scale = scale
        if (
            index == 0
            and preferred_scale is not None
            and point is not None
            and ratio <= PREFERRED_SCALE_EARLY_ACCEPT_RATIO
            and (
                not require_button_color
                or _button_brown_ratio(image_bgra, width, height, point, scale)
                >= MIN_BUTTON_BROWN_RATIO
            )
        ):
            return AutoDetectionResult(anchor=point, mismatch_ratio=ratio, scale=scale)
        if best_ratio <= STRONG_MATCH_RATIO:
            break

    if best_point is None or best_ratio > MAX_MISMATCH_RATIO:
        raise ValueError("未能可靠识别“自动填充”按钮，请使用手动校准")
    if (
        require_button_color
        and _button_brown_ratio(image_bgra, width, height, best_point, best_scale)
        < MIN_BUTTON_BROWN_RATIO
    ):
        raise ValueError("识别到相似文字，但未检测到棕色“自动填充”按钮，请使用手动校准")
    return AutoDetectionResult(anchor=best_point, mismatch_ratio=best_ratio, scale=best_scale)


def layout_points_from_anchor(anchor: Point, scale: float = 1.0) -> dict[str, Point]:
    """从文字锚点推导当前固定魔方面板中的所有操作位置。"""
    offsets = {
        "auto_fill": (29, 7),
        "category_dropdown": (84, 7),
        "category_equipment": (28, 41),
        "category_material": (28, 69),
        "combine": (152, 7),
        "return_items": (4, -45),
    }
    return {
        key: Point(anchor.x + round(offset_x * scale), anchor.y + round(offset_y * scale))
        for key, (offset_x, offset_y) in offsets.items()
    }
