"""アイテムをジャンル/プロジェクト単位に束ねる。

Inboxが増えると、受験関連とクロコ本体改修と雑多なアイデアが一列に並んで
見分けがつかなくなる（2026-08-01、本人の指摘）。優先度と違い「どの束に
属するか」は価値判断ではなく機械的な仕分けなので、種別と同じくGeminiに
任せる（catalog_related.py・dedupe.pyと同種の、狭く機械的な判定）。

**ジャンルは受験・エディタ・アプリ開発・クロコ本体の4区分に固定する**
（2026-09-05、本人の判断）。Geminiはこの中から選ぶだけで、当てはまらなければ
未分類（空文字列）のまま通す。新規作成はさせない。

当初は「既存Inbox実データから機械収集した一覧」を候補にしていたが、
過去の誤判定で生まれた粒度違いのジャンル（「YouTube DLエクステンション」
「Windows環境設定」等、個々のアプリ企画ごとの細分化）がそのまま
「選んでいい候補」として渡り続け、新規作成を禁止しただけでは絞ったことに
ならなかった（実地でクロコ本体46件中30件・その他4ジャンル22件が誤分類）。
本人の棚卸しで4区分に統合し、以後はこの固定リストだけを候補にする。
区分を増減したいときは `ALLOWED_GENRES` を直接編集する（本人だけが行う）。
"""

from __future__ import annotations

from . import inbox, log
from .config import Config
from . import notion as nt
from .gemini import Gemini

# Geminiに見せてよい既存ジャンル一覧。固定4区分（上のdocstring参照）。
ALLOWED_GENRES = ["受験", "エディタ", "アプリ開発", "クロコ本体"]


def assign_for_capture_batch(gemini: Gemini, items: list[dict]) -> list[str]:
    """1つの生ログから分割された全アイテムのジャンルを1回でまとめて判定する。

    分割後のアイテムを1件ずつ判定すると、同じ生ログ由来の話題でも判定が
    ブレて別ジャンルに割れる不具合が実地で頻発した（2026-09-05、本人の指摘）。
    1回のAPI呼び出しで全部まとめて見せることで、同じ話題を同じジャンルへ
    寄せやすくする（assign_genres_batchの発想を捕捉フェーズにも適用）。
    失敗時・未分類判定時はそのアイテムだけ空文字列にする。
    """
    if not items:
        return []
    try:
        payload = [
            {"id": str(i), "title": item["title"], "body": item["body"]}
            for i, item in enumerate(items)
        ]
        assignments = gemini.assign_genres_batch(payload, ALLOWED_GENRES)
    except Exception as exc:
        log.warn(f"ジャンル判定に失敗しました（未分類のまま続行します）: {exc}")
        return [""] * len(items)
    return [assignments.get(str(i), "") for i in range(len(items))]


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

    payload = [
        {"id": item.id, "title": item.title, "body": client.get_page_text(item.id)}
        for item in targets
    ]
    try:
        assignments = gemini.assign_genres_batch(payload, ALLOWED_GENRES)
    except Exception as exc:
        log.warn(f"ジャンル一括判定に失敗しました（未分類のまま終了します）: {exc}")
        return 0

    updated = 0
    for item in targets:
        genre = assignments.get(item.id)
        if not genre:
            # 既存ジャンルに実質同じ括りが無い＝未分類は正常な判定結果
            # （新規ジャンル作成は本人の判断のみのため、2026-09-05）。
            log.log(f"  [未分類] {item.title}")
            continue
        client.update_page(item.id, {inbox.P_GENRE: {"rich_text": nt.rich_text(genre)}})
        updated += 1
        log.log(f"  [{genre}] {item.title}")
    return updated
