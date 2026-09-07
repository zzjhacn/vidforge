"""时间轴计算：把「每句实测时长」累加成「每张图的停留区间」。

核心原则——画面的停留时长由音频说了算，不是预设的秒数：

    组时长 = Σ(句时长) + 句间静音 + 尾部留白
    组时长 = max(组时长, min_group_duration)   ← 短句防闪切兜底

因为组时长可能被 min_group_duration 拉长，下一组的音频从「上一组画面结束」开始，
这样画面与声音始终对齐，拉长的部分就是自然的静默停顿。
"""

from __future__ import annotations

from dataclasses import dataclass

from .script import Group


@dataclass
class Timeline:
    groups: list[Group]
    total: float

    def summary(self) -> str:
        lines = []
        for g in self.groups:
            lines.append(
                f"  [{g.index}] {g.asset_name:<12} "
                f"{g.start:6.2f}s → {g.end:6.2f}s  "
                f"(时长 {g.dur:5.2f}s, 语音 {g.audio_dur:5.2f}s, "
                f"{len(g.sentences)} 句)"
            )
            for s in g.sentences:
                lines.append(
                    f"        ├ {s.start:6.2f}s → {s.end:6.2f}s  {s.text}"
                )
        lines.append(f"  总时长 {self.total:.2f}s")
        return "\n".join(lines)


def build_timeline(
    groups: list[Group],
    silence_ms: int = 200,
    tail_padding_ms: int = 400,
    min_group_duration: float = 5.5,
) -> Timeline:
    silence = silence_ms / 1000.0
    tail = tail_padding_ms / 1000.0
    cursor = 0.0

    for g in groups:
        g.start = cursor
        audio_cursor = cursor
        for s in g.sentences:
            s.start = audio_cursor
            s.end = audio_cursor + s.audio_dur
            audio_cursor = s.end + silence

        if g.sentences:
            # 末尾多算了一次句间静音，去掉它
            g.audio_dur = max(0.0, (audio_cursor - silence) - g.start)
        else:
            g.audio_dur = 0.0

        dur = g.audio_dur + tail
        if dur < min_group_duration:
            dur = min_group_duration
        g.dur = dur
        cursor = g.start + g.dur

    return Timeline(groups=groups, total=cursor)
