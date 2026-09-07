"""字幕渲染：Pillow 逐句绘制透明 PNG，合成阶段用 overlay 烧录。

为什么不用 ffmpeg 的 subtitles/drawtext 滤镜：本机的 ffmpeg 未编译
libass / libfreetype（drawtext、subtitles 滤镜均不存在）。Pillow 方案
反而更可控——中文按像素宽度断行、圆角底框、描边文字都能精确控制。
"""

from __future__ import annotations

import glob
import warnings
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# 各平台常见的中文字体（路径, 字体索引）。按优先级排列，取第一个存在的。
# 跨平台关键：不要在场景 YAML 里写死绝对路径，否则换系统会直接崩。
_FONT_CANDIDATES: list[tuple[str, int]] = [
    # macOS
    ("/System/Library/Fonts/Hiragino Sans GB.ttc", 0),
    ("/System/Library/Fonts/STHeiti Medium.ttc", 0),
    ("/System/Library/Fonts/PingFang.ttc", 0),
    ("/Library/Fonts/Arial Unicode.ttf", 0),
    # Linux（Debian/Ubuntu/CentOS 常见位置）
    ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 0),
    ("/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf", 0),
    ("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", 0),
    ("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc", 0),
    ("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", 0),
    # Windows
    ("C:/Windows/Fonts/msyh.ttc", 0),
    ("C:/Windows/Fonts/simhei.ttf", 0),
]

# 上面的路径没命中时，再用 glob 兜底扫一遍（发行版目录结构差异较大）
_FONT_GLOBS: list[str] = [
    "/usr/share/fonts/**/*CJK*.ttc",
    "/usr/share/fonts/**/*CJK*.otf",
    "/usr/share/fonts/**/wqy*.ttc",
    "/usr/local/share/fonts/**/*CJK*",
    str(Path.home() / ".fonts" / "**" / "*CJK*"),
]


def _resolve_font(cfg: dict, size: int) -> ImageFont.FreeTypeFont:
    """解析字幕字体，三级 fallback：

    1. YAML 显式指定的 font（存在才用，否则告警后继续探测）
    2. 按平台探测系统中文字体（macOS / Linux / Windows）
    3. PIL 默认字体 + 明确告警（中文会显示为方块，但不崩）

    这样即使换到没有中文字体的环境，也只是字幕难看，而不是整个流程报错退出。
    """
    explicit = cfg.get("font")
    if explicit:
        p = Path(str(explicit)).expanduser()
        if p.exists():
            return ImageFont.truetype(
                str(p), size, index=int(cfg.get("font_index", 0) or 0)
            )
        warnings.warn(
            f"字幕：配置里的字体不存在，已回退为自动探测（{p}）",
            stacklevel=2,
        )

    for path, index in _FONT_CANDIDATES:
        if Path(path).exists():
            return ImageFont.truetype(path, size, index=index)

    for pattern in _FONT_GLOBS:
        for hit in sorted(glob.glob(pattern)):
            try:
                return ImageFont.truetype(hit, size)
            except OSError:
                continue

    warnings.warn(
        "字幕：未找到中文字体，中文可能显示为方块。"
        "请安装 Noto Sans CJK（Linux: apt install fonts-noto-cjk），"
        "或在场景 YAML 的 subtitle.font 显式指定字体文件路径。",
        stacklevel=2,
    )
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def _hex_to_rgba(color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    color = color.lstrip("#")
    r, g, b = int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)
    return (r, g, b, alpha)


def _wrap_by_width(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """按像素宽度断行（中英文混排时比按字数断行更均匀）。"""
    lines: list[str] = []
    current = ""
    for ch in text:
        if current and font.getlength(current + ch) > max_width:
            lines.append(current)
            current = ch
        else:
            current += ch
    if current:
        lines.append(current)
    return lines


def render_subtitle(
    text: str,
    cfg: dict,
    width: int,
    height: int,
    out_path: Path,
) -> Path:
    """把一句字幕渲染成与画布同尺寸的透明 PNG。"""
    font = _resolve_font(cfg, int(cfg["size"]))
    side_margin = int(width * 0.08)
    max_text_width = width - side_margin * 2

    lines = _wrap_by_width(text, font, max_text_width)
    size = int(cfg["size"])
    line_height = int(size * float(cfg.get("line_height", 1.35)))
    block_height = line_height * len(lines)

    padding = int(cfg.get("box_padding", 26))
    box_height = block_height + padding * 2
    box_bottom = int(cfg.get("bottom_margin", 300))
    box_top = height - box_bottom - box_height

    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if cfg.get("box", True):
        widest = max(font.getlength(line) for line in lines)
        box_width = int(widest) + padding * 2
        box_left = (width - box_width) // 2
        radius = int(cfg.get("box_radius", 22))
        rgba = _hex_to_rgba(cfg.get("box_color", "#000000"), int(cfg.get("box_alpha", 96)))
        draw.rounded_rectangle(
            [box_left, box_top, box_left + box_width, box_top + box_height],
            radius=radius,
            fill=rgba,
        )

    stroke_w = int(cfg.get("stroke_width", 5))
    fill = _hex_to_rgba(cfg.get("color", "#FFFFFF"))
    stroke_fill = _hex_to_rgba(cfg.get("stroke_color", "#000000"))

    y = box_top + padding
    for line in lines:
        line_w = font.getlength(line)
        x = (width - line_w) / 2
        draw.text(
            (x, y),
            line,
            font=font,
            fill=fill,
            stroke_width=stroke_w,
            stroke_fill=stroke_fill,
        )
        y += line_height

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(str(out_path))
    return out_path
