"""終了時のまとめ表示。

「要確認」は本人の判断待ちだが、Notionを見に行かないと気づけない。
放置され続けるのが一番まずいので、実行の最後に必ず画面へ出す。
（通知の仕組みそのものは未着手：仕様書4章）
"""

from __future__ import annotations

from collections import Counter

from .config import Config
from . import inbox, log, usage
from . import notion as nt


def show(config: Config) -> None:
    """Inbox DBの現状をまとめて出す。失敗しても本処理は既に終わっているので黙って諦める。"""
    try:
        client = nt.Notion(config.notion_token, config.notion_version)
        data_source_id = config.inbox_data_source_id or client.resolve_data_source_id(
            config.inbox_database_id
        )
        items = [inbox.InboxItem(page) for page in client.query_data_source(data_source_id)]
    except Exception as exc:
        log.warn(f"まとめの取得に失敗しました（処理自体は完了しています）: {exc}")
        return

    counts = Counter(item.status for item in items)
    log.log("")
    log.log("=" * 60)
    log.log("Inbox の現状")
    order = [
        inbox.STATUS_DOING,
        inbox.STATUS_TODO,
        inbox.STATUS_REVIEW,
        inbox.STATUS_DONE,
        inbox.STATUS_EXCLUDED,
    ]
    for status in order:
        if counts.get(status):
            log.log(f"  {status:<6} {counts[status]:>3} 件")

    review = [item for item in items if item.status == inbox.STATUS_REVIEW]
    if review:
        log.log("")
        log.log(f"■ 確認してほしいものが {len(review)} 件あります")
        for item in review:
            log.log(f"  ・{item.title}")
            reason = _last_log_entry(item.result_log)
            if reason:
                log.log(f"      {reason}")

    doing = [item for item in items if item.status == inbox.STATUS_DOING]
    if doing:
        log.log("")
        log.log(f"■ 途中のものが {len(doing)} 件あります（次回の起動で再開されます）")
        for item in doing:
            log.log(f"  ・{item.title}")

    spent = [item.tokens for item in items if item.tokens > 0]
    if spent:
        average = sum(spent) // len(spent)
        log.log("")
        log.log("■ トークンの実績")
        log.log(f"  これまでの合計    {usage.compact(sum(spent))}（{len(spent)}件分）")
        log.log(f"  1件あたりの平均   {usage.compact(average)}")
        log.log(
            f"  最小 / 最大       {usage.compact(min(spent))} / {usage.compact(max(spent))}"
        )
        remaining = [
            item
            for item in items
            if item.status in (inbox.STATUS_TODO, inbox.STATUS_DOING)
            and item.kind not in inbox.NON_IMPLEMENTABLE_KINDS
        ]
        if remaining:
            log.log(
                f"  残り{len(remaining)}件を同じペースで進めると"
                f" 約{usage.compact(average * len(remaining))}"
            )
        log.log("  ※ プランの残量そのものは取得できない。セッション内の /usage で見ること")

    log.log("=" * 60)


def _last_log_entry(result_log: str) -> str:
    """進捗ログの最後の1件だけを取り出す。全部出すと画面が埋まるため。"""
    entries = [line.strip() for line in (result_log or "").splitlines() if line.strip()]
    if not entries:
        return ""
    last = entries[-1]
    return last[:160] + ("…" if len(last) > 160 else "")
