"""下書きエディタでファイルを開く。Windowsのファイル関連付けから呼ばれる。

`.md` の既定アプリとして登録する入口。拡張子が `.pyw` なので pythonw.exe が動き、
**コンソールが一瞬も出ない**。exe化はしない（PyInstaller等の外部パッケージが要る。
得られるのは「Pythonが無くても動く」だけで、ここでは意味が無い）。

中身は `croco/editor_app.py`（tkinter製のネイティブなエディタ）。
既に窓が開いていれば、新しいタブとしてそちらに渡す。

pythonw には標準エラーが無く、例外を出しても誰も気づけない。失敗はダイアログで見せる。
"""

import ctypes
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from croco import editor_app  # noqa: E402


if __name__ == "__main__":
    try:
        sys.exit(editor_app.main(sys.argv))
    except Exception:
        ctypes.windll.user32.MessageBoxW(
            0, "開けませんでした。\n\n" + traceback.format_exc(), "下書きエディタ", 0x10)
        sys.exit(1)
