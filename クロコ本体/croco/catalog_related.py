"""台帳(catalog.db)のフォルダloreから、関連しそうな既存フォルダを見つけてプロンプトに差し込む。

`related.py` がNotion Inbox DB内のアイテム同士の関連を扱うのに対し、こちらは
クロコ管轄プロジェクト配下のトップレベルフォルダ（`catalog_db.folder_lore_index()`が
返す、README.mdにloreを持つフォルダ）を対象にする。判断はGeminiに任せる
（2026-09-17、本人の設計：「すべてのフォルダにREADME以下の情報の索引用の情報を
それこそ台帳でフォルダのloreとして持たせてそれをGeminiに読み込ませればいい。
その上で関連性を判断してフォルダなし具体ファイル名などを挙げればいい」）。

失敗しても本体のdispatch/consultを巻き込まない（related.pyと同じ方針）。
"""

from __future__ import annotations

import catalog_db

from . import log
from .gemini import Gemini


def find_candidates(
    gemini: Gemini, *, current_title: str, current_body: str
) -> list[str]:
    """関連しそうな既存フォルダ名を返す。

    候補は台帳上のトップレベルフォルダ全件のうち、loreが設定されているものだけ
    （未設定のフォルダは判定材料が無く、渡しても無意味なため）。
    """
    try:
        entries = catalog_db.folder_lore_index()
        candidates = [{"folder": folder, "lore": lore} for folder, lore in entries if lore]
        if not candidates:
            return []
        return gemini.find_related_folders(current_title, current_body, candidates)
    except Exception as exc:
        log.warn(f"台帳フォルダの関連判定に失敗しました（無視して続行します）: {exc}")
        return []


def render_section(folders: list[str]) -> str:
    """プロンプトに差し込む「関連しそうな既存フォルダ（台帳）」セクションを組み立てる。"""
    if not folders:
        return ""
    lines = [
        "## 関連しそうな既存フォルダ（台帳、参考）",
        "気づいた場合のみ使ってください。関連が薄いと感じたら無視して構いません。",
        "`python catalog_cli.py index` で一覧、`python catalog_cli.py show \"<README.mdの絶対パス>\"` で詳細を読めます。",
    ]
    for folder in folders:
        lines.append(f"- {folder}")
    return "\n".join(lines) + "\n"
