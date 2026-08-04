"""捕捉フェーズで、同じ「予定」を二重登録しないための重複判定。

「まとめノート」（元メモを見やすく統合したもの）を作ると、既に登録済みの
予定が別の言い回しでもう一度Geminiに拾われることがある
（実例：2026-07-31、筑波大学AC入試の日程が5組重複していた）。
逐語転記の原則は崩さず、「これは同じ予定か」だけをGeminiに判定させる
（related.pyと同じ、狭く機械的な判定）。

迷ったら重複扱いにしない。誤って結合すると本文が混ざって後から分離できなくなる
（別々に作る誤りは手で気づいて片付けられるが、誤結合はそうはいかない）。
"""

from __future__ import annotations

from . import inbox, log
from . import notion as nt
from .config import Config
from .gemini import Gemini


def find_duplicate(
    client: nt.Notion,
    gemini: Gemini,
    config: Config,
    *,
    data_source_id: str,
    title: str,
    body: str,
    scheduled_date: str,
) -> inbox.InboxItem | None:
    """同一の予定だと高い確信で判定できた既存アイテムを返す。無ければNone。

    候補は既存の「予定」全件（日付での事前絞り込みはしない。Notion側の
    date一致フィルタは表記ゆれに弱いため、Geminiに文脈ごと渡して判断させる）。
    失敗（Notion・Gemini側のどちらでも）した場合は重複なしとして扱い、
    通常どおり新規登録させる（この機能が無くても捕捉自体は成立していたため、
    ここで本体（登録の可否）を巻き込む理由がない）。
    """
    if not scheduled_date:
        return None
    try:
        pages = client.query_data_source(
            data_source_id,
            filter_={"property": inbox.P_KIND, "select": {"equals": inbox.KIND_SCHEDULE}},
        )
        items = [inbox.InboxItem(p) for p in pages]
        if not items:
            return None
        candidates = [
            {
                "id": item.id,
                "title": item.title,
                "scheduled": item.scheduled,
                "body": client.get_page_text(item.id),
            }
            for item in items
        ]
        duplicate_id = gemini.find_duplicate_schedule(title, body, scheduled_date, candidates)
    except Exception as exc:
        log.warn(f"重複判定に失敗しました（無視して新規登録します）: {exc}")
        return None

    if not duplicate_id:
        return None
    by_id = {item.id: item for item in items}
    return by_id.get(duplicate_id)


def merge_into(client: nt.Notion, existing: inbox.InboxItem, *, new_title: str, new_body: str) -> None:
    """新しい予定の本文を、既存アイテムへ逐語で結合する（要約はしない）。"""
    note = f"---\n（統合: 重複と判定された「{new_title}」の記載をここへ統合）\n{new_body}"
    client.append_blocks(existing.id, nt.paragraph_blocks(note))

    stamp = inbox.now_iso()[:16].replace("T", " ")
    entry = f"[{stamp}] 捕捉フェーズが重複と判定し、新規登録の代わりにここへ統合しました。"
    updated_log = f"{existing.result_log}\n{entry}".strip() if existing.result_log else entry
    client.update_page(existing.id, {inbox.P_RESULT: {"rich_text": nt.rich_text(updated_log)}})
