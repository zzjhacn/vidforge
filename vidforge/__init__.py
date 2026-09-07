"""vidforge — 模板化口播视频生成引擎。

场景无关内核 + 场景 Profile：
  script    文案切句与素材绑定（split marker 语义定位）
  tts       逐句合成 + ffprobe 实测时长
  timeline  音频时长驱动的画面停留区间（含短句防闪切兜底）
  subtitle  Pillow 渲染字幕（本机 ffmpeg 无 libass/freetype 的替代方案）
  compose   片段渲染 → 拼接 → 字幕烧录 → 音画合流
"""

__version__ = "0.1.0"
