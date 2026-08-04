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

# 保留理由が空のもの。Notionに保存される値ではなく、表示上の受け皿。
UNRECORDED = "（理由の記録なし）"
UNRECORDED_ACTION = "中身を見て判断（保留理由を記録する前に溜まったもの）"


def show(
    config: Config, *, run_items: int = 0, run_tokens: int = 0
) -> list[inbox.InboxItem] | None:
    """Inbox DBの現状をまとめて出し、読み取った全アイテムを返す。

    返り値は、この直後の相談フェーズが同じ内容を取り直さずに済むようにするため。
    失敗しても本処理は既に終わっているので黙って諦める。
    """
    try:
        client = nt.Notion(config.notion_token, config.notion_version)
        data_source_id = config.inbox_data_source_id or client.resolve_data_source_id(
            config.inbox_database_id
        )
        items = [inbox.InboxItem(page) for page in client.query_data_source(data_source_id)]
    except Exception as exc:
        log.warn(f"まとめの取得に失敗しました（処理自体は完了しています）: {exc}")
        return None

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
        _show_review(review)

    doing = [item for item in items if item.status == inbox.STATUS_DOING]
    if doing:
        log.log("")
        log.log(f"■ 途中のものが {len(doing)} 件あります（次回の起動で再開されます）")
        for item in doing:
            log.log(f"  ・{item.title}")

    spent = [item.tokens for item in items if item.tokens > 0]
    if run_items or spent:
        log.log("")
        log.log("■ トークンの実績")
    if run_items:
        log.log(f"  今回の起動        {usage.compact(run_tokens)}（{run_items}件分）")
    if spent:
        average = sum(spent) // len(spent)
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
    if run_items or spent:
        log.log("  ※ プランの残量そのものは取得できない。セッション内の /usage で見ること")

    log.log("=" * 60)
    return items


def _show_review(review: list[inbox.InboxItem]) -> None:
    """「要確認」を保留理由ごとに束ねて出す。

    1件ずつ並べると、見るたびに「これは何で止まってるんだっけ」を
    1件ずつ思い出すことになる。理由が同じものは対処も同じなので、
    束ねて「で、何をすればいいのか」を1行付ける方が読む側は安い。

    グループ内の並び順・優先度ラベルは `croco_cli.py list` と同じ基準
    （`inbox.priority_sort_key` / `inbox.priority_label`）を使う
    （2026-08-01、当初list専用だったのを本人の指摘で共通化）。
    """
    grouped: dict[str, list[inbox.InboxItem]] = {}
    for item in review:
        # 理由が空のものを既知の理由に混ぜない。保留理由を記録する前に溜まったものや、
        # Notion上で手で「要確認」にしたものがここに来る。
        # 分からないものに嘘のラベルを貼ると、束ねる意味がなくなる。
        grouped.setdefault(item.hold_reason or UNRECORDED, []).append(item)

    # 既知の理由を決めた順で出し、知らない理由は最後に回す。
    order = [r for r in inbox.HOLD_ACTIONS if r in grouped]
    order += [r for r in grouped if r not in inbox.HOLD_ACTIONS]

    for reason in order:
        group = sorted(grouped[reason], key=inbox.priority_sort_key)
        action = inbox.HOLD_ACTIONS.get(reason) or (
            UNRECORDED_ACTION if reason == UNRECORDED else ""
        )
        head = f"  【{reason}】{len(group)}件"
        log.log(f"{head} … {action}" if action else head)
        for item in group:
            tag = inbox.compact_tag(item)
            tag_block = f"[{tag}] " if tag else ""
            log.log(f"    ・{tag_block}{item.title}")
            note = _last_log_entry(item.result_log)
            if note:
                log.log(f"        {note}")


def _last_log_entry(result_log: str) -> str:
    """進捗ログの最後の1件だけを取り出す。全部出すと画面が埋まるため。"""
    entries = [line.strip() for line in (result_log or "").splitlines() if line.strip()]
    if not entries:
        return ""
    last = entries[-1]
    return last[:160] + ("…" if len(last) > 160 else "")
