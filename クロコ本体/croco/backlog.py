"""Notionに溜まった「クロコ自身の改修」を、開発側のセッションへ渡す。

クロコ本体を直すのはクロコ自身ではない。`クロコ本体` 配下は無人実行のクロコに
とって書き換え禁止で、これは外さない（自分を縛っている設定がそこにあるため）。
だから改修依頼は捕捉の時点で `保留理由=クロコ自身の改修` として「要確認」に
隔離され、**そこで止まったままになる**。

止まったものを実際に直すには、本人がNotionを開いて読み、Claude Code に
貼り直すことになる。その往復を無くすのがこのコマンド。

    python run_croco.py --backlog

**出力はファイルに書かず標準出力に出す。** 生成ファイルを置くと
(1) 公開リポジトリに載る (2) 実体（Notion）とすぐズレる が同時に起きる。
開発側のセッションはコマンドを叩けるのだから、必要になった瞬間に
取りに行けばよく、置いておく必要がない。

直したあとは `python croco_cli.py done <ページID> "..."` で閉じる。
書き戻しは既存の CLI がそのまま使えるので、ここには書き込み経路を持たせない。
"""

from __future__ import annotations

from . import inbox, log
from . import notion as nt
from .config import Config

# 開発側に渡す対象のステータス。「完了」「対象外」は済んだものなので出さない。
OPEN_STATUSES = frozenset(
    {inbox.STATUS_REVIEW, inbox.STATUS_TODO, inbox.STATUS_DOING}
)

SEPARATOR = "=" * 60


def select(items: list[inbox.InboxItem], *, all_reasons: bool) -> list[inbox.InboxItem]:
    """開発側に渡すアイテムを選ぶ。

    既定は「クロコ自身の改修」だけ。他の保留理由は本人がやること・本人が
    決めることであって、開発側のセッションが拾うと越権になる。
    全部見たいときのために `all_reasons` は残しておく。
    """
    picked = [item for item in items if item.status in OPEN_STATUSES]
    if not all_reasons:
        picked = [item for item in picked if item.hold_reason == inbox.HOLD_CROCO]
    return sorted(picked, key=inbox.sort_key)


def format_items(items: list[inbox.InboxItem], bodies: dict[str, str]) -> str:
    """渡す内容を組み立てる。読むのは人ではなく Claude Code のセッション。

    **ページIDを必ず添える。** これが無いと読んだ側が書き戻せず、
    直したのにNotion側が「要確認」のまま残る。
    """
    if not items:
        return "クロコ自身の改修として溜まっているものはありません。"

    lines = [
        SEPARATOR,
        f"クロコ側に溜まっている改修依頼: {len(items)}件",
        "直したら `python croco_cli.py done <ページID> \"何をしたか\"` で閉じること。",
        SEPARATOR,
    ]
    for number, item in enumerate(items, start=1):
        lines.append("")
        lines.append(f"[{number}] {item.title}")
        lines.append(f"    ページID : {item.id}")
        detail = item.status
        if item.hold_reason and item.hold_reason != inbox.HOLD_NONE:
            detail += f"（{item.hold_reason}）"
        lines.append(f"    状態     : {detail}")
        spoken = (item.page.get("created_time") or "")[:16].replace("T", " ")
        if spoken:
            lines.append(f"    捕捉      : {spoken}")
        lines.append("    --- 本文（本人のブレスト生ログの逐語転記） ---")
        body = (bodies.get(item.id) or "").strip() or "（本文なし）"
        lines.extend("    " + line for line in body.splitlines())
        progress = (item.result_log or "").strip()
        if progress:
            lines.append("    --- これまでの経緯 ---")
            lines.extend("    " + line for line in progress.splitlines())
    lines.append("")
    return "\n".join(lines)


def run(config: Config, *, all_reasons: bool = False) -> int:
    """Notionから取ってきて標準出力に出す。返り値は件数。"""
    client = nt.Notion(config.notion_token, config.notion_version)
    data_source_id = config.inbox_data_source_id or client.resolve_data_source_id(
        config.inbox_database_id
    )
    items = [
        inbox.InboxItem(page) for page in client.query_data_source(data_source_id)
    ]
    picked = select(items, all_reasons=all_reasons)

    # 本文はページ本体（子ブロック）にある。件数分だけ取りに行く。
    bodies: dict[str, str] = {}
    for item in picked:
        try:
            bodies[item.id] = client.get_page_text(item.id)
        except Exception as exc:
            # 1件読めなくても残りは渡せる。落とさずに印を残す。
            log.warn(f"[{item.title}] 本文の読み取りに失敗しました: {exc}")
            bodies[item.id] = ""

    print(format_items(picked, bodies))
    return len(picked)
