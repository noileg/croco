"""実装フェーズ：Inbox DB から1件選び、クロコ（Claude Code）を無人で起動する。

仕様書2.5章-5のとおり、起動したらそのまま実装に着手する前提。
本人が横で承認操作をしないため、パーミッションは事前に決めた設定で通す
（`--permission-mode auto` ＋ croco_settings.json の deny リスト）。

処理順は仕様書2.5章-10で確定した「新規の『未処理』より『処理中』を優先」。
これにより通常の再開と中断からの復旧が同じ経路になる。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
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

## 着手してはいけないもの
以下に該当する場合は、**内容を書き始めず** `review` に回して終了してください。
本人の判断が要るものを無人で進めてはいけません。

- **本人名義で提出する文書の作成**：自己推薦書、志望理由書、出願書類、
  大学のレポート、エントリーシート等。AIが書いたことが問題になり得るため、
  これらは無人実行の対象外と決められています。
  下調べや必要書類の洗い出しまでは可能ですが、本文は書かないでください。
- **現実世界の行動が必要なもの**：書類の取り寄せ、郵送、窓口手続き、予約など。
  あなたには実行できないので、何が必要かを整理して `review` に回してください。
- **課金・契約・外部への送信を伴う操作**。

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

    # 「予定」（カレンダー表示用）と「資料」（参照用）は実装対象ではない。
    targets = [
        item for item in items if item.kind not in inbox.NON_IMPLEMENTABLE_KINDS
    ]
    if not targets:
        log.log("着手できるアイテムはありませんでした。")
        return False

    targets.sort(key=inbox.sort_key)
    item = _choose(targets, config)
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


def _choose(targets: list[inbox.InboxItem], config: Config) -> inbox.InboxItem:
    """着手するアイテムを決める。

    候補が複数あるとき、本人がその場にいれば選べるようにする。
    ただしPC起動時に自動で走ることが前提なので、**待ち続けてはいけない**。
    制限時間を過ぎたら先頭（＝処理中を優先した並び順の先頭）で自動的に進める。
    画面が無い状況（リダイレクト等）では、そもそも尋ねない。
    """
    if len(targets) == 1 or config.pick_timeout <= 0:
        return targets[0]
    if not (sys.stdin and sys.stdin.isatty()):
        return targets[0]

    log.log(f"着手できるアイテムが {len(targets)} 件あります:")
    for index, item in enumerate(targets, 1):
        mark = "※再開" if item.status == inbox.STATUS_DOING else "　　　"
        log.log(f"  {index:2}) {mark} {item.title}")
    log.log(
        f"番号を入れてEnter（{config.pick_timeout}秒で 1) を自動選択）: ",
    )

    answer: list[str] = []

    def _read() -> None:
        try:
            answer.append(sys.stdin.readline())
        except Exception:
            pass

    # 入力待ちのスレッドはデーモンにしておく。制限時間を過ぎたら放置して先へ進み、
    # プロセス終了時にまとめて片付けさせる（stdin待ちは中断できないため）。
    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    reader.join(timeout=config.pick_timeout)

    if not answer:
        log.log("→ 時間切れ。1) で進みます。")
        return targets[0]

    text = answer[0].strip()
    if not text:
        return targets[0]
    try:
        index = int(text)
    except ValueError:
        log.warn(f"番号として読めません（{text!r}）。1) で進みます。")
        return targets[0]
    if not 1 <= index <= len(targets):
        log.warn(f"範囲外です（{index}）。1) で進みます。")
        return targets[0]
    return targets[index - 1]


def _render_event(line: str) -> list[str]:
    """stream-json の1行を、画面に出す読める形へ変換する。

    形式が想定と違っても落とさない。可視化のための処理であって、
    ここで例外を投げて実装フェーズ全体を落とすのは本末転倒なため、
    解釈できないものは生のまま出す。
    """
    text = line.strip()
    if not text:
        return []
    try:
        event = json.loads(text)
    except json.JSONDecodeError:
        return [text]
    if not isinstance(event, dict):
        return [text]

    kind = event.get("type")

    if kind == "assistant":
        return _render_assistant(event.get("message") or {})

    if kind == "user":
        # ツールの実行結果。全文を出すと膨大になるので件数だけ示す。
        blocks = ((event.get("message") or {}).get("content")) or []
        results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
        errors = [b for b in results if b.get("is_error")]
        if errors:
            return [f"  ← ツール結果: {len(results)}件（うちエラー {len(errors)}件）"]
        return [f"  ← ツール結果: {len(results)}件"] if results else []

    if kind == "result":
        parts = [f"[完了] {event.get('subtype', '')}"]
        if event.get("num_turns"):
            parts.append(f"{event['num_turns']}ターン")
        if event.get("total_cost_usd"):
            parts.append(f"${event['total_cost_usd']:.4f}")
        summary = " / ".join(parts)
        final = (event.get("result") or "").strip()
        return [summary, final] if final else [summary]

    if kind == "system" and event.get("subtype") == "init":
        return [f"[起動] model={event.get('model', '?')} cwd={event.get('cwd', '?')}"]

    return []


def _render_assistant(message: dict) -> list[str]:
    lines: list[str] = []
    for block in message.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            body = (block.get("text") or "").strip()
            if body:
                lines.append(body)
        elif block.get("type") == "tool_use":
            lines.append(f"  → {block.get('name', '?')}: {_summarize_tool_input(block.get('input'))}")
    return lines


def _summarize_tool_input(value: object) -> str:
    """ツールの引数を1行に収める。長い本文で画面が埋まるのを防ぐ。"""
    if not isinstance(value, dict):
        return ""
    for key in ("command", "file_path", "pattern", "path", "url", "prompt", "description"):
        found = value.get(key)
        if isinstance(found, str) and found.strip():
            flat = " ".join(found.split())
            return flat[:120] + ("…" if len(flat) > 120 else "")
    return ", ".join(sorted(value)[:5])


def _resolve_claude(config: Config) -> str | None:
    """claude の実行ファイルを探す。

    PATH だけに頼らないのが要点。`claude` はインストーラが対話セッションにしか
    通していない場合があり（このPCでは実際にUser/Machineどちらの永続PATHにも
    入っていなかった）、スタートアップから起動したプロセスでは見つからない。
    捕捉フェーズだけ動いて実装フェーズが毎回失敗する、という分かりにくい壊れ方をする。
    """
    found = shutil.which(config.claude_command)
    if found:
        return found

    # PATHで見つからない場合の既定のインストール先。
    candidates = [
        Path.home() / ".local" / "bin" / "claude.exe",
        Path.home() / ".local" / "bin" / "claude",
        Path.home() / "AppData" / "Local" / "Programs" / "claude" / "claude.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            log.warn(
                f"`{config.claude_command}` はPATHに無いため {candidate} を使います。"
                " スタートアップ起動でも確実にするには、.env の"
                " CROCO_CLAUDE_COMMAND にこのパスを設定してください。"
            )
            return str(candidate)
    return None


def _launch_claude(config: Config, prompt: str) -> int:
    """クロコを非対話モードで起動する。

    - `-p`      : 非対話（承認プロンプトを出さない）
    - `--permission-mode auto` : 日常操作は自動承認し、危険操作は内蔵判定でブロック
    - `--settings`             : 上に重ねる deny リスト（仕様書2.5章-9）
    - `--add-dir`              : 作業対象ディレクトリを明示
    """
    projects_dir: Path = config.projects_dir
    projects_dir.mkdir(parents=True, exist_ok=True)

    executable = _resolve_claude(config)
    if not executable:
        log.error(
            f"`{config.claude_command}` が見つかりません。"
            " .env の CROCO_CLAUDE_COMMAND にフルパスを設定してください。"
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
        # 既定の text 形式は完了までバッファされるため、実行中は画面に何も出ない。
        # 無人実行とはいえ本人が横目で様子を見る前提なので、
        # 逐次イベントが流れる stream-json を使って進行を可視化する。
        "--output-format",
        "stream-json",
        "--verbose",
    ]

    log.log(f"クロコを起動します（作業ディレクトリ: {projects_dir}）")
    log.log("--- ここからクロコの出力 ---")

    # 出力を貯め込まずに1行ずつ流す。
    # 実装中は本人が動画を見ながら横目で進捗を眺める想定なので、
    # 終わるまで何も表示されないと様子が分からず、止め時も判断できない。
    process = subprocess.Popen(
        command,
        cwd=str(projects_dir),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
    )
    assert process.stdout is not None
    for line in process.stdout:
        for rendered in _render_event(line):
            log.log(rendered)
    process.wait()

    log.log(f"--- クロコの出力ここまで (exit={process.returncode}) ---")
    return process.returncode
