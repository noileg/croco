"""二重起動の防止。

実装フェーズは1回の起動で何十分も走りうるので、その最中に手動でもう一度
起動すると、同じInboxアイテムを2つのプロセスが掴んで進捗ログが混ざる。
スタートアップ起動と手動起動が重なるのは十分ありえるため、ロックを持たせる。

異常終了でロックが残った場合に永久に起動できなくなると困るので、
記録したPIDが生きているかを確認し、死んでいれば奪い取る。
"""

from __future__ import annotations

import os
from pathlib import Path


class AlreadyRunning(RuntimeError):
    def __init__(self, pid: int) -> None:
        super().__init__(f"クロコは既に実行中です（PID {pid}）。")
        self.pid = pid


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class SingleInstance:
    """`with SingleInstance(path):` で囲った範囲を単一実行に限定する。"""

    def __init__(self, path: Path) -> None:
        self._path = path

    def __enter__(self) -> "SingleInstance":
        self._path.parent.mkdir(parents=True, exist_ok=True)

        if self._path.is_file():
            try:
                other = int(self._path.read_text(encoding="utf-8").strip())
            except (ValueError, OSError):
                other = -1
            if other > 0 and other != os.getpid() and _pid_alive(other):
                raise AlreadyRunning(other)
            # 前回が異常終了して残ったロック。奪って続行する。
            self._path.unlink(missing_ok=True)

        self._path.write_text(str(os.getpid()), encoding="utf-8")
        return self

    def __exit__(self, *_exc: object) -> None:
        try:
            if self._path.is_file():
                if self._path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                    self._path.unlink(missing_ok=True)
        except OSError:
            pass
