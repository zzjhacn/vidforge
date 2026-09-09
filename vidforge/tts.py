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
  * 方案的「默认值」（音色列表、端点、Key、模型名、语速、采样率…）一律来自
    配置层（vidforge/data/tts_schemes.yaml，可用 configs/ 或 ~/.vidforge/ 下的
    同名文件覆盖），本模块不写死任何默认值——见 ttsconfig.py。
  * 默认方案由配置层的 default_scheme 决定（出厂为 edge，向下兼容）。

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
import uuid
from abc import ABC, abstractmethod
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import ttsconfig
from .util import probe_duration
from .wsclient import MinimalWS, OP_BINARY, OP_CLOSE, OP_TEXT, WSError


class TTSError(RuntimeError):
    """TTS 合成相关的可预期失败。"""


def _clean_rate(value: str | None) -> str:
    """edge-tts 要求 rate/volume 非空，缺省补 '+0%'。"""
    return value if value else "+0%"


# ════════════════════════════════════════════════════════════
# 标准化接口
# ════════════════════════════════════════════════════════════

# 公共参数（各方案通用语义）：值缺失时回退到方案配置的 defaults 段
PUBLIC_KEYS = ("voice", "rate", "volume")


class TTSScheme(ABC):
    """标准化 TTS 方案接口（按名字注册）。

    子类只需实现 synthesize() 并声明 name；其余展示信息与默认值
    （label / 能力开关 / 音色列表 / 默认参数）一律从配置层读取，
    类属性仅作为「配置缺失时」的最小兜底。
    """

    name: str = ""
    label: str = ""
    supports_rate: bool = True
    supports_volume: bool = False
    needs_endpoint: bool = False

    # ── 配置层访问 ────────────────────────────────────────────
    def meta(self) -> dict[str, Any]:
        """方案的展示信息与能力开关（配置优先，类属性兜底）。"""
        m = ttsconfig.scheme_meta(self.name)
        return {
            "label": m["label"] if m.get("label") else (self.label or self.name),
            "supports_rate": (
                self.supports_rate if m["supports_rate"] is None
                else bool(m["supports_rate"])
            ),
            "supports_volume": (
                self.supports_volume if m["supports_volume"] is None
                else bool(m["supports_volume"])
            ),
            "needs_endpoint": (
                self.needs_endpoint if m["needs_endpoint"] is None
                else bool(m["needs_endpoint"])
            ),
        }

    def defaults(self) -> dict[str, Any]:
        """公共参数默认值（voice / rate / volume），来自配置层 defaults 段。"""
        return ttsconfig.scheme_defaults(self.name)

    def params(self) -> dict[str, Any]:
        """方案专属参数默认值（endpoint / key / model / ...），来自配置层 params 段。"""
        return ttsconfig.scheme_params(self.name)

    def voices(self) -> list[dict[str, str | None]]:
        """可选音色列表 [{"id","label","model"}, ...]，来自配置层 voices 段。

        ``model`` 为音色绑定的模型（openai / bailian 需要，其余方案为 None）。
        """
        return ttsconfig.scheme_voices(self.name)

    # ── 取值辅助：配置层默认 + 场景/页面覆盖 ──────────────────
    def sub_params(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """方案专属参数 = 配置层 params 默认 + 上层配置覆盖（空值视为未设置）。

        额外处理「音色绑定模型」：若上层未显式指定 model，则按当前选中的
        音色从 voices 段取绑定模型，覆盖 params.model 默认值。这样只写
        voice 不写 model 也能落到正确的模型上。
        """
        merged = dict(self.params())
        override = cfg.get(self.name) or {}
        for k, v in override.items():
            if v is not None and v != "":
                merged[k] = v
        if not str(override.get("model") or "").strip():
            bound = self.voice_model(cfg)
            if bound:
                merged["model"] = bound
        return merged

    def voice_model(self, cfg: dict[str, Any] | None = None) -> str | None:
        """当前选中音色所绑定的模型名（未登记则返回 None）。"""
        voice = str(self._pub(cfg or {}, "voice") or "").strip()
        if not voice:
            return None
        for v in self.voices():
            if v["id"] == voice:
                return v["model"]
        return None

    def _pub(self, cfg: dict[str, Any], key: str, fallback: Any = None) -> Any:
        """公共参数：上层配置 → 方案配置默认 → 代码极小兜底。"""
        v = cfg.get(key)
        if v is None or v == "":
            v = self.defaults().get(key)
        if v is None or v == "":
            v = fallback
        return v

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
    """方案 1：edge-tts。默认值（音色列表 / voice / rate / volume）全在配置层。"""

    name = "edge"
    label = "Edge TTS（微软，免费 · 非官方接口）"   # 配置缺失时的兜底
    supports_rate = True
    supports_volume = True
    needs_endpoint = False

    def synthesize(self, texts, cfg, workdir):
        import random
        import edge_tts  # 惰性导入：仅方案 1 的专属依赖

        workdir.mkdir(parents=True, exist_ok=True)
        voice = self._pub(cfg, "voice", "zh-CN-YunxiNeural")
        rate = _clean_rate(self._pub(cfg, "rate"))
        volume = _clean_rate(self._pub(cfg, "volume"))
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

    def _endpoint(self, cfg: dict[str, Any]) -> str:
        url = (self.sub_params(cfg).get("endpoint") or "").strip()
        if not url:
            raise TTSError(
                f"方案 {self.name} 缺少 endpoint，请在配置 tts.{self.name}.endpoint 中填写"
                f"（或在 TTS 方案配置的 params.endpoint 中预设）"
            )
        return url.rstrip("/")

    def _post(self, url: str, payload: dict, cfg: dict[str, Any]) -> bytes:
        sub = self.sub_params(cfg)
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
        concurrency = int(self.sub_params(cfg).get("concurrency", 4))

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
    """方案 2：OpenAI 兼容端点。音色列表与 model / 端点等默认值全在配置层。"""

    name = "openai"
    label = "OpenAI 兼容端点（/v1/audio/speech）"
    supports_rate = True          # 通过 speed 字段映射
    supports_volume = False
    needs_endpoint = True
    audio_ext = "wav"

    @staticmethod
    def _rate_to_speed(
        value: str | None, lo: float = 0.25, hi: float = 4.0
    ) -> float:
        """'+10%' → 1.1，'-20%' → 0.8，'+0%'/None → 1.0。

        lo/hi 用于按各方案的取值范围钳制（OpenAI 宽、Sambert 为 0.5~2.0）。
        """
        if not value:
            return 1.0
        s = value.strip()
        if s.endswith("%"):
            s = s[:-1]
        try:
            pct = float(s)
        except ValueError:
            return 1.0
        return round(max(lo, min(hi, 1.0 + pct / 100.0)), 3)

    def _build_payload(self, text: str, cfg: dict[str, Any]) -> dict:
        sub = self.sub_params(cfg)
        return {
            "model": sub.get("model") or "local",         # 兜底：配置缺失时
            "input": text,
            "voice": self._pub(cfg, "voice", "alloy"),
            "response_format": sub.get("format") or "wav",
            "speed": self._rate_to_speed(self._pub(cfg, "rate")),
        }


# ════════════════════════════════════════════════════════════
# 方案 3：阿里云百炼（qwen-audio TTS / SpeechSynthesizer）
# ════════════════════════════════════════════════════════════

class BailianScheme(HTTPTTSScheme):
    """方案 3：阿里云百炼 HTTP TTS。音色与 model / 采样率默认值全在配置层。

    注：可用音色以百炼控制台为准，直接在配置文件的 voices 段增删即可。
    """

    name = "bailian"
    label = "阿里云百炼（qwen-audio TTS）"
    supports_rate = False
    supports_volume = False
    needs_endpoint = True
    audio_ext = "wav"

    def _build_payload(self, text: str, cfg: dict[str, Any]) -> dict:
        sub = self.sub_params(cfg)
        return {
            # 下列 or 仅为「配置缺失」时的兜底，正常取值来自配置层
            "model": sub.get("model") or "qwen-audio-3.0-tts-flash",
            "input": {
                "text": text,
                "voice": self._pub(cfg, "voice", "longanhuan_v3.6"),
                "format": sub.get("format") or "wav",
                "sample_rate": int(sub.get("sample_rate") or 24000),
            },
        }


# ════════════════════════════════════════════════════════════
# 方案 4：阿里云百炼 · Sambert 实时语音合成（WebSocket）
# ════════════════════════════════════════════════════════════

class SambertScheme(TTSScheme):
    """Sambert 实时语音合成：WebSocket 协议，音频以 binary 帧流式下发。

    与 HTTP 类方案（openai / bailian）的三点关键差异：
      1. 走 WebSocket（wss），鉴权在握手请求头；
      2. **音色即 model**（sambert-zhichu-v1 这类完整模型名），没有独立 voice 参数；
      3. 一次 run-task 发完整文本（streaming="out"，不支持流式输入），
         音频由多个 binary 帧陆续下发，收到 task-finished 才算结束。

    官方文档建议复用连接处理多个任务，因此默认一条连接串行跑一整块句子；
    若连接中途失效，自动新建连接重跑当前句（各一次重试）。
    """

    name = "sambert"
    label = "阿里云百炼 · Sambert 实时语音合成（WebSocket）"
    supports_rate = True            # parameters.rate：0.5~2.0
    supports_volume = True          # parameters.volume：0~100
    needs_endpoint = True           # 可填 WorkspaceId 专属域名，留空用配置层 default_endpoint

    # ── 参数映射（vidforge 语义 → Sambert parameters）────────
    @staticmethod
    def _volume_to_sambert(value: str | int | None) -> int:
        """edge 语义 '+0%' → 50；'-100%' → 0；'+100%' → 100。"""
        if value is None or value == "":
            return 50
        if isinstance(value, (int, float)):
            pct = float(value)
        else:
            s = str(value).strip().rstrip("%")
            try:
                pct = float(s)
            except ValueError:
                return 50
        return int(max(0, min(100, 50 + pct / 2)))

    def _build_run_task(self, text: str, cfg: dict[str, Any], task_id: str) -> dict:
        sub = self.sub_params(cfg)
        fmt = (sub.get("format") or "wav").strip()
        return {
            "header": {
                "action": "run-task",
                "task_id": task_id,
                "streaming": "out",   # Sambert 不支持流式输入，固定 out
            },
            "payload": {
                "task_group": "audio",
                "task": "tts",
                "function": "SpeechSynthesizer",
                "model": self._pub(cfg, "voice", "sambert-zhichu-v1"),  # 音色即 model
                "input": {"text": text},
                "parameters": {
                    "text_type": "PlainText",
                    "format": fmt,
                    "sample_rate": int(sub.get("sample_rate") or 16000),
                    "volume": (
                        int(sub["volume"]) if sub.get("volume") is not None
                        else self._volume_to_sambert(self._pub(cfg, "volume"))
                    ),
                    "rate": (
                        float(sub["rate"]) if sub.get("rate") is not None
                        else OpenAITTSScheme._rate_to_speed(
                            self._pub(cfg, "rate"), 0.5, 2.0
                        )
                    ),
                    "pitch": float(sub.get("pitch", 1.0)),
                    "word_timestamp_enabled": bool(
                        sub.get("word_timestamp_enabled", False)
                    ),
                },
            },
        }

    # ── 单句合成（复用给定连接）──────────────────────────────
    def _synth_one(
        self, ws: MinimalWS, text: str, cfg: dict[str, Any]
    ) -> bytes:
        if not text.strip():
            raise TTSError("文本为空，跳过合成")
        ws.send_text(
            json.dumps(self._build_run_task(text, cfg, str(uuid.uuid4())),
                       ensure_ascii=False)
        )
        audio = bytearray()
        while True:
            opcode, payload = ws.recv()
            if opcode == OP_BINARY:
                audio.extend(payload)          # 音频流：逐块累积
            elif opcode == OP_TEXT:
                evt = json.loads(payload.decode("utf-8", "replace"))
                head = evt.get("header", {}) or {}
                event = head.get("event")
                if event == "task-failed":
                    raise TTSError(
                        f"Sambert 合成失败：{head.get('error_code')} - "
                        f"{head.get('error_message')}"
                    )
                if event == "task-finished":
                    break
                # task-started / result-generated（时间戳）忽略
            elif opcode == OP_CLOSE:
                raise TTSError("服务端在任务完成前关闭了连接")
        if not audio:
            raise TTSError("未收到音频数据（空音频流）")
        return bytes(audio)

    def synthesize(self, texts, cfg, workdir):
        workdir.mkdir(parents=True, exist_ok=True)
        sub = self.sub_params(cfg)
        # endpoint 留空 → 回退到配置层的 default_endpoint；两者都没设就报错
        url = (sub.get("endpoint") or sub.get("default_endpoint") or "").strip()
        if not url:
            raise TTSError(
                "Sambert 缺少 endpoint：请在配置 tts.sambert.endpoint 中填写"
                "（或在 TTS 方案配置的 params.default_endpoint 中预设）"
            )
        key = (sub.get("key") or "").strip()
        timeout = float(sub.get("timeout", 30))
        no_verify = bool(sub.get("no_verify", False))
        concurrency = max(1, int(sub.get("concurrency", 1)))
        reuse = bool(sub.get("reuse", True))

        headers: dict[str, str] = {}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        ws_id = (sub.get("workspace") or "").strip()
        if ws_id:
            headers["X-DashScope-WorkSpace"] = ws_id

        ext_map = {"wav": "wav", "mp3": "mp3", "pcm": "pcm"}
        fmt = (sub.get("format") or "wav").strip()
        ext = ext_map.get(fmt, "wav")
        if fmt == "pcm":
            # 硬约束：时长必须 ffprobe 实测，裸 PCM 无头信息无法探测
            raise TTSError(
                "Sambert 的 pcm 格式无文件头，无法实测时长（会破坏时间轴）；"
                "请将 tts.sambert.format 设为 wav 或 mp3"
            )

        def _run_chunk(idxs: list[int]) -> list[tuple[int, Path]]:
            out: list[tuple[int, Path]] = []
            ws = None
            try:
                for i in idxs:
                    if ws is None or not reuse:
                        if ws is not None:
                            ws.close()
                        ws = MinimalWS(url, headers, timeout, no_verify)
                        ws.connect()
                    try:
                        audio = self._synth_one(ws, texts[i], cfg)
                    except (WSError, TTSError):
                        if ws is not None:
                            ws.close()
                            ws = None
                        # 连接层失效：新建连接重跑一次（仅一次）
                        ws = MinimalWS(url, headers, timeout, no_verify)
                        ws.connect()
                        audio = self._synth_one(ws, texts[i], cfg)
                    p = workdir / f"seg_{i:02d}.{ext}"
                    p.write_bytes(audio)
                    out.append((i, p))
                return out
            finally:
                if ws is not None:
                    ws.close()

        # 每块一条连接，块内串行（连接复用）；块间并行
        chunks = [list(range(i, len(texts), concurrency)) for i in range(concurrency)]
        chunks = [c for c in chunks if c]
        results: list[tuple[int, Path]] = []
        if len(chunks) == 1:
            results = _run_chunk(chunks[0])
        else:
            with ThreadPoolExecutor(max_workers=len(chunks)) as ex:
                for part in ex.map(_run_chunk, chunks):
                    results.extend(part)
        paths = [p for _, p in sorted(results)]
        return _finalize(paths)


# ════════════════════════════════════════════════════════════
# 注册表 & 入口
# ════════════════════════════════════════════════════════════

REGISTRY: dict[str, type[TTSScheme]] = {
    "edge": EdgeScheme,
    "openai": OpenAITTSScheme,
    "bailian": BailianScheme,
    "sambert": SambertScheme,
}


def default_scheme() -> str:
    """未指定 tts.scheme 时使用的方案名（来自配置层 default_scheme）。"""
    name = ttsconfig.default_scheme()
    # 配置层若指向未实现的方案，回退到第一个已注册方案
    return name if name in REGISTRY else next(iter(REGISTRY))


def get_scheme(cfg: dict[str, Any]) -> TTSScheme:
    """按配置中的 tts.scheme（兼容旧 provider 字段）解析出方案实例。"""
    name = (
        (cfg.get("scheme") or cfg.get("provider") or "").strip()
        or default_scheme()
    )
    cls = REGISTRY.get(name)
    if cls is None:
        raise TTSError(
            f"未知的 TTS 方案：{name!r}；可选：{', '.join(REGISTRY)}"
        )
    return cls()


def list_schemes() -> list[dict[str, Any]]:
    """暴露给 Web / CLI 的方案清单（含各自音色列表，全部从配置层读取）。"""
    out: list[dict[str, Any]] = []
    for cls in REGISTRY.values():
        meta = cls().meta()
        out.append({
            "name": cls.name,
            "label": meta["label"],
            # model：该音色绑定的模型名（openai/bailian 用；其余方案为 null）
            "voices": [
                {"id": v["id"], "label": v["label"], "model": v["model"]}
                for v in cls().voices()
            ],
            "supports_rate": meta["supports_rate"],
            "supports_volume": meta["supports_volume"],
            "needs_endpoint": meta["needs_endpoint"],
        })
    return out


def resolve(cfg: dict[str, Any]) -> dict[str, Any]:
    """补全缺失的公共参数 + 方案专属子块，返回一份可直接喂给 synthesize 的完整配置。

    优先级：上层配置（cfg 自身） > 配置层方案默认 > 代码极小兜底。
    """
    scheme = get_scheme(cfg)
    out = dict(cfg)
    for k in PUBLIC_KEYS:
        if out.get(k) in (None, ""):
            out[k] = scheme.defaults().get(k)
    # 用与合成时同一套取值逻辑（含「音色绑定模型」推导），
    # 保证日志展示 / 配置回显与实际合成拿到的是同一份参数
    out[scheme.name] = scheme.sub_params(cfg)
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
