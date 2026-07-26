"""`.md` を下書きエディタで開くようにWindowsへ登録する。

    python setup_md_association.py --check     いまの状態を見るだけ
    python setup_md_association.py             登録する
    python setup_md_association.py --remove    元に戻す

書き込むのは **HKCU（このユーザーだけ）** で、管理者権限は要らない。
`.md` には UserChoice（Windowsが「既定のアプリ」画面で付ける保護付きの指定）が
無いことを確認済みなので、`HKCU\\Software\\Classes\\.md` を書けば素直に効く。
もし後から「既定のアプリ」で別のものを選ぶと、そちらが優先される。

元の関連付けは `PreviousProgId` に控えておき、`--remove` で戻す。
"""

from __future__ import annotations

import ctypes
import sys
import winreg
from pathlib import Path

PROG_ID = "croco.markdown"
DESCRIPTION = "Markdown（下書きエディタ）"
EXTENSION = ".md"
CLASSES = r"Software\Classes"

SHCNE_ASSOCCHANGED = 0x08000000
SHCNF_IDLIST = 0x0000


def launcher() -> Path:
    return Path(__file__).resolve().parent / "open_md.pyw"


def icon_path() -> Path:
    """エクスプローラで .md に出るアイコン。`make_icon.py` が作る。"""
    return Path(__file__).resolve().parent / "assets" / "editor.ico"


def pythonw() -> Path:
    """コンソールを出さないPython。これが無いと黒い窓が一瞬光る。"""
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return candidate if candidate.exists() else Path(sys.executable)


def open_command() -> str:
    return f'"{pythonw()}" "{launcher()}" "%1"'


def _read(path: str, name: str = "") -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _write(path: str, value: str, name: str = "") -> None:
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)


def _notify_shell() -> None:
    """エクスプローラに関連付けの変更を伝える。再起動しなくても反映される。"""
    ctypes.windll.shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)


def show() -> int:
    user_choice = _read(
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer"
        rf"\FileExts\{EXTENSION}\UserChoice", "ProgId")
    print(f"{EXTENSION} の割り当て : {_read(rf'{CLASSES}\{EXTENSION}')!r}")
    print(f"UserChoice        : {user_choice!r}"
          + ("  ← これがあると上の指定より優先される" if user_choice else ""))
    print(f"登録済みのコマンド: "
          f"{_read(rf'{CLASSES}\{PROG_ID}\shell\open\command')!r}")
    print(f"これから書く内容  : {open_command()!r}")
    if not launcher().is_file():
        print(f"!! ランチャが見つかりません: {launcher()}")
        return 1
    return 0


def install() -> int:
    if not launcher().is_file():
        print(f"ランチャが見つかりません: {launcher()}")
        return 1
    previous = _read(rf"{CLASSES}\{EXTENSION}") or ""
    if previous != PROG_ID:
        # 戻せるように控える。上書きしてから控えると元が分からなくなる
        _write(rf"{CLASSES}\{PROG_ID}", previous, "PreviousProgId")
    _write(rf"{CLASSES}\{PROG_ID}", DESCRIPTION)
    _write(rf"{CLASSES}\{PROG_ID}\shell\open\command", open_command())
    if icon_path().is_file():
        _write(rf"{CLASSES}\{PROG_ID}\DefaultIcon", f'"{icon_path()}",0')
    # 「プログラムから開く」の一覧にも出しておく
    _write(rf"{CLASSES}\{EXTENSION}\OpenWithProgids", "", PROG_ID)
    _write(rf"{CLASSES}\{EXTENSION}", PROG_ID)
    _notify_shell()
    print(f"{EXTENSION} を下書きエディタに割り当てました。")
    print(f"  {open_command()}")
    # 控えてある値を読み直して出す。2回目以降は previous が自分自身になるので、
    # それをそのまま出すと「元の割り当て＝croco.markdown」と嘘になる。
    kept = _read(rf"{CLASSES}\{PROG_ID}", "PreviousProgId")
    if kept:
        print(f"  元の割り当て（{kept}）は控えてあります。--remove で戻せます。")
    return 0


def remove() -> int:
    previous = _read(rf"{CLASSES}\{PROG_ID}", "PreviousProgId")
    if _read(rf"{CLASSES}\{EXTENSION}") == PROG_ID:
        _write(rf"{CLASSES}\{EXTENSION}", previous or "")
    for path in (rf"{CLASSES}\{PROG_ID}\shell\open\command",
                 rf"{CLASSES}\{PROG_ID}\shell\open",
                 rf"{CLASSES}\{PROG_ID}\shell",
                 rf"{CLASSES}\{PROG_ID}"):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
        except OSError:
            pass
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            rf"{CLASSES}\{EXTENSION}\OpenWithProgids",
                            0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, PROG_ID)
    except OSError:
        pass
    _notify_shell()
    print(f"{EXTENSION} の割り当てを外しました（元: {previous or 'なし'}）。")
    return 0


def main(argv: list[str]) -> int:
    if "--check" in argv:
        return show()
    if "--remove" in argv:
        return remove()
    return install()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
