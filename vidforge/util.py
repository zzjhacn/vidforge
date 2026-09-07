"""通用工具：ffmpeg 定位、时长探测、命令执行、音频轨构建。

设计要点：ffmpeg 二进制可能不在 PATH（例如被 IDE 的 PATH shim 挡住），
因此这里显式列出常见安装路径做兜底探测。
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import wave
from pathlib import Path

FFMPEG_CANDIDATES = [
    "/opt/homebrew/bin/ffmpeg",
    "/usr/local/bin/ffmpeg",
    "/opt/local/bin/ffmpeg",
]
FFPROBE_CANDIDATES = [
    "/opt/homebrew/bin/ffprobe",
    "/usr/local/bin/ffprobe",
    "/opt/local/bin/ffprobe",
]


class FFmpegNotFound(RuntimeError):
    pass


def _locate(candidates: list[str], name: str) -> str:
    for c in candidates:
        if Path(c).exists():
            return c
    found = shutil.which(name)
    if found:
        return found
    raise FFmpegNotFound(
        f"找不到 {name}。已尝试：{', '.join(candidates)} 与 PATH。"
    )


_FFMPEG: str | None = None
_FFPROBE: str | None = None


def ffmpeg() -> str:
    global _FFMPEG
    if _FFMPEG is None:
        _FFMPEG = _locate(FFMPEG_CANDIDATES, "ffmpeg")
    return _FFMPEG


def ffprobe() -> str:
    global _FFPROBE
    if _FFPROBE is None:
        _FFPROBE = _locate(FFPROBE_CANDIDATES, "ffprobe")
    return _FFPROBE


def run(cmd: list[str], desc: str = "", quiet: bool = True) -> str:
    """执行命令，失败时抛出带完整 stderr 的异常。"""
    proc = subprocess.run(
        cmd, capture_output=True, text=True, errors="replace"
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-15:]
        raise RuntimeError(
            f"命令失败{f'（{desc}）' if desc else ''}: {' '.join(cmd)}\n"
            + "\n".join(tail)
        )
    return proc.stdout


def probe_duration(path: Path) -> float:
    """用 ffprobe 读取媒体真实时长（秒）。

    这是时间轴的基础——字幕与画面切换全部依赖实测值，不做字数估算。
    """
    out = run(
        [
            ffprobe(),
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(path),
        ],
        desc=f"探测时长 {path.name}",
    )
    return float(json.loads(out)["format"]["duration"])


def image_size(path: Path) -> tuple[int, int]:
    out = run(
        [
            ffprobe(),
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            str(path),
        ],
        desc=f"读取尺寸 {path.name}",
    )
    stream = json.loads(out)["streams"][0]
    return int(stream["width"]), int(stream["height"])


def to_wav(src: Path, dst: Path, sample_rate: int = 24000) -> Path:
    """统一音频参数为 s16le / stereo / 固定采样率，便于后续按样本拼接。"""
    run(
        [
            ffmpeg(), "-y", "-i", str(src),
            "-ar", str(sample_rate),
            "-ac", "2",
            "-c:a", "pcm_s16le",
            str(dst),
        ],
        desc=f"转 WAV {src.name}",
    )
    return dst


def _read_wav_samples(path: Path) -> tuple[list[int], int, int]:
    with wave.open(str(path), "rb") as w:
        channels = w.getnchannels()
        rate = w.getframerate()
        raw = w.readframes(w.getnframes())
    # s16le → 有符号 16 位小端
    import array

    samples = array.array("h")
    samples.frombytes(raw)
    if samples.itemsize != 2:
        raise RuntimeError("仅支持 16 位 PCM WAV")
    return list(samples), rate, channels


def build_audio_track(
    placements: list[tuple[float, Path]],
    total_duration: float,
    out_path: Path,
    sample_rate: int = 24000,
) -> Path:
    """把若干段音频按各自的起始时间混合到一条与视频等长的音轨上。

    placements: [(start_sec, wav_path), ...]
    用 Python 直接操作 PCM 样本，避免依赖 ffmpeg 的 adelay/amix 滤镜链，
    时间定位精确且行为可预测。
    """
    import array

    channels = 2
    total_frames = int(math.ceil(total_duration * sample_rate))
    buf = array.array("h", bytes(total_frames * channels * 2))

    for start_sec, wav_path in placements:
        samples, rate, ch = _read_wav_samples(wav_path)
        if rate != sample_rate:
            raise RuntimeError(
                f"{wav_path.name} 采样率 {rate} 与目标 {sample_rate} 不一致"
            )
        offset = int(round(start_sec * sample_rate)) * channels
        if ch == 1:
            # 单声道复制到双声道
            stereo: list[int] = []
            for s in samples:
                stereo.append(s)
                stereo.append(s)
            samples = stereo
        for i, value in enumerate(samples):
            idx = offset + i
            if idx >= len(buf):
                break
            merged = buf[idx] + value
            # 简单限幅，防止叠加溢出
            if merged > 32767:
                merged = 32767
            elif merged < -32768:
                merged = -32768
            buf[idx] = merged

    with wave.open(str(out_path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(buf.tobytes())
    return out_path
