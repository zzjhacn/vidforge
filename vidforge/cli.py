"""命令行入口：读配置 → 生成视频。

编排逻辑在 pipeline.py（与 Web 页面共用），这里只负责参数解析和把进度打印出来。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .pipeline import BuildError, build


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

    def progress(msg: str, _pct: float | None = None) -> None:
        print(msg)

    try:
        build(config_path, dry_run=args.dry_run, progress=progress)
    except BuildError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
