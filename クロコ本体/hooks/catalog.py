"""PostToolUseフック：書き込まれたファイルを資料管理DB(items)へ機械的に登録する。

**2026-09-24以降、実際に呼ばれているのはGo版（`hooks/catalog_hook.exe`、ソースは
`hooks/catalog_go/`）。** `~/.claude/settings.json`のcommandをexeに切り替えた。このPython版は
ロールバック用に残してあるだけで、以後の変更はGo版に入れる（両方を直すと食い違う）。
戻すときはsettings.jsonのcommandを `python -X utf8 "…\\hooks\\catalog.py"` に戻す。
起動時間は中央値でPython約98ms→Go約17ms（`catalog_go/tests/compare.py`で両者の出力一致も確認）。

設計の経緯は `クロコ本体/記録/検討中_資料管理DB化.md` を見ること。

このフックが埋めるのは path・project（クロコ管轄プロジェクト直下のトップレベル
フォルダ名から自動推定）・updated_at だけ。stage/status/lore は判断が要るため
自動では埋めない（`catalog_cli.py` で後から本人・AIが埋める）。「台帳に載って
いること」だけを機械的に保証し、「それが何であるか」の判断とは分離する。

対象はWrite/Edit/NotebookEditに加えてReadも含む(2026-08-19、本人指摘：
「読んだものは登録」)。既存の未登録ファイルを一括でscanして本人に分類を
聞きにいく代わりに、普段の作業で読む・書くたびに自然に台帳が埋まっていく形にする
（登録＝機械的な最低限であって、分類の質問はしないため、Readで発火しても
本人に負荷はかからない）。

`クロコ管轄プロジェクト/.claude/hooks/auto_commit.py` と同じPostToolUseフックだが
役割が別（あちらはgit履歴、こちらは台帳登録）なので統合せず別フックとして並べる。

このファイル自体は`クロコ本体/`側に置いてある。`クロコ管轄プロジェクト/.claude/`配下に
置かなかった理由は`catalog_db.py`のdocstring参照（auto_commit.pyの
フォルダ単位git化に巻き込まれないため）。

**フックの登録（settings.jsonのhooks）は、実際にはグローバル`~/.claude/settings.json`
に置かれている**（`クロコ管轄プロジェクト/.claude/settings.json`ではない。いつ・
なぜこの場所に変わったかの経緯は不明——当初の想定と食い違っていることが
2026-08-22に判明したのみで、記録は残っていない）。このスクリプトは絶対パスで
参照される。

2026-08-22判明：グローバルsettings.json内のこのフックのcommandだけ、他の
グローバルフック（file_watch系等）と違い`-X utf8`が付いていなかった。
Windows既定ロケール(cp932)でstdinのJSONペイロードが読まれ、日本語パスを含む
ファイルに対して`UnicodeEncodeError`（surrogates not allowed）が発生し、
`main()`のtry/exceptで握りつぶされて台帳登録が黙って失敗し続けていた
（`クロコ本体/logs/catalog_hook_errors.log`で確認）。クロコ管轄プロジェクト配下は
ほぼ全パスが日本語のため、実質ほぼ常に失敗していたと見られる。`-X utf8`を付けて
修正済み。

## relations連携によるshadow-tracking（2026-08-20）

「読んだファイルが外部で変わったら気づく」既存の仕組み（`~/.claude/hooks/
file_watch_track.py`/`file_watch_check.py`）はcroco非依存のグローバルフックで、
Readしたファイルだけを追跡する。これをrelationsと繋げて、**あるファイルを
触ったとき、そのファイルが依存している先（outgoing relations、`relate`の
from_id側）の中身も一緒に追跡対象へ含める**ようにした（例：自己推薦書を
編集中に、それが引用している日誌が別セッションで変更されても気づける）。

方向は「引用してる側」だけ（1ホップのみ。依存先のさらに依存先までは追わない）。
本人指摘（2026-08-20）：「非依存側（file_watch本体）はやっぱDBにやらせる仕事
じゃない」——なので**file_watch_check.py/file_watch_track.py側は一切改造しない**。
向こうが既に見ているstateファイル（`~/.claude/tmp/file_watch/{session_id}.json`）に、
このフックからスナップショットを追記するだけで済ませる。file_watch_check.pyは
stateファイルの中身がどう増えたか関知しない作りなので、これで連携できる。

TEXT_EXTENSIONSは`file_watch_track.py`と同じ集合を複製している（別ディレクトリ
（`~/.claude/hooks/`）にあるスクリプトを跨いでimportするのは壊れやすいため。
`catalog_cli.py`の`_git_commit_folder`がauto_commit.pyのロジックを複製している
のと同じ理由・同じ方針）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import catalog_db  # noqa: E402

TRACKED_TOOLS = {"Edit", "Write", "NotebookEdit", "Read"}

FILE_WATCH_STATE_DIR = Path.home() / ".claude" / "tmp" / "file_watch"

# file_watch_track.pyのTEXT_EXTENSIONSと同じ集合(複製の理由は上記docstring参照)。
FILE_WATCH_TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown",
    ".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".html", ".htm", ".css", ".scss",
    ".csv", ".tsv",
    ".ps1", ".sh", ".bash", ".bat",
    ".java", ".c", ".h", ".hpp", ".cpp", ".cs", ".go", ".rs", ".rb", ".php",
    ".sql", ".xml", ".log",
}


def _shadow_track_related(file_path: str, session_id: str | None) -> None:
    """file_pathのoutgoing relations先も、file_watchの監視対象に加える。

    既に追跡中(直接Read済み、または既にshadow済み)のパスは上書きしない
    ——上書きすると、まだcheck.py側に見せていない変更を黙って握りつぶし
    (baselineを現在時点にリセットしてしまい)、通知されないまま消える。
    """
    if not session_id:
        return
    relations = catalog_db.get_relations(file_path)
    targets = {r["to_id"] for r in relations if r["from_id"] == file_path}
    if not targets:
        return

    state_path = FILE_WATCH_STATE_DIR / f"{session_id}.json"
    state = {}
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            state = {}

    changed = False
    for target in targets:
        if target in state:
            continue
        p = Path(target)
        if not p.is_file() or p.suffix.lower() not in FILE_WATCH_TEXT_EXTENSIONS:
            continue
        try:
            content = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        state[target] = {
            "hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "content": content,
        }
        changed = True

    if not changed:
        return

    FILE_WATCH_STATE_DIR.mkdir(parents=True, exist_ok=True)
    # atomic write化の理由はfile_watch_track.py側のコメント参照(2026-08-19)。
    tmp_path = state_path.with_name(f"{state_path.name}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(state_path)


LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "catalog_hook_errors.log"


def _log_error(context: str, exc: Exception) -> None:
    """失敗を黙って握りつぶすのは変えず(croco全体の「失敗したら何もしない」方針)、
    後から気づけるように最低限の記録だけ残す(2026-08-21、本人指摘:
    「起こること自体が問題」。DBロック競合等が無音で消えるのを防ぐ)。
    リトライも通知もしない、追記だけ。ログ自体の失敗はさらに握りつぶす。
    """
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = f"{datetime.now().isoformat(timespec='seconds')} {context}: {exc!r}\n"
        # contextにfile_pathを生埋め込みしている呼び出し元があり、壊れた文字列
        # (サロゲート混入等)だとここでの書き込み自体がUnicodeEncodeErrorになり、
        # 記録が残らず消えていた(2026-08-22判明)。errors="backslashreplace"で
        # エンコード不能な文字だけエスケープ表示に落とし、書き込み自体は必ず通す。
        with open(LOG_PATH, "a", encoding="utf-8", errors="backslashreplace") as f:
            f.write(line)
    except Exception:
        pass


def _already_registered(file_path: str) -> bool:
    return catalog_db.get_item(file_path) is not None


def _basename_warning(file_path: str) -> str | None:
    """新規作成されたファイルと同名の既存ファイルが台帳にあれば気づかせる文言を返す。

    2026-09-17、本人指摘：「作業コピー」「適用.py」等、複製して専用化する運用が
    実際に多いのに、relationsが一切繋がれていないケースが台帳調査で多数見つかった
    （下線一括ほか_下書きエディタ機能追加/作業コピー、面接想定質問リストと
    受験判断フォームのbuild.py/items.py/server.py 等）。CLAUDE.mdの文書指示だけでは
    実行されなかった（実際に規約は複数セッションで読み込まれていたことをログで確認済み）
    ため、機械的に気づかせる方向に倒す。強制（ブロック）はせず、気づきの機会を
    作るだけに留める——同名でも無関係なケース（README.md等）はよくあるため。
    """
    try:
        others = catalog_db.find_by_basename(file_path)
    except Exception:
        return None
    if not others:
        return None
    listing = "\n".join(f"  - {p}" for p in others[:5])
    more = f"\n  ...他{len(others) - 5}件" if len(others) > 5 else ""
    return (
        f"[台帳] 同名ファイルが既に台帳にあります: {Path(file_path).name}\n{listing}{more}\n"
        "複製・専用化・移植の関係があれば `catalog_cli.py relate <新> derived_from <旧>` "
        "等で繋ぐこと（無関係な同名なら無視してよい）。"
    )


def _new_folder_lore_reminder(file_path: str) -> str | None:
    """新規フォルダ自身のREADME.mdなら、loreを埋めるよう気づかせる文言を返す。

    判定（「新規フォルダのREADMEか」）はcatalog_db.is_project_readmeに任せ、
    ここではメッセージの組み立てだけ行う（_basename_warningと同じ役割分担、
    2026-09-19、本人指摘：「台帳側でフォルダかを判定する」）。loreは意味理解が
    要るためフックからは自動で埋められず（本ファイル冒頭docstring参照）、
    気づきの注入に留める。
    """
    if not catalog_db.is_project_readme(file_path):
        return None
    return (
        f"[台帳] 新規フォルダのREADME.mdです。loreが未設定だと関連判定"
        f"(catalog_related.py)の候補に出てきません。\n"
        f'`python catalog_cli.py set "{file_path}" lore "<1〜3文の要約>"` で埋めること。'
    )


def _new_file_register_reminder(file_path: str) -> str | None:
    """管轄プロジェクト配下の新規ファイルに、lore・relationの登録を「やる前提」で促す。

    2026-09-24、本人指示：「loreもrelationも台帳登録できるものはやるようにさせといて。
    やるかどうかの判断を向こうにさせるな。やる前提の上で、なお不要なら不要でいい」。
    そのため「必要なら」ではなく「やること」と書き、例外（一時ファイル・生成物等）に
    当たるときだけ、飛ばした理由を返答に一言書かせる（黙って飛ばさせない）。
    新規フォルダのREADME.mdは_new_folder_lore_reminderが専用文言を出すので対象外。
    """
    if catalog_db.project_for_path(file_path) is None:
        return None
    if catalog_db.is_project_readme(file_path):
        return None
    return (
        f"[台帳] 新規ファイルを登録しました: {Path(file_path).name}\n"
        f"このファイルについて、以下を行うこと（やるかどうかは検討しない。行う前提）:\n"
        f'  1. lore: `python catalog_cli.py set "{file_path}" lore "<1〜3文で何のファイルか>"`\n'
        f"  2. relation: 元にしたもの・参照しているもの・置き換えるもの・同じ目的の別ファイルが"
        f"あれば `catalog_cli.py relate <新> <種別> <旧>`"
        f"（種別は `catalog_cli.py values relation_type` の既存値から選ぶ）\n"
        f"一時ファイル・生成物・使い捨てなど、台帳に残す意味が無いと言い切れる場合だけ省いてよい。"
        f"その場合は省いた理由を返答に一言書くこと。"
    )


QUOTA_CACHE_PATH = Path.home() / ".claude" / "hooks" / "quota_cache.json"
READ_NOTICE_DIR = Path.home() / ".claude" / "tmp" / "catalog_read_notice"

# 週次クォータがこの使用率以上ならRead時の登録促しを出さない。
# quota_guard_pretool.pyのTHRESHOLDと同じ値（本人指定、2026-09-24）。
WEEKLY_QUOTA_LIMIT = 88


def _weekly_quota_exceeded() -> bool:
    """週次(7日)クォータが閾値以上ならTrue。

    quota_guard_pretool.pyがstatusLine経由で書かれたquota_cache.jsonを読むのと同じ
    仕組みを借りている（外部API呼び出しはしない）。キャッシュが読めない・値が
    不明のときはFalse（＝促しを出す側）に倒す：促しは「やる前提」の既定動作で、
    止めるのは高使用率が確認できたときだけ。週次はリセットが遅いので5時間枠の
    ような鮮度(fetched_at)判定はせず、リセット時刻を過ぎていれば無効とだけ見る。
    """
    try:
        cache = json.loads(QUOTA_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return False
    used = cache.get("seven_day_used")
    if not isinstance(used, (int, float)):
        return False
    resets_at = cache.get("seven_day_resets_at")
    if isinstance(resets_at, (int, float)) and resets_at <= time.time():
        return False
    return used >= WEEKLY_QUOTA_LIMIT


def _read_missing_register_reminder(file_path: str, session_id: str | None) -> str | None:
    """Readしたファイルの台帳にloreまたはrelationが無ければ、埋めるよう促す。

    2026-09-24、本人指示：「readでも未登録の要素(relationなど)を追加するように指示を
    してくれ」。新規Write時（_new_file_register_reminder）と同じ「やる前提」の文面。
    Readは頻度が高いので、同一セッション内では同じファイルに1回しか出さない
    （出しても埋まらなかったファイルで毎回繰り返すと文脈を食うだけのため）。
    """
    if catalog_db.project_for_path(file_path) is None:
        return None
    item = catalog_db.get_item(file_path)
    if item is None:
        return None
    missing = []
    if not (item["lore"] or "").strip():
        missing.append("lore")
    if not catalog_db.get_relations(file_path):
        missing.append("relation")
    if not missing:
        return None

    sid = "".join(c if c.isalnum() else "_" for c in (session_id or "default"))
    flag = READ_NOTICE_DIR / f"{sid}.json"
    try:
        seen = set(json.loads(flag.read_text(encoding="utf-8")))
    except Exception:
        seen = set()
    if file_path in seen:
        return None
    seen.add(file_path)
    try:
        READ_NOTICE_DIR.mkdir(parents=True, exist_ok=True)
        flag.write_text(json.dumps(sorted(seen), ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass

    lines = [
        f"[台帳] 読んだファイルに未登録の項目があります: {Path(file_path).name}"
        f"（未登録: {' / '.join(missing)}）",
        "中身を読んだ今、分かる範囲で埋めること（やるかどうかは検討しない。行う前提）:",
    ]
    if "lore" in missing:
        lines.append(f'  - lore: `python catalog_cli.py set "{file_path}" lore "<1〜3文で何のファイルか>"`')
    if "relation" in missing:
        lines.append(
            "  - relation: 元にしたもの・参照しているもの・置き換えるもの・同じ目的の別ファイルが"
            "分かれば `catalog_cli.py relate <このファイル> <種別> <相手>`"
            "（種別は `catalog_cli.py values relation_type` の既存値から選ぶ）"
        )
    lines.append(
        "中身から判断できない、または台帳に残す意味が無いと言い切れる項目だけ省いてよい。"
        "省いたら理由を返答に一言書くこと。"
    )
    return "\n".join(lines)


def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return

    tool_name = payload.get("tool_name")
    if tool_name not in TRACKED_TOOLS:
        return
    file_path = (payload.get("tool_input") or {}).get("file_path")
    if not file_path:
        return

    is_new = not _already_registered(file_path)

    # Read時、既に台帳にあるファイルはupsertしない(2026-08-21、本人指摘:
    # 「AIがreadした履歴はいらない」。Readは中身を変えないので、既存登録に
    # 対して書き込む価値が無い。書き込み頻度そのものを減らし、DBロック競合の
    # 発生機会を減らす狙いも兼ねる。Write/Edit/NotebookEditは中身が実際に
    # 変わるので従来通り毎回書く)。
    if tool_name != "Read" or is_new:
        try:
            catalog_db.upsert_item(
                path=file_path,
                project=catalog_db.project_for_path(file_path),
                updated_at=datetime.now().isoformat(timespec="seconds"),
            )
        except Exception as exc:
            _log_error(f"upsert_item({file_path})", exc)

    try:
        _shadow_track_related(file_path, payload.get("session_id"))
    except Exception as exc:
        _log_error(f"shadow_track_related({file_path})", exc)

    # 新規Writeのときだけ、気づきを注入する
    # （Edit/NotebookEditや既存ファイルの上書きは新設ではないので対象外）。
    if tool_name == "Write" and is_new:
        notices = []
        try:
            warning = _basename_warning(file_path)
            if warning:
                notices.append(warning)
        except Exception as exc:
            _log_error(f"basename_warning({file_path})", exc)
        try:
            reminder = _new_folder_lore_reminder(file_path)
            if reminder:
                notices.append(reminder)
        except Exception as exc:
            _log_error(f"new_folder_lore_reminder({file_path})", exc)
        try:
            register = _new_file_register_reminder(file_path)
            if register:
                notices.append(register)
        except Exception as exc:
            _log_error(f"new_file_register_reminder({file_path})", exc)
        if notices:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": "\n\n".join(notices),
                }
            }, ensure_ascii=False))

    # Read時：週次クォータが高いときは出さない（Writeの新規登録促しは対象外）
    if tool_name == "Read" and not _weekly_quota_exceeded():
        try:
            reminder = _read_missing_register_reminder(file_path, payload.get("session_id"))
            if reminder:
                print(json.dumps({
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": reminder,
                    }
                }, ensure_ascii=False))
        except Exception as exc:
            _log_error(f"read_missing_register_reminder({file_path})", exc)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # フックの失敗でクロコ本体の動作を止めない(auto_commit.pyと同じ方針)。
        # ただし記録だけは残す(上記_log_error参照)。
        _log_error("main", exc)
