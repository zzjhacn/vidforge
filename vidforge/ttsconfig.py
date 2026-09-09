"""TTS 方案配置的加载与查询（配置层）。

设计目的：把 TTS 方案的「默认值」从代码里彻底抽离——音色列表、端点 URL、
API Key、模型名、语速、采样率等一律写在 YAML 里，代码只读不算。

配置文件查找顺序（第一个存在者生效，后者作为兜底）：

  1. 环境变量 ``VIDFORGE_TTS_CONFIG`` 指定的文件
  2. 当前工作目录下的 ``configs/tts_schemes.yaml``
  3. ``~/.vidforge/tts_schemes.yaml``
  4. 包内 ``vidforge/data/tts_schemes.yaml``（出厂默认，随包发布）

建议：把出厂默认复制到 2 或 3 再改，升级 vidforge 时不受影响。

与场景配置（configs/swim.yaml 的 ``tts:`` 块）的分工：
   本文件     = 方案级默认值（对所有出片生效）
   场景 YAML  = 场景级覆盖（仅对该场景生效），优先级更高

环境变量引用：配置值里的 ``${VAR}`` 会在加载时展开（用于 API Key 等敏感值），
未定义的变量展开为空字符串。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

CONFIG_FILENAME = "tts_schemes.yaml"
ENV_CONFIG_VAR = "VIDFORGE_TTS_CONFIG"

# 出厂默认（包内，随包发布，请勿直接修改）
BUILTIN_PATH = Path(__file__).parent / "data" / CONFIG_FILENAME

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class TTSConfigError(RuntimeError):
    """TTS 方案配置缺失或格式错误。"""


# ── 加载与缓存 ────────────────────────────────────────────────
# 只在文件 mtime 变化时重新读盘：既避免每次合成都解析 YAML，
# 又能在改完配置后刷新页面 / 重跑命令即刻生效。

_CACHE: dict[str, Any] = {"path": None, "mtime": None, "data": None}


def _candidate_paths() -> list[Path]:
    paths: list[Path] = []
    env = os.environ.get(ENV_CONFIG_VAR, "").strip()
    if env:
        paths.append(Path(env).expanduser())
    paths.append(Path.cwd() / "configs" / CONFIG_FILENAME)
    paths.append(Path.home() / ".vidforge" / CONFIG_FILENAME)
    paths.append(BUILTIN_PATH)
    return paths


def config_path() -> Path:
    """当前生效的配置文件路径（供日志 / 排障展示）。"""
    load()
    return _CACHE["path"]  # type: ignore[return-value]


def _expand_env(value: Any) -> Any:
    """递归展开字符串里的 ${VAR}（用于 API Key 等敏感值）。"""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(
            lambda m: os.environ.get(m.group(1), ""), value
        )
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load(force: bool = False) -> dict[str, Any]:
    """加载配置（带 mtime 缓存；force=True 强制重读）。"""
    for path in _candidate_paths():
        if not path.is_file():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if (
            not force
            and _CACHE["path"] == path
            and _CACHE["mtime"] == mtime
            and _CACHE["data"] is not None
        ):
            return _CACHE["data"]  # type: ignore[return-value]
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise TTSConfigError(
                f"TTS 方案配置格式错误（顶层应为映射）：{path}"
            )
        data = _expand_env(data)
        _CACHE.update({"path": path, "mtime": mtime, "data": data})
        return data
    raise TTSConfigError(
        f"找不到 TTS 方案配置文件（已查找：{', '.join(str(p) for p in _candidate_paths())}）"
    )


def reload() -> dict[str, Any]:
    """强制重新加载（改完配置后想立即生效时调用）。"""
    return load(force=True)


# ── 查询 API ──────────────────────────────────────────────────

def _schemes() -> dict[str, Any]:
    data = load()
    schemes = data.get("schemes") or {}
    if not isinstance(schemes, dict):
        raise TTSConfigError(
            f"TTS 方案配置的 schemes 段格式错误：{config_path()}"
        )
    return schemes


def default_scheme() -> str:
    """未指定 scheme 时使用的方案名。"""
    name = (load().get("default_scheme") or "").strip()
    return name or "edge"


def scheme_names() -> list[str]:
    return list(_schemes().keys())


def scheme_node(name: str) -> dict[str, Any]:
    """某方案的完整配置节点（不存在则返回空字典）。"""
    node = _schemes().get(name) or {}
    return node if isinstance(node, dict) else {}


def _tri(node: dict[str, Any], key: str) -> bool | None:
    """三态读取：配置未写该键返回 None（由调用方回退到代码兜底）。"""
    if key not in node:
        return None
    return bool(node[key])


def scheme_meta(name: str) -> dict[str, Any]:
    """方案的展示信息与能力开关（label / supports_* / needs_endpoint）。

    能力开关为三态：配置里没写 → None（回退到代码类属性）。
    """
    node = scheme_node(name)
    return {
        "label": node.get("label") or name,
        "supports_rate": _tri(node, "supports_rate"),
        "supports_volume": _tri(node, "supports_volume"),
        "needs_endpoint": _tri(node, "needs_endpoint"),
    }


def scheme_defaults(name: str) -> dict[str, Any]:
    """方案的公共参数默认值（voice / rate / volume）。"""
    node = scheme_node(name)
    defaults = node.get("defaults") or {}
    return dict(defaults) if isinstance(defaults, dict) else {}


def scheme_params(name: str) -> dict[str, Any]:
    """方案的专属参数默认值（endpoint / key / model / sample_rate / ...）。"""
    node = scheme_node(name)
    params = node.get("params") or {}
    return dict(params) if isinstance(params, dict) else {}


def scheme_voices(name: str) -> list[dict[str, str | None]]:
    """方案的可选音色列表 [{"id","label","model"}, ...]。

    支持两种写法：映射 ``{id, label, model}`` 与二元组简写 ``[id, label]``。

    ``model`` 是「音色绑定模型」：openai / bailian 这类多模型后端的音色
    与模型名是绑定的（同一个 voice id 在不同模型下不一定可用），因此在
    voices 段把模型名一并登记；页面选中音色后自动回填模型名，
    运行期也会用它兜底。不需要绑定的方案（edge / sambert）可省略该字段。
    """
    out: list[dict[str, str | None]] = []
    for item in scheme_node(name).get("voices") or []:
        if isinstance(item, dict):
            vid = item.get("id")
            if vid is None:
                continue
            model = item.get("model")
            out.append({
                "id": str(vid),
                "label": str(item.get("label") or vid),
                "model": str(model).strip() if model else None,
            })
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            out.append({"id": str(item[0]), "label": str(item[1]), "model": None})
        elif isinstance(item, (list, tuple)) and len(item) == 1:
            out.append({"id": str(item[0]), "label": str(item[0]), "model": None})
    return out


def voice_model(name: str, voice_id: str) -> str | None:
    """某音色绑定的模型名（未登记则返回 None）。"""
    for v in scheme_voices(name):
        if v["id"] == voice_id:
            return v["model"]
    return None
