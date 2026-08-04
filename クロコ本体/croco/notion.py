"""Notion API クライアント。

APIバージョン 2026-03-11 系を前提とする。重要な前提が2つある。

1. 2025-09-03 以降、DB（database）とデータソース（data source）が分離された。
   DB配下のページ作成・クエリの親には `database_id` ではなく
   `data_source_id` を指定する。DBのIDしか手元に無い場合は
   `resolve_data_source_id()` で解決できる。
2. Move Page API（`POST /v1/pages/{id}/move`）が使える。
   これで「未処理置き場」→「処理済み置き場」の移動を行う（仕様書2.5章-3）。
"""

from __future__ import annotations

from typing import Any, Iterator

from . import httpjson

API_BASE = "https://api.notion.com/v1"

# Notionのrich_text 1つあたりの文字数上限。超えると400になるので分割して送る。
# **この「文字数」はUTF-16コードユニット数で数えられる（実測）。**
# Pythonのlen()はコードポイント数なので、絵文字などBMP外の文字が混じると
# 数え方がずれる（🔴 は len()では1、Notionでは2）。len()で2000ずつ切ると
# 「2000字のはずが2002」と言われて400になる。→ _split_utf16() を使うこと。
RICH_TEXT_LIMIT = 2000

# rich_text 配列の要素数上限。1ブロックに入る文字数はこの積が上限になる。
RICH_TEXT_PARTS = 100

# 1回の children 追加で送れるブロック数の上限。
CHILDREN_LIMIT = 100


class Notion:
    def __init__(self, token: str, version: str) -> None:
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Notion-Version": version,
        }

    def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        return httpjson.request_json(
            f"{API_BASE}{path}",
            method=method,
            headers=self._headers,
            payload=payload,
        )

    def _paginate(
        self, method: str, path: str, payload: dict | None = None
    ) -> Iterator[dict]:
        """`has_more` / `next_cursor` を辿って全件を返す。"""
        cursor: str | None = None
        while True:
            if method == "GET":
                sep = "&" if "?" in path else "?"
                url = f"{path}{sep}page_size=100"
                if cursor:
                    url += f"&start_cursor={cursor}"
                data = self._call(method, url)
            else:
                body = dict(payload or {})
                body["page_size"] = 100
                if cursor:
                    body["start_cursor"] = cursor
                data = self._call(method, path, body)

            yield from data.get("results", [])

            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")
            if not cursor:
                return

    # --- データソース解決 -----------------------------------------------

    def resolve_data_source_id(self, database_id: str) -> str:
        """DBのIDから、その最初のデータソースIDを解決する。

        1つのDBが複数データソースを持ち得る仕様だが、Inbox DBは
        単一データソースの前提なので先頭を採る。
        """
        database = self._call("GET", f"/databases/{database_id}")
        sources = database.get("data_sources") or []
        if not sources:
            raise RuntimeError(
                f"DB {database_id} にデータソースが見つかりません。"
                " APIバージョンが 2025-09-03 以降か確認してください。"
            )
        return sources[0]["id"]

    def update_data_source_schema(self, data_source_id: str, properties: dict) -> dict:
        """データソースのプロパティ定義を追加・更新する。

        既存のプロパティを含めずに追加分だけ渡せばよい（差分適用される）。
        新規DB作成時の `initial_data_source.properties` と違い、既存DBへの
        列追加はこちら（`PATCH /data_sources/{id}`）を使う。
        """
        return self._call(
            "PATCH", f"/data_sources/{data_source_id}", {"properties": properties}
        )

    # --- 読み取り -------------------------------------------------------

    def get_page(self, page_id: str) -> dict:
        return self._call("GET", f"/pages/{page_id}")

    def list_child_pages(self, parent_page_id: str) -> list[dict]:
        """親ページ直下の子ページを列挙する。

        「未処理置き場」に子ページとして存在すること自体が未処理の印
        （仕様書2.5章-3）なので、ここで拾えたものが処理対象になる。
        """
        children = []
        for block in self._paginate("GET", f"/blocks/{parent_page_id}/children"):
            if block.get("type") == "child_page":
                children.append(
                    {
                        "id": block["id"],
                        "title": block.get("child_page", {}).get("title", ""),
                    }
                )
        return children

    def get_parent_page_id(self, page_id: str) -> str | None:
        """指定ページの親ページIDを返す（親がページでない場合は None）。"""
        parent = self.get_page(page_id).get("parent", {})
        return parent.get("page_id")

    def list_descendants(self, page_id: str) -> list[dict]:
        """指定ページ配下の全ページ・DBを再帰的に列挙する（読み取り専用）。

        「未処理置き場」等の直下1階層しか見ない list_child_pages と違い、
        子ページのそのまた子まで辿る。DBは中身（行）までは展開せず
        1件として返す（行が見たい場合は query_data_source を使う）。
        """
        return list(self._walk_descendants(page_id, depth=0))

    def _walk_descendants(self, page_id: str, *, depth: int) -> Iterator[dict]:
        for block in self._paginate("GET", f"/blocks/{page_id}/children"):
            block_type = block.get("type")
            if block_type == "child_page":
                title = block.get("child_page", {}).get("title", "")
                yield {"id": block["id"], "title": title, "type": "page", "depth": depth}
                yield from self._walk_descendants(block["id"], depth=depth + 1)
            elif block_type == "child_database":
                title = block.get("child_database", {}).get("title", "")
                yield {"id": block["id"], "title": title, "type": "database", "depth": depth}

    def get_page_text(self, page_id: str) -> str:
        """ページ本文をプレーンテキストとして取り出す。

        生ログを逐語で扱う必要があるため（仕様書2.5章-3）、
        書式は落とすが文字は落とさない方針で抽出する。
        """
        lines: list[str] = []
        for block in self._paginate("GET", f"/blocks/{page_id}/children"):
            lines.extend(_block_to_lines(block))
            # 入れ子のブロック（トグル配下など）も取りこぼさない。
            if block.get("has_children") and block.get("type") != "child_page":
                nested = self.get_page_text(block["id"])
                if nested:
                    lines.extend(nested.splitlines())
        return "\n".join(lines)

    def query_data_source(
        self, data_source_id: str, *, filter_: dict | None = None, sorts: list | None = None
    ) -> list[dict]:
        payload: dict[str, Any] = {}
        if filter_:
            payload["filter"] = filter_
        if sorts:
            payload["sorts"] = sorts
        return list(
            self._paginate("POST", f"/data_sources/{data_source_id}/query", payload)
        )

    # --- 書き込み -------------------------------------------------------

    def create_page(
        self, *, data_source_id: str, properties: dict, children: list | None = None
    ) -> dict:
        payload: dict[str, Any] = {
            "parent": {"type": "data_source_id", "data_source_id": data_source_id},
            "properties": properties,
        }
        if children:
            payload["children"] = children
        return self._call("POST", "/pages", payload)

    def update_page(self, page_id: str, properties: dict) -> dict:
        return self._call("PATCH", f"/pages/{page_id}", {"properties": properties})

    def move_page(self, page_id: str, *, new_parent_page_id: str) -> dict:
        """ページを別の親ページ配下へ移動する。

        Inbox DBへの登録が成功したものだけをここで移動させる。
        失敗したものは動かさず「未処理置き場」に残すことで、
        次回起動時に自然に再試行される（仕様書2.5章-3の冪等性設計）。
        """
        return self._call(
            "POST",
            f"/pages/{page_id}/move",
            {"parent": {"type": "page_id", "page_id": new_parent_page_id}},
        )

    def append_blocks(self, page_id: str, children: list) -> dict:
        return self._call("PATCH", f"/blocks/{page_id}/children", {"children": children})

    def get_block_children(self, page_id: str) -> list[dict]:
        """直下のブロックだけを返す（入れ子は辿らない）。"""
        return list(self._paginate("GET", f"/blocks/{page_id}/children"))

    def delete_block(self, block_id: str) -> dict:
        """ブロックを削除する。子ブロックも一緒に消える。"""
        return self._call("DELETE", f"/blocks/{block_id}")

    def replace_children(self, page_id: str, children: list) -> None:
        """ページの中身をまるごと入れ替える。

        追記でなく入れ替えなのは、自動更新されるページが際限なく伸びるのを
        防ぐため。削除はブロック1つにつき1回のAPI呼び出しになるので、
        **入れ替える側はブロック数を少なく作ること**（status.py がコードブロックに
        まとめているのはこのため。1行1段落で作ると数百回叩くことになる）。
        """
        for block in self.get_block_children(page_id):
            self.delete_block(block["id"])
        for i in range(0, len(children), CHILDREN_LIMIT):
            self.append_blocks(page_id, children[i : i + CHILDREN_LIMIT])


# --- 変換ヘルパ ---------------------------------------------------------


def _block_to_lines(block: dict) -> list[str]:
    """ブロック1つをテキスト行に変換する。"""
    block_type = block.get("type", "")
    content = block.get(block_type)
    if not isinstance(content, dict):
        return []

    rich_text = content.get("rich_text")
    if not isinstance(rich_text, list):
        return []

    text = "".join(part.get("plain_text", "") for part in rich_text)
    if not text:
        return [""] if block_type == "paragraph" else []

    # 箇条書き・見出しは、元の構造が分かる最小限の記号だけ残す。
    prefix = {
        "bulleted_list_item": "- ",
        "numbered_list_item": "- ",
        "to_do": "- ",
        "quote": "> ",
        "heading_1": "# ",
        "heading_2": "## ",
        "heading_3": "### ",
    }.get(block_type, "")
    return [prefix + text]


def _split_utf16(value: str, limit: int) -> list[str]:
    """UTF-16コードユニット数が limit を超えないように切る。

    Notionが数える文字数はUTF-16基準なので、Pythonのスライスで切ると
    絵文字を含む文字列で上限を超える（RICH_TEXT_LIMIT のコメント参照）。
    サロゲートペアを割ると文字自体が壊れるので、必ず文字単位で積む。
    """
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for char in value:
        width = 2 if ord(char) > 0xFFFF else 1
        if size + width > limit:
            chunks.append("".join(current))
            current = []
            size = 0
        current.append(char)
        size += width
    if current:
        chunks.append("".join(current))
    return chunks


def rich_text(value: str) -> list[dict]:
    """文字列を rich_text 配列へ。長い場合は上限ごとに分割する。"""
    if not value:
        return []
    return [
        {"type": "text", "text": {"content": chunk}}
        for chunk in _split_utf16(value, RICH_TEXT_LIMIT)
    ]


def paragraph_blocks(text: str) -> list[dict]:
    """プレーンテキストを段落ブロックの配列へ。

    Notionのブロック1つあたりの文字数上限を踏まえ、行単位で分ける。
    逐語転記が前提なので、空行も段落として保持する。
    """
    blocks: list[dict] = []
    for line in text.split("\n"):
        for i in range(0, max(len(line), 1), RICH_TEXT_LIMIT):
            blocks.append(
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {"rich_text": rich_text(line[i : i + RICH_TEXT_LIMIT])},
                }
            )
    return blocks


def code_blocks(text: str, *, language: str = "plain text") -> list[dict]:
    """プレーンテキストをコードブロックの配列へ。

    段落と違い、1ブロックに rich_text を100要素まで積めるので
    最大20万字が1ブロックに収まる。**ブロック数を抑えたいときはこちら。**
    等幅で表示されるので、ツリーや原文の表示にも都合がよい。
    """
    limit = RICH_TEXT_LIMIT * RICH_TEXT_PARTS
    blocks: list[dict] = []
    for i in range(0, max(len(text), 1), limit):
        blocks.append(
            {
                "object": "block",
                "type": "code",
                "code": {
                    "rich_text": rich_text(text[i : i + limit]),
                    "language": language,
                },
            }
        )
    return blocks


def plain_text_of(prop: dict | None) -> str:
    """title / rich_text プロパティからプレーンテキストを取り出す。"""
    if not prop:
        return ""
    parts = prop.get("title") or prop.get("rich_text") or []
    return "".join(part.get("plain_text", "") for part in parts)


def page_title(page: dict) -> str:
    """ページのタイトルを取り出す。

    通常ページは properties["title"]、DB行はDB側で決めた任意の名前
    （Inbox DBなら"タイトル"）を使うため、キー名でなく type=="title" で探す。
    """
    for prop in (page.get("properties") or {}).values():
        if prop.get("type") == "title":
            return plain_text_of(prop)
    return ""


def select_of(prop: dict | None) -> str:
    if not prop:
        return ""
    selected = prop.get("select")
    return selected.get("name", "") if selected else ""


def date_of(prop: dict | None) -> str:
    if not prop:
        return ""
    value = prop.get("date")
    return value.get("start", "") if value else ""
