"""视频合成：片段渲染 → 拼接 → 字幕烧录 → 音画合流。

所有片段用统一参数（分辨率/帧率/编码）生成，保证 concat demuxer 可以无损拼接。
最终字幕在拼接后一次性烧录，避免每个片段重复编码字幕层。
"""

from __future__ import annotations

from pathlib import Path

from .profile import Profile
from .script import Group
from .util import ffmpeg, run


def _kenburns_filter(zoom_from: float, zoom_to: float, frames: int, fps: int) -> str:
    """在放大 2 倍的源上做缓慢推近，保证 zoom>1 时画质不打折。"""
    return (
        "scale=iw*2:ih*2,"
        f"zoompan=z='{zoom_from}+({zoom_to}-{zoom_from})*on/{frames}'"
        ":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d={frames}:s={{width}}x{{height}}:fps={fps}"
    )


def render_clip(
    asset_cfg: dict,
    group: Group,
    profile: Profile,
    out_path: Path,
) -> Path:
    """渲染一个组的视频片段：静态图 + 动效 → 定长无声视频。"""
    width, height, fps = profile.width, profile.height, profile.fps
    frames = max(1, round(group.dur * fps))
    src = str(profile.resolve(asset_cfg["file"]))
    fit = asset_cfg.get("fit", "cover")
    motion = asset_cfg.get("motion", {})
    bg_motion = asset_cfg.get("background_motion", {})

    if fit == "contain":
        # 前景完整显示（零信息损失），模糊放大背景兜底；背景可单独做缓慢推近
        blur = int(asset_cfg.get("background_blur", 24))
        bg_filters = [
            f"scale={width}:{height}:force_original_aspect_ratio=increase",
            f"crop={width}:{height}",
            f"boxblur={blur}:2",
        ]
        if bg_motion.get("type") == "kenburns":
            zf, zt = float(bg_motion.get("from", 1.0)), float(bg_motion.get("to", 1.08))
            bg_filters.append(
                f"zoompan=z='{zf}+({zt}-{zf})*on/{frames}'"
                f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
                f":d={frames}:s={width}x{height}:fps={fps}"
            )
        parts = [
            "[1:v]" + ",".join(bg_filters) + "[bg];",
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease[fg];",
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p[v]",
        ]
        filter_complex = "".join(parts)
        cmd = [
            ffmpeg(), "-y",
            "-loop", "1", "-framerate", str(fps), "-i", src,
            "-loop", "1", "-framerate", str(fps), "-i", src,
            "-filter_complex", filter_complex,
            "-map", "[v]", "-frames:v", str(frames),
        ]
    else:
        # cover：满屏 + Ken Burns 推近
        zf = float(motion.get("from", 1.0))
        zt = float(motion.get("to", 1.06))
        zp = _kenburns_filter(zf, zt, frames, fps).format(width=width, height=height)
        filter_complex = f"[0:v]{zp},format=yuv420p[v]"
        cmd = [
            ffmpeg(), "-y",
            "-loop", "1", "-framerate", str(fps), "-i", src,
            "-filter_complex", filter_complex,
            "-map", "[v]", "-frames:v", str(frames),
        ]

    o = profile.output
    cmd += [
        "-c:v", o["video_codec"], "-preset", o["preset"],
        "-crf", str(min(int(o["crf"]), 16)),  # 中间片用更高质量，字幕阶段还要再编码一次
        "-pix_fmt", "yuv420p",
        str(out_path),
    ]
    run(cmd, desc=f"渲染片段 [{group.index}] {asset_cfg['name']}")
    return out_path


def concat_clips(clips: list[Path], out_path: Path, workdir: Path) -> Path:
    """无损拼接全部片段（参数一致时 -c copy 可用）。"""
    listing = workdir / "clips.txt"
    listing.write_text(
        "".join(f"file '{c.resolve()}'\n" for c in clips), encoding="utf-8"
    )
    run(
        [
            ffmpeg(), "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(listing),
            "-c", "copy",
            str(out_path),
        ],
        desc="拼接片段",
    )
    return out_path


def assemble(
    video: Path,
    audio: Path,
    subtitles: list[tuple[Path, float, float]],
    profile: Profile,
    out_path: Path,
) -> Path:
    """字幕烧录 + 音画合流，一步出片。"""
    width, height = profile.width, profile.height
    inputs = ["-i", str(video)]
    for sub, _, _ in subtitles:
        inputs += ["-i", str(sub)]
    inputs += ["-i", str(audio)]

    chain = []
    prev = "0:v"
    for i, (_, start, end) in enumerate(subtitles):
        label = f"v{i}"
        # 字幕比语音多停留 0.15s，避免句间闪烁
        chain.append(
            f"[{prev}][{i + 1}:v]overlay=0:0:"
            f"enable='between(t,{start:.3f},{end + 0.15:.3f})'[{label}]"
        )
        prev = label
    chain.append(f"[{prev}]format=yuv420p[vout]")
    filter_complex = ";".join(chain)

    o = profile.output
    cmd = [
        ffmpeg(), "-y", *inputs,
        "-filter_complex", filter_complex,
        "-map", "[vout]", "-map", f"{len(subtitles) + 1}:a",
        "-c:v", o["video_codec"], "-preset", o["preset"], "-crf", str(o["crf"]),
        "-c:a", o["audio_codec"], "-b:a", o["audio_bitrate"],
        "-shortest",
        str(out_path),
    ]
    run(cmd, desc="烧录字幕并合流")
    return out_path
