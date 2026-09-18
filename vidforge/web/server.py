"""零依赖 Web 操作页面（仅用 Python 标准库）。

设计取舍：**不引入任何第三方 Web 框架**。核心依赖仍然只有 edge-tts / PyYAML / Pillow，
命令行用户完全不受影响——不装也能用，装了也不影响。

启动：
    python -m vidforge.web [--port 8765]
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from ..pipeline import BuildError, build
from ..tts import REGISTRY, TTSError, default_scheme, get_scheme, list_schemes, synthesize
from ..util import list_xfade_transitions

STATIC_DIR = Path(__file__).parent / "static"

CANVAS_PRESETS = {
    "1080x1920": (1080, 1920),
    "1920x1080": (1920, 1080),
    "1080x1080": (1080, 1080),
}

# 音色试听用的固定短句：用来在正式出片前快速验证「方案 / 音色 / 端点 / Key」是否可用。
# 刻意不接受用户自定义文本——否则页面会变成任人滥用的免费 TTS 代理。
TEST_TEXT = "你好，这是一次语音合成测试。"

# 试听音频的 Content-Type（各方案输出格式不同，按扩展名映射）
_TEST_AUDIO_TYPES = {
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".m4a": "audio/mp4",
    ".flac": "audio/flac",
}


# ════════════════════════════════════════════════════════════
# multipart/form-data 解析（标准库没有现成实现，手工解析）
# ════════════════════════════════════════════════════════════

def parse_multipart(body: bytes, content_type: str) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """解析 multipart 请求体，返回 (文本字段, 文件列表)。

    文件元素：{"field": str, "filename": str, "data": bytes}
    """
    m = re.search(r"boundary=([^;]+)", content_type or "")
    if not m:
        raise ValueError("缺少 boundary")
    boundary = m.group(1).strip().strip('"')
    delim = b"--" + boundary.encode()

    fields: dict[str, str] = {}
    files: list[dict[str, Any]] = []

    for part in body.split(delim):
        part = part.strip(b"\r\n")
        if not part or part in (b"--", b"-"):
            continue
        idx = part.find(b"\r\n\r\n")
        if idx < 0:
            continue
        head = part[:idx].decode("utf-8", "replace")
        content = part[idx + 4:]
        if content.endswith(b"\r\n"):          # 去掉结尾的分隔换行
            content = content[:-2]

        name = filename = None
        for line in head.split("\r\n"):
            if line.lower().startswith("content-disposition:"):
                for seg in line.split(";"):
                    seg = seg.strip()
                    low = seg.lower()
                    if low.startswith("name="):
                        name = seg.split("=", 1)[1].strip('"')
                    elif low.startswith("filename="):
                        filename = seg.split("=", 1)[1].strip('"')
        if not name:
            continue
        if filename:
            files.append({"field": name, "filename": filename, "data": content})
        else:
            fields[name] = content.decode("utf-8", "replace")
    return fields, files


# ════════════════════════════════════════════════════════════
# 任务管理（后台线程生成，前端轮询进度）
# ════════════════════════════════════════════════════════════

TASKS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def _task_update(task_id: str, **kw: Any) -> None:
    with _LOCK:
        TASKS[task_id].update(kw)


def _task_log(task_id: str, msg: str, percent: float | None) -> None:
    with _LOCK:
        t = TASKS[task_id]
        t["logs"].append(msg)
        if percent is not None:
            t["percent"] = max(t["percent"], float(percent))
        t["last_message"] = msg.strip() or t["last_message"]


def _make_bind(n_images: int, n_markers: int) -> list[dict[str, str]]:
    """按「N 张图 / M 个锚点」自动生成绑定规则。

    规则：图 i(0..n-2) 绑 before:i，最后一张绑 from:M-1。
    锚点不足时退化处理，保证不报错。
    """
    if n_images <= 1:
        return [{"asset": "img0", "range": "all"}]
    rules = []
    for i in range(n_images - 1):
        rules.append({"asset": f"img{i}", "range": f"before:{min(i, max(n_markers - 1, 0))}"})
    last = max(n_markers - 1, 0)
    rules.append({"asset": f"img{n_images - 1}", "range": f"from:{last}"})
    return rules


def _build_yaml(cfg: dict[str, Any], assets: list[str], markers: list[str]) -> str:
    from ..ttsconfig import scheme_params

    w, h = CANVAS_PRESETS.get(cfg.get("canvas", "1080x1920"), (1080, 1920))
    scheme = (cfg.get("scheme") or default_scheme()).strip()
    # 方案元信息（label/能力/默认参数）全部来自配置层，避免在 Web 层硬编码
    sch = get_scheme({"scheme": scheme})
    meta = sch.meta()
    supports_rate = bool(meta.get("supports_rate", True))
    default_voice = sch.defaults().get("voice") or ""
    params_default = scheme_params(scheme)
    lines = [
        "name: web",
        "",
        "canvas:",
        f"  width: {w}",
        f"  height: {h}",
        "  fps: 30",
        "",
        "tts:",
        f"  scheme: {scheme}",
        f"  voice: {cfg.get('voice') or default_voice}",
    ]
    if supports_rate:
        rate_default = sch.defaults().get("rate") or "+0%"
        lines.append(f"  rate: \"{cfg.get('rate') or rate_default}\"")
    lines += [
        "  silence_between_ms: 200",
        "  tail_padding_ms: 400",
    ]
    # 选中需要端点的方案且用户填写了 endpoint/key 时，写出对应子块
    endpoint = (cfg.get("endpoint") or "").strip()
    key = (cfg.get("key") or "").strip()
    model = (cfg.get("model") or "").strip()
    if not model and scheme in ("openai", "bailian"):
        # 音色绑定模型兜底：页面未回填模型名时按所选音色推导，
        # 写出的 YAML 与运行期 sub_params() 的推导结果保持一致
        model = sch.voice_model(
            {"scheme": scheme, "voice": cfg.get("voice") or default_voice}
        ) or ""
    if scheme in ("openai", "bailian", "sambert") and (endpoint or key or model):
        lines.append(f"  {scheme}:")
        if endpoint:
            lines.append(f'    endpoint: "{endpoint}"')
        if key:
            lines.append(f'    key: "{key}"')
        # sambert 的「音色即 model」，不再单独写 model，避免与 voice 冲突
        if model and scheme != "sambert":
            lines.append(f'    model: "{model}"')
        if scheme in ("bailian", "sambert"):
            default_sr = str(params_default.get("sample_rate") or 24000)
            sr = (cfg.get("sample_rate") or default_sr).strip()
            lines.append(f"    sample_rate: {sr}")
        # no_verify 默认不写（安全默认）；本机缺 CA 证书时在 YAML 手工置 true
    lines += [
        "",
        "timeline:",
        "  min_group_duration: 5.5",
        "",
    ]
    # 转场（Web 仅做全局；type=random 由渲染期展开为具体转场名；none/off 则省略=硬切）
    ttype = (cfg.get("transition") or "fade").strip().lower()
    if ttype not in ("", "none", "off", "false"):
        # 转场时长：夹在 [0.1, 2.0]，避免吞掉最短组（timeline.min_group_duration=5.5s）
        try:
            tdur = float(cfg.get("transition_duration", 0.5) or 0.5)
        except (TypeError, ValueError):
            tdur = 0.5
        tdur = min(max(tdur, 0.1), 2.0)
        lines += [
            "transition:",
            f"  type: {ttype}",
            f"  duration: {tdur:.2f}",
            "",
        ]
    lines += [
        "script:",
        "  source: assets/script.txt",
        f"  max_sentence_chars: {int(cfg.get('max_sentence_chars', 26))}",
    ]
    if markers:
        lines.append("  split_markers:")
        for mk in markers:
            lines.append(f'    - "{mk}"')
    lines += ["", "assets:"]
    for i, fn in enumerate(assets):
        lines += [
            f"  - name: img{i}",
            f"    file: assets/{fn}",
            f"    fit: {cfg.get('fit', 'contain')}",
            "    background: blur",
            "    background_blur: 24",
            "    motion: { type: kenburns, from: 1.0, to: 1.05 }",
        ]
    lines += ["", "bind:"]
    for r in _make_bind(len(assets), len(markers)):
        lines.append(f'  - {{ asset: {r["asset"]}, range: "{r["range"]}" }}')
    lines += [
        "",
        "subtitle:",
        f"  enabled: {str(cfg.get('subtitle') == '1').lower()}",
        f"  size: {int(cfg.get('subtitle_size', 58))}",
        f"  bottom_margin: {int(cfg.get('bottom_margin', 260))}",
        "  max_chars_per_line: 15",
        "  box: true",
        "  box_alpha: 110",
        "",
        "output:",
        "  file: output/out.mp4",
        "  video_codec: libx264",
        "  preset: medium",
        "  crf: 20",
        "  audio_codec: aac",
        "  audio_bitrate: 192k",
        "",
    ]
    return "\n".join(lines)


def _tts_test_cfg(req: dict[str, Any]) -> dict[str, Any]:
    """按页面传入的字段构造 tts 配置块。

    以 tts.resolve() 补全缺失参数（来自配置层方案默认），再覆盖页面字段；
    与正式出片共用同一份默认值和同一套方案实现，因此「试听能过」等价于
    「生成时的配音也能过」，不会出现两套逻辑各自为政。
    """
    from ..tts import resolve as tts_resolve

    scheme = (req.get("scheme") or default_scheme()).strip()
    if scheme not in REGISTRY:
        raise TTSError(f"未知的 TTS 方案：{scheme}；可选：{', '.join(REGISTRY)}")

    # 起点：指定 scheme（+ 页面音色），其余由 tts.resolve 用配置层默认值补全。
    # 音色先于 resolve 传入，resolve 才能按「音色绑定模型」推导出正确的 model
    base: dict[str, Any] = {"scheme": scheme}
    if (req.get("voice") or "").strip():
        base["voice"] = req["voice"].strip()
    cfg = tts_resolve(base)

    # 页面字段覆盖（空值视为未指定，不覆盖）
    if (req.get("rate") or "").strip():
        cfg["rate"] = req["rate"].strip()

    sub = cfg.get(scheme) or {}
    for key in ("endpoint", "key", "model"):
        val = (req.get(key) or "").strip()
        if val:
            sub[key] = val
    if scheme == "sambert":
        # Sambert 音色即 model，绝不允许 model 覆盖掉选中的音色
        sub.pop("model", None)
    sr = str(req.get("sample_rate") or "").strip()
    if sr.isdigit():
        sub["sample_rate"] = int(sr)
    cfg[scheme] = sub
    return cfg


def run_task(task_id: str, root: Path) -> None:
    """后台线程：调用 pipeline.build 生成视频。"""
    try:
        _task_update(task_id, status="running", percent=1,
                     last_message="开始生成")
        result = build(
            root / "configs" / "scene.yaml",
            dry_run=False,
            progress=lambda msg, pct: _task_log(task_id, msg, pct),
        )
        _task_update(
            task_id,
            status="done",
            percent=100,
            last_message="生成完成",
            output=str(result.output_path),
            duration=round(result.total_duration, 2),
            size_mb=round(result.size_mb, 2),
        )
    except BuildError as exc:
        _task_update(task_id, status="error", error=f"错误：{exc}",
                     last_message=str(exc))
    except Exception as exc:                                   # noqa: BLE001
        _task_update(task_id, status="error", error=f"{type(exc).__name__}: {exc}",
                     last_message=f"失败：{exc}")


# ════════════════════════════════════════════════════════════
# HTTP 处理
# ════════════════════════════════════════════════════════════

class Handler(BaseHTTPRequestHandler):
    server_version = "vidforge/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        pass    # 静音默认日志，避免刷屏

    # ── 工具 ────────────────────────────────────────────────
    def _json(self, data: Any, code: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype: str, download: bool = False) -> None:
        if not path.exists():
            self.send_error(404)
            return
        size = path.stat().st_size
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                start = int(m.group(1)) if m.group(1) else 0
                end = int(m.group(2)) if m.group(2) else size - 1
                end = min(end, size - 1)
                length = end - start + 1
                self.send_response(206)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                self.send_header("Content-Length", str(length))
                self.send_header("Accept-Ranges", "bytes")
                if download:
                    self.send_header("Content-Disposition",
                                     f'attachment; filename="{path.name}"')
                self.end_headers()
                with open(path, "rb") as f:
                    f.seek(start)
                    self.wfile.write(f.read(length))
                return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(size))
        self.send_header("Accept-Ranges", "bytes")
        if download:
            self.send_header("Content-Disposition",
                             f'attachment; filename="{path.name}"')
        self.end_headers()
        with open(path, "rb") as f:
            shutil.copyfileobj(f, self.wfile)

    # ── 路由 ────────────────────────────────────────────────
    def do_GET(self) -> None:                                  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        elif path == "/api/meta":
            self._json({
                # 各 TTS 方案及其音色列表（Web 据此做「先选方案、再选音色」）
                "schemes": list_schemes(),
                "canvas": list(CANVAS_PRESETS.keys()),
                # 本机 ffmpeg 实际支持的 xfade 转场名（供前端下拉动态生成；含 random 由前端追加）
                "transitions": list_xfade_transitions(),
                # 当前生效的默认方案（来自配置层 default_scheme）
                "default_scheme": default_scheme(),
                # 试听固定句：单一来源在后端，前端只负责展示
                "test_text": TEST_TEXT,
            })
        elif path.startswith("/api/task/"):
            tid = unquote(path.split("/api/task/")[1])
            with _LOCK:
                t = TASKS.get(tid)
                data = dict(t) if t else None
            self._json(data if data else {"error": "任务不存在"},
                       200 if data else 404)
        elif path.startswith("/api/preview/"):
            tid = unquote(path.split("/api/preview/")[1])
            with _LOCK:
                out = TASKS.get(tid, {}).get("output")
            if not out:
                self.send_error(404)
                return
            self._file(Path(out), "video/mp4")
        elif path.startswith("/api/download/"):
            tid = unquote(path.split("/api/download/")[1])
            with _LOCK:
                out = TASKS.get(tid, {}).get("output")
            if not out:
                self.send_error(404)
                return
            self._file(Path(out), "video/mp4", download=True)
        else:
            self.send_error(404)

    def _tts_test(self) -> None:
        """音色试听：用固定短句跑一次当前配置的 TTS，直接回传音频字节。

        成功 → 音频二进制 + X-Audio-Duration（实测秒数）；
        失败 → JSON {"error": ...}，供前端原样展示（多为 Key/端点/音色配置问题）。
        """
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        try:
            req = json.loads(body or b"{}")
        except ValueError:
            self._json({"error": "请求体不是合法 JSON"}, 400)
            return

        try:
            cfg = _tts_test_cfg(req if isinstance(req, dict) else {})
            with tempfile.TemporaryDirectory(prefix="vidforge-ttstest-") as td:
                results = synthesize([TEST_TEXT], cfg, Path(td))
                if not results:
                    raise TTSError("未生成音频（方案返回空结果）")
                audio_path, duration = results[0]
                audio = audio_path.read_bytes()
                suffix = audio_path.suffix.lower()
        except TTSError as exc:                                # 可预期失败：配置/端点/音色
            self._json({"error": str(exc)}, 400)
            return
        except Exception as exc:                               # noqa: BLE001
            self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
            return

        if not audio:
            self._json({"error": "端点返回空音频"}, 400)
            return
        self.send_response(200)
        self.send_header("Content-Type",
                         _TEST_AUDIO_TYPES.get(suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(audio)))
        self.send_header("X-Audio-Duration", f"{duration:.3f}")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(audio)

    def do_POST(self) -> None:                                 # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/tts-test":
            self._tts_test()
            return
        if path != "/api/generate":
            self.send_error(404)
            return
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            self._json({"error": "请求格式错误，需要 multipart/form-data"}, 400)
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""

        try:
            fields, files = parse_multipart(body, ctype)
        except ValueError as exc:
            self._json({"error": str(exc)}, 400)
            return

        images = [f for f in files if f["filename"]]
        script_text = fields.get("script", "").strip()
        if not images:
            self._json({"error": "请至少上传一张卡片图"}, 400)
            return
        if not script_text:
            self._json({"error": "文案不能为空"}, 400)
            return

        markers = [m.strip() for m in fields.get("markers", "").split(",") if m.strip()]
        task_id = uuid.uuid4().hex[:12]
        root = Path(fields.get("workroot") or (Path.cwd() / "web_work")) / task_id
        (root / "configs").mkdir(parents=True, exist_ok=True)
        (root / "assets").mkdir(parents=True, exist_ok=True)

        # 保存图片（按上传顺序命名，保证与绑定顺序一致）
        names = []
        for i, img in enumerate(images):
            ext = Path(img["filename"]).suffix.lower() or ".png"
            if ext not in (".png", ".jpg", ".jpeg", ".webp"):
                ext = ".png"
            fn = f"card{i}{ext}"
            (root / "assets" / fn).write_bytes(img["data"])
            names.append(fn)

        (root / "assets" / "script.txt").write_text(script_text, encoding="utf-8")
        (root / "configs" / "scene.yaml").write_text(
            _build_yaml(fields, names, markers), encoding="utf-8"
        )

        with _LOCK:
            TASKS[task_id] = {
                "id": task_id, "status": "queued", "percent": 0,
                "logs": [], "last_message": "已排队", "output": None,
                "error": None, "created": time.time(), "workdir": str(root),
                "images": len(names),
            }
        threading.Thread(target=run_task, args=(task_id, root), daemon=True).start()
        self._json({"task_id": task_id})


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(prog="vidforge-web", description="vidforge 操作页面")
    p.add_argument("--port", type=int, default=8765, help="端口（默认 8765）")
    p.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    p.add_argument("--workroot", default=None,
                   help="任务工作目录（默认当前目录下的 web_work）")
    args = p.parse_args(argv)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"vidforge 操作页面已启动 → http://{args.host}:{args.port}")
    print("按 Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
    return 0
