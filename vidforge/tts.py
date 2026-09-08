"""语音合成：标准化 TTS 方案（scheme）接口。

设计约束（继承自内核的两条硬规则，任何方案都不能破）：
  1. 逐句合成而非整段——每句单独出音频，字幕才能精确钉在句子上；
  2. 时长必须 ffprobe 实测——数字读法让「字数÷语速」估算误差达 ±15%，
     画面会切早。下游 timeline / 音轨 / 字幕只读 (path, dur)，
     引擎内部怎么出声一概不关心。

标准化接口（本模块的核心）：
  * 每个 TTS 方案是一个按名字注册的类（TTSScheme），对外暴露同一契约：
        synthesize(texts, cfg, workdir) -> list[(Path, float)]
    cfg 是该方案的完整配置块（含 voice / rate / volume / 专属参数）。
  * 新增方案 = 写一个子类 + 在 REGISTRY 里登记名字，内核与 Web 零改动。
  * 不同方案的「可选音色列表」由各自在类上声明（VOICES），经 list_schemes()
    暴露给 Web，做「先选方案、再选音色」的两步选择。
  * 默认方案为 edge（向下兼容 swim.yaml 与既有行为）。

依赖约定（严格遵守项目「零额外依赖」原则）：
  * 仅用标准库做网络（urllib），不引入 requests 等第三方包。
  * edge-tts 属于「方案 1」的专属依赖，惰性导入：只用 openai / bailian
    时即便没装 edge-tts 也不会整体报错。
"""

from __future__ import annotations

import asyncio
import json
import ssl
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .util import probe_duration


class TTSError(RuntimeError):
    """TTS 合成相关的可预期失败。"""


def _clean_rate(value: str | None) -> str:
    """edge-tts 要求 rate/volume 非空，缺省补 '+0%'。"""
    return value if value else "+0%"


# ════════════════════════════════════════════════════════════
# 标准化接口
# ════════════════════════════════════════════════════════════

class TTSScheme(ABC):
    """标准化 TTS 方案接口（按名字注册）。

    子类必须实现 synthesize()，并声明以下类属性：
      name / label / voices / supports_rate / supports_volume / needs_endpoint
    """

    name: str = ""
    label: str = ""
    # (音色 id, 展示名)，作为 Web 中该方案的音色下拉列表（系统级配置）
    voices: list[tuple[str, str]] = []
    supports_rate: bool = True
    supports_volume: bool = False
    needs_endpoint: bool = False

    @abstractmethod
    def synthesize(
        self, texts: list[str], cfg: dict[str, Any], workdir: Path
    ) -> list[tuple[Path, float]]:
        """逐句合成，返回 [(音频路径, 实测时长), ...]，顺序与 texts 一致。"""


def _finalize(paths: list[Path]) -> list[tuple[Path, float]]:
    """逐文件实测时长，统一产出 (path, dur) 列表。"""
    out: list[tuple[Path, float]] = []
    for p in paths:
        if not p.exists() or p.stat().st_size == 0:
            raise RuntimeError(f"语音合成失败或结果为空：{p}")
        out.append((p, probe_duration(p)))
    return out


# ════════════════════════════════════════════════════════════
# 方案 1：edge-tts（默认，向下兼容）
# ════════════════════════════════════════════════════════════

class EdgeScheme(TTSScheme):
    name = "edge"
    label = "Edge TTS（微软，免费 · 非官方接口）"
    supports_rate = True
    supports_volume = True
    needs_endpoint = False
    # 常见中文音色（离线静态列表，避免启动时联网拉取）
    voices = [
        ("zh-CN-YunxiNeural", "云希 · 年轻男声"),
        ("zh-CN-YunjianNeural", "云健 · 男声解说"),
        ("zh-CN-YunyangNeural", "云扬 · 男声新闻"),
        ("zh-CN-YunxiaNeural", "云晓 · 男声少年"),
        ("zh-CN-XiaoxiaoNeural", "晓晓 · 女声温柔"),
        ("zh-CN-XiaoyiNeural", "晓伊 · 女声活泼"),
        ("zh-CN-XiaoshuangNeural", "晓双 · 女声少年"),
    ]

    def synthesize(self, texts, cfg, workdir):
        import random
        import edge_tts  # 惰性导入：仅方案 1 的专属依赖

        workdir.mkdir(parents=True, exist_ok=True)
        voice = cfg.get("voice") or "zh-CN-YunxiNeural"
        rate = _clean_rate(cfg.get("rate"))
        volume = _clean_rate(cfg.get("volume"))
        paths: list[Path] = []

        async def _one(idx: int, text: str, out: Path) -> None:
            # 串行合成：并发会显著提高服务端风控的拒绝率（历史踩坑结论）
            async with asyncio.Semaphore(1):
                last: Exception | None = None
                for attempt in range(4):
                    try:
                        comm = edge_tts.Communicate(
                            text, voice, rate=rate, volume=volume
                        )
                        await comm.save(str(out))
                        if out.exists() and out.stat().st_size > 0:
                            return
                        last = RuntimeError("返回空音频")
                    except Exception as err:  # noqa: BLE001
                        last = err
                    await asyncio.sleep(1.0 + attempt * 1.5 + random.random())
                raise RuntimeError(
                    f"语音合成失败（已重试 4 次）：{out.name}"
                ) from last

        async def _all() -> None:
            tasks = []
            for i, text in enumerate(texts):
                out = workdir / f"seg_{i:02d}.mp3"
                paths.append(out)
                tasks.append(_one(i, text, out))
            await asyncio.gather(*tasks)

        asyncio.run(_all())
        return _finalize(paths)


# ════════════════════════════════════════════════════════════
# HTTP 基类（方案 2 / 3 共用：OpenAI 兼容 / 阿里云百炼）
# ════════════════════════════════════════════════════════════

class HTTPTTSScheme(TTSScheme):
    """HTTP 端点方案的共用逻辑：POST 文本 → 收音频字节落盘 → 统一实测时长。

    子类只需实现 _build_payload(text, cfg) 描述请求体，并声明 audio_ext。
    通用网络层（带退避重试、可选跳过 TLS 校验）在此实现一次。
    """

    audio_ext: str = "wav"

    def _build_payload(self, text: str, cfg: dict[str, Any]) -> dict:
        raise NotImplementedError

    def _sub(self, cfg: dict[str, Any]) -> dict:
        return cfg.get(self.name, {}) or {}

    def _endpoint(self, cfg: dict[str, Any]) -> str:
        url = (self._sub(cfg).get("endpoint") or "").strip()
        if not url:
            raise TTSError(
                f"方案 {self.name} 缺少 endpoint，请在配置 tts.{self.name}.endpoint 中填写"
            )
        return url.rstrip("/")

    def _post(self, url: str, payload: dict, cfg: dict[str, Any]) -> bytes:
        sub = self._sub(cfg)
        timeout = float(sub.get("timeout", 30))
        no_verify = bool(sub.get("no_verify", False))
        key = (sub.get("key") or "").strip()

        data = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"

        ctx = ssl._create_unverified_context() if no_verify else None
        last: Exception | None = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    url, data=data, headers=headers, method="POST"
                )
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                    audio = resp.read()
                if not audio:
                    raise TTSError("端点返回空音频")
                return audio
            except urllib.error.HTTPError as e:  # 4xx/5xx：请求本身有问题，不重试
                body = e.read().decode("utf-8", "replace")
                raise TTSError(f"HTTP {e.code}: {body[:500]}") from e
            except urllib.error.URLError as e:  # 网络层：退避重试
                last = e
                if attempt < 2:
                    time.sleep(1.0 + attempt * 1.5)
                    continue
                raise TTSError(f"连接失败：{e.reason}") from e
        raise TTSError(f"连接失败：{last}") from last

    def synthesize(self, texts, cfg, workdir):
        workdir.mkdir(parents=True, exist_ok=True)
        url = self._endpoint(cfg)
        concurrency = int(self._sub(cfg).get("concurrency", 4))

        def _worker(item: tuple[int, str]) -> tuple[int, Path]:
            idx, text = item
            out = workdir / f"seg_{idx:02d}.{self.audio_ext}"
            payload = self._build_payload(text, cfg)
            out.write_bytes(self._post(url, payload, cfg))
            return idx, out

        # 本地/云端端点可并行，用有界线程池；顺序由 map 保证与 texts 一致
        results: list[tuple[int, Path]] = []
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as ex:
            for r in ex.map(_worker, list(enumerate(texts))):
                results.append(r)
        paths = [p for _, p in sorted(results)]
        return _finalize(paths)


# ════════════════════════════════════════════════════════════
# 方案 2：OpenAI 兼容端点（/v1/audio/speech）
# ════════════════════════════════════════════════════════════

class OpenAITTSScheme(HTTPTTSScheme):
    name = "openai"
    label = "OpenAI 兼容端点（/v1/audio/speech）"
    supports_rate = True          # 通过 speed 字段映射
    supports_volume = False
    needs_endpoint = True
    audio_ext = "wav"
    voices = [
        ("longshuo", "cosyvoice-v1龙硕"),
        ("longshu", "cosyvoice-v1龙书"),
        ("longxiaobai", "cosyvoice-v1龙小白"),
        ("longxiaocheng", "cosyvoice-v1龙小诚"),
        ("longxiaoxia", "cosyvoice-v1龙小夏"),
        ("longanlingxin", "qwen-audio-3.0-tts-plus龙安灵心"),
        ("longanlufeng", "qwen-audio-3.0-tts-plus龙安鲁风"),
        ("longanfengyue", "qwen-audio-3.0-tts-flash龙安风悦"),
        ("longanhuan_v3.6", "qwen-audio-3.0-tts-flash龙安欢"),
        ("longjielidou_v3.6", "qwen-audio-3.0-tts-flash龙杰力豆"),
        ("longpaopao_v3.6", "qwen-audio-3.0-tts-flash龙泡泡"),
        ("longchuanshu_v3.6", "qwen-audio-3.0-tts-flash龙川叔"),
    ]

    @staticmethod
    def _rate_to_speed(value: str | None) -> float:
        """'+10%' → 1.1，'-20%' → 0.8，'+0%'/None → 1.0。"""
        if not value:
            return 1.0
        s = value.strip()
        if s.endswith("%"):
            s = s[:-1]
        try:
            pct = float(s)
        except ValueError:
            return 1.0
        return round(max(0.25, min(4.0, 1.0 + pct / 100.0)), 3)

    def _build_payload(self, text: str, cfg: dict[str, Any]) -> dict:
        sub = self._sub(cfg)
        return {
            "model": sub.get("model") or "local",
            "input": text,
            "voice": cfg.get("voice") or "alloy",
            "response_format": sub.get("format") or "wav",
            "speed": self._rate_to_speed(cfg.get("rate")),
        }


# ════════════════════════════════════════════════════════════
# 方案 3：阿里云百炼（qwen-audio TTS / SpeechSynthesizer）
# ════════════════════════════════════════════════════════════

class BailianScheme(HTTPTTSScheme):
    name = "bailian"
    label = "阿里云百炼（qwen-audio TTS）"
    supports_rate = False
    supports_volume = False
    needs_endpoint = True
    audio_ext = "wav"
    # 注：以下为百炼平台公开示例音色（longanhuan_v3.6 由用户提供确认）；
    # 实际可用音色以控制台为准，在此处直接增删即可（单一来源）。
    voices = [
        ("longanhuan_v3.6", "龙安欢 v3.6"),
        ("longxiaochun_v3.6", "龙晓春 v3.6"),
        ("longcheng_v3.6", "龙城 v3.6"),
        ("longxiaoxia_v3.6", "龙晓霞 v3.6"),
        ("longwan_v3.6", "龙婉 v3.6"),
        ("longtu_v3.6", "龙图 v3.6"),
        ("longhua_v3.6", "龙华 v3.6"),
        ("longshu_v3.6", "龙书 v3.6"),
    ]

    def _build_payload(self, text: str, cfg: dict[str, Any]) -> dict:
        sub = self._sub(cfg)
        return {
            "model": sub.get("model") or "qwen-audio-3.0-tts-flash",
            "input": {
                "text": text,
                "voice": cfg.get("voice") or "longanhuan_v3.6",
                "format": sub.get("format") or "wav",
                "sample_rate": int(sub.get("sample_rate", 24000)),
            },
        }


# ════════════════════════════════════════════════════════════
# 注册表 & 入口
# ════════════════════════════════════════════════════════════

REGISTRY: dict[str, type[TTSScheme]] = {
    "edge": EdgeScheme,
    "openai": OpenAITTSScheme,
    "bailian": BailianScheme,
}

DEFAULT_SCHEME = "edge"


def get_scheme(cfg: dict[str, Any]) -> TTSScheme:
    """按配置中的 tts.scheme（兼容旧 provider 字段）解析出方案实例。"""
    name = cfg.get("scheme") or cfg.get("provider") or DEFAULT_SCHEME
    cls = REGISTRY.get(name)
    if cls is None:
        raise TTSError(
            f"未知的 TTS 方案：{name!r}；可选：{', '.join(REGISTRY)}"
        )
    return cls()


def list_schemes() -> list[dict[str, Any]]:
    """暴露给 Web / CLI 的方案清单（含各自音色列表）。"""
    out = []
    for cls in REGISTRY.values():
        out.append({
            "name": cls.name,
            "label": cls.label,
            "voices": [{"id": v, "label": t} for v, t in cls.voices],
            "supports_rate": cls.supports_rate,
            "supports_volume": cls.supports_volume,
            "needs_endpoint": cls.needs_endpoint,
        })
    return out


def synthesize(
    texts: list[str],
    cfg: dict[str, Any],
    workdir: Path,
) -> list[tuple[Path, float]]:
    """按配置中的 tts.scheme 选择方案并逐句合成。

    这是内核唯一的 TTS 入口：profile.tts 整块传入，由方案自行解析
    voice / rate / volume / 专属参数。返回 [(路径, 实测时长), ...]。
    """
    if not texts:
        return []
    return get_scheme(cfg).synthesize(texts, cfg, workdir)
