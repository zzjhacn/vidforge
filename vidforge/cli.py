"""命令行编排：读配置 → 切句 → 绑定 → TTS → 时间轴 → 渲染 → 成片。"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import compose, script as script_mod, subtitle, timeline, tts
from .profile import Profile
from .util import build_audio_track, image_size, to_wav


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vidforge",
        description="模板化口播视频生成引擎：固定卡片 + 固定文案格式 → 快速成片",
    )
    parser.add_argument("--config", "-c", required=True, help="场景配置 YAML 路径")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只打印切句、绑定与时间轴预览，不合成视频",
    )
    args = parser.parse_args(argv)

    config_path = Path(args.config).expanduser().resolve()
    profile = Profile.load(config_path)
    workdir = config_path.parent.parent / "work"
    outdir = config_path.parent.parent / "output"
    workdir.mkdir(parents=True, exist_ok=True)
    outdir.mkdir(parents=True, exist_ok=True)

    # ── 1. 文案 ────────────────────────────────────────────────
    script_path = profile.resolve(profile.script_cfg["source"])
    text = script_path.read_text(encoding="utf-8")
    sentences = script_mod.split_sentences(
        text, max_chars=int(profile.script_cfg.get("max_sentence_chars", 26))
    )
    if not sentences:
        print("错误：文案为空", file=sys.stderr)
        return 1
    groups = script_mod.build_groups(
        sentences,
        profile.bind,
        profile.script_cfg.get("split_markers", []),
    )
    if not groups:
        print("错误：绑定规则没有匹配到任何句子", file=sys.stderr)
        return 1

    print(f"切句结果（{len(sentences)} 句，{len(groups)} 组）：")
    for g in groups:
        body = " | ".join(s.text for s in g.sentences)
        print(f"  [{g.index}] → {g.asset_name}: {body}")

    # ── 2. 语音合成（逐句，时长实测）─────────────────────────
    t = profile.tts
    print(f"\n语音合成：{t['voice']} rate={t['rate']} …")
    synthesized = tts.synthesize(
        list(sentences),
        voice=t["voice"], rate=t["rate"], volume=t["volume"],
        workdir=workdir / "tts",
    )
    by_index = {i: (p, d) for i, (p, d) in enumerate(synthesized)}
    for g in groups:
        for s in g.sentences:
            p, d = by_index[s.index]
            s.audio_path = str(p)
            s.audio_dur = d

    # ── 3. 时间轴 ─────────────────────────────────────────────
    tl = timeline.build_timeline(
        groups,
        silence_ms=int(t["silence_between_ms"]),
        tail_padding_ms=int(t["tail_padding_ms"]),
        min_group_duration=float(profile.timeline["min_group_duration"]),
    )
    print("\n时间轴预览：")
    print(tl.summary())

    if args.dry_run:
        print("\n(dry-run 结束，未生成视频)")
        return 0

    # ── 4. 校验素材并渲染片段 ─────────────────────────────────
    print("\n渲染视频片段 …")
    clips: list[Path] = []
    for g in groups:
        asset_cfg = profile.asset(g.asset_name)
        image_path = profile.resolve(asset_cfg["file"])
        if not image_path.exists():
            print(f"错误：素材不存在 {image_path}", file=sys.stderr)
            return 1
        w, h = image_size(image_path)
        fit = asset_cfg.get("fit", "cover")
        if fit == "cover" and (w, h) != (profile.width, profile.height):
            print(
                f"  警告：{image_path.name} 为 {w}x{h}，"
                f"与画布 {profile.width}x{profile.height} 不同（cover 模式将裁切填充）"
            )
        clip = workdir / f"clip_{g.index}.mp4"
        compose.render_clip(asset_cfg, g, profile, clip)
        clips.append(clip)
        print(f"  [{g.index}] {g.asset_name} → {clip.name} ({g.dur:.2f}s)")

    # ── 5. 音轨：按时间轴精确混排 ─────────────────────────────
    print("\n构建音轨 …")
    voice_wav = workdir / "voice.wav"
    wav_paths = []
    placements = []
    for g in groups:
        for s in g.sentences:
            wav = to_wav(Path(s.audio_path), workdir / f"wav_{s.index:02d}.wav")
            wav_paths.append(wav)
            placements.append((s.start, wav))
    build_audio_track(placements, tl.total, voice_wav)

    # ── 6. 拼接 + 字幕 + 合流 ─────────────────────────────────
    print("拼接片段 …")
    merged = compose.concat_clips(clips, workdir / "merged.mp4", workdir)

    subs: list[tuple[Path, float, float]] = []
    if profile.subtitle.get("enabled", True):
        print("渲染字幕 …")
        for g in groups:
            for s in g.sentences:
                sub_path = workdir / f"sub_{s.index:02d}.png"
                subtitle.render_subtitle(
                    s.text, profile.subtitle, profile.width, profile.height, sub_path
                )
                subs.append((sub_path, s.start, s.end))

    output_path = profile.resolve(profile.output["file"])
    output_path.parent.mkdir(parents=True, exist_ok=True)
    print("烧录字幕并合流 …")
    compose.assemble(merged, voice_wav, subs, profile, output_path)

    size_mb = output_path.stat().st_size / 1024 / 1024
    print(f"\n完成：{output_path}（{size_mb:.1f} MB，{tl.total:.1f} 秒）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
