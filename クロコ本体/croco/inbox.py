"""Inbox DB のスキーマ定義と読み書き。

プロパティ構成は仕様書2.5章-10の表に対応する。
DB作成（setup_notion.py）と読み書き（capture / dispatch）で
同じ定義を共有し、名前のズレが起きないようここに集約する。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from . import notion as nt

# --- プロパティ名（Notion上の表示名） ---------------------------------

P_TITLE = "タイトル"
P_KIND = "種別"
P_SCHEDULED = "予定日"
P_STATUS = "ステータス"
P_SPOKEN_AT = "発話日時"
P_CREATED_AT = "作成日時"
P_STARTED_AT = "処理開始日時"
P_FINISHED_AT = "処理日時"
P_RESULT = "実行結果"
P_ATTEMPTS = "試行回数"

# --- 選択肢 -----------------------------------------------------------

KIND_IDEA = "アイデア"
KIND_SCHEDULE = "予定"

STATUS_TODO = "未処理"
STATUS_DOING = "処理中"
STATUS_DONE = "完了"
STATUS_REVIEW = "要確認"

# DB作成時に渡すスキーマ定義。
SCHEMA: dict[str, Any] = {
    P_TITLE: {"title": {}},
    P_KIND: {
        "select": {
            "options": [
                {"name": KIND_IDEA, "color": "blue"},
                {"name": KIND_SCHEDULE, "color": "green"},
            ]
        }
    },
    P_SCHEDULED: {"date": {}},
    P_STATUS: {
        "select": {
            "options": [
                {"name": STATUS_TODO, "color": "default"},
                {"name": STATUS_DOING, "color": "yellow"},
                {"name": STATUS_DONE, "color": "green"},
                {"name": STATUS_REVIEW, "color": "red"},
            ]
        }
    },
    P_SPOKEN_AT: {"date": {}},
    P_CREATED_AT: {"created_time": {}},
    P_STARTED_AT: {"date": {}},
    P_FINISHED_AT: {"date": {}},
    P_RESULT: {"rich_text": {}},
    P_ATTEMPTS: {"number": {"format": "number"}},
}


def normalize_date(value: str | None) -> str:
    """Notionのdateプロパティに渡せる形式なら返し、そうでなければ空文字列。

    ここで弾いておかないと、Geminiが「8月3日」のような非ISO表記を返した場合に
    Notionが400を返し、**そのアイテムごと登録に失敗する**。登録に失敗すると
    元のメモが移動されず、毎回の起動で同じ失敗を繰り返すことになる。
    日付だけ落として本文を残す方が損失が小さいため、そう倒す。
    """
    text = (value or "").strip()
    if not text:
        return ""
    # Notionが受け付けるのは ISO 8601（日付のみ、または日時）。
    for pattern in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            datetime.strptime(text, pattern)
            return text
        except ValueError:
            continue
    # タイムゾーン付きなど、上記で拾えない正当な形式も受け付ける。
    try:
        datetime.fromisoformat(text)
        return text
    except ValueError:
        return ""


def build_properties(item: dict, *, spoken_at: str | None) -> dict:
    """Gemini の出力1件を Notion のプロパティ辞書に変換する。

    ステータスは作成時つねに「未処理」固定なのでここでハードコードする
    （Gemini のスキーマには含めない：仕様書2.5章-11）。
    """
    properties: dict[str, Any] = {
        P_TITLE: {"title": nt.rich_text(item["title"])},
        P_KIND: {"select": {"name": item.get("kind") or KIND_IDEA}},
        P_STATUS: {"select": {"name": STATUS_TODO}},
        P_ATTEMPTS: {"number": 0},
    }

    # Geminiが日付を取れていれば入れる。「アイデア」に分類されたものでも
    # 日付が取れているなら捨てない（分類を誤っていた場合に情報が失われるため。
    # カレンダー表示は種別で絞られるので、入っていても表示は乱れない）。
    scheduled = normalize_date(item.get("scheduled_date"))
    if scheduled:
        properties[P_SCHEDULED] = {"date": {"start": scheduled}}

    # 「未処理置き場」子ページの作成日時をそのまま写す。
    # LLMに現在時刻を自己申告させない（仕様書2.5章-10）。
    if spoken_at:
        properties[P_SPOKEN_AT] = {"date": {"start": spoken_at}}

    return properties


class InboxItem:
    """Inbox DB の1行。"""

    def __init__(self, page: dict) -> None:
        self.page = page
        self.id: str = page["id"]
        props = page.get("properties", {})
        self.title = nt.plain_text_of(props.get(P_TITLE))
        self.kind = nt.select_of(props.get(P_KIND))
        self.status = nt.select_of(props.get(P_STATUS))
        self.scheduled = nt.date_of(props.get(P_SCHEDULED))
        self.result_log = nt.plain_text_of(props.get(P_RESULT))
        attempts = props.get(P_ATTEMPTS) or {}
        self.attempts = int(attempts.get("number") or 0)
        self.url: str = page.get("url", "")

    def __repr__(self) -> str:
        return f"<InboxItem {self.status} {self.title!r}>"


def pending_filter() -> dict:
    """「処理中」または「未処理」を拾うフィルタ。"""
    return {
        "or": [
            {"property": P_STATUS, "select": {"equals": STATUS_DOING}},
            {"property": P_STATUS, "select": {"equals": STATUS_TODO}},
        ]
    }


def sort_key(item: InboxItem) -> tuple:
    """処理順。

    仕様書2.5章-10で確定したとおり、新規の「未処理」より「処理中」を優先する。
    これにより通常の再開と、中断からの復旧が同じ仕組みで処理される。
    同順位内では発話が古いものから。
    """
    status_rank = 0 if item.status == STATUS_DOING else 1
    created = item.page.get("created_time", "")
    return (status_rank, created)


def now_iso() -> str:
    """Notionのdateプロパティに渡せる形式のローカル現在時刻。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")
