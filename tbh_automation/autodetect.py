from __future__ import annotations

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


@dataclass(frozen=True)
class AutoDetectionResult:
    anchor: Point
    mismatch_ratio: float
    scale: float = 1.0

    @property
    def confidence(self) -> float:
        return 1.0 - self.mismatch_ratio


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
    best_point: Point | None = None
    best_ratio = 1.0
    best_scale = 1.0
    scales = SUPPORTED_SCALES
    if preferred_scale is not None:
        # DPI 感知后截图为物理像素，优先搜索当前显示器的缩放比例。匹配足够
        # 强时可立即结束；其他比例仍保留为游戏自身 UI 缩放的回退。
        scales = tuple(sorted(SUPPORTED_SCALES, key=lambda value: abs(value - preferred_scale)))
    for index, scale in enumerate(scales):
        point, ratio = _best_match_at_scale(gray, width, height, scale)
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
        "grid_top_left": (39, -132),
        "grid_bottom_right": (119, -52),
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
