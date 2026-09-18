"""场景配置（Profile）的加载与访问。

Profile 是"引擎"与"脚本"的分界线：内核代码不含任何游泳相关的逻辑，
所有场景差异（素材、绑定规则、动效、字幕样式）都写在这份 YAML 里。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


DEFAULTS: dict[str, Any] = {
    "canvas": {"width": 1080, "height": 1920, "fps": 30},
    "tts": {
        # ── 公共参数（各方案共用语义）────────────────────────
        # scheme 的默认值、voice/rate/volume 的具体取值，
        # 以及各方案的专属参数（endpoint/key/model/sample_rate/...）和
        # 可选音色列表，全部由配置层提供：
        #   vidforge/data/tts_schemes.yaml（包内出厂默认）
        #   可在 configs/tts_schemes.yaml 或 ~/.vidforge/tts_schemes.yaml 覆盖。
        # 见 vidforge/ttsconfig.py。
        #
        # 这里只保留「方案无关键」默认值。
        "silence_between_ms": 200,
        "tail_padding_ms": 400,
    },
    "timeline": {"min_group_duration": 5.5},
    "subtitle": {
        "enabled": True,
        # None = 自动探测系统中文字体（跨平台）。
        # 需要固定字体时在场景 YAML 里显式写 font 路径；找不到会自动回退。
        "font": None,
        "font_index": 0,
        "size": 58,
        "line_height": 1.35,
        "color": "#FFFFFF",
        "stroke_width": 5,
        "stroke_color": "#000000",
        "bottom_margin": 300,
        "max_chars_per_line": 15,
        "box": True,
        "box_color": "#000000",
        "box_alpha": 96,
        "box_padding": 26,
        "box_radius": 22,
    },
    "transition": {
        # 图片切换转场（烘焙进每张片段开头的 incoming 过渡）。
        # type: xfade 转场名（fade/dissolve/pixelize/wipeleft/...，见 `ffmpeg -h filter=xfade`）
        #       也可写 "random" → 每次切换随机选一种（运行时按本机 ffmpeg 探测列表取）。
        #       写 "none"/"off" 或省略 → 硬切（无转场）。
        # duration: 转场时长（秒），会被裁剪到不超过该组时长。
        "type": "fade",
        "duration": 0.5,
    },
    "output": {
        "video_codec": "libx264",
        "preset": "medium",
        "crf": 20,
        "audio_codec": "aac",
        "audio_bitrate": "192k",
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class Profile:
    def __init__(self, data: dict[str, Any], config_path: Path):
        self.raw = data
        self.config_path = config_path
        self.base_dir = config_path.parent.parent  # configs/ 的上一级 = 项目根
        merged = _deep_merge(DEFAULTS, data)
        self.name: str = data.get("name", "unnamed")
        self.canvas: dict = merged["canvas"]
        self.tts: dict = merged["tts"]
        self.timeline: dict = merged["timeline"]
        self.subtitle: dict = merged["subtitle"]
        self.transition: dict = merged["transition"]
        self.output: dict = merged["output"]
        self.script_cfg: dict = data.get("script", {})
        self.assets: list[dict] = data.get("assets", [])
        self.bind: list[dict] = data.get("bind", [])

    @classmethod
    def load(cls, path: Path) -> "Profile":
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return cls(data, Path(path))

    def resolve(self, relative: str) -> Path:
        """把配置里的路径解析为绝对路径（相对项目根目录）。"""
        p = Path(relative)
        if p.is_absolute():
            return p
        return (self.base_dir / p).resolve()

    def asset(self, name: str) -> dict:
        for a in self.assets:
            if a.get("name") == name:
                return a
        raise KeyError(f"配置中找不到素材：{name}")

    @property
    def width(self) -> int:
        return int(self.canvas["width"])

    @property
    def height(self) -> int:
        return int(self.canvas["height"])

    @property
    def fps(self) -> int:
        return int(self.canvas["fps"])
