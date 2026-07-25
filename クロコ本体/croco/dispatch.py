"""実装フェーズ：Inbox DB から1件選び、クロコ（Claude Code）を無人で起動する。

仕様書2.5章-5のとおり、起動したらそのまま実装に着手する前提。
本人が横で承認操作をしないため、パーミッションは事前に決めた設定で通す
（`--permission-mode auto` ＋ croco_settings.json の deny リスト）。

処理順は仕様書2.5章-10で確定した「新規の『未処理』より『処理中』を優先」。
これにより通常の再開と中断からの復旧が同じ経路になる。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from .config import CROCO_HOME, Config
from . import inbox, log
from . import notion as nt

# クロコに渡す設定ファイル。--settings で明示的に指定する。
SETTINGS_PATH = CROCO_HOME / "croco_settings.json"

PROMPT_TEMPLATE = """\
あなたは「クロコ」として、本人が不在のまま自動で起動されました。
以下のアイデアの実装を、可能なところまで自分で進めてください。

## 対象アイテム
- Notionページ ID: {page_id}
- タイトル: {title}

## 内容（本人のブレスト生ログの逐語転記。要約されていないので冗長です）
{body}

## これまでの進捗
{progress}

## 作業場所
`{projects_dir}` の下に、このアイデア専用のフォルダを1つ作って作業してください。
既に対応するフォルダがあればそれを使ってください。
**このディレクトリの外には書き込まないでください。**

## 進捗の記録（必ず行うこと）
区切りの良いところまで進んだら、次のコマンドで Notion に進捗を記録してください。
gitのコミットメッセージのように簡潔に書いてください。

```
python "{cli_path}" log {page_id} "やったことの要約"
```

作業が完全に終わったら:
```
python "{cli_path}" done {page_id} "最終的な成果の要約"
```

自分では判断できず本人の確認が必要になったら:
```
python "{cli_path}" review {page_id} "何を確認してほしいか"
```

## 進め方
- 1回の起動で全部終わらせる必要はありません。中断しても次回の起動で続きから再開できます。
- 終わらない場合も、必ず `log` で進捗を残してから終了してください。次回のあなたはそれだけを頼りに再開します。
- 不明点があって進めない場合は、勝手に仕様を決めず `review` に回してください。
"""


def run(config: Config) -> bool:
    """1件処理する。処理対象があれば True。"""
    client = nt.Notion(config.notion_token, config.notion_version)

    data_source_id = config.inbox_data_source_id or client.resolve_data_source_id(
        config.inbox_database_id
    )

    pages = client.query_data_source(data_source_id, filter_=inbox.pending_filter())
    items = [inbox.InboxItem(page) for page in pages]

    # 「予定」は実装対象ではなくカレンダー表示用なので着手しない（仕様書2.5章-6）。
    targets = [item for item in items if item.kind != inbox.KIND_SCHEDULE]
    if not targets:
        log.log("着手できるアイテムはありませんでした。")
        return False

    targets.sort(key=inbox.sort_key)
    item = targets[0]
    log.log(f"着手します: [{item.status}] {item.title}")

    # 再試行の上限（仕様書2.5章-12）。無限ループの防止。
    if item.attempts >= config.max_retries:
        log.warn(
            f"試行回数が上限({config.max_retries})に達しました。「要確認」に変更します。"
        )
        client.update_page(
            item.id,
            {
                inbox.P_STATUS: {"select": {"name": inbox.STATUS_REVIEW}},
                inbox.P_RESULT: {
                    "rich_text": nt.rich_text(
                        _appended(
                            item.result_log,
                            f"試行回数が上限({config.max_retries})に達したため要確認に変更",
                        )
                    )
                },
            },
        )
        return True

    body = client.get_page_text(item.id)

    if config.dry_run:
        log.log("（dry-run）以下のプロンプトでクロコを起動します:")
        log.log(_build_prompt(config, item, body))
        return True

    _mark_started(client, item)

    prompt = _build_prompt(config, item, body)
    exit_code = _launch_claude(config, prompt)

    if exit_code != 0:
        log.error(
            f"クロコが異常終了しました (exit={exit_code})。"
            " ステータスは「処理中」のまま残すので、次回起動時に再開されます。"
        )
    return True


def _appended(existing: str, message: str) -> str:
    stamp = inbox.now_iso()[:16].replace("T", " ")
    entry = f"[{stamp}] {message}"
    return f"{existing}\n{entry}".strip() if existing else entry


def _mark_started(client: nt.Notion, item: inbox.InboxItem) -> None:
    """着手をNotionに記録する。

    「処理開始日時」は最初に着手したときだけ入れる。発話から着手までの
    ラグを測るための値なので、再開のたびに上書きしてはいけない（仕様書2.5章-10）。
    """
    properties: dict = {
        inbox.P_STATUS: {"select": {"name": inbox.STATUS_DOING}},
        inbox.P_ATTEMPTS: {"number": item.attempts + 1},
    }
    if item.status != inbox.STATUS_DOING:
        properties[inbox.P_STARTED_AT] = {"date": {"start": inbox.now_iso()}}
    client.update_page(item.id, properties)


def _build_prompt(config: Config, item: inbox.InboxItem, body: str) -> str:
    progress = item.result_log.strip() or "（まだありません。今回が初回です）"
    return PROMPT_TEMPLATE.format(
        page_id=item.id,
        title=item.title,
        body=body,
        progress=progress,
        projects_dir=config.projects_dir,
        cli_path=CROCO_HOME / "croco_cli.py",
    )


def _launch_claude(config: Config, prompt: str) -> int:
    """クロコを非対話モードで起動する。

    - `-p`      : 非対話（承認プロンプトを出さない）
    - `--permission-mode auto` : 日常操作は自動承認し、危険操作は内蔵判定でブロック
    - `--settings`             : 上に重ねる deny リスト（仕様書2.5章-9）
    - `--add-dir`              : 作業対象ディレクトリを明示
    """
    projects_dir: Path = config.projects_dir
    projects_dir.mkdir(parents=True, exist_ok=True)

    executable = shutil.which(config.claude_command)
    if not executable:
        log.error(
            f"`{config.claude_command}` が見つかりません。"
            " PATHを確認するか .env の CROCO_CLAUDE_COMMAND を設定してください。"
        )
        return 127

    command = [
        executable,
        "-p",
        prompt,
        "--permission-mode",
        "auto",
        "--settings",
        str(SETTINGS_PATH),
        "--add-dir",
        str(projects_dir),
    ]

    log.log(f"クロコを起動します（作業ディレクトリ: {projects_dir}）")
    process = subprocess.run(
        command,
        cwd=str(projects_dir),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )

    if process.stdout:
        log.log("--- クロコの出力 ---\n" + process.stdout.strip())
    if process.stderr.strip():
        log.warn("--- クロコのエラー出力 ---\n" + process.stderr.strip())

    return process.returncode
