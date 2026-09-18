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


def _render_to_canvas(
    asset_cfg: dict,
    profile: Profile,
    fps: int,
    in_label: str,
    out_label: str,
    frames: int | None = None,
    motion: bool = True,
) -> str:
    """把一张图渲染成满画布视频流（cover 满屏裁切 / contain 完整显示+模糊背景）。

    返回 filter 片段字符串，以 `format=yuv420p{out_label}` 结尾。
    motion=True 时套用 Ken Burns 推近（cover 用前景、contain 用背景层）；
    转场用的「上一张图」只需短暂出现，传 motion=False 取静态帧即可。
    """
    width, height = profile.width, profile.height
    fit = asset_cfg.get("fit", "cover")

    if fit == "contain":
        blur = int(asset_cfg.get("background_blur", 24))
        bg_filters = [
            f"scale={width}:{height}:force_original_aspect_ratio=increase",
            f"crop={width}:{height}",
            f"boxblur={blur}:2",
        ]
        if motion:
            m = asset_cfg.get("background_motion", {})
            if m.get("type") == "kenburns" and frames:
                zf, zt = float(m.get("from", 1.0)), float(m.get("to", 1.08))
                bg_filters.append(
                    _kenburns_filter(zf, zt, frames, fps).format(width=width, height=height)
                )
        fg_filters = [f"scale={width}:{height}:force_original_aspect_ratio=decrease"]
        return (
            f"{in_label}{','.join(bg_filters)}[bg];"
            f"{in_label}{','.join(fg_filters)}[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p{out_label}"
        )

    # cover：满屏 + （可选）Ken Burns 推近
    if motion:
        m = asset_cfg.get("motion", {})
        zf = float(m.get("from", 1.0))
        zt = float(m.get("to", 1.06))
        zp = _kenburns_filter(zf, zt, frames, fps).format(width=width, height=height)
        return f"{in_label}{zp},format=yuv420p{out_label}"
    return (
        f"{in_label}scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},format=yuv420p{out_label}"
    )


def render_clip(
    asset_cfg: dict,
    group: Group,
    profile: Profile,
    out_path: Path,
    prev_asset_cfg: dict | None = None,
    transition: dict | None = None,
) -> Path:
    """渲染一个组的视频片段：静态图 + 动效（+ 可选 incoming 转场）→ 定长无声视频。

    转场（transition 非空且 prev 存在）以「片段开头 T 秒 xfade(上一张, 当前张)」
    烘焙进本片段：本片段时长仍为 group.dur，**音频时间轴零改动**，concat 仍走 -c copy。
    首段无 prev → 天然硬切（满足「首硬切」）；其余段默认都做 incoming 过渡。
    """
    width, height, fps = profile.width, profile.height, profile.fps
    frames = max(1, round(group.dur * fps))
    src = str(profile.resolve(asset_cfg["file"]))

    inputs = ["-loop", "1", "-framerate", str(fps), "-i", src]
    # 当前张：完整时长 group.dur 的画布流（含 Ken Burns）
    cur_parts = _render_to_canvas(asset_cfg, profile, fps, "[0:v]", "[cur]", frames=frames, motion=True)

    if transition and prev_asset_cfg is not None:
        # 转场时长裁剪到不超过本组时长（xfade 要求 <= 两路输入时长）
        T = max(0.1, min(float(transition["duration"]), group.dur - 0.05))
        prev_src = str(profile.resolve(prev_asset_cfg["file"]))
        inputs += ["-loop", "1", "-framerate", str(fps), "-t", f"{T:.3f}", "-i", prev_src]
        # 上一张：仅转场期间短暂出现，取静态帧即可
        prev_parts = _render_to_canvas(prev_asset_cfg, profile, fps, "[1:v]", "[prev]", motion=False)
        tname = transition["type"]
        filter_complex = (
            cur_parts + ";"
            + prev_parts + ";"
            f"[prev][cur]xfade=transition={tname}:duration={T:.3f}:offset=0,format=yuv420p[v]"
        )
        map_label = "[v]"
    else:
        # 无转场：当前张直接出
        filter_complex = cur_parts
        map_label = "[cur]"

    o = profile.output
    cmd = [
        ffmpeg(), "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", map_label, "-frames:v", str(frames),
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
