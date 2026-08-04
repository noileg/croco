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
# 似た名前だが別物（2026-07-27に改名して区別を明確化。旧称は起草日時＝発話日時）。
# 起草日時＝アイデアが最初に生まれた時刻（「未処理置き場」子ページのcreated_timeを
#           スクリプトがコピーしたもの）。
# 作成日時＝このInbox行自体がいつ作られたか（Notionのcreated_time、自動付与。
#           捕捉フェーズでAPI登録された時刻）。
P_SPOKEN_AT = "起草日時"
P_CREATED_AT = "作成日時"
P_STARTED_AT = "処理開始日時"
P_FINISHED_AT = "処理日時"
P_RESULT = "実行結果"
P_ATTEMPTS = "試行回数"
# そのアイテムに費やしたトークン（セッションをまたいで積み上げる）。
# 「どれくらい使いそうか」を過去の実績から見積もるための材料でもある。
P_TOKENS = "トークン"
# なぜ「要確認」に落ちたか。「要確認」が理由の分からない山になると、
# 見るたびに1件ずつ「これは何で止まってるんだっけ」を思い出す羽目になる。
# 隔離した側が理由を書き残しておけば、まとめで種類ごとに束ねて出せる。
P_HOLD_REASON = "保留理由"
# セッション中に本人が指示したときだけ立つ、任意の優先度（2026-07-31）。
# Geminiには推定させない。値を誰がどう決めるかは croco_cli.py の priority コマンド側。
P_PRIORITY = "優先度"
# アイテムが属するジャンル/プロジェクト（例：「受験」「クロコ本体」）（2026-08-01）。
# 優先度と違い価値判断ではなく機械的な仕分けなので、種別と同じくGeminiに任せる
# （croco/genre.py）。自由記述にしているのは、selectだと事前に選択肢を
# 列挙する必要があり、増えるたびにスキーマ変更が要るため。
P_GENRE = "ジャンル"

# --- 選択肢 -----------------------------------------------------------

KIND_IDEA = "アイデア"
KIND_SCHEDULE = "予定"
KIND_MATERIAL = "資料"

# 実装フェーズが着手しない種別。
# 「予定」はカレンダー表示用、「資料」は参照用で、どちらも実装の対象ではない。
# これらを弾かないと、実装フェーズが順番に拾って「実装しよう」としてしまう。
NON_IMPLEMENTABLE_KINDS = frozenset({KIND_SCHEDULE, KIND_MATERIAL})

STATUS_TODO = "未処理"
STATUS_DOING = "処理中"
STATUS_DONE = "完了"
STATUS_REVIEW = "要確認"
# 実装フェーズの対象ではないもの（予定・資料）。
# 「未処理」は着手待ちのフラグとして機能しているので、着手されようのないものを
# そこに置き続けるとキューが濁る。かといって「完了」にすると、何もしていないのに
# 完了したことになり、後から解析するときにデータが歪む。
STATUS_EXCLUDED = "対象外"

# --- 保留理由 ---------------------------------------------------------
# 「要確認」に落ちる経路ごとに、何をすればいいかが1対1で決まるように分ける。
# 前半4つは捕捉時にGeminiが判定するもの（gemini.py のenumと一致させること。
# ズレていないかは test_offline.py で確認している）。後半2つはクロコ側で付ける。

HOLD_NONE = "なし"  # 本人対応は不要＝自動で着手してよい
HOLD_DOCUMENT = "本人名義の文書"
HOLD_REAL_WORLD = "現実世界の行動"
HOLD_CROCO = "クロコ自身の改修"
HOLD_JUDGEMENT = "本人の判断"
HOLD_RETRIES = "試行回数の上限"
HOLD_ASKED = "クロコからの相談"

# 表示順と、「で、何をすればいいのか」の一行。
# ここに行動が書けない理由は、そもそも分類として役に立っていない。
HOLD_ACTIONS: dict[str, str] = {
    HOLD_DOCUMENT: "AIに書かせないと決めたもの。本人が書く（下調べまでは頼める）",
    HOLD_REAL_WORLD: "取り寄せ・手続き・予約など。本人が動く",
    HOLD_CROCO: "クロコ自身の改修。本人が普通にClaude Codeを開いて直す",
    HOLD_JUDGEMENT: "本人の価値判断が中身のもの。決めてから戻す",
    HOLD_RETRIES: "同じ失敗を繰り返して止まった。原因を見る",
    HOLD_ASKED: "クロコが判断に詰まって聞いてきた。答えて「未処理」に戻す",
}

HOLD_REASONS = frozenset({HOLD_NONE, *HOLD_ACTIONS})

# --- 優先度 -------------------------------------------------------------
# 種別を問わず（要相談タスク全般に）任意でつけられる3段階（2026-07-31）。
# 未設定は「中」と同じ扱い：普段は予定日順に流れ、明示的に「高」を
# つけたものだけが日付に関係なく上に来る（本人の指定）。

PRIORITY_HIGH = "高"
PRIORITY_MID = "中"
PRIORITY_LOW = "低"
PRIORITIES = (PRIORITY_HIGH, PRIORITY_MID, PRIORITY_LOW)

PRIORITY_ORDER: dict[str, int] = {PRIORITY_HIGH: 0, PRIORITY_MID: 1, PRIORITY_LOW: 2}


def priority_rank(priority: str) -> int:
    """並び替え用の重み。未設定・未知の値は「中」扱い。"""
    return PRIORITY_ORDER.get(priority, PRIORITY_ORDER[PRIORITY_MID])


def priority_sort_key(item: "InboxItem") -> tuple:
    """要相談リストの並び順。

    優先度（高→中/未設定→低）を主軸に、同じ優先度内では予定日が近い順
    （2026-07-31、本人の指定）。予定日が無いもの（予定以外の種別が大半）は
    その優先度グループの末尾へ回し、その中は作成順。
    `croco_cli.py list` 用に作った並び順だが、ランチャー起動時に出す
    要相談リスト（report.py・consult.py）にも同じ基準を適用する
    （2026-08-01、当初list専用だったのを本人の指摘で共通化）。
    """
    no_schedule = 0 if item.scheduled else 1
    return (
        priority_rank(item.priority),
        no_schedule,
        item.scheduled or item.page.get("created_time", ""),
    )


def priority_label(item: "InboxItem") -> str:
    """表示用の優先度ラベル。未設定でも並び替え上の既定値（中）を明示する。"""
    return item.priority or PRIORITY_MID


def elapsed_days(item: "InboxItem") -> int | None:
    """作成日時（Notionの`created_time`）からの経過日数。取得できなければNone。

    当初は`last_edited_time`（最終更新）を使っていたが、ジャンル一括付与のような
    メタデータ更新でも書き換わってしまい、中身が何も進んでいないアイテムまで
    「今日更新」と表示される副作用があった（2026-08-01）。
    このアーキテクチャは「即処分」が原則（さっさと実装するためのもので、
    寝かせておく前提が無い）なので、知りたいのは「最後にいつ触ったか」ではなく
    「いつから存在し続けているか」。作成日時なら触るだけでは動かないので、
    こちらに変更した（本人の指摘）。
    """
    created = item.page.get("created_time", "")
    if not created:
        return None
    try:
        made = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (datetime.now().astimezone() - made).days


def elapsed_label(item: "InboxItem") -> str:
    """表示用の経過日数ラベル。取得できなければ空文字。"""
    days = elapsed_days(item)
    if days is None:
        return ""
    return "今日" if days <= 0 else f"{days}日前"


def compact_tag(item: "InboxItem") -> str:
    """ジャンル・優先度・経過日数を1つの角括弧にまとめた表示用タグ。

    要相談リストが「保留理由」「ジャンル」「優先度」「経過日数」と角括弧4つ
    並ぶまで詰め込まれ読みにくくなったための整理（2026-08-01、本人の指摘）。
    保留理由は「対処が保留理由ごとに決まる」別種の情報なので、こちらには含めず
    呼び出し元で別枠にする。並び順（優先度が主軸、項番22/25）は変えず表記だけ圧縮。
    """
    parts = [p for p in (item.genre, priority_label(item), elapsed_label(item)) if p]
    return "/".join(parts)

# DB作成時に渡すスキーマ定義。
SCHEMA: dict[str, Any] = {
    P_TITLE: {"title": {}},
    P_KIND: {
        "select": {
            "options": [
                {"name": KIND_IDEA, "color": "blue"},
                {"name": KIND_SCHEDULE, "color": "green"},
                {"name": KIND_MATERIAL, "color": "gray"},
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
                {"name": STATUS_EXCLUDED, "color": "gray"},
            ]
        }
    },
    P_SPOKEN_AT: {"date": {}},
    P_CREATED_AT: {"created_time": {}},
    P_STARTED_AT: {"date": {}},
    P_FINISHED_AT: {"date": {}},
    P_RESULT: {"rich_text": {}},
    P_ATTEMPTS: {"number": {"format": "number"}},
    P_TOKENS: {"number": {"format": "number"}},
    P_HOLD_REASON: {
        "select": {
            "options": [
                {"name": HOLD_NONE, "color": "default"},
                {"name": HOLD_DOCUMENT, "color": "red"},
                {"name": HOLD_REAL_WORLD, "color": "orange"},
                {"name": HOLD_CROCO, "color": "purple"},
                {"name": HOLD_JUDGEMENT, "color": "yellow"},
                {"name": HOLD_RETRIES, "color": "brown"},
                {"name": HOLD_ASKED, "color": "blue"},
            ]
        }
    },
    P_PRIORITY: {
        "select": {
            "options": [
                {"name": PRIORITY_HIGH, "color": "red"},
                {"name": PRIORITY_MID, "color": "yellow"},
                {"name": PRIORITY_LOW, "color": "gray"},
            ]
        }
    },
    P_GENRE: {"rich_text": {}},
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


def build_properties(item: dict, *, spoken_at: str | None, genre: str = "") -> dict:
    """Gemini の出力1件を Notion のプロパティ辞書に変換する。

    ステータスは作成時つねに「未処理」固定なのでここでハードコードする
    （Gemini のスキーマには含めない：仕様書2.5章-11）。
    """
    kind = item.get("kind") or KIND_IDEA

    # 本人自身が対応すべきものは、最初から自動キューに入れない。
    # 実装フェーズ側の指示（プロンプト）で自制させる方法もあるが、指示は破られうる。
    # 分類の段階で「要確認」に落としておけば、そもそも着手対象に選ばれない。
    # 値が欠けていた・知らない値だった場合は安全側（本人対応が必要）に倒す。
    reason = item.get("human_reason") or HOLD_JUDGEMENT
    if reason not in HOLD_REASONS:
        reason = HOLD_JUDGEMENT

    if kind in NON_IMPLEMENTABLE_KINDS:
        # 予定・資料はそもそも着手されないので、キューに残さない。
        status = STATUS_EXCLUDED
    elif reason != HOLD_NONE:
        status = STATUS_REVIEW
    else:
        status = STATUS_TODO

    properties: dict[str, Any] = {
        P_TITLE: {"title": nt.rich_text(item["title"])},
        P_KIND: {"select": {"name": kind}},
        P_STATUS: {"select": {"name": status}},
        P_ATTEMPTS: {"number": 0},
        P_HOLD_REASON: {"select": {"name": reason}},
    }
    if genre:
        properties[P_GENRE] = {"rich_text": nt.rich_text(genre)}
    if status == STATUS_REVIEW:
        properties[P_RESULT] = {
            "rich_text": nt.rich_text(
                f"[{now_iso()[:16].replace('T', ' ')}] "
                f"着手せず要確認にした（{reason}）"
            )
        }

    # Geminiが日付を取れていれば入れる。「アイデア」に分類されたものでも
    # 日付が取れているなら捨てない（分類を誤っていた場合に情報が失われるため。
    # カレンダー表示は種別で絞られるので、入っていても表示は乱れない）。
    scheduled = normalize_date(item.get("scheduled_date"))
    if scheduled:
        value = {"start": scheduled}
        # 「〜から〜まで」の期間は終了日も持たせる。
        # Notionは end < start を拒否するので、逆転していたら捨てる。
        end = normalize_date(item.get("scheduled_end"))
        if end and end > scheduled:
            value["end"] = end
        properties[P_SCHEDULED] = {"date": value}

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
        self.hold_reason = nt.select_of(props.get(P_HOLD_REASON))
        self.priority = nt.select_of(props.get(P_PRIORITY))
        self.genre = nt.plain_text_of(props.get(P_GENRE))
        attempts = props.get(P_ATTEMPTS) or {}
        self.attempts = int(attempts.get("number") or 0)
        tokens = props.get(P_TOKENS) or {}
        self.tokens = int(tokens.get("number") or 0)
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
