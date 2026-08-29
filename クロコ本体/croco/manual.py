"""手動モード（`--manual`）：Inbox を一覧表示して、選んだ1件をクロコに渡す。

クロコは基本つねに動いているもの（スタートアップ起動＋裏で実装）なので、
「今この1件をやってほしい」を割り込ませる口が要る。それがこれ。
Ctrl+Alt+K（`launcher/croco_manual.ahk`）から起動する想定。

通常実行（run_croco.py 引数なし）との違い:

- 捕捉・期限切れチェック・現状書き出し・相談をやらない。Inbox を見て渡すだけ。
- 二重起動ロック（croco/lock.py）を取らない・見ない。通常実行と並行で動かす前提。
- 自動判断をしない。時間切れの自動着手も、残り1件の即決もない。
  終了は Ctrl+C（「何もせず抜ける」ための入力は用意しない）。
- 試行回数を増やさない。手で選んで渡すのは無人リトライではないため
  （仕様書4章「複数日にまたがる項目が試行回数上限を誤検知する」の手動経路での回避）。
- 「要確認」のアイテムはステータスを変えない。ここで「処理中」に倒すと、
  次の無人実行の dispatch が拾ってしまう（クロコ自身の改修などを無人で
  触りにいく事故になる）。渡すが、状態は「要確認」のまま。

一覧は Inbox（未処理・処理中）。`0` + Enter で「要確認」の一覧に移る。

既知の制限：使用トークンの実測（croco/usage.py、cwd と時刻で拾う）は、
通常実行と同時に走っている間はお互いのセッション分が混ざりうる。
手動での並行実行を許すと決めた以上ここは許容し、トークン列は概算とみなす。
"""

from __future__ import annotations

import time

from . import inbox, log
from . import notion as nt
from .config import Config
from .dispatch import (
    _build_prompt,
    _mark_started,
    _record_usage,
    launch_claude,
    open_editor,
)
from .gemini import Gemini


def run(config: Config) -> int:
    client = nt.Notion(config.notion_token, config.notion_version)
    gemini = Gemini(
        config.gemini_api_key,
        model=config.gemini_model,
        thinking_level=config.gemini_thinking_level,
        temperature=config.gemini_temperature,
    )
    data_source_id = config.inbox_data_source_id or client.resolve_data_source_id(
        config.inbox_database_id
    )
    items = [inbox.InboxItem(page) for page in client.query_data_source(data_source_id)]

    inbox_targets = sorted(
        (
            it
            for it in items
            if it.status in (inbox.STATUS_TODO, inbox.STATUS_DOING)
            and it.kind not in inbox.NON_IMPLEMENTABLE_KINDS
        ),
        key=inbox.sort_key,
    )
    review_items = sorted(
        (it for it in items if it.status == inbox.STATUS_REVIEW),
        key=inbox.priority_sort_key,
    )

    item = _pick(inbox_targets, review_items)
    if item is None:
        return 0

    _hand_over(config, client, gemini, item)
    return 0


def _pick(
    inbox_targets: list[inbox.InboxItem], review_items: list[inbox.InboxItem]
) -> inbox.InboxItem | None:
    """一覧を出して1件選ばせる。時間制限なし。中断は Ctrl+C（KeyboardInterrupt）。"""
    showing_review = False
    while True:
        rows = review_items if showing_review else inbox_targets
        _print_list(rows, review=showing_review)
        if showing_review:
            prompt = "番号を入れてEnter（Ctrl+Cで終了）: "
        else:
            prompt = "番号を入れてEnter（0で要確認の一覧、Ctrl+Cで終了）: "
        try:
            text = input(prompt).strip()
        except EOFError:
            return None
        if not text:
            continue
        if not showing_review and text == "0":
            showing_review = True
            continue
        try:
            index = int(text)
        except ValueError:
            log.warn(f"番号として読めません（{text!r}）。")
            continue
        if not 1 <= index <= len(rows):
            log.warn(f"範囲外です（{index}）。")
            continue
        return rows[index - 1]


def _print_list(rows: list[inbox.InboxItem], *, review: bool) -> None:
    heading = "要確認" if review else "Inbox（未処理・処理中）"
    log.log("")
    log.log(f"=== {heading}: {len(rows)}件 ===")
    if not rows:
        log.log("  （なし）")
        return
    for index, it in enumerate(rows, 1):
        mark = "※再開" if it.status == inbox.STATUS_DOING else "　　　"
        reason = (
            f"［{it.hold_reason}］"
            if it.hold_reason not in ("", inbox.HOLD_NONE)
            else ""
        )
        spent = f"（これまで {it.tokens:,}トークン）" if it.tokens else ""
        log.log(f"  {index:2}) {mark} {reason}{it.title}{spent}")


def _hand_over(
    config: Config, client: nt.Notion, gemini: Gemini, item: inbox.InboxItem
) -> None:
    log.log("")
    log.log(f"渡します: [{item.status}] {item.title}")

    body = client.get_page_text(item.id)

    # 「要確認」はステータスを触らない（モジュールの docstring 参照）。
    # Inbox（未処理・処理中）のものだけ「処理中」に倒す。試行回数は増やさない。
    if item.status in (inbox.STATUS_TODO, inbox.STATUS_DOING):
        _mark_started(client, item, bump_attempts=False)
    open_editor(config, item)

    prompt = _build_prompt(
        config, item, body, client=client, gemini=gemini, manual=True
    )
    started_at = time.time()
    try:
        exit_code = launch_claude(config, prompt)
    except KeyboardInterrupt:
        log.warn("中断されました。")
        _record_usage(client, config, item, since=started_at)
        return
    _record_usage(client, config, item, since=started_at)
    log.log(f"クロコのセッションが終了しました (exit={exit_code})")
