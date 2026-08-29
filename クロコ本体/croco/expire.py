"""滞留アイテムを検知し、そのまま消さずに再開を問う。

「立て込んでいると、必要なタスクでも新しいものに押し出されて後回しになり続ける」
という懸念への対応（2026-08-12、本人指摘）。単純に「別の場所へ送る」と
静かに消えたのと区別がつかなくなるため、そうはしない。

  1. sweep()        : 未処理・処理中のまま `expire_days` を超えたものを
                       「期限切れ」に変える（Notionから消えも動きもしない）。
  2. offer_resume()  : 起動時に「期限切れ」の一覧を見せ、再開する番号を聞く。
                       選ばれたものだけ「未処理」に戻し、今回のdispatchで拾えるようにする。

経過日数の基準は `inbox.inactive_days()`（**最終更新**＝`last_edited_time`基準）。
当初は`inbox.elapsed_days()`（作成日時基準）を流用していたが、当日に何度も
セッションを重ねて実際に作業しているアイテムまで、作成が古いというだけで
即座に「期限切れ」と判定される不具合が実地で発覚した（2026-08-12、本人が
メンテ中に偶然クロコを起動して発見）。詳細は`inbox.inactive_days`のdocstring参照。
"""

from __future__ import annotations

import sys

from . import inbox, log, notify
from . import notion as nt
from .config import Config
from .dispatch import read_line

EXPIRED_FILTER = {"property": inbox.P_STATUS, "select": {"equals": inbox.STATUS_EXPIRED}}


def sweep(client: nt.Notion, config: Config, data_source_id: str) -> list[inbox.InboxItem]:
    """未処理・処理中のまま期限切れ判定になったものをステータスだけ変える。

    返り値は現在「期限切れ」の全アイテム（今回の分＋前回以前から残っている分）。
    """
    threshold = config.expire_days
    if threshold > 0:
        candidates = [
            inbox.InboxItem(page)
            for page in client.query_data_source(data_source_id, filter_=inbox.pending_filter())
        ]
        for item in candidates:
            days = inbox.inactive_days(item)
            if days is not None and days >= threshold:
                client.update_page(item.id, {inbox.P_STATUS: {"select": {"name": inbox.STATUS_EXPIRED}}})

    expired = [
        inbox.InboxItem(page)
        for page in client.query_data_source(data_source_id, filter_=EXPIRED_FILTER)
    ]
    return sorted(expired, key=inbox.priority_sort_key)


def offer_resume(
    client: nt.Notion, config: Config, expired: list[inbox.InboxItem]
) -> int:
    """「期限切れ」の一覧を見せ、再開する番号を聞く。「未処理」に戻した件数を返す。

    consult.offer と同じく、既定は「聞かない」。画面が無い・時間切れ・
    何も入力しない、のいずれでも黙って終わる（期限切れのまま残るだけ）。
    """
    if not expired:
        return 0
    if config.dry_run or not config.interactive:
        return 0
    if not (sys.stdin and sys.stdin.isatty()):
        return 0

    notify.waiting(config)
    log.log("")
    log.log(f"{len(expired)}件、{config.expire_days}日以上動きが無く「期限切れ」になっています。")
    for index, item in enumerate(expired, 1):
        tag = inbox.compact_tag(item)
        tag_block = f"[{tag}] " if tag else ""
        log.log(f"  {index:2}) {tag_block}{item.title}")
    log.log(
        f"再開する番号をスペース区切りで（{config.consult_timeout:.0f}秒、"
        "何もしなければ期限切れのまま）: "
    )

    answer = read_line(config.consult_timeout)
    if not answer:
        return 0

    resumed = 0
    for token in answer.split():
        try:
            index = int(token)
        except ValueError:
            continue
        if not 1 <= index <= len(expired):
            continue
        item = expired[index - 1]
        client.update_page(item.id, {inbox.P_STATUS: {"select": {"name": inbox.STATUS_TODO}}})
        log.log(f"  再開: {item.title}")
        resumed += 1
    return resumed


def run(config: Config) -> int:
    """検知から再開確認までを1回で行う。「未処理」に戻した件数を返す。"""
    client = nt.Notion(config.notion_token, config.notion_version)
    data_source_id = config.inbox_data_source_id or client.resolve_data_source_id(
        config.inbox_database_id
    )
    expired = sweep(client, config, data_source_id)
    return offer_resume(client, config, expired)
