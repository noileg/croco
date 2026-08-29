"""PostToolUseフック：書き込まれたファイルを資料管理DB(items)へ機械的に登録する。

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

    # Read時、既に台帳にあるファイルはupsertしない(2026-08-21、本人指摘:
    # 「AIがreadした履歴はいらない」。Readは中身を変えないので、既存登録に
    # 対して書き込む価値が無い。書き込み頻度そのものを減らし、DBロック競合の
    # 発生機会を減らす狙いも兼ねる。Write/Edit/NotebookEditは中身が実際に
    # 変わるので従来通り毎回書く)。
    if tool_name != "Read" or not _already_registered(file_path):
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


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # フックの失敗でクロコ本体の動作を止めない(auto_commit.pyと同じ方針)。
        # ただし記録だけは残す(上記_log_error参照)。
        _log_error("main", exc)
