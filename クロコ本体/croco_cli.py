"""クロコ自身が進捗を書き戻すための CLI。

無人実行中のクロコは、このコマンド経由でのみ Notion に触る。
Notion の資格情報をクロコのコンテキストに載せずに済み、
できる操作も「担当アイテムの進捗更新」「Inboxの参照」だけに限定できる
（仕様書2.5章-12：トークンのスコープを絞る方針と同じ発想）。

使い方:
    python croco_cli.py log    <page_id> "進捗メッセージ"
    python croco_cli.py done   <page_id> "最終的な成果の要約"
    python croco_cli.py review <page_id> "確認してほしいこと"
    python croco_cli.py resume <page_id> "決まったこと"
    python croco_cli.py list
    python croco_cli.py show   <page_id>
"""

from __future__ import annotations

import sys

from croco import inbox, notify, notion as nt
from croco.config import Config, ConfigError

# 節目で鳴らす音。ここで鳴らすのは run_croco.py 側では間に合わないため。
# 対話モードのクロコは作業を終えてもウィンドウが開いたままで、本人が `/exit`
# するまで run_croco.py は subprocess.run() で止まったまま先に進めない。
# **「この件が片付いた」瞬間はこのコマンドでしか観測できない。**
# `log` では鳴らさない。区切りごとに鳴らすと狼少年になる。
SOUNDS = {
    "done": notify.finished,      # 片付いた。画面を見に来ていい
    "review": notify.waiting,     # 聞きたいことがある。本人待ち
}

# done を拒否するステータス。要確認＝人の判断待ち、対象外＝実装対象外（予定・資料）。
# 関連アイテムを見せる機能（croco/related.py）を足したことで、クロコが
# 要確認アイテムのidを知る機会が増えた。プロンプトの指示だけでは破られうるので、
# ここを最後の砦にする（2026-07-30）。先に resume でキューに戻してから done を打たせる。
BLOCKED_DONE_STATUSES = {inbox.STATUS_REVIEW, inbox.STATUS_EXCLUDED}

# list/show の本文冒頭に添える抜粋の長さ。要約はしない（逐語の先頭を切るだけ）。
EXCERPT_LEN = 60


def _append_log(client: nt.Notion, page_id: str, message: str) -> str:
    """実行結果に1行追記して、追記後の全文を返す。

    gitのコミットメッセージのような簡潔な進捗ログを積み上げる形式
    （仕様書2.5章-10）。次のセッションはこれを読んで続きから再開する。
    """
    page = client.get_page(page_id)
    existing = nt.plain_text_of(page.get("properties", {}).get(inbox.P_RESULT))
    stamp = inbox.now_iso()[:16].replace("T", " ")
    entry = f"[{stamp}] {message}"
    return f"{existing}\n{entry}".strip() if existing else entry


def _cmd_list(client: nt.Notion, config: Config) -> int:
    """Inbox全件を、本文冒頭つきで一覧する（読み取り専用）。

    タイトルは元々「本文を指す短いラベル」で意味解釈しないものなので、
    関連判断の材料としては薄い。本文の冒頭（逐語のまま、要約はしない）を
    添えることで、show を個別に呼ばなくても大まかな中身が分かるようにする。
    件数が増えると1件ごとにNotionへ本文取得を投げる分だけ遅くなる
    （この規模ではまだ気にする段階ではないはず）。
    """
    data_source_id = config.inbox_data_source_id or client.resolve_data_source_id(
        config.inbox_database_id
    )
    pages = client.query_data_source(data_source_id)
    items = [inbox.InboxItem(page) for page in pages]
    if not items:
        print("Inboxは空です。")
        return 0
    items.sort(key=lambda i: i.page.get("created_time", ""))
    for item in items:
        reason = (
            f" [{item.hold_reason}]"
            if item.hold_reason and item.hold_reason != inbox.HOLD_NONE
            else ""
        )
        excerpt = client.get_page_text(item.id).replace("\n", " ").strip()[:EXCERPT_LEN]
        print(f"{item.id}\t{item.status}/{item.kind}{reason}\t{item.title}\t{excerpt}")
    return 0


def _cmd_show(client: nt.Notion, page_id: str) -> int:
    """1件の本文・経緯まで読む（読み取り専用）。"""
    page = client.get_page(page_id)
    item = inbox.InboxItem(page)
    body = client.get_page_text(page_id)
    reason = (
        f" / 保留理由: {item.hold_reason}"
        if item.hold_reason and item.hold_reason != inbox.HOLD_NONE
        else ""
    )
    print(f"タイトル: {item.title}")
    print(f"種別: {item.kind} / ステータス: {item.status}{reason}")
    print("--- 本文 ---")
    print(body or "（本文なし）")
    if item.result_log.strip():
        print("--- これまでの経緯 ---")
        print(item.result_log)
    return 0


def _guard_done(client: nt.Notion, page_id: str) -> bool:
    """`done`が要確認／対象外のアイテムに向けて呼ばれていないか確認する。

    関連アイテムを見せる機能（croco/related.py）を足したことで、クロコが
    要確認アイテムのidを知る機会が増えた。プロンプトの指示（着手するな）だけでは
    破られうるので、ここを最後の砦にする。
    """
    page = client.get_page(page_id)
    status = nt.select_of(page.get("properties", {}).get(inbox.P_STATUS))
    if status in BLOCKED_DONE_STATUSES:
        print(
            f"このアイテムは現在「{status}」のため done にできません。"
            " 先に resume でキューに戻してから done を呼んでください。",
            file=sys.stderr,
        )
        return False
    return True


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2

    config = Config()
    client = nt.Notion(config.notion_token, config.notion_version)

    if argv[0] == "list":
        return _cmd_list(client, config)
    if argv[0] == "show":
        if len(argv) < 2:
            print("page_idが要ります。", file=sys.stderr)
            return 2
        return _cmd_show(client, argv[1])

    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2

    command, page_id = argv[0], argv[1]
    message = " ".join(argv[2:]).strip()
    if not message:
        print("メッセージが空です。", file=sys.stderr)
        return 2

    if command == "done" and not _guard_done(client, page_id):
        return 1

    updated = _append_log(client, page_id, message)
    properties: dict = {inbox.P_RESULT: {"rich_text": nt.rich_text(updated)}}

    if command == "log":
        pass
    elif command == "done":
        properties[inbox.P_STATUS] = {"select": {"name": inbox.STATUS_DONE}}
        properties[inbox.P_FINISHED_AT] = {"date": {"start": inbox.now_iso()}}
    elif command == "review":
        properties[inbox.P_STATUS] = {"select": {"name": inbox.STATUS_REVIEW}}
        # 何で止まっているかをまとめで束ねられるよう、経路ごとに理由を残す。
        properties[inbox.P_HOLD_REASON] = {"select": {"name": inbox.HOLD_ASKED}}
    elif command == "resume":
        # review の逆。話が決着したものを着手キューに戻す。
        # **保留理由は消さない。** 「なぜ人が要ったか」はそのまま仕事の性質でもあり、
        # 戻したあとの着手でも線引き（本人名義の文書なら本文を書かない等）に使う。
        properties[inbox.P_STATUS] = {"select": {"name": inbox.STATUS_TODO}}
        # 相談で止まっていた回は失敗ではないので、試行回数は数え直す。
        properties[inbox.P_ATTEMPTS] = {"number": 0}
    else:
        print(f"不明なコマンド: {command}", file=sys.stderr)
        print(__doc__, file=sys.stderr)
        return 2

    client.update_page(page_id, properties)
    print(f"記録しました ({command}): {message}")
    # 書き込みが通ってから鳴らす。失敗したのに終わった音がすると信用できなくなる。
    sound = SOUNDS.get(command)
    if sound is not None:
        sound(config)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
