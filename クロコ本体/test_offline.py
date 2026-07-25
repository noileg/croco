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
check("既定の連続処理上限", cfg.max_items, 3)
check("既定のトークン上限は無制限", cfg.token_budget, 0)
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
            {"title": "案A", "body": "本文A", "kind": "アイデア", "scheduled_date": "", "scheduled_end": ""},
            {"title": "", "body": "タイトル無しの本文", "kind": "予定", "scheduled_date": "2026-08-03"},
            {"title": "空本文", "body": "   ", "kind": "アイデア", "scheduled_date": ""},
            {"title": "変な種別", "body": "本文C", "kind": "その他", "scheduled_date": ""},
        ]
    )
)
check("gemini: 空本文を除外", len(parsed_items), 3)
check("gemini: タイトル補完", parsed_items[1]["title"], "タイトル無しの本文")
check("gemini: 未知の種別はアイデアに寄せる", parsed_items[2]["kind"], "アイデア")
check(
    "gemini: 資料は有効な種別として通す",
    gemini._parse_items(response_with([{"title": "前提", "body": "注意事項", "kind": "資料", "scheduled_date": ""}]))[0]["kind"],
    "資料",
)

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
check("inbox: 予定は対象外で作られる", props[inbox.P_STATUS]["select"]["name"], "対象外")
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

# --- 期間（開始〜終了） ------------------------------------------------------
span = inbox.build_properties(
    {"title": "出願期間", "body": "x", "kind": "予定",
     "scheduled_date": "2026-08-06", "scheduled_end": "2026-08-12"},
    spoken_at=None,
)[inbox.P_SCHEDULED]["date"]
check("期間: 開始", span["start"], "2026-08-06")
check("期間: 終了", span.get("end"), "2026-08-12")

single = inbox.build_properties(
    {"title": "発表", "body": "x", "kind": "予定",
     "scheduled_date": "2026-10-01", "scheduled_end": ""},
    spoken_at=None,
)[inbox.P_SCHEDULED]["date"]
check("単日: endを付けない", "end" in single, False)

reversed_span = inbox.build_properties(
    {"title": "逆転", "body": "x", "kind": "予定",
     "scheduled_date": "2026-08-12", "scheduled_end": "2026-08-06"},
    spoken_at=None,
)[inbox.P_SCHEDULED]["date"]
check("終了が開始より前なら捨てる", "end" in reversed_span, False)

bad_end = inbox.build_properties(
    {"title": "不正な終了", "body": "x", "kind": "予定",
     "scheduled_date": "2026-08-06", "scheduled_end": "8月12日"},
    spoken_at=None,
)[inbox.P_SCHEDULED]["date"]
check("非ISOの終了は捨てるが開始は残す", ("end" in bad_end, bad_end["start"]), (False, "2026-08-06"))

# --- 本人対応が必要なものは着手キューに入れない -------------------------------
for reason in ("本人名義の文書", "現実世界の行動", "クロコ自身の改修", "本人の判断"):
    human = inbox.build_properties(
        {"title": "x", "body": "x", "kind": "アイデア",
         "scheduled_date": "", "human_reason": reason},
        spoken_at=None,
    )
    check(f"本人対応({reason}): 要確認", human[inbox.P_STATUS]["select"]["name"], "要確認")
    check(f"本人対応({reason}): 保留理由が入る",
          human[inbox.P_HOLD_REASON]["select"]["name"], reason)
    sent_text = "".join(c["text"]["content"] for c in human[inbox.P_RESULT]["rich_text"])
    check(f"本人対応({reason}): 理由が実行結果に残る", reason in sent_text, True)

auto = inbox.build_properties(
    {"title": "ロガー実装", "body": "x", "kind": "アイデア",
     "scheduled_date": "", "human_reason": "なし"},
    spoken_at=None,
)
check("通常: ステータスが未処理", auto[inbox.P_STATUS]["select"]["name"], "未処理")
check("通常: 実行結果は空のまま", inbox.P_RESULT in auto, False)
check("通常: 保留理由はなし", auto[inbox.P_HOLD_REASON]["select"]["name"], "なし")

# 知らない値・欠落は「なし」に倒さない（倒すと自動着手されてしまう）
for broken in ({"human_reason": "でたらめ"}, {"human_reason": ""}, {}):
    item = {"title": "x", "body": "x", "kind": "アイデア", "scheduled_date": "", **broken}
    check(f"不正な保留理由は要確認へ倒す({broken})",
          inbox.build_properties(item, spoken_at=None)[inbox.P_STATUS]["select"]["name"], "要確認")

# 保留理由の選択肢が、Geminiに提示しているenumを網羅していること。
# 別ファイルに同じ文字列を書いているので、片方だけ増やすと黙って握り潰される。
check("保留理由: Geminiのenumをすべて知っている",
      gemini.VALID_HUMAN_REASONS <= inbox.HOLD_REASONS, True)
check("保留理由: DBの選択肢が定義と一致",
      {o["name"] for o in inbox.SCHEMA[inbox.P_HOLD_REASON]["select"]["options"]},
      set(inbox.HOLD_REASONS))
check("保留理由: 「なし」以外はすべて対処法が書いてある",
      set(inbox.HOLD_ACTIONS) | {inbox.HOLD_NONE}, set(inbox.HOLD_REASONS))
check("保留理由: Geminiの既定値は要確認になる側",
      gemini.DEFAULT_HUMAN_REASON != inbox.HOLD_NONE, True)

# --- 着手対象でない種別は「対象外」にする -------------------------------------
for kind in ("予定", "資料"):
    st = inbox.build_properties(
        {"title": "x", "body": "x", "kind": kind, "scheduled_date": "", "human_reason": "なし"},
        spoken_at=None,
    )[inbox.P_STATUS]["select"]["name"]
    check(f"{kind}: ステータスが対象外", st, "対象外")

# 種別による判定が保留理由より優先される（予定はそもそも着手されないため）
st = inbox.build_properties(
    {"title": "x", "body": "x", "kind": "予定", "scheduled_date": "", "human_reason": "本人の判断"},
    spoken_at=None,
)[inbox.P_STATUS]["select"]["name"]
check("予定は保留理由があっても対象外", st, "対象外")

check("対象外はステータスの選択肢にある",
      "対象外" in [o["name"] for o in inbox.SCHEMA[inbox.P_STATUS]["select"]["options"]], True)

# 着手候補を拾うフィルタが対象外を含まないこと
statuses = [c["select"]["equals"] for c in inbox.pending_filter()["or"]]
check("着手フィルタは未処理と処理中だけ", sorted(statuses), sorted(["未処理", "処理中"]))

# 値が欠けている・enumにない場合は安全側（要確認）に倒す
missing = gemini._parse_items(response_with([{"title": "T", "body": "B", "kind": "アイデア"}]))[0]
check("human_reason欠落時は安全側", missing["human_reason"], gemini.DEFAULT_HUMAN_REASON)
check(
    "human_reasonがenum外なら安全側",
    gemini._parse_items(response_with([{"title": "T", "body": "B", "kind": "アイデア", "human_reason": "でたらめ"}]))[0]["human_reason"],
    gemini.DEFAULT_HUMAN_REASON,
)
check(
    "human_reason=なしは尊重する",
    gemini._parse_items(response_with([{"title": "T", "body": "B", "kind": "アイデア", "human_reason": "なし"}]))[0]["human_reason"],
    "なし",
)
check(
    "human_reason=クロコ自身の改修を尊重する",
    gemini._parse_items(response_with([{"title": "T", "body": "B", "kind": "アイデア", "human_reason": "クロコ自身の改修"}]))[0]["human_reason"],
    "クロコ自身の改修",
)

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

# --- 実装フェーズが着手しない種別 --------------------------------------------
check("着手対象外: 予定", "予定" in inbox.NON_IMPLEMENTABLE_KINDS, True)
check("着手対象外: 資料", "資料" in inbox.NON_IMPLEMENTABLE_KINDS, True)
check("着手対象: アイデア", "アイデア" in inbox.NON_IMPLEMENTABLE_KINDS, False)
check("種別の選択肢に資料がある", "資料" in [o["name"] for o in inbox.SCHEMA[inbox.P_KIND]["select"]["options"]], True)
check("並び順: 未処理は古い順", [i.page["created_time"] for i in items[1:]], ["2026-06-01", "2026-07-01"])
check("InboxItem: 試行回数", make_item("未処理", "x", attempts=2).attempts, 2)

# --- stream-json の描画 ------------------------------------------------------
from croco.dispatch import _render_event  # noqa: E402


def ev(obj):
    return _render_event(json.dumps(obj))


check("描画: 本文の前後空白を落とす", ev({"type": "assistant", "message": {"content": [{"type": "text", "text": "  調べます  "}]}}), ["調べます"])
check(
    "描画: ツールは名前と要点だけ",
    ev({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Write", "input": {"file_path": "a.md", "content": "長い本文" * 100}}]}}),
    ["  → Write: a.md"],
)
check(
    "描画: 長いコマンドは切り詰める",
    len(ev({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "x" * 500}}]}})[0]) < 140,
    True,
)
check(
    "描画: ツール結果はエラー件数を出す",
    ev({"type": "user", "message": {"content": [{"type": "tool_result"}, {"type": "tool_result", "is_error": True}]}}),
    ["  ← ツール結果: 2件（うちエラー 1件）"],
)
check("描画: 未知のイベントは黙殺", ev({"type": "未知"}), [])
check("描画: 空行は黙殺", _render_event("   "), [])
check("描画: JSONでない行はそのまま出す", _render_event("raw line"), ["raw line"])
check("描画: JSON配列でも落ちない", _render_event("[1,2]"), ["[1,2]"])
check(
    "描画: 完了イベント",
    ev({"type": "result", "subtype": "success", "num_turns": 7, "total_cost_usd": 0.1234, "result": "できました"}),
    ["[完了] success / 7ターン / $0.1234", "できました"],
)

# --- 使用トークンの集計 --------------------------------------------------------
from croco import usage as _usage  # noqa: E402

u = _usage.Usage(input_tokens=10, output_tokens=20, cache_write_tokens=30, cache_read_tokens=900, messages=2)
check("usage: freshにキャッシュ読みを含めない", u.fresh, 60)
check("usage: totalには含める", u.total, 960)
check("usage: 加算", (u + u).fresh, 120)
check("usage: 表示に桁の丸めが入る", "60" in u.format(), True)
check("compact: 千", _usage.compact(1500), "1.5k")
check("compact: 百万", _usage.compact(2_500_000), "2.5M")
check("compact: そのまま", _usage.compact(999), "999")

with tempfile.TemporaryDirectory() as tmp:
    jsonl = Path(tmp) / "s.jsonl"
    jsonl.write_text(
        json.dumps({"cwd": "C:/work", "message": {"id": "a", "usage": {"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4}}}) + "\n"
        # 同じidの重複は数えない
        + json.dumps({"message": {"id": "a", "usage": {"input_tokens": 1, "output_tokens": 2, "cache_creation_input_tokens": 3, "cache_read_input_tokens": 4}}}) + "\n"
        + json.dumps({"message": {"id": "b", "usage": {"input_tokens": 10, "output_tokens": 20, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}}) + "\n"
        + "壊れた行\n"
        + json.dumps({"type": "user", "message": {"content": "usageなし"}}) + "\n",
        encoding="utf-8",
    )
    parsed = _usage.read(jsonl)
    check("usage: 重複を除いて集計", parsed.messages, 2)
    check("usage: 合計", (parsed.input_tokens, parsed.output_tokens), (11, 22))
    check("usage: 壊れた行で落ちない", parsed.fresh, 36)
    check("usage: 記録からcwdを読む", _usage._transcript_cwd(jsonl), "C:/work")

check("usage: 記録が無ければNone", _usage.measure(Path(r"C:\存在しない"), since=0), None)

# --- 連続処理のループ制御 ------------------------------------------------------
# 実際にクロコを起動せずに止まり方だけを見たいので、dispatch.run を差し替える。
from croco import dispatch as _dispatch  # noqa: E402

O = _dispatch.Outcome


def loop(outcomes, **env):
    settings = {"_ENV_PATH": "dummy", "CROCO_PICK_TIMEOUT": "0"}
    settings.update(env)
    calls = []
    original = _dispatch.run

    def fake(config, *, exclude=frozenset()):
        calls.append(exclude)
        # 用意した数を超えて呼ばれたら「対象なし」を返す。
        # 止まるべき所で止まらない不具合を、例外ではなく件数の食い違いとして出すため。
        index = len(calls) - 1
        return outcomes[index] if index < len(outcomes) else O(None, True, 0)

    _dispatch.run = fake
    try:
        return _dispatch.run_all(Config(settings)), calls
    finally:
        _dispatch.run = original


summary, calls = loop([O("a", True, 100), O("b", True, 200), O("c", True, 300), O("d", True, 400)])
check("連続処理: 上限3件で止まる", (summary.items, len(calls)), (3, 3))
check("連続処理: トークンを合計する", summary.tokens, 600)
check("連続処理: 処理済みを次に渡して除外させる", calls[2], frozenset({"a", "b"}))

summary, calls = loop([O("a", True, 10), O(None, True, 0)])
check("連続処理: 対象が尽きたら止まる", (summary.items, len(calls)), (1, 2))

summary, calls = loop([O("a", False, 50), O("b", True, 10)])
check("連続処理: 異常終了・中断なら次に進まない", (summary.items, summary.tokens, len(calls)), (1, 50, 1))

summary, _ = loop([O("a", True, 500), O("b", True, 500)], CROCO_TOKEN_BUDGET="400")
check("連続処理: トークン上限を超えたら止まる", summary.items, 1)

summary, calls = loop([O("a", True, 10), O("b", True, 10)], CROCO_MAX_ITEMS="1")
check("連続処理: 上限1なら1件だけ", (summary.items, len(calls)), (1, 1))

summary, calls = loop([O("a", True, 0), O("b", True, 0)], CROCO_DRY_RUN="1")
check("連続処理: dry-runは1件だけ", (summary.items, len(calls)), (1, 1))

summary, calls = loop([O("a", True, 10)], CROCO_MAX_ITEMS="0")
check("連続処理: 上限0なら着手しない", (summary.items, len(calls)), (0, 0))

# --- 「要確認」を保留理由ごとに束ねて出す -----------------------------------------
from croco import log as _log, report as _report  # noqa: E402


def review_item(title, reason):
    props = {
        inbox.P_TITLE: {"title": [{"plain_text": title}]},
        inbox.P_STATUS: {"select": {"name": "要確認"}},
    }
    if reason is not None:
        props[inbox.P_HOLD_REASON] = {"select": {"name": reason}}
    return inbox.InboxItem({"id": title, "properties": props})


lines = []
original_log = _log.log
_log.log = lines.append
try:
    _report._show_review([
        review_item("旧いやつ", None),
        review_item("研究室調べ", inbox.HOLD_JUDGEMENT),
        review_item("dispatchを直して", inbox.HOLD_CROCO),
        review_item("志望理由書", inbox.HOLD_DOCUMENT),
        review_item("自己推薦書", inbox.HOLD_DOCUMENT),
    ])
finally:
    _log.log = original_log

heads = [l for l in lines if l.startswith("  【")]
check("要確認: 同じ理由をまとめる", heads[0].startswith("  【本人名義の文書】2件"), True)
check("要確認: 定義した順で出す",
      [h.split("】")[0] for h in heads],
      ["  【本人名義の文書", "  【クロコ自身の改修", "  【本人の判断", "  【（理由の記録なし）"])
check("要確認: 対処法を添える", all(" … " in h for h in heads), True)
check("要確認: 全件が出ている", len([l for l in lines if l.startswith("    ・")]), 5)
check("要確認: InboxItemが保留理由を読める",
      review_item("x", inbox.HOLD_CROCO).hold_reason, inbox.HOLD_CROCO)

# --- 相談フェーズ ---------------------------------------------------------------
from croco import consult as _consult  # noqa: E402

check("相談: 全ての保留理由にクロコ向けの注意がある",
      set(_consult.REASON_NOTES), set(inbox.HOLD_ACTIONS))
check("相談: 「なし」には注意を持たない", inbox.HOLD_NONE in _consult.REASON_NOTES, False)
check("既定の相談猶予", cfg.consult_timeout, 20.0)
# 着手の猶予より短いこと。相談は「要確認」が1件でもあれば毎回出るので、
# 長いと全処理が終わったあとに毎回その分だけ待たされる。
check("相談の猶予は着手より短い", cfg.consult_timeout < cfg.pick_timeout, True)
check("相談猶予0で聞かなくなる", Config({"CROCO_CONSULT_TIMEOUT": "0"}).consult_timeout, 0.0)
check("エディタの既定パス", cfg.editor_path.name, "Twitter-like-char-counter.html")

consult_prompt = _consult.PROMPT_TEMPLATE.format(
    page_id="pid", title="自己推薦書", hold_reason=inbox.HOLD_DOCUMENT,
    body="B", progress="P", reason_note=_consult.REASON_NOTES[inbox.HOLD_DOCUMENT],
    cli_path="C:/cli.py", projects_dir="C:/proj",
)
check("相談: resume の呼び方が入っている", "resume pid" in consult_prompt, True)
check("相談: log と review の呼び方も入っている",
      ("log pid" in consult_prompt, "review pid" in consult_prompt), (True, True))
check("相談: 推測で埋めるなと言っている", "推測で埋めない" in consult_prompt, True)
check("相談: 文書なら本文を書かないと言っている", "本文はあなたが書きません" in consult_prompt, True)

# --- 相談から戻ったアイテムが review に跳ね返らないこと ---------------------------
# resume は保留理由を残す。残った理由が「着手してはいけないもの」に形の上で当たるため、
# 但し書きが無いと resume と review を往復し続ける。
resumed = inbox.InboxItem({
    "id": "x",
    "properties": {
        inbox.P_TITLE: {"title": [{"plain_text": "自己推薦書"}]},
        inbox.P_STATUS: {"select": {"name": inbox.STATUS_TODO}},
        inbox.P_HOLD_REASON: {"select": {"name": inbox.HOLD_DOCUMENT}},
    },
})
note = _dispatch._resumed_note(resumed)
check("再開: 相談済みだと伝える", "相談済み" in note, True)
check("再開: reviewに戻すなと言っている", "`review` に\n回す必要はありません" in note, True)
check("再開: 線引きは守れと言っている", "線引きそのものは引き続き守る" in note, True)
check("再開: 保留理由を本文に出す", inbox.HOLD_DOCUMENT in note, True)

fresh = inbox.InboxItem({
    "id": "y",
    "properties": {
        inbox.P_TITLE: {"title": [{"plain_text": "ロガー実装"}]},
        inbox.P_STATUS: {"select": {"name": inbox.STATUS_TODO}},
        inbox.P_HOLD_REASON: {"select": {"name": inbox.HOLD_NONE}},
    },
})
check("再開: 普通のアイテムには付けない", _dispatch._resumed_note(fresh), "")
check("再開: 保留理由が空でも付けない",
      _dispatch._resumed_note(inbox.InboxItem({"id": "z", "properties": {}})), "")

# 実装用プロンプトが組み立てられること（差し込み漏れがあると本番で落ちる）
built = _dispatch.PROMPT_TEMPLATE.format(
    page_id="pid", title="T", body="B", progress="P",
    resumed_note=note, projects_dir="C:/proj", cli_path="C:/cli.py",
)
check("実装プロンプト: 相談済みの但し書きが入る", "相談済み" in built, True)
check("実装プロンプト: 未展開の差し込みが残っていない", "{" in built.replace("{page_id}", ""), False)

# --- 結果 ----------------------------------------------------------------
if failures:
    print(f"FAILED ({len(failures)})")
    for f in failures:
        print("  - " + f)
    sys.exit(1)
print("すべて通過しました")
