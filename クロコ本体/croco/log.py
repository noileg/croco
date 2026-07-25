"""実行ログ。

無人実行が前提なので、後から「何が起きたか」を必ず追えるようにする。
標準出力と日付ごとのログファイルの両方に出す。
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

_log_file: Path | None = None


def setup(log_dir: Path) -> Path:
    """ログファイルを準備して、そのパスを返す。"""
    global _log_file
    log_dir.mkdir(parents=True, exist_ok=True)
    _log_file = log_dir / f"croco_{datetime.now():%Y-%m-%d}.log"
    return _log_file


def log(message: str, *, level: str = "INFO") -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {level:<5} {message}"
    stream = sys.stderr if level in ("WARN", "ERROR") else sys.stdout
    print(line, file=stream, flush=True)
    if _log_file is not None:
        with _log_file.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def warn(message: str) -> None:
    log(message, level="WARN")


def error(message: str) -> None:
    log(message, level="ERROR")
