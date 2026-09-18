"""视频生成编排（命令行与 Web 页面共用）。

**为什么单独抽出来**：编排逻辑原本写在 cli.py 的 main() 里，和 argparse、print 混在一起。
Web 页面要复用同一套逻辑，就必须把它抽成纯函数——调用方只负责"怎么展示进度"。

好处是命令行和页面永远走同一条代码路径，不会出现「命令行能出片、页面不行」的偏差。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import compose, script as script_mod, subtitle, timeline, tts
from .profile import Profile
from .util import build_audio_track, image_size, resolve_transition, to_wav


class BuildError(RuntimeError):
    """可预期的构建失败（文案为空、素材缺失、绑定无匹配等）。"""


@dataclass
class BuildResult:
    output_path: Path | None = None      # dry-run 时为 None
    total_duration: float = 0.0
    size_mb: float = 0.0
    sentences: list = field(default_factory=list)
    groups: list = field(default_factory=list)
    timeline: Any = None
    dry_run: bool = False


# progress(message, percent) —— percent 可为 None，表示无明确进度
ProgressFn = Callable[[str, float | None], None]


def build(
    config_path: Path,
    dry_run: bool = False,
    progress: ProgressFn | None = None,
) -> BuildResult:
    """按场景配置生成视频。

    Args:
        config_path: 场景 YAML 路径（须位于 <项目根>/configs/ 下）
        dry_run: 只做切句/合成/时间轴，不渲染成片
        progress: 进度回调，签名 (message: str, percent: float | None)

    Raises:
        BuildError: 可预期的失败（文案为空、素材不存在等）
    """
    def emit(msg: str, pct: float | None = None) -> None:
        if progress:
            progress(msg, pct)

    profile = Profile.load(config_path)
    workdir = config_path.parent.parent / "work"
    outdir = config_path.parent.parent / "output"
    workdir.mkdir(parents=True, exist_ok=True)
    outdir.mkdir(parents=True, exist_ok=True)

    # ── 1. 文案切分与绑定 ────────────────────────────────────
    script_path = profile.resolve(profile.script_cfg["source"])
    text = script_path.read_text(encoding="utf-8")
    sentences = script_mod.split_sentences(
        text, max_chars=int(profile.script_cfg.get("max_sentence_chars", 26))
    )
    if not sentences:
        raise BuildError("文案为空")
    groups = script_mod.build_groups(
        sentences,
        profile.bind,
        profile.script_cfg.get("split_markers", []),
    )
    if not groups:
        raise BuildError("绑定规则没有匹配到任何句子")

    emit(f"切句结果（{len(sentences)} 句，{len(groups)} 组）：", 5)
    for g in groups:
        body = " | ".join(s.text for s in g.sentences)
        emit(f"  [{g.index}] → {g.asset_name}: {body}")

    # ── 2. 语音合成（逐句，时长实测）─────────────────────────
    # 用 tts.resolve 把缺省的 voice/rate/volume/方案子块从配置层补齐，
    # 日志里看到的就是「真正会出片」的方案与音色
    t = tts.resolve(profile.tts)
    scheme = t.get("scheme") or t.get("provider") or "edge"
    emit(f"\n语音合成：scheme={scheme} voice={t.get('voice')} …", 10)
    synthesized = tts.synthesize(
        list(sentences),
        t,
        workdir=workdir / "tts",
    )
    by_index = {i: (p, d) for i, (p, d) in enumerate(synthesized)}
    for g in groups:
        for s in g.sentences:
            p, d = by_index[s.index]
            s.audio_path = str(p)
            s.audio_dur = d
    emit("语音合成完成", 32)

    # ── 3. 时间轴 ────────────────────────────────────────────
    tl = timeline.build_timeline(
        groups,
        silence_ms=int(t["silence_between_ms"]),
        tail_padding_ms=int(t["tail_padding_ms"]),
        min_group_duration=float(profile.timeline["min_group_duration"]),
    )
    emit("\n时间轴预览：", 36)
    emit(tl.summary())

    if dry_run:
        emit("\n(dry-run 结束，未生成视频)", 100)
        return BuildResult(
            sentences=sentences, groups=groups, timeline=tl, dry_run=True,
            total_duration=tl.total,
        )

    # ── 4. 校验素材并渲染片段 ────────────────────────────────
    emit("\n渲染视频片段 …", 40)
    clips: list[Path] = []
    for i, g in enumerate(groups):
        asset_cfg = profile.asset(g.asset_name)
        prev_asset_cfg = profile.asset(groups[i - 1].asset_name) if i > 0 else None
        image_path = profile.resolve(asset_cfg["file"])
        if not image_path.exists():
            raise BuildError(f"素材不存在 {image_path}")
        w, h = image_size(image_path)
        fit = asset_cfg.get("fit", "cover")
        if fit == "cover" and (w, h) != (profile.width, profile.height):
            emit(
                f"  警告：{image_path.name} 为 {w}x{h}，"
                f"与画布 {profile.width}x{profile.height} 不同（cover 模式将裁切填充）"
            )
        # 转场：资产级 transition 优先，回退全局 profile.transition；random 已展开为具体转场名。
        # 首段无 prev → 强制硬切（无 incoming 转场）。
        tr = resolve_transition(asset_cfg.get("transition") or profile.transition)
        prev_for_render = prev_asset_cfg if tr else None
        clip = workdir / f"clip_{g.index}.mp4"
        compose.render_clip(
            asset_cfg, g, profile, clip,
            prev_asset_cfg=prev_for_render,
            transition=tr,
        )
        clips.append(clip)
        tag = f"转场={tr['type']}" if prev_for_render else "硬切"
        emit(f"  [{g.index}] {g.asset_name} → {clip.name} ({g.dur:.2f}s, {tag})",
             40 + int(28 * (i + 1) / len(groups)))

    # ── 5. 音轨：按时间轴精确混排 ────────────────────────────
    emit("\n构建音轨 …", 70)
    voice_wav = workdir / "voice.wav"
    placements = []
    for g in groups:
        for s in g.sentences:
            wav = to_wav(Path(s.audio_path), workdir / f"wav_{s.index:02d}.wav")
            placements.append((s.start, wav))
    build_audio_track(placements, tl.total, voice_wav)

    # ── 6. 拼接 + 字幕 + 合流 ────────────────────────────────
    emit("拼接片段 …", 76)
    merged = compose.concat_clips(clips, workdir / "merged.mp4", workdir)

    subs: list[tuple[Path, float, float]] = []
    if profile.subtitle.get("enabled", True):
        emit("渲染字幕 …", 82)
        for g in groups:
            for s in g.sentences:
                sub_path = workdir / f"sub_{s.index:02d}.png"
                subtitle.render_subtitle(
                    s.text, profile.subtitle, profile.width, profile.height, sub_path
                )
                subs.append((sub_path, s.start, s.end))

    output_path = profile.resolve(profile.output["file"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    emit("烧录字幕并合流 …", 90)
    compose.assemble(merged, voice_wav, subs, profile, output_path)

    size_mb = output_path.stat().st_size / 1024 / 1024
    emit(f"\n完成：{output_path}（{size_mb:.1f} MB，{tl.total:.1f} 秒）", 100)
    return BuildResult(
        output_path=output_path,
        total_duration=tl.total,
        size_mb=size_mb,
        sentences=sentences,
        groups=groups,
        timeline=tl,
    )
