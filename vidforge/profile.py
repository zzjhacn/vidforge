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
        # scheme：TTS 方案名（按名字注册，见 vidforge/tts.py 的 REGISTRY）。
        # 可选：edge（默认，微软 Edge TTS）| openai（OpenAI 兼容端点）| bailian（阿里云百炼）
        "scheme": "edge",
        "voice": "zh-CN-YunxiNeural",
        "rate": "+10%",
        "volume": "+0%",
        "silence_between_ms": 200,
        "tail_padding_ms": 400,
        # 各方案的专属参数（仅当选中该方案时读取；缺失则用下方默认值）
        "openai": {
            "endpoint": "",          # 必填，例如 http://127.0.0.1:5000/v1/audio/speech
            "key": "",               # 本地服务通常留空
            "model": "local",        # 占位，多数本地后端忽略
            "format": "wav",
            "concurrency": 4,        # 云端端点可并行
            "timeout": 30,
            "no_verify": False,      # 本机缺 CA 根证书时再临时置 true
        },
        "bailian": {
            "endpoint": "",          # 必填，百炼完整路径
            "key": "",               # Bearer token（DashScope API Key）
            "model": "qwen-audio-3.0-tts-flash",
            "sample_rate": 24000,
            "format": "wav",
            "timeout": 30,
            "no_verify": False,      # 本机缺 CA 根证书时再临时置 true
        },
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
