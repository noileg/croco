"""関連しそうな既存アイテムを見つけて、プロンプトに差し込む形へ整える。

分割によってDB上は別行になっていても、本人の頭の中では地続きの話であることが
普通にある。クロコに探させる（`croco_cli.py list`/`show`）だけでは、面倒がって
自発的には横断を探しに行かない。だから判定済みの候補を**最初からプロンプトに
書いておく**（2026-07-30、本人の指摘）。

判定はGeminiにやらせる（既存アイテムの一覧から関連するものを選ぶだけの、
狭く機械的なタスクなので）。失敗しても本体のdispatch/consultを巻き込まない。
"""

from __future__ import annotations

from . import inbox, log
from . import notion as nt
from .config import Config
from .gemini import Gemini


def find_candidates(
    client: nt.Notion,
    gemini: Gemini,
    config: Config,
    *,
    current_id: str,
    current_title: str,
    current_body: str,
) -> list[inbox.InboxItem]:
    """関連しそうな既存アイテムを返す。

    失敗（Notion・Gemini側のどちらでも）した場合は空リストで返し、
    呼び出し元（dispatch/consult）を止めない。これは補助機能であって、
    本体の着手・相談を道連れにする理由がないため。

    候補は現在時点のInbox全件（自分自身を除く）。本文まで取得して渡すのは、
    タイトルだけでは関連判定の材料として薄いため（タイトルは意味解釈しない
    短いラベルに留める設計。gemini.py冒頭のdocstring参照）。
    件数が増えるとアイテムごとにNotionへ本文取得を投げる分だけ遅くなる
    （この規模ではまだ気にする段階ではないはず。増えたら対処する）。
    """
    try:
        data_source_id = config.inbox_data_source_id or client.resolve_data_source_id(
            config.inbox_database_id
        )
        pages = client.query_data_source(data_source_id)
        items = [inbox.InboxItem(p) for p in pages if p.get("id") != current_id]
        if not items:
            return []
        candidates = [
            {
                "id": item.id,
                "title": item.title,
                "kind": item.kind,
                "status": item.status,
                "body": client.get_page_text(item.id),
            }
            for item in items
        ]
        related_ids = gemini.find_related(current_title, current_body, candidates)
    except Exception as exc:
        log.warn(f"関連アイテムの判定に失敗しました（無視して続行します）: {exc}")
        return []

    by_id = {item.id: item for item in items}
    return [by_id[i] for i in related_ids if i in by_id]


def render_section(related: list[inbox.InboxItem], *, allow_review: bool) -> str:
    """プロンプトに差し込む「関連しそうな既存アイテム」セクションを組み立てる。

    dispatch（allow_review=False）では、要確認のアイテムが混ざっていても
    「見えるが触るな、本人に伝えるだけにしろ」と明示する。要確認は人の判断待ちで、
    無人セッションが関連に気づいたからと勝手に着手・完了させてよい対象ではない
    （croco_cli.py側のガードとは別の、プロンプト上の一線）。
    consult（allow_review=True）は要確認同士の関連付けをむしろ歓迎するので制限しない。
    """
    if not related:
        return ""
    lines = [
        "## 関連しそうな既存アイテム（参考）",
        "気づいた場合のみ使ってください。関連が薄いと感じたら無視して構いません。",
        "`python croco_cli.py show <id>` で中身を読めます。",
    ]
    for item in related:
        reason = (
            f"［{item.hold_reason}］"
            if item.hold_reason and item.hold_reason != inbox.HOLD_NONE
            else ""
        )
        note = ""
        if not allow_review and item.status == inbox.STATUS_REVIEW:
            note = "（要確認のため、今回のセッションでは着手せず本人に伝えるだけにしてください）"
        lines.append(f"- {item.id} {reason}{item.title}{note}")
    return "\n".join(lines) + "\n"
