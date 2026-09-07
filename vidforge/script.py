"""口播文案的切句与素材绑定。

切句分两级：
  1. 按句末标点（。！？）切成句子，这是字幕的最小单位；
  2. 超过 max_sentence_chars 的长句，再按逗号贪心合并成子句，
     避免一句 40 字把字幕挤成三行、也让朗读有自然停顿。

绑定规则写在 profile 的 bind 段，用 split marker 定位：
  before:N   → 第 N 个标记之前的所有句子
  through:N  → 到第 N 个标记所在句为止（含）——标记句归这一组
  after:N    → 第 N 个标记所在句之后的所有句子（不含标记句）
  from:N     → 第 N 个标记（含）之后的所有句子
  all        → 全部句子
through 与 after 互补成对，正好实现「标记句归前组、其余归后组」；
这样即使每次的开场白长短不同（1 句或 2 句），数据部分也能稳定落到第二张卡片。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

SENTENCE_ENDS = "。！？!?；;"
CLAUSE_ENDS = "，,、"


@dataclass
class Sentence:
    index: int
    text: str
    audio_path: str | None = None
    audio_dur: float = 0.0
    start: float = 0.0
    end: float = 0.0
    group: int = -1


@dataclass
class Group:
    index: int
    asset_name: str
    sentences: list[Sentence] = field(default_factory=list)
    start: float = 0.0
    dur: float = 0.0
    audio_dur: float = 0.0

    @property
    def end(self) -> float:
        return self.start + self.dur

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.sentences)


def split_sentences(text: str, max_chars: int = 26) -> list[str]:
    """把整段文案切成适合朗读与显示字幕的句子。"""
    normalized = re.sub(r"\s+", "", text or "").strip()
    if not normalized:
        return []

    raw: list[str] = []
    buf = ""
    for ch in normalized:
        buf += ch
        if ch in SENTENCE_ENDS:
            raw.append(buf)
            buf = ""
    if buf:
        raw.append(buf)

    # 长句按逗号二次切分
    sentences: list[str] = []
    for sent in raw:
        if len(sent) <= max_chars:
            sentences.append(sent)
            continue
        clauses: list[str] = []
        cb = ""
        for ch in sent:
            cb += ch
            if ch in CLAUSE_ENDS or ch in SENTENCE_ENDS:
                clauses.append(cb)
                cb = ""
        if cb:
            clauses.append(cb)

        # 贪心合并短子句，直到接近上限
        merged = ""
        for clause in clauses:
            if merged and len(merged) + len(clause) > max_chars:
                sentences.append(merged)
                merged = clause
            else:
                merged += clause
        if merged:
            sentences.append(merged)

    return [s for s in sentences if s.strip()]


def locate_markers(sentences: list[str], markers: list[str]) -> list[int]:
    """返回每个 marker 首次出现的句子索引；未命中为 -1。"""
    result = []
    for marker in markers or []:
        key = re.sub(r"\s+", "", marker)
        hit = -1
        for i, sent in enumerate(sentences):
            if key and key in re.sub(r"\s+", "", sent):
                hit = i
                break
        result.append(hit)
    return result


def _parse_range(rule: str, marker_indices: list[int], total: int) -> tuple[int, int]:
    """把 bind 规则解析为句子的 [start, end) 区间。"""
    rule = (rule or "all").strip()
    if rule == "all":
        return 0, total

    kind, _, arg = rule.partition(":")
    kind = kind.strip().lower()
    try:
        idx = int(arg)
    except ValueError:
        idx = 0
    anchor = marker_indices[idx] if 0 <= idx < len(marker_indices) else -1

    if kind == "before":
        if anchor < 0:
            return 0, 0
        return 0, anchor
    if kind == "through":
        # 标记句归本组；标记未命中时退化为全量（后续组自然为空）
        if anchor < 0:
            return 0, total
        return 0, anchor + 1
    if kind == "after":
        # 与 through 互补：标记句的下一句开始
        if anchor < 0:
            return total, total
        return anchor + 1, total
    if kind == "from":
        if anchor < 0:
            return 0, total
        return anchor, total
    raise ValueError(f"无法解析的绑定规则：{rule}（支持 all/before/through/after/from:N）")


def build_groups(
    sentences: list[str],
    bind_rules: list[dict],
    markers: list[str],
) -> list[Group]:
    """把句子按绑定规则分组，每组对应一张素材图。"""
    total = len(sentences)
    if total == 0:
        return []

    marker_indices = locate_markers(sentences, markers)
    groups: list[Group] = []
    cursor = 0

    for gi, rule in enumerate(bind_rules):
        spec = rule.get("range", "all")
        start, end = _parse_range(spec, marker_indices, total)
        # 区间不得越界、不得回退
        start = max(start, cursor)
        end = max(end, start)
        end = min(end, total)
        chunk = [
            Sentence(index=i, text=sentences[i]) for i in range(start, end)
        ]
        groups.append(
            Group(index=gi, asset_name=rule.get("asset", f"asset{gi}"), sentences=chunk)
        )
        cursor = end

    # 兜底：marker 未命中导致句子没分完时，把剩余句子并入最后一组
    if cursor < total and groups:
        groups[-1].sentences.extend(
            Sentence(index=i, text=sentences[i]) for i in range(cursor, total)
        )
    for g in groups:
        for s in g.sentences:
            s.group = g.index
    return [g for g in groups if g.sentences]
