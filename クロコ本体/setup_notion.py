"""Notion側の実体（置き場ページ2つ＋Inbox DB）を作る初期セットアップ。

一度だけ実行する。作成後に表示される設定行を `.env` に貼れば準備完了。

前提:
  - Notion Integration を作成し、その NOTION_TOKEN を .env に書いてあること
  - 親になるページを1つ用意し、そのページを Integration に接続（Connect）してあること
    ※ トークンのスコープを絞る方針（仕様書2.5章-12）のため、
      ワークスペース全体ではなくこの親ページだけを接続すること

使い方:
    python setup_notion.py <親ページのID または URL>
    python setup_notion.py --check         # 既存の設定で疎通確認だけする
    python setup_notion.py --status-page   # 「管轄プロジェクトの現状」ページを足す
"""

from __future__ import annotations

import re
import sys

from croco import inbox
from croco import notion as nt
from croco.config import Config, ConfigError

UNPROCESSED_TITLE = "未処理置き場"
PROCESSED_TITLE = "処理済み置き場"
INBOX_TITLE = "クロコ Inbox"
STATUS_TITLE = "管轄プロジェクトの現状"


def extract_page_id(value: str) -> str:
    """NotionのURLでもIDでも受け付けて、ID部分を取り出す。"""
    candidates = re.findall(r"[0-9a-fA-F]{32}|[0-9a-fA-F-]{36}", value)
    if not candidates:
        raise SystemExit(f"ページIDを読み取れませんでした: {value}")
    raw = candidates[-1].replace("-", "")
    return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


def create_child_page(client: nt.Notion, parent_page_id: str, title: str) -> str:
    page = client._call(  # noqa: SLF001 - セットアップ専用の素朴な呼び出し
        "POST",
        "/pages",
        {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "properties": {"title": {"title": nt.rich_text(title)}},
        },
    )
    return page["id"]


def create_inbox_database(client: nt.Notion, parent_page_id: str) -> tuple[str, str]:
    """Inbox DB を作り、(database_id, data_source_id) を返す。"""
    database = client._call(  # noqa: SLF001
        "POST",
        "/databases",
        {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": nt.rich_text(INBOX_TITLE),
            "is_inline": False,
            "initial_data_source": {"properties": inbox.SCHEMA},
        },
    )
    sources = database.get("data_sources") or []
    if not sources:
        raise SystemExit("DBは作成されましたが、データソースIDを取得できませんでした。")
    return database["id"], sources[0]["id"]


def check(config: Config) -> int:
    """既存の設定で読み取りが通るか確かめる。"""
    client = nt.Notion(config.notion_token, config.notion_version)

    print(f"Notion APIバージョン: {config.notion_version}")

    children = client.list_child_pages(config.unprocessed_page_id)
    print(f"[OK] 未処理置き場を読めました（子ページ {len(children)} 件）")

    client.get_page(config.processed_page_id)
    print("[OK] 処理済み置き場を読めました")

    data_source_id = config.inbox_data_source_id or client.resolve_data_source_id(
        config.inbox_database_id
    )
    print(f"[OK] Inbox DB のデータソース: {data_source_id}")

    rows = client.query_data_source(data_source_id)
    print(f"[OK] Inbox DB を読めました（{len(rows)} 件）")

    missing = _missing_properties(rows)
    if missing:
        print(f"[警告] 期待するプロパティが見つかりません: {', '.join(missing)}")

    print("\n疎通確認に成功しました。")
    return 0


def _missing_properties(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    present = set(rows[0].get("properties", {}))
    return [name for name in inbox.SCHEMA if name not in present]


def setup(config: Config, parent_page_id: str) -> int:
    client = nt.Notion(config.notion_token, config.notion_version)

    print(f"親ページ {parent_page_id} の下に作成します...\n")

    unprocessed_id = create_child_page(client, parent_page_id, UNPROCESSED_TITLE)
    print(f"[作成] {UNPROCESSED_TITLE}: {unprocessed_id}")

    processed_id = create_child_page(client, parent_page_id, PROCESSED_TITLE)
    print(f"[作成] {PROCESSED_TITLE}: {processed_id}")

    database_id, data_source_id = create_inbox_database(client, parent_page_id)
    print(f"[作成] {INBOX_TITLE}: {database_id}")

    print("\n" + "=" * 60)
    print("以下を .env に追記してください:")
    print("=" * 60)
    print(f"NOTION_UNPROCESSED_PAGE_ID={unprocessed_id}")
    print(f"NOTION_PROCESSED_PAGE_ID={processed_id}")
    print(f"NOTION_INBOX_DATABASE_ID={database_id}")
    print(f"NOTION_INBOX_DATA_SOURCE_ID={data_source_id}")
    print("=" * 60)

    print(
        "\n次にNotion上で手動で行うこと:\n"
        f"  1. 「{INBOX_TITLE}」にカレンダービューを追加し、\n"
        f"     フィルタを「{inbox.P_KIND} = {inbox.KIND_SCHEDULE}」、\n"
        f"     日付に「{inbox.P_SCHEDULED}」を指定する（仕様書2.5章-6）\n"
        "  2. スマホのNotion AIチャットで捕捉する際は、毎回\n"
        f"     「{UNPROCESSED_TITLE}のサブページにメモを作って」と明示的に指示する\n"
    )
    return 0


def add_status_page(config: Config) -> int:
    """「管轄プロジェクトの現状」ページを、既存の置き場と同じ親の下に作る。

    親ページIDは .env に持っていないが、「未処理置き場」の親を辿れば分かる。
    初回セットアップで使ったURLを本人が覚えている前提にしない。
    """
    client = nt.Notion(config.notion_token, config.notion_version)

    if config.status_page_id:
        print(f"すでに設定されています: {config.status_page_id}")
        print("作り直したい場合は .env の NOTION_STATUS_PAGE_ID を消してから再実行してください。")
        return 0

    parent = client.get_page(config.unprocessed_page_id).get("parent", {})
    parent_page_id = parent.get("page_id")
    if not parent_page_id:
        print(
            f"「{UNPROCESSED_TITLE}」の親ページを特定できませんでした（parent={parent}）。",
            file=sys.stderr,
        )
        return 1

    page_id = create_child_page(client, parent_page_id, STATUS_TITLE)
    print(f"[作成] {STATUS_TITLE}: {page_id}")
    print("\n以下を .env に追記してください:")
    print(f"NOTION_STATUS_PAGE_ID={page_id}")
    print(
        "\n中身はクロコが起動のたびに書き直します"
        "（変化が無い回は書き直しません）。\n"
        "手で編集しても次の起動で消えるので、書き込まないこと。"
    )
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2

    config = Config()

    if argv[0] == "--check":
        return check(config)
    if argv[0] == "--status-page":
        return add_status_page(config)

    return setup(config, extract_page_id(argv[0]))


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
