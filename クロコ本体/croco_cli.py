"""クロコ自身が進捗を書き戻すための CLI。

無人実行中のクロコは、このコマンド経由でのみ Notion に触る。
Notion の資格情報をクロコのコンテキストに載せずに済み、
できる操作も「担当アイテムの進捗更新」「Inboxの参照」だけに限定できる
（仕様書2.5章-12：トークンのスコープを絞る方針と同じ発想）。

使い方:
    python croco_cli.py log      <page_id> "進捗メッセージ"
    python croco_cli.py done     <page_id> "最終的な成果の要約"
    python croco_cli.py review   <page_id> "確認してほしいこと"
    python croco_cli.py resume   <page_id> "決まったこと"
    python croco_cli.py priority <page_id> 高|中|低
    python croco_cli.py genre    <page_id> <ジャンル名>
    python croco_cli.py list
    python croco_cli.py show     <page_id>
    python croco_cli.py tree
    python croco_cli.py read     <page_id>

tree / read は、クロコ用の親ページ配下（Inbox DB以外の子ページも含む）を
閲覧するための読み取り専用コマンド。list/show と同じ境界の考え方で、
書き込み系の操作は一切持たない。
"""

from __future__ import annotations

import re
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

# done を要確認／対象外から直接呼んだ場合、resume を経由させず注記だけ添えて完了させる。
# 当初はここでブロックし、先に resume でキューに戻させていた（2026-07-30、
# related.pyが要確認アイテムのidを見せるようになったことへの対策）。
# だが「完了にしてと言っても完了にならない」という2段階の摩擦が実際に起きた
# （2026-08-01、本人の指摘）。doneは常にメッセージが必須で、それ自体が
# 「意図して閉じた」証拠になるため、resumeを別コマンドとして強制する安全効果は薄いと
# 判断し直接完了できるようにした。どの状態から完了したかはログに残す。
AUTO_RESUME_ON_DONE_STATUSES = {inbox.STATUS_REVIEW, inbox.STATUS_EXCLUDED}

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


_ORIGIN_ID_RE = re.compile(
    r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?"
    r"[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}"
)


def _origin_key(origin: str) -> str:
    """「由来メモ」文字列から、突き合わせ用のページIDを取り出して正規化する。

    値は `タイトル (ページID)` 形式だが、一致判定に使うのはIDだけ
    （タイトルは読みやすさ用の飾り）。IDが見当たらなければ空文字を返し、
    呼び出し側はカスケードしない（この改修より前の既存アイテムや、手書きの
    由来メモで、無関係なものを誤って巻き込むのを防ぐ）。
    """
    match = _ORIGIN_ID_RE.search(origin or "")
    return match.group(0).replace("-", "").lower() if match else ""


def _cascade_done_to_siblings(
    client: nt.Notion, config: Config, done_page_id: str
) -> None:
    """同じ「由来メモ」を持つ他アイテムも一緒に「完了」にする。

    1本のメモが複数アイテムに割れ、その一部をまとめて実装したとき、
    実装したぶんだけ done を打てば残りも連動して閉じる（2026-09-09、本人指示：
    「同じメモ由来別のタスクを含めて処理した場合、処理したタスクまで
    すべてdoneにする」）。グループの一致は「起草日時」の近似ではなく
    「由来メモ」カラムの一致で見る。

    - 「由来メモ」が空（この改修より前に作られたアイテム）なら何もしない。
    - 「由来メモ」にはユニークなページIDが入るので、別メモと衝突しない。
    - 対象は「完了」以外すべて（未処理・処理中・対象外・要確認）。
    """
    origin = nt.plain_text_of(
        client.get_page(done_page_id).get("properties", {}).get(inbox.P_ORIGIN)
    )
    key = _origin_key(origin)
    if not key:
        return

    data_source_id = config.inbox_data_source_id or client.resolve_data_source_id(
        config.inbox_database_id
    )
    closed = 0
    for page in client.query_data_source(data_source_id):
        sibling = inbox.InboxItem(page)
        if sibling.id == done_page_id or sibling.status == inbox.STATUS_DONE:
            continue
        if _origin_key(sibling.origin) != key:
            continue
        updated = _append_log(
            client,
            sibling.id,
            f"同一メモ由来（{origin}）のアイテムを完了したため、まとめて完了",
        )
        client.update_page(
            sibling.id,
            {
                inbox.P_STATUS: {"select": {"name": inbox.STATUS_DONE}},
                inbox.P_FINISHED_AT: {"date": {"start": inbox.now_iso()}},
                inbox.P_RESULT: {"rich_text": nt.rich_text(updated)},
            },
        )
        closed += 1
    if closed:
        print(f"同一メモ由来のため、他 {closed} 件もまとめて「完了」にしました。")


def _cmd_list(client: nt.Notion, config: Config) -> int:
    """Inbox全件を、本文冒頭つきで一覧する（読み取り専用）。

    タイトルは元々「本文を指す短いラベル」で意味解釈しないものなので、
    関連判断の材料としては薄い。本文の冒頭（逐語のまま、要約はしない）を
    添えることで、show を個別に呼ばなくても大まかな中身が分かるようにする。
    予定日を出すのは、能動的に「迫っている予定を言及する」仕組みは作らず、
    見に行けば分かる状態にしておけば十分という判断のため（2026-07-31）。
    予定日が無い項目は、並び替えのフォールバックに使っている作成日時を
    代わりに出す（並び順の根拠が画面から見えるように、2026-08-01）。
    優先度も同じ理由で、未設定でも並び替え上の既定値（中）を明示する。
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
    items.sort(key=inbox.priority_sort_key)
    for item in items:
        reason = (
            f" [{item.hold_reason}]"
            if item.hold_reason and item.hold_reason != inbox.HOLD_NONE
            else ""
        )
        tag = inbox.compact_tag(item)
        tag_label = f" [{tag}]" if tag else ""
        if item.scheduled:
            date_label = f" 予定日:{item.scheduled}"
        else:
            created = item.page.get("created_time", "")
            date_label = f" 作成日時:{created[:16].replace('T', ' ')}" if created else ""
        excerpt = client.get_page_text(item.id).replace("\n", " ").strip()[:EXCERPT_LEN]
        print(
            f"{item.id}\t{item.status}/{item.kind}{reason}{tag_label}{date_label}"
            f"\t{item.title}\t{excerpt}"
        )
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


def _cmd_tree(client: nt.Notion, config: Config) -> int:
    """クロコ用の親ページ配下を全階層一覧する（読み取り専用）。

    「未処理置き場」の親を辿って親ページIDを得る（setup_notion.py の
    add_status_page と同じ手。親ページID自体は.envに持っていない）。
    """
    parent_id = client.get_parent_page_id(config.unprocessed_page_id)
    if not parent_id:
        print("親ページを特定できませんでした。", file=sys.stderr)
        return 1
    for item in client.list_descendants(parent_id):
        indent = "  " * item["depth"]
        mark = "DB" if item["type"] == "database" else "page"
        print(f"{indent}[{mark}] {item['title']}\t{item['id']}")
    return 0


def _cmd_read(client: nt.Notion, page_id: str) -> int:
    """任意ページの本文を表示する（Inbox項目に限らない、読み取り専用）。"""
    page = client.get_page(page_id)
    body = client.get_page_text(page_id)
    print(f"タイトル: {nt.page_title(page)}")
    print("--- 本文 ---")
    print(body or "（本文なし）")
    return 0


def _done_transition_note(client: nt.Notion, page_id: str) -> str:
    """`done`が要確認／対象外から直接呼ばれた場合の注記を返す（通常は空文字）。"""
    page = client.get_page(page_id)
    status = nt.select_of(page.get("properties", {}).get(inbox.P_STATUS))
    if status in AUTO_RESUME_ON_DONE_STATUSES:
        return f"（{status}から直接完了）"
    return ""


def _cmd_priority(client: nt.Notion, page_id: str, value: str) -> int:
    """優先度を立てる／変える（本人がセッション中に指示したときだけ呼ぶ）。

    Geminiには推定させない方針（2026-07-31）なので、判定は本人の発言のみが
    根拠になる。空文字列を渡せば未設定（＝中扱い）に戻せる。
    """
    if value and value not in inbox.PRIORITIES:
        print(
            f"優先度は {'/'.join(inbox.PRIORITIES)} のいずれかで指定してください"
            "（未設定に戻すなら空文字列）。",
            file=sys.stderr,
        )
        return 2
    prop = {"select": {"name": value}} if value else {"select": None}
    client.update_page(page_id, {inbox.P_PRIORITY: prop})
    print(f"優先度を設定しました: {value or '（未設定）'}")
    return 0


def _cmd_genre(client: nt.Notion, page_id: str, value: str) -> int:
    """ジャンルを設定する／変える。

    ジャンルは受験・エディタ・アプリ開発・クロコ本体の4区分に固定
    （2026-09-05、本人の判断。croco/genre.py の ALLOWED_GENRES）。
    Geminiにはこの4つの中から選ばせるだけで新規作成はさせない。
    区分そのものを増減したいときは ALLOWED_GENRES を直接編集する
    （その変更自体が「本人が決めた」という記録になる）。
    空文字列を渡せば未分類に戻せる。
    """
    client.update_page(page_id, {inbox.P_GENRE: {"rich_text": nt.rich_text(value)}})
    print(f"ジャンルを設定しました: {value or '（未分類）'}")
    return 0


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
    if argv[0] == "priority":
        if len(argv) < 2:
            print("page_idが要ります。", file=sys.stderr)
            return 2
        value = argv[2] if len(argv) > 2 else ""
        return _cmd_priority(client, argv[1], value)
    if argv[0] == "genre":
        if len(argv) < 2:
            print("page_idが要ります。", file=sys.stderr)
            return 2
        value = argv[2] if len(argv) > 2 else ""
        return _cmd_genre(client, argv[1], value)
    if argv[0] == "tree":
        return _cmd_tree(client, config)
    if argv[0] == "read":
        if len(argv) < 2:
            print("page_idが要ります。", file=sys.stderr)
            return 2
        return _cmd_read(client, argv[1])

    if len(argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2

    command, page_id = argv[0], argv[1]
    message = " ".join(argv[2:]).strip()
    if not message:
        print("メッセージが空です。", file=sys.stderr)
        return 2

    if command == "done":
        message += _done_transition_note(client, page_id)

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
    if command == "done":
        _cascade_done_to_siblings(client, config, page_id)
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
