"""ターミナルと同じモニタにウィンドウを出すための座標計算（Windows専用）。

二画面で使っているので、下書きエディタが**ターミナルと違う画面に出ると気づけない**。
ブラウザ任せだと前回閉じた位置に出るだけなので、こちらで位置を決めて渡す。

ターミナルのウィンドウは `GetConsoleWindow()` では掴めない。Windows Terminal は
ConPTY 越しなので 0 が返る（2026-07-26 に実地で確認）。代わりに
**親プロセスを遡って、見えるウィンドウを持つ祖先**を探す。
実測では python.exe <- powershell.exe <- WindowsTerminal.exe と辿れた。

どこにも当たらなければ None を返す。位置指定を諦めるだけで、開くこと自体は止めない。
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

try:
    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
except AttributeError:  # Windows以外
    _user32 = _kernel32 = None  # type: ignore[assignment]

TH32CS_SNAPPROCESS = 0x00000002
MONITOR_DEFAULTTONEAREST = 0x00000002


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", wintypes.DWORD)]


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_char * 260),
    ]


_ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _ancestor_pids(limit: int = 10) -> list[int]:
    """自分から先祖へ向かって並べたPID。辿れなくなったところで止める。"""
    snapshot = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snapshot == -1:
        return [os.getpid()]
    entry = _PROCESSENTRY32()
    entry.dwSize = ctypes.sizeof(entry)
    parents: dict[int, int] = {}
    try:
        if _kernel32.Process32First(snapshot, ctypes.byref(entry)):
            while True:
                parents[entry.th32ProcessID] = entry.th32ParentProcessID
                if not _kernel32.Process32Next(snapshot, ctypes.byref(entry)):
                    break
    finally:
        _kernel32.CloseHandle(snapshot)

    chain, pid = [], os.getpid()
    while pid and len(chain) < limit:
        chain.append(pid)
        pid = parents.get(pid, 0)
        if pid in chain:  # 壊れた親子関係で回り続けない
            break
    return chain


# これより小さいウィンドウは人が見ている窓ではないとみなす。
# `IsWindowVisible` は**サイズ0の窓にも真を返す**。裏方のウィンドウ（実測では
# 先祖の claude.exe が持っていた）を掴むと、幅0の位置指定を出してしまう。
MIN_WINDOW = (200, 100)


def window_size(hwnd: int) -> tuple[int, int] | None:
    """ウィンドウの外枠の大きさ。人が見ている窓でなければ None。"""
    if not hwnd or not _user32.IsWindowVisible(hwnd):
        return None
    rect = _RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    size = (rect.right - rect.left, rect.bottom - rect.top)
    if size[0] < MIN_WINDOW[0] or size[1] < MIN_WINDOW[1]:
        return None
    return size


def _visible_window_of(pids: list[int]) -> int | None:
    """指定PIDのどれかが持つ、人が見ているトップレベルウィンドウ。近い先祖を優先。"""
    found: dict[int, int] = {}

    def visit(hwnd, _lparam):
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and pid.value not in found and window_size(hwnd):
            found[pid.value] = hwnd
        return True

    _user32.EnumWindows(_ENUMPROC(visit), 0)
    for pid in pids:  # 近い先祖ほど当てにできる
        if pid in found:
            return found[pid]
    return None


def terminal_window() -> int | None:
    """このプロセスを動かしているターミナルのウィンドウ。"""
    if _user32 is None:
        return None
    pids = _ancestor_pids()
    foreground = _user32.GetForegroundWindow()
    if window_size(foreground):
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(foreground, ctypes.byref(pid))
        if pid.value in pids:
            return foreground  # 手前に居るのが自分の親なら、それが当のウィンドウ
    window = _visible_window_of(pids)
    if window:
        return window
    # 先祖に辿り着けなくても、本人が見ているのは手前のウィンドウのはず
    return foreground if window_size(foreground) else None


def monitor_work_area(hwnd: int) -> tuple[int, int, int, int] | None:
    """そのウィンドウが載っているモニタの作業領域 (左, 上, 右, 下)。

    タスクバーを除いた範囲。二画面なので座標は負にも画面幅以上にもなる。
    """
    if _user32 is None or not hwnd:
        return None
    monitor = _user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    if not _user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return None
    work = info.rcWork
    return (work.left, work.top, work.right, work.bottom)


# ターミナルの大きさが読めなかったときの目安。既定のターミナルくらい。
FALLBACK_SIZE = (1120, 640)


def editor_rect() -> tuple[int, int, int, int] | None:
    """下書きエディタを置く位置と大きさ (x, y, 幅, 高さ)。分からなければ None。

    ターミナルと同じモニタに、**ターミナルと同じ大きさ**で置く。
    画面いっぱいに広げると書く場所としては大きすぎる、というのが本人の感覚。
    位置は**ターミナルが居ない側の端**へ寄せて縦は中央。同じ大きさだと
    半分ずつには収まらないので、重なりを最小にする置き方にしている。
    """
    if _user32 is None:
        return None
    window = terminal_window()
    if not window:
        return None
    work = monitor_work_area(window)
    if not work:
        return None
    work_left, work_top, work_right, work_bottom = work
    work_width = work_right - work_left
    work_height = work_bottom - work_top

    rect = _RECT()
    if _user32.GetWindowRect(window, ctypes.byref(rect)):
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        on_left = (rect.left + rect.right) // 2 < work_left + work_width // 2
    else:
        width, height = FALLBACK_SIZE
        on_left = True
    width = min(width, work_width)
    height = min(height, work_height)

    left = work_right - width if on_left else work_left
    top = work_top + (work_height - height) // 2
    return (left, top, width, height)
