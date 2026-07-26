"""下書きエディタ（ネイティブ版）。Windows標準のメモ帳を下敷きにしている。

**なぜブラウザをやめたか。** `.md` の既定アプリにするためだけに、
ブラウザ＋ローカルサーバ＋専用プロファイルという一式が必要になっていた。
ネイティブなら**その一式が丸ごと要らない**。ファイルは直接読み書きでき、
ポートもトークンもプロファイル（68MB）も消える。起動も 2.7秒 → 0.3秒 になる。
Ctrl+W も、ブラウザだと窓を閉じる操作として先に食われて実装できなかった。

外部パッケージは使わない。tkinter はPythonに同梱されている。

**文字数の数え方は HTML版（Twitter-like-char-counter.html）から忠実に移植した。**
ここが変われば道具の意味が変わる。書き直しではなく移植であることを守ること。
JSの `\\w` は ASCII のみなので、Python側では明示的に `[A-Za-z0-9_]` と書いている
（`\\w` のままだと日本語が単語文字に含まれ、記法の判定が変わってしまう）。
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import threading
import tkinter as tk
import unicodedata
import webbrowser
from dataclasses import dataclass, field, asdict
from pathlib import Path
from tkinter import filedialog, font as tkfont, messagebox, ttk

from . import screen

APP_NAME = "下書きエディタ"
PRESETS = (400, 600, 800, 1000, 1200, 1600, 2000)

# --- 記法の判定（HTML版からの移植） ----------------------------------------
RE_HR = re.compile(r"^\s*([-*_])\s*(?:\1\s*){2,}$")
RE_FENCE = re.compile(r"^\s*(```|~~~)")
RE_HEAD_PREFIX = re.compile(r"^(\s*#{1,6}\s+)")
RE_QUOTE_PREFIX = re.compile(r"^(\s*>\s?)")
RE_LIST_PREFIX = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+)")
RE_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
RE_TABLE_SEP = re.compile(r"^\s*\|[\s|:-]+\|\s*$")
RE_LINK = re.compile(r"(!?)\[([^\]\n]*)\]\(([^)\n]*)\)")
RE_HEAD = re.compile(r"^\s*(#{1,6})\s+(.*)$")

# 行をまたがない装飾記号。JSの \w は ASCII のみ。
RE_INLINE = (
    re.compile(r"\*\*|__"),
    re.compile(r"~~"),
    re.compile(r"(?<![A-Za-z0-9_*])\*(?!\*)|(?<![A-Za-z0-9_])_(?!_)"),
    re.compile(r"`"),
)


# プレビューで拾う装飾。画像 `![...](...)` は表示名だけ残す（第6グループ）。
RE_INLINE_RENDER = re.compile(
    r"\*\*(.+?)\*\*|__(.+?)__|~~(.+?)~~|`([^`]+)`"
    r"|(?<![A-Za-z0-9_*])\*([^*\n]+)\*|!?\[([^\]\n]*)\]\(([^)\n]*)\)")


def _display_width(text: str) -> int:
    """画面上の幅。日本語は半角2つ分として数える（表の桁を揃えるため）。"""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _split_row(line: str) -> list[str]:
    """表の1行をセルに割る。装飾記号は桁がずれるので落とす。"""
    body = line.strip().strip("|")
    cells = []
    for cell in body.split("|"):
        cell = cell.strip()
        cell = RE_INLINE_RENDER.sub(
            lambda m: next((g for g in m.groups()[:6] if g is not None), ""), cell)
        cells.append(cell)
    return cells


def table_lines(rows: list[list[str]], widths: list[int], width_of) -> list[str]:
    """表の各行を組む。**全行の幅が揃うこと**が要件。

    罫線にASCIIを使うのは趣味ではなく必要から。`─` `│` `┼` はUnicode上
    「曖昧幅（Ambiguous）」で半角に数えられるのに、日本語フォントでは全角で
    描かれる。数えた幅と描かれる幅が食い違い、桁が合わなくなる。
    ASCIIなら数えた通りに描かれる。

    見出し行を太字にもしない。Tkは太字を合成で作るため字幅が変わり
    （実測: 「項目」が32px→34px）、それだけで桁がずれる。
    """
    columns = len(widths)

    def row_line(row: list[str]) -> str:
        cells = []
        for i in range(columns):
            cell = row[i] if i < len(row) else ""
            cells.append(cell + " " * max(0, widths[i] - width_of(cell)))
        return "| " + " | ".join(cells) + " |"

    lines = [row_line(rows[0]),
             "|-" + "-|-".join("-" * width for width in widths) + "-|"]
    lines.extend(row_line(row) for row in rows[1:])
    return lines


def mark_markdown_syntax(text: str, mask: bytearray) -> None:
    """記法として使われている文字を数えない印にする。"""
    def clear(start: int, end: int) -> None:
        for i in range(max(0, start), min(end, len(mask))):
            mask[i] = 0

    pos = 0
    in_fence = False
    for line in text.split("\n"):
        start = pos
        pos += len(line) + 1

        if RE_FENCE.match(line):
            in_fence = not in_fence
            clear(start, start + len(line))
            continue
        if in_fence:
            continue
        if RE_HR.match(line):
            clear(start, start + len(line))
            continue

        for pattern in (RE_HEAD_PREFIX, RE_QUOTE_PREFIX, RE_LIST_PREFIX):
            found = pattern.match(line)
            if found:
                clear(start, start + len(found.group(1)))

        if RE_TABLE_ROW.match(line):
            if RE_TABLE_SEP.match(line):
                clear(start, start + len(line))
            else:
                for i, char in enumerate(line):
                    if char == "|":
                        mask[start + i] = 0

    for pattern in RE_INLINE:
        for found in pattern.finditer(text):
            clear(found.start(), found.end())

    # [表示文字](URL) は表示文字だけ数える。画像は丸ごと落とす。
    for found in RE_LINK.finditer(text):
        if found.group(1):
            clear(found.start(), found.end())
            continue
        clear(found.start(), found.start() + 1)
        close = found.start() + 1 + len(found.group(2))
        clear(close, found.end())


def build_mask(text: str, strip_markdown: bool, include_whitespace: bool) -> bytearray:
    mask = bytearray(b"\x01" * len(text))
    if strip_markdown:
        mark_markdown_syntax(text, mask)
    if not include_whitespace:
        for i, char in enumerate(text):
            if char.isspace():
                mask[i] = 0
    return mask


def analyze(text: str, limit: int, strip_markdown: bool,
            include_whitespace: bool) -> tuple[int, int]:
    """(数えた文字数, 上限を超え始める位置) を返す。"""
    mask = build_mask(text, strip_markdown, include_whitespace)
    total = 0
    split_index = len(text)
    for i in range(len(text)):
        if not mask[i]:
            continue
        total += 1
        if total == limit:
            split_index = i + 1
    if limit <= 0:
        split_index = 0
    elif total <= limit:
        split_index = len(text)
    return total, split_index


# --- 文書 -------------------------------------------------------------------


@dataclass
class Doc:
    title: str = "無題"
    text: str = ""
    path: str = ""
    limit: int = 800
    include_whitespace: bool = True
    strip_markdown: bool = False
    crlf: bool = False          # 元の改行を覚えておき、黙って変えない
    dirty: bool = False

    def label(self) -> str:
        name = Path(self.path).name if self.path else self.title
        return ("*" if self.dirty else "") + name


def home() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "croco-editor"


def session_path() -> Path:
    return home() / "session.json"


def instance_path() -> Path:
    return home() / "instance.json"


def read_file(path: Path) -> tuple[str, bool]:
    """(本文, 元がCRLFか) を返す。日本語のテキストは utf-8 とは限らない。"""
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-8", "cp932"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")
    return text.replace("\r\n", "\n"), "\r\n" in text


def write_file(path: Path, text: str, crlf: bool) -> None:
    """書いてから差し替える。途中で落ちても元のファイルを壊さない。"""
    if crlf:
        text = text.replace("\n", "\r\n")
    temp = path.with_name(path.name + ".croco-tmp")
    temp.write_bytes(text.encode("utf-8"))
    os.replace(temp, path)


# --- 既に開いている窓へ渡す（単一インスタンス）------------------------------


def _process_alive(pid: int) -> bool:
    if not pid or os.name != "nt":
        return bool(pid)
    import ctypes
    from ctypes import wintypes
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    code = wintypes.DWORD()
    ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(handle)
    return bool(ok) and code.value == 259  # STILL_ACTIVE


def hand_off(target: Path | None) -> bool:
    """既に動いている窓があれば開かせて True。無ければ False。

    **接続で生存を確かめる前にPIDを見る。** この環境では誰も居ないポートへの
    接続が拒否されずタイムアウトまで待たされる（Nortonが拒否応答を握り潰す）。
    """
    try:
        info = json.loads(instance_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not _process_alive(int(info.get("pid") or 0)):
        return False
    try:
        with socket.create_connection(("127.0.0.1", int(info["port"])), timeout=0.5) as sock:
            sock.sendall((str(target) if target else "").encode("utf-8"))
        return True
    except OSError:
        return False


class Listener:
    """他のプロセスから「これを開いて」を受け取る口。"""

    def __init__(self, on_open) -> None:
        self.on_open = on_open
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))   # 空きポートでよい。固定する理由が無い
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        home().mkdir(parents=True, exist_ok=True)
        instance_path().write_text(
            json.dumps({"pid": os.getpid(), "port": self.port}), encoding="utf-8")
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                data = conn.recv(4096).decode("utf-8", errors="replace").strip()
            self.on_open(data)


# --- 画面 -------------------------------------------------------------------


APP_ID = "croco.draft-editor"


def _claim_app_identity() -> None:
    """Windowsに自分のアプリだと名乗る。**窓を作る前に呼ぶこと。**

    タスクバーは AppUserModelID でアプリを識別する。pythonw で起動している
    以上、既定ではPythonのIDを引き継ぐので、ウィンドウのアイコンを差し替えても
    **タスクバーだけPythonのアイコンのまま**になる。
    """
    if os.name != "nt":
        return
    import ctypes
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except (AttributeError, OSError):
        pass


class EditorApp:
    def __init__(self, first: Path | None) -> None:
        _claim_app_identity()
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        # ターミナルと同じ画面に出す。二画面なので別の画面に出ると気づけない
        rect = screen.editor_rect()
        self.root.geometry("%dx%d+%d+%d" % (rect[2], rect[3], rect[0], rect[1])
                           if rect else "1000x680")
        icon = Path(__file__).resolve().parent.parent / "assets" / "editor.ico"
        if icon.is_file():
            try:
                self.root.iconbitmap(default=str(icon))
            except tk.TclError:
                pass   # アイコンが読めなくても開けなくなる理由は無い
        self.docs: list[Doc] = []
        self.pending: list[str] = []
        self.redraw_job: str | None = None
        self.link_targets: dict[str, str] = {}

        self._build_fonts()
        self._build_menu()
        self._build_toolbar()
        self._build_panes()
        self._build_status()
        self._bind_keys()

        self._restore_session()
        if first is not None:
            self.open_path(first)
        if not self.docs:
            self.new_doc()

        self.listener = Listener(self.pending.append)
        self.root.after(150, self._drain_pending)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

    # --- 組み立て ---------------------------------------------------------
    def _build_fonts(self) -> None:
        self.body_font = tkfont.Font(family="Yu Gothic UI", size=11)
        # 表の桁を揃えるには、**日本語も半角の丁度2倍になる**等幅が要る。
        # Consolas は日本語を持たず別フォントに落ちるため 1.71倍になり、表が崩れた。
        # ＭＳ ゴシックなら罫線も丸数字も 2.00倍で揃う（実測して選んでいる）。
        families = set(tkfont.families())
        mono = next((f for f in ("ＭＳ ゴシック", "MS Gothic", "Consolas")
                     if f in families), "Courier New")
        self.mono_font = tkfont.Font(family=mono, size=11)
        self.head_fonts = [
            tkfont.Font(family="Yu Gothic UI", size=size, weight="bold")
            for size in (18, 15, 13, 12, 11, 11)
        ]
        self.mono_bold_font = tkfont.Font(family=mono, size=11, weight="bold")
        self.mono_unit = max(1, self.mono_font.measure(" "))
        self.bold_font = tkfont.Font(family="Yu Gothic UI", size=11, weight="bold")
        self.italic_font = tkfont.Font(family="Yu Gothic UI", size=11, slant="italic")

    def _build_menu(self) -> None:
        bar = tk.Menu(self.root)
        file_menu = tk.Menu(bar, tearoff=0)
        file_menu.add_command(label="新規タブ　Ctrl+N", command=self.new_doc)
        file_menu.add_command(label="開く　Ctrl+O", command=self.open_dialog)
        file_menu.add_command(label="保存　Ctrl+S", command=self.save)
        file_menu.add_command(label="名前を付けて保存　Ctrl+Shift+S",
                              command=lambda: self.save(force_new=True))
        file_menu.add_separator()
        file_menu.add_command(label="タブを閉じる　Ctrl+W", command=self.close_tab)
        file_menu.add_command(label="終了", command=self.quit)
        bar.add_cascade(label="ファイル", menu=file_menu)

        edit_menu = tk.Menu(bar, tearoff=0)
        edit_menu.add_command(label="元に戻す　Ctrl+Z",
                              command=lambda: self._edit_event("<<Undo>>"))
        edit_menu.add_command(label="やり直し　Ctrl+Y",
                              command=lambda: self._edit_event("<<Redo>>"))
        edit_menu.add_separator()
        edit_menu.add_command(label="切り取り　Ctrl+X",
                              command=lambda: self._edit_event("<<Cut>>"))
        edit_menu.add_command(label="コピー　Ctrl+C",
                              command=lambda: self._edit_event("<<Copy>>"))
        edit_menu.add_command(label="貼り付け　Ctrl+V",
                              command=lambda: self._edit_event("<<Paste>>"))
        edit_menu.add_command(label="すべて選択　Ctrl+A", command=self.select_all)
        bar.add_cascade(label="編集", menu=edit_menu)

        view_menu = tk.Menu(bar, tearoff=0)
        self.show_preview = tk.BooleanVar(value=True)
        view_menu.add_checkbutton(label="プレビューを表示　Ctrl+P",
                                  variable=self.show_preview,
                                  command=self.toggle_preview)
        bar.add_cascade(label="表示", menu=view_menu)
        self.root.config(menu=bar)

    def _build_toolbar(self) -> None:
        bar = ttk.Frame(self.root, padding=(8, 6))
        bar.pack(fill="x")
        ttk.Label(bar, text="上限").pack(side="left")
        self.limit_var = tk.StringVar(value="800")
        entry = ttk.Entry(bar, textvariable=self.limit_var, width=6, justify="right")
        entry.pack(side="left", padx=(4, 2))
        self.limit_var.trace_add("write", lambda *_: self._on_limit_change())
        ttk.Label(bar, text="字").pack(side="left", padx=(0, 10))
        for number in PRESETS:
            ttk.Button(bar, text=str(number), width=5,
                       command=lambda n=number: self.limit_var.set(str(n))
                       ).pack(side="left", padx=1)

        self.include_ws = tk.BooleanVar(value=True)
        self.strip_md = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="空白も数える", variable=self.include_ws,
                        command=self._on_option_change).pack(side="left", padx=(14, 4))
        ttk.Checkbutton(bar, text="記法を数えない", variable=self.strip_md,
                        command=self._on_option_change).pack(side="left")

    def _build_panes(self) -> None:
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="x", padx=6)
        self.tabs.bind("<<NotebookTabChanged>>", lambda _e: self._on_tab_change())
        self.tabs.bind("<ButtonRelease-2>", self._on_middle_click)

        self.panes = ttk.PanedWindow(self.root, orient="horizontal")
        self.panes.pack(fill="both", expand=True, padx=6, pady=(4, 0))

        editor_frame = ttk.Frame(self.panes)
        self.editor = tk.Text(editor_frame, undo=True, wrap="word", relief="flat",
                              font=self.body_font, padx=10, pady=8,
                              maxundo=-1, autoseparators=True)
        scroll = ttk.Scrollbar(editor_frame, command=self.editor.yview)
        self.editor.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.editor.pack(side="left", fill="both", expand=True)
        self.panes.add(editor_frame, weight=3)

        preview_frame = ttk.Frame(self.panes)
        self.preview = tk.Text(preview_frame, wrap="word", relief="flat",
                               font=self.body_font, padx=10, pady=8,
                               state="disabled", background="#f7f7f5")
        pscroll = ttk.Scrollbar(preview_frame, command=self.preview.yview)
        self.preview.configure(yscrollcommand=pscroll.set)
        pscroll.pack(side="right", fill="y")
        self.preview.pack(side="left", fill="both", expand=True)
        self.preview_frame = preview_frame
        self.panes.add(preview_frame, weight=2)

        self.editor.tag_configure("over", background="#ffd9d9")
        for level, head_font in enumerate(self.head_fonts, start=1):
            self.preview.tag_configure(
                f"h{level}", font=head_font, foreground="#1b1b1b",
                spacing1=14 if level == 1 else 10, spacing3=6)
        self.preview.tag_configure("bold", font=self.bold_font)
        self.preview.tag_configure("italic", font=self.italic_font)
        self.preview.tag_configure("strike", overstrike=True)
        self.preview.tag_configure("code", font=self.mono_font, background="#eceae5")
        self.preview.tag_configure("block", font=self.mono_font, background="#f0eee9",
                                   lmargin1=16, lmargin2=16, spacing1=2, spacing3=2)
        self.preview.tag_configure("quote", foreground="#5a5a5a", lmargin1=18,
                                   lmargin2=18, background="#f0efec")
        self.preview.tag_configure("link", foreground="#1a5fb4", underline=True)
        self.preview.tag_configure("rule", foreground="#c4c0b8", justify="center",
                                   spacing1=8, spacing3=8)
        self.preview.tag_configure("table", font=self.mono_font, spacing2=0)
        self.preview.tag_configure("para", spacing2=3, spacing3=6)
        # 入れ子のリストは深さ分だけ下げる。折り返した2行目以降も揃える
        for depth in range(4):
            self.preview.tag_configure(
                f"list{depth}", lmargin1=14 + depth * 20, lmargin2=28 + depth * 20,
                spacing2=3)

        self.editor.bind("<<Modified>>", self._on_modified)

    def _build_status(self) -> None:
        bar = ttk.Frame(self.root, padding=(10, 4))
        bar.pack(fill="x")
        self.count_label = ttk.Label(bar, text="")
        self.count_label.pack(side="left")
        self.file_label = ttk.Label(bar, text="", foreground="#666666")
        self.file_label.pack(side="right")

    def _bind_keys(self) -> None:
        # Text の既定の割り当てを潰す必要があるので "break" を返す
        def bind(sequence, handler):
            self.root.bind_all(sequence, lambda e: (handler(), "break")[1])

        bind("<Control-n>", self.new_doc)
        bind("<Control-o>", self.open_dialog)
        bind("<Control-s>", self.save)
        bind("<Control-S>", lambda: self.save(force_new=True))
        bind("<Control-w>", self.close_tab)
        bind("<Control-a>", self.select_all)
        bind("<Control-p>", lambda: (self.show_preview.set(not self.show_preview.get()),
                                     self.toggle_preview()))
        bind("<Control-Tab>", lambda: self._cycle_tab(1))

    # --- 文書の出し入れ ---------------------------------------------------
    @property
    def doc(self) -> Doc:
        return self.docs[self.tabs.index("current")]

    def new_doc(self, doc: Doc | None = None) -> None:
        self.docs.append(doc or Doc())
        frame = ttk.Frame(self.tabs)
        self.tabs.add(frame, text=self.docs[-1].label())
        self.tabs.select(len(self.docs) - 1)

    def open_path(self, path: Path) -> None:
        path = path.resolve()
        for index, doc in enumerate(self.docs):
            if doc.path and Path(doc.path) == path:   # 開き直しはタブを増やさない
                self.tabs.select(index)
                return
        try:
            text, crlf = read_file(path)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"開けませんでした:\n{path}\n\n{exc}")
            return
        doc = Doc(title=path.stem, text=text, path=str(path), crlf=crlf,
                  strip_markdown=path.suffix.lower() in (".md", ".markdown"))
        current = self.docs and self.doc
        if current and not current.text and not current.path and not current.dirty:
            self.docs[self.tabs.index("current")] = doc   # 空の無題タブは使い回す
            self._load_into_view()
            self._refresh_tab_label()
        else:
            self.new_doc(doc)

    def open_dialog(self) -> None:
        name = filedialog.askopenfilename(
            title="開く",
            filetypes=[("テキスト / Markdown", "*.md *.markdown *.txt"),
                       ("すべて", "*.*")])
        if name:
            self.open_path(Path(name))

    def save(self, force_new: bool = False) -> None:
        doc = self.doc
        doc.text = self.editor.get("1.0", "end-1c")
        target = None if force_new or not doc.path else Path(doc.path)
        if target is None:
            name = filedialog.asksaveasfilename(
                title="名前を付けて保存",
                defaultextension=".md",
                initialfile=Path(doc.path).name if doc.path else doc.title + ".md",
                filetypes=[("Markdown", "*.md"), ("テキスト", "*.txt"),
                           ("すべて", "*.*")])
            if not name:
                return
            target = Path(name)
        try:
            write_file(target, doc.text, doc.crlf)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"保存できませんでした:\n{target}\n\n{exc}")
            return
        doc.path = str(target)
        doc.dirty = False
        self._refresh_tab_label()
        self._update_status()

    def close_tab(self) -> None:
        if not self.docs:
            return
        index = self.tabs.index("current")
        doc = self.docs[index]
        doc.text = self.editor.get("1.0", "end-1c")
        if doc.dirty and not self._confirm_discard(doc):
            return
        self.tabs.forget(index)
        del self.docs[index]
        if not self.docs:
            self.new_doc()
        else:
            self._load_into_view()

    def _confirm_discard(self, doc: Doc) -> bool:
        answer = messagebox.askyesnocancel(
            APP_NAME, f"「{doc.label().lstrip('*')}」の変更を保存しますか？")
        if answer is None:
            return False
        if answer:
            self.save()
            return not doc.dirty
        return True

    def quit(self) -> None:
        if self.docs:
            self.doc.text = self.editor.get("1.0", "end-1c")
        self._save_session()
        self.root.destroy()

    # --- 画面の更新 -------------------------------------------------------
    def _on_tab_change(self) -> None:
        if self.docs:
            self._load_into_view()

    def _load_into_view(self) -> None:
        doc = self.doc
        self.editor.edit_modified(False)
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", doc.text)
        self.editor.edit_reset()
        self.limit_var.set(str(doc.limit))
        self.include_ws.set(doc.include_whitespace)
        self.strip_md.set(doc.strip_markdown)
        self.editor.edit_modified(False)
        self._schedule_redraw(immediate=True)

    def _on_modified(self, _event) -> None:
        if not self.editor.edit_modified():
            return
        self.editor.edit_modified(False)
        if self.docs:
            self.doc.dirty = True
            self._refresh_tab_label()
        self._schedule_redraw()

    def _on_limit_change(self) -> None:
        if not self.docs:
            return
        try:
            self.doc.limit = max(0, int(self.limit_var.get() or 0))
        except ValueError:
            return
        self._schedule_redraw(immediate=True)

    def _on_option_change(self) -> None:
        if not self.docs:
            return
        self.doc.include_whitespace = self.include_ws.get()
        self.doc.strip_markdown = self.strip_md.get()
        self._schedule_redraw(immediate=True)

    def _schedule_redraw(self, immediate: bool = False) -> None:
        """打つたびに数え直すと重いので少し待つ。"""
        if self.redraw_job is not None:
            self.root.after_cancel(self.redraw_job)
        self.redraw_job = self.root.after(1 if immediate else 120, self._redraw)

    def _redraw(self) -> None:
        self.redraw_job = None
        if not self.docs:
            return
        doc = self.doc
        doc.text = self.editor.get("1.0", "end-1c")
        self._update_status()
        if self.show_preview.get():
            self._render_preview(doc.text)

    def _update_status(self) -> None:
        doc = self.doc
        total, split_index = analyze(doc.text, doc.limit, doc.strip_markdown,
                                     doc.include_whitespace)
        self.editor.tag_remove("over", "1.0", "end")
        if doc.limit > 0 and total > doc.limit:
            self.editor.tag_add("over", f"1.0 + {split_index} chars", "end")
            self.count_label.configure(
                text=f"{total} / {doc.limit} 字（{total - doc.limit} 字超過）",
                foreground="#c01c28")
        else:
            self.count_label.configure(
                text=f"{total} / {doc.limit} 字", foreground="")
        self.file_label.configure(
            text=(doc.path or "未保存") + ("（変更あり）" if doc.dirty else ""))

    def _refresh_tab_label(self) -> None:
        for index, doc in enumerate(self.docs):
            self.tabs.tab(index, text=doc.label())
        self.root.title(f"{self.doc.label().lstrip('*')} - {APP_NAME}")

    # --- プレビュー -------------------------------------------------------
    # md を「読む」のが主な用途なので、見出し・入れ子リスト・コードブロック・
    # 引用に加えて**表まで描く**。表は等幅フォントで桁を揃える方式にした。
    # tkinter に表組みの仕組みは無いが、日本語の幅を数えて詰めれば十分読める。

    def _render_preview(self, text: str) -> None:
        self.preview.configure(state="normal")
        for name in self.link_targets:      # 前回のリンクを持ち越さない
            self.preview.tag_delete(name)
        self.link_targets = {}
        self.preview.delete("1.0", "end")
        lines = text.split("\n")
        index = 0
        while index < len(lines):
            line = lines[index]

            if RE_FENCE.match(line):
                index = self._render_fence(lines, index)
                continue
            if RE_TABLE_ROW.match(line):
                consumed = self._render_table(lines, index)
                if consumed:
                    index += consumed
                    continue
            if RE_HR.match(line):
                self.preview.insert("end", "─" * 40 + "\n", ("rule",))
                index += 1
                continue

            head = RE_HEAD.match(line)
            if head:
                level = min(len(head.group(1)), len(self.head_fonts))
                self._insert_inline(head.group(2), (f"h{level}",))
                index += 1
                continue

            quote = RE_QUOTE_PREFIX.match(line)
            if quote:
                self._insert_inline("▌ " + line[len(quote.group(1)):], ("quote",))
                index += 1
                continue

            listed = RE_LIST_PREFIX.match(line)
            if listed:
                prefix = listed.group(1)
                depth = min((len(prefix) - len(prefix.lstrip())) // 2, 3)
                ordered = re.match(r"^\s*(\d+)[.)]", prefix)
                bullet = f"{ordered.group(1)}. " if ordered else ("・", "‐", "◦", "·")[depth]
                self.preview.insert("end", bullet, (f"list{depth}",))
                self._insert_inline(line[len(prefix):], (f"list{depth}",))
                index += 1
                continue

            if not line.strip():
                self.preview.insert("end", "\n")
            else:
                self._insert_inline(line, ("para",))
            index += 1
        self.preview.configure(state="disabled")

    def _cells_wide(self, text: str) -> int:
        """半角いくつ分か。**Unicodeの表ではなく実際のフォントで測る。**

        「曖昧幅」の文字（罫線、丸数字、矢印など）は、Unicodeの定義では
        半角扱いなのに日本語フォントでは全角で描かれる。表を揃えるには
        表示に使う当のフォントに聞くしかない。
        """
        return round(self.mono_font.measure(text) / self.mono_unit)

    def _pad_cell(self, text: str, width: int) -> str:
        return text + " " * max(0, width - self._cells_wide(text))

    def _render_fence(self, lines: list[str], start: int) -> int:
        """``` で囲まれた部分をそのまま出す。次に読む行番号を返す。"""
        index = start + 1
        body = []
        while index < len(lines) and not RE_FENCE.match(lines[index]):
            body.append(lines[index])
            index += 1
        self.preview.insert("end", "\n".join(body) + "\n", ("block",))
        return index + 1 if index < len(lines) else index

    def _render_table(self, lines: list[str], start: int) -> int:
        """表を等幅で桁を揃えて描く。表として読めなければ 0 を返す。"""
        rows = []
        index = start
        while index < len(lines) and RE_TABLE_ROW.match(lines[index]):
            rows.append(_split_row(lines[index]))
            index += 1
        if len(rows) < 2 or not RE_TABLE_SEP.match(lines[start + 1]):
            return 0
        content = [rows[0], *rows[2:]]          # 区切り行は組み直すので捨てる
        columns = max(len(row) for row in content)
        widths = [0] * columns
        for row in content:
            for i, cell in enumerate(row):
                widths[i] = max(widths[i], self._cells_wide(cell))
        for line in table_lines(content, widths, self._cells_wide):
            self.preview.insert("end", line + "\n", ("table",))
        self.preview.insert("end", "\n")
        return index - start

    def _insert_inline(self, line: str, base: tuple[str, ...]) -> None:
        """太字・斜体・打ち消し・コード・リンクを拾う。画像は表示名だけ残す。"""
        pos = 0
        for found in RE_INLINE_RENDER.finditer(line):
            if found.start() > pos:
                self.preview.insert("end", line[pos:found.start()], base)
            groups = found.groups()
            if groups[0] is not None or groups[1] is not None:
                self.preview.insert("end", groups[0] or groups[1], base + ("bold",))
            elif groups[2] is not None:
                self.preview.insert("end", groups[2], base + ("strike",))
            elif groups[3] is not None:
                self.preview.insert("end", groups[3], base + ("code",))
            elif groups[4] is not None:
                self.preview.insert("end", groups[4], base + ("italic",))
            else:
                self.preview.insert("end", groups[5] or groups[6],
                                    base + ("link", self._link_tag(groups[6])))
            pos = found.end()
        self.preview.insert("end", line[pos:] + "\n", base)

    def _link_tag(self, url: str) -> str:
        """リンク1つ分のタグを作る。押したら開けるようにするため個別に持つ。"""
        name = f"url{len(self.link_targets)}"
        self.link_targets[name] = url
        self.preview.tag_bind(name, "<Button-1>", lambda _e, u=url: self.open_link(u))
        self.preview.tag_bind(
            name, "<Enter>", lambda _e: self.preview.configure(cursor="hand2"))
        self.preview.tag_bind(
            name, "<Leave>", lambda _e: self.preview.configure(cursor=""))
        return name

    def open_link(self, url: str) -> None:
        """リンク先を開く。**同じフォルダのファイルなら新しいタブで開く。**

        md を読むのに使う以上、メモ同士のリンクをたどれる方が役に立つ。
        外部URLだけブラウザに投げる。
        """
        if re.match(r"^[a-z][a-z0-9+.-]*://", url, re.IGNORECASE):
            webbrowser.open(url)
            return
        base = Path(self.doc.path).parent if self.doc.path else Path.cwd()
        target = (base / url.split("#", 1)[0]).expanduser()
        if target.is_file():
            self.open_path(target)
        elif url.startswith("#"):
            pass          # 見出しへのリンクは対象外
        else:
            messagebox.showinfo(APP_NAME, f"開けませんでした:\n{url}")

    # --- 雑務 -------------------------------------------------------------
    def toggle_preview(self) -> None:
        if self.show_preview.get():
            self.panes.add(self.preview_frame, weight=2)
            self._schedule_redraw(immediate=True)
        else:
            self.panes.forget(self.preview_frame)

    def select_all(self) -> None:
        self.editor.tag_add("sel", "1.0", "end-1c")

    def _edit_event(self, event: str) -> None:
        self.editor.event_generate(event)

    def _cycle_tab(self, step: int) -> None:
        if self.docs:
            self.tabs.select((self.tabs.index("current") + step) % len(self.docs))

    def _on_middle_click(self, event) -> None:
        try:
            index = self.tabs.index(f"@{event.x},{event.y}")
        except tk.TclError:
            return
        self.tabs.select(index)
        self.close_tab()

    def _drain_pending(self) -> None:
        while self.pending:
            raw = self.pending.pop(0)
            if raw:
                self.open_path(Path(raw))
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        self.root.after(150, self._drain_pending)

    # --- 前回の続き -------------------------------------------------------
    def _restore_session(self) -> None:
        try:
            saved = json.loads(session_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for item in saved.get("docs", []):
            try:
                self.new_doc(Doc(**item))
            except TypeError:
                continue

    def _save_session(self) -> None:
        home().mkdir(parents=True, exist_ok=True)
        keep = [asdict(d) for d in self.docs if d.text.strip() or d.path]
        try:
            session_path().write_text(
                json.dumps({"docs": keep}, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def run(self) -> None:
        self._load_into_view()
        self._refresh_tab_label()
        self.root.mainloop()


def main(argv: list[str]) -> int:
    target = Path(argv[1]).expanduser() if len(argv) > 1 else None
    if target is not None and not target.is_file():
        tk.Tk().withdraw()
        messagebox.showerror(APP_NAME, f"ファイルが見つかりません:\n{target}")
        return 1
    if hand_off(target):
        return 0   # 既に開いている窓に渡した
    EditorApp(target).run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
