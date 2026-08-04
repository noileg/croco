"""アイテムをジャンル/プロジェクト単位に束ねる。

Inboxが増えると、受験関連とクロコ本体改修と雑多なアイデアが一列に並んで
見分けがつかなくなる（2026-08-01、本人の指摘）。優先度と違い「どの束に
属するか」は価値判断ではなく機械的な仕分けなので、種別と同じくGeminiに
任せる（related.py・dedupe.pyと同種の、狭く機械的な判定）。

既存ジャンルと同じものは同じ表記で束ねる。表記ゆれで似た束が増殖しないよう、
毎回すでに使われているジャンル一覧をGeminiへ渡し、実質同じなら既存の表記を
そのまま使わせる。
"""

from __future__ import annotations

from . import inbox, log
from . import notion as nt
from .config import Config
from .gemini import Gemini


def existing_genres(items: list[inbox.InboxItem]) -> list[str]:
    """現在使われているジャンルの重複無し一覧。"""
    seen: list[str] = []
    for item in items:
        g = (item.genre or "").strip()
        if g and g not in seen:
            seen.append(g)
    return seen


def assign_for_capture(
    client: nt.Notion, gemini: Gemini, *, data_source_id: str, title: str, body: str
) -> str:
    """捕捉フェーズの新規アイテム1件のジャンルを判定する。失敗時は空文字列。

    候補（既存ジャンル一覧）は都度Inbox全件から作り直す（dedupe.pyと同じ方式）。
    同じ捕捉セッション内で複数アイテムを処理する場合、直前に決めた新しい
    ジャンルを次のアイテムが再利用できるようにするため。
    """
    try:
        pages = client.query_data_source(data_source_id)
        items = [inbox.InboxItem(p) for p in pages]
        genre = gemini.assign_genre(title, body, existing_genres(items))
    except Exception as exc:
        log.warn(f"ジャンル判定に失敗しました（未分類のまま続行します）: {exc}")
        return ""
    return genre.strip()


def backfill(client: nt.Notion, gemini: Gemini, config: Config) -> int:
    """ジャンル未設定の既存アイテムへ一括で割り振る。手動で叩く棚卸し用。

    **1回のAPI呼び出しで全対象をまとめて判定する。** 1件ずつ呼ぶと件数分だけ
    リクエストを消費し、Gemini無料枠の日次上限（実測20件/日、2026-08-01）に
    即座に当たるため（本人の指摘で修正。当初は1件ずつ呼んでいて64件中23件で
    枠を使い切った）。本文取得（Notion側）は件数分だけ必要だが、これは
    無料枠の制約を受けない。
    """
    data_source_id = config.inbox_data_source_id or client.resolve_data_source_id(
        config.inbox_database_id
    )
    pages = client.query_data_source(data_source_id)
    items = [inbox.InboxItem(p) for p in pages]
    targets = [item for item in items if not (item.genre or "").strip()]
    if not targets:
        return 0

    existing = existing_genres(items)
    payload = [
        {"id": item.id, "title": item.title, "body": client.get_page_text(item.id)}
        for item in targets
    ]
    try:
        assignments = gemini.assign_genres_batch(payload, existing)
    except Exception as exc:
        log.warn(f"ジャンル一括判定に失敗しました（未分類のまま終了します）: {exc}")
        return 0

    updated = 0
    for item in targets:
        genre = assignments.get(item.id)
        if not genre:
            log.warn(f"[{item.title}] ジャンルが返ってきませんでした（未分類のまま）")
            continue
        client.update_page(item.id, {inbox.P_GENRE: {"rich_text": nt.rich_text(genre)}})
        updated += 1
        log.log(f"  [{genre}] {item.title}")
    return updated
