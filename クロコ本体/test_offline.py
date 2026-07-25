"""ネットワークに触らない範囲の動作確認。

    python test_offline.py

APIキー不要。設定やスキーマをいじった後の回帰確認に使う。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from croco import gemini, inbox, notion as nt
from croco.config import Config, ConfigError, _parse_env_file

failures = []


def check(label, got, want):
    if got != want:
        failures.append(f"{label}\n    got : {got!r}\n    want: {want!r}")


# --- .env パース ---------------------------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    env_file = Path(tmp) / ".env"
    env_file.write_text(
        "# comment\n"
        "\n"
        "NOTION_TOKEN=ntn_abc123\n"
        'GEMINI_API_KEY="quoted-key"\n'
        "export CROCO_MAX_RETRIES=5\n"
        "EMPTY=\n"
        "WEIRD_NO_EQUALS\n",
        encoding="utf-8",
    )
    parsed = _parse_env_file(env_file)
    check("env: 通常値", parsed.get("NOTION_TOKEN"), "ntn_abc123")
    check("env: 引用符剥がし", parsed.get("GEMINI_API_KEY"), "quoted-key")
    check("env: export接頭辞", parsed.get("CROCO_MAX_RETRIES"), "5")
    check("env: =なしの行は無視", "WEIRD_NO_EQUALS" in parsed, False)

# --- 必須設定が無いときのエラー -------------------------------------------
cfg = Config({"_ENV_PATH": "dummy"})
try:
    cfg.notion_token
    failures.append("必須設定が無いのに例外が出ませんでした")
except ConfigError as exc:
    check("設定エラーにキー名が含まれる", "NOTION_TOKEN" in str(exc), True)

# --- 既定値 --------------------------------------------------------------
check("既定モデル", cfg.gemini_model, "gemini-3.6-flash")
check("既定thinking", cfg.gemini_thinking_level, "minimal")
check("既定リトライ上限", cfg.max_retries, 3)
check("既定Notionバージョン", cfg.notion_version, "2026-03-11")
check("projects_dir 既定", cfg.projects_dir.name, "クロコ管轄プロジェクト")

# --- rich_text の分割 -----------------------------------------------------
long_text = "あ" * 4500
chunks = nt.rich_text(long_text)
check("rich_text: 分割数", len(chunks), 3)
check("rich_text: 各要素が上限以下", all(len(c["text"]["content"]) <= 2000 for c in chunks), True)
check("rich_text: 連結すると原文", "".join(c["text"]["content"] for c in chunks), long_text)
check("rich_text: 空文字", nt.rich_text(""), [])

# --- ブロック → テキスト ---------------------------------------------------
blocks = [
    {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "最初の行"}]}},
    {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"plain_text": "箇条書き"}]}},
    {"type": "heading_2", "heading_2": {"rich_text": [{"plain_text": "見出し"}]}},
    {"type": "paragraph", "paragraph": {"rich_text": []}},
    {"type": "image", "image": {"caption": []}},
]
lines = []
for b in blocks:
    lines.extend(nt._block_to_lines(b))
check("block→text", lines, ["最初の行", "- 箇条書き", "## 見出し", ""])

# --- 段落ブロック化 --------------------------------------------------------
para = nt.paragraph_blocks("一行目\n\n三行目")
check("paragraph_blocks: 行数（空行も保持）", len(para), 3)
check("paragraph_blocks: 空行の中身", para[1]["paragraph"]["rich_text"], [])

# --- プロパティ読み出し ----------------------------------------------------
check("plain_text_of: title", nt.plain_text_of({"title": [{"plain_text": "T"}]}), "T")
check("plain_text_of: None", nt.plain_text_of(None), "")
check("select_of: 空", nt.select_of({"select": None}), "")
check("date_of", nt.date_of({"date": {"start": "2026-08-03"}}), "2026-08-03")

# --- Gemini レスポンスのパース ----------------------------------------------
def response_with(items):
    return {"candidates": [{"content": {"parts": [{"text": json.dumps({"items": items})}]}}]}


parsed_items = gemini._parse_items(
    response_with(
        [
            {"title": "案A", "body": "本文A", "kind": "アイデア", "scheduled_date": ""},
            {"title": "", "body": "タイトル無しの本文", "kind": "予定", "scheduled_date": "2026-08-03"},
            {"title": "空本文", "body": "   ", "kind": "アイデア", "scheduled_date": ""},
            {"title": "変な種別", "body": "本文C", "kind": "その他", "scheduled_date": ""},
        ]
    )
)
check("gemini: 空本文を除外", len(parsed_items), 3)
check("gemini: タイトル補完", parsed_items[1]["title"], "タイトル無しの本文")
check("gemini: 未知の種別はアイデアに寄せる", parsed_items[2]["kind"], "アイデア")

for bad, label in [
    ({"candidates": []}, "候補なし"),
    ({"candidates": [{"content": {"parts": [{"text": "not json"}]}}]}, "JSONでない"),
    ({"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}, "items無し"),
]:
    try:
        gemini._parse_items(bad)
        failures.append(f"gemini: {label} で例外が出ませんでした")
    except RuntimeError:
        pass

# --- Inbox プロパティ構築 ---------------------------------------------------
props = inbox.build_properties(
    {"title": "予定の件", "body": "x", "kind": "予定", "scheduled_date": "2026-08-03"},
    spoken_at="2026-07-25T10:00:00.000Z",
)
check("inbox: ステータス固定", props[inbox.P_STATUS]["select"]["name"], "未処理")
check("inbox: 試行回数初期値", props[inbox.P_ATTEMPTS]["number"], 0)
check("inbox: 予定日", props[inbox.P_SCHEDULED]["date"]["start"], "2026-08-03")
check("inbox: 発話日時", props[inbox.P_SPOKEN_AT]["date"]["start"], "2026-07-25T10:00:00.000Z")

idea_props = inbox.build_properties(
    {"title": "案", "body": "x", "kind": "アイデア", "scheduled_date": "2026-08-03"},
    spoken_at=None,
)
check("inbox: アイデアでも取れた日付は捨てない", idea_props[inbox.P_SCHEDULED]["date"]["start"], "2026-08-03")
check("inbox: 発話日時なし", inbox.P_SPOKEN_AT in idea_props, False)

bad_date_props = inbox.build_properties(
    {"title": "案", "body": "x", "kind": "予定", "scheduled_date": "8月3日"},
    spoken_at=None,
)
check("inbox: 非ISO日付は落として本文を残す", inbox.P_SCHEDULED in bad_date_props, False)

# --- 日付の正規化 ----------------------------------------------------------
check("date: 日付のみ", inbox.normalize_date("2026-08-03"), "2026-08-03")
check("date: 日時", inbox.normalize_date("2026-08-03T14:30:00"), "2026-08-03T14:30:00")
check("date: 分まで", inbox.normalize_date("2026-08-03T14:30"), "2026-08-03T14:30")
check("date: TZ付き", inbox.normalize_date("2026-08-03T14:30:00+09:00"), "2026-08-03T14:30:00+09:00")
check("date: 和文表記は弾く", inbox.normalize_date("8月3日"), "")
check("date: 自然文は弾く", inbox.normalize_date("来週の火曜"), "")
check("date: 空", inbox.normalize_date(""), "")
check("date: None", inbox.normalize_date(None), "")
check("date: 不正な日付", inbox.normalize_date("2026-13-45"), "")

# --- 並び順（処理中を優先） --------------------------------------------------
def make_item(status, created, attempts=0):
    return inbox.InboxItem(
        {
            "id": f"{status}-{created}",
            "created_time": created,
            "properties": {
                inbox.P_TITLE: {"title": [{"plain_text": "t"}]},
                inbox.P_STATUS: {"select": {"name": status}},
                inbox.P_KIND: {"select": {"name": "アイデア"}},
                inbox.P_ATTEMPTS: {"number": attempts},
            },
        }
    )


items = [
    make_item("未処理", "2026-07-01"),
    make_item("処理中", "2026-07-20"),
    make_item("未処理", "2026-06-01"),
]
items.sort(key=inbox.sort_key)
check("並び順: 処理中が先頭", items[0].status, "処理中")
check("並び順: 未処理は古い順", [i.page["created_time"] for i in items[1:]], ["2026-06-01", "2026-07-01"])
check("InboxItem: 試行回数", make_item("未処理", "x", attempts=2).attempts, 2)

# --- 結果 ----------------------------------------------------------------
if failures:
    print(f"FAILED ({len(failures)})")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("すべて通過しました")
