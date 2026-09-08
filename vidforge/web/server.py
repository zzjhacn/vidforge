"""零依赖 Web 操作页面（仅用 Python 标准库）。

设计取舍：**不引入任何第三方 Web 框架**。核心依赖仍然只有 edge-tts / PyYAML / Pillow，
命令行用户完全不受影响——不装也能用，装了也不影响。

启动：
    python -m vidforge.web [--port 8765]
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from ..pipeline import BuildError, build
from ..tts import list_schemes

STATIC_DIR = Path(__file__).parent / "static"

CANVAS_PRESETS = {
    "1080x1920": (1080, 1920),
    "1920x1080": (1920, 1080),
    "1080x1080": (1080, 1080),
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
    w, h = CANVAS_PRESETS.get(cfg.get("canvas", "1080x1920"), (1080, 1920))
    scheme = (cfg.get("scheme") or "edge").strip()
    # 从方案注册表取元信息，按 needs_endpoint / supports_rate 决定写哪些字段
    meta = {s["name"]: s for s in list_schemes()}.get(scheme, {})
    supports_rate = bool(meta.get("supports_rate", True))
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
        f"  voice: {cfg.get('voice', 'zh-CN-YunxiNeural')}",
    ]
    if supports_rate and scheme != "bailian":
        lines.append(f"  rate: \"{cfg.get('rate', '+10%')}\"")
    lines += [
        "  silence_between_ms: 200",
        "  tail_padding_ms: 400",
    ]
    # 选中需要端点的方案且用户填写了 endpoint 时，写出对应子块
    endpoint = (cfg.get("endpoint") or "").strip()
    if endpoint and scheme in ("openai", "bailian"):
        lines.append(f"  {scheme}:")
        lines.append(f'    endpoint: "{endpoint}"')
        key = (cfg.get("key") or "").strip()
        if key:
            lines.append(f'    key: "{key}"')
        model = (cfg.get("model") or "").strip()
        if model:
            lines.append(f'    model: "{model}"')
        if scheme == "bailian":
            sr = (cfg.get("sample_rate") or "24000").strip()
            lines.append(f"    sample_rate: {sr}")
        # no_verify 默认不写（安全默认）；本机缺 CA 证书时在 YAML 手工置 true
    lines += [
        "",
        "timeline:",
        "  min_group_duration: 5.5",
        "",
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
        f"  enabled: {str(bool(cfg.get('subtitle', True))).lower()}",
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

    def do_POST(self) -> None:                                 # noqa: N802
        if urlparse(self.path).path != "/api/generate":
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
