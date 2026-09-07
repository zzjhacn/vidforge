"""语音合成：逐句 TTS + 时长实测。

两条硬规则：
  1. 逐句合成而非整段——每句单独出音频，才能把字幕精确钉在句子上；
  2. 时长必须 ffprobe 实测——数字读法（"35分36秒"要念成"三十五分三十六秒"）
     让"字数÷语速"的估算误差可达 ±15%，足以让画面切换时话还没说完。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import edge_tts

from .util import probe_duration


def _clean(value: str | None) -> str:
    return value if value else "+0%"


async def _synth_one(
    text: str,
    index: int,
    voice: str,
    rate: str,
    volume: str,
    out_path: Path,
    semaphore: asyncio.Semaphore,
) -> None:
    """单句合成，带指数退避重试。

    实测该接口存在随机失败（NoAudioReceived，与参数无关，属服务端
    频率风控），重试 + 串行是稳定通过的关键。
    """
    import random

    async with semaphore:
        last_err: Exception | None = None
        for attempt in range(4):
            try:
                communicate = edge_tts.Communicate(
                    text, voice, rate=_clean(rate), volume=_clean(volume)
                )
                await communicate.save(str(out_path))
                if out_path.exists() and out_path.stat().st_size > 0:
                    return
                last_err = RuntimeError("返回空音频")
            except Exception as err:  # noqa: BLE001
                last_err = err
            await asyncio.sleep(1.0 + attempt * 1.5 + random.random())
        raise RuntimeError(
            f"语音合成失败（已重试 4 次）：{out_path.name}"
        ) from last_err


async def _synth_all(
    texts: list[str], voice: str, rate: str, volume: str, workdir: Path
) -> list[Path]:
    workdir.mkdir(parents=True, exist_ok=True)
    # 串行合成：并发会显著提高服务端风控的拒绝率
    semaphore = asyncio.Semaphore(1)
    paths: list[Path] = []
    tasks = []
    for i, text in enumerate(texts):
        out = workdir / f"seg_{i:02d}.mp3"
        paths.append(out)
        tasks.append(
            _synth_one(text, i, voice, rate, volume, out, semaphore)
        )
    await asyncio.gather(*tasks)
    return paths


def synthesize(
    texts: list[str],
    voice: str,
    rate: str,
    volume: str,
    workdir: Path,
) -> list[tuple[Path, float]]:
    """合成全部句子，返回 [(音频路径, 实测时长), ...]。"""
    if not texts:
        return []
    paths = asyncio.run(_synth_all(texts, voice, rate, volume, workdir))
    result: list[tuple[Path, float]] = []
    for p in paths:
        if not p.exists() or p.stat().st_size == 0:
            raise RuntimeError(f"语音合成失败或结果为空：{p}")
        result.append((p, probe_duration(p)))
    return result
