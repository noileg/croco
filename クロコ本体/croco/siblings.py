"""同じ「由来メモ」（分割元の生ログ）を持つ兄弟アイテムを、機械的に見つけてプロンプトに差し込む。

`related.py`（Gemini判定）と違い、こちらは判定が要らない。同じ生ログから割れた
アイテムは `croco/inbox.py` の `P_ORIGIN` に分割元ページIDが入っており、文字列一致
だけで確実に束ねられる（`croco_cli.py` の `_cascade_done_to_siblings` が完了の
連動に使っているのと同じ根拠。ここではプロンプトへの提示に使う）。

Gemini呼び出しを増やさずに済む分、まずこちらで拾えるものを拾う
（2026-09-18、本人指摘：「分割されたものは元になったメモ由来で機械的に
結合して表示するようになってるはずなんだけど」への対応。当時は完了の連動にしか
使われておらず、プロンプトへの提示はしていなかったため新設した）。
"""

from __future__ import annotations

import re

from . import inbox, log
from . import notion as nt
from .config import Config

ORIGIN_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?"
    r"[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}"
)


def origin_key(origin: str) -> str:
    """「由来メモ」文字列から、突き合わせ用のページIDを取り出して正規化する。

    値は `タイトル (ページID)` 形式だが、一致判定に使うのはIDだけ
    （タイトルは読みやすさ用の飾り）。IDが見当たらなければ空文字を返し、
    呼び出し側はカスケードしない（この改修より前の既存アイテムや、手書きの
    由来メモで、無関係なものを誤って巻き込むのを防ぐ。croco_cli.pyの
    `_cascade_done_to_siblings`と同じ判断）。
    """
    match = ORIGIN_ID_RE.search(origin or "")
    return match.group(0).replace("-", "").lower() if match else ""


def find_siblings(
    client: nt.Notion, config: Config, *, current_id: str, current_origin: str
) -> list[inbox.InboxItem]:
    """同じ由来メモを持つ、現在のアイテム以外の全アイテムを返す（完了含む）。

    判定はGeminiを使わない文字列一致のみ。失敗の余地はNotion API呼び出し
    くらいしかないが、related.py・catalog_related.pyと同じく失敗時は
    空リストで返し、本体のdispatch/consultを巻き込まない。
    """
    key = origin_key(current_origin)
    if not key:
        return []
    try:
        data_source_id = config.inbox_data_source_id or client.resolve_data_source_id(
            config.inbox_database_id
        )
        pages = client.query_data_source(data_source_id)
    except Exception as exc:
        log.warn(f"由来メモ兄弟の取得に失敗しました（無視して続行します）: {exc}")
        return []
    result = []
    for page in pages:
        item = inbox.InboxItem(page)
        if item.id == current_id:
            continue
        if origin_key(item.origin) != key:
            continue
        result.append(item)
    return result


def render_section(siblings: list[inbox.InboxItem]) -> str:
    """プロンプトに差し込む「同じメモから割れた兄弟アイテム」セクションを組み立てる。"""
    if not siblings:
        return ""
    lines = [
        "## 同じメモから割れた兄弟アイテム（機械的に検出、参考）",
        "同じ生ログを分割した際に生まれた別アイテムです。地続きの話の可能性が高いので、"
        "関連する内容なら踏まえてください。`python croco_cli.py show <id>` で中身を読めます。",
    ]
    for item in siblings:
        status_note = f"［{item.status}］" if item.status != inbox.STATUS_TODO else ""
        lines.append(f"- {item.id} {status_note}{item.title}")
    return "\n".join(lines) + "\n"
