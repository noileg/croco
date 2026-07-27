"""実装フェーズ：Inbox DB からアイテムを選び、クロコ（Claude Code）を起動する。

仕様書2.5章-5のとおり、起動したらそのまま実装に着手する前提。
本人が横で承認操作をしないため、パーミッションは事前に決めた設定で通す
（`--permission-mode auto` ＋ croco_settings.json の deny リスト）。

処理順は仕様書2.5章-10で確定した「新規の『未処理』より『処理中』を優先」。
これにより通常の再開と中断からの復旧が同じ経路になる。

1件で終わりにせず、セッションが終わったら次のアイテムへ続けて進む。
ただし上限を設ける（`CROCO_MAX_ITEMS`）。1回の起動で無制限に走らせると
離席中にプランの使用枠を使い切るため。
"""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import NamedTuple

from .config import CROCO_HOME, Config
from . import inbox, log, usage
from . import notion as nt

try:
    import msvcrt  # Windows のキー入力（制限時間つきの入力に使う）
except ImportError:  # Windows以外
    msvcrt = None  # type: ignore[assignment]

# クロコに渡す設定ファイル。--settings で明示的に指定する。
SETTINGS_PATH = CROCO_HOME / "croco_settings.json"

PROMPT_TEMPLATE = """\
あなたは「クロコ」として、Notionに溜まったアイデアを実装するために自動で起動されました。
以下のアイデアの実装を、可能なところまで自分で進めてください。

本人はこの画面を見ているかもしれませんし、離れているかもしれません。
**返事を待たずに自分で進めてください。** ただし途中で本人から方針の修正や
追加の指示が入ることがあります。その場合はそちらを優先してください。

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

成果物を作ったら、そのフォルダに `README.md` を置いてください
（何を作ったか・どう使うか・前提）。後で公開するかを本人が判断する材料になります。
GitHubへの公開そのものはあなたの仕事ではありません（`git push` と `gh` は拒否されます）。
ローカルの `git init` / `commit` までは行って構いません。

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

  ただし、**本文を書く以外の手伝いはしてよい**と決まっています。線引きはこうです。

  - してよい：下調べ、必要書類の洗い出し、**要件の整理**（字数制限・締切・必須項目など）、
    構成や骨子の相談、`下書き/` にある本人の原稿を**読んで指摘する**こと
    （「ここは字数の割に情報が薄い」「この段落と次の繋がりが弱い」など）。
  - してはいけない：**本文そのものを書くこと**。書き出し例・言い換え案・清書・
    「こう直すといい」という**文章そのもの**の提示も含みます。
    直すべき箇所と理由までを言い、文は本人に書かせてください。

  `下書き/` 配下は Edit も Write も拒否されます。読むのは自由です。
  要件を書き残す先は `下書き/` の**外**にしてください（例：`要件.md`）。
- **現実世界の行動が必要なもの**：書類の取り寄せ、郵送、窓口手続き、予約など。
  あなたには実行できないので、何が必要かを整理して `review` に回してください。
- **クロコ自身（このワークフロー基盤）の改修**：`クロコ本体` 配下は書き換えが
  禁止されています。禁止しているのは、あなたを縛っている設定ファイルがそこにあるためです。
  改修の依頼だと分かったら、試さずに `review` に回してください。本人が別途対応します。
- **課金・契約・外部への送信を伴う操作**。
{resumed_note}
## 進め方
- 1回の起動で全部終わらせる必要はありません。中断しても次回の起動で続きから再開できます。
- 終わらない場合も、必ず `log` で進捗を残してから終了してください。次回のあなたはそれだけを頼りに再開します。
- 不明点があって進めない場合は、勝手に仕様を決めず `review` に回してください。
"""


class Outcome(NamedTuple):
    """1件分の処理結果。"""

    page_id: str | None  # 処理したアイテム。対象が無ければ None
    ok: bool  # 続けて次のアイテムに進んでよいか
    tokens: int  # 今回使ったトークン（新規処理分）


class RunSummary(NamedTuple):
    """1回の起動で実装フェーズが何をしたか。"""

    items: int
    tokens: int


def run_all(config: Config) -> RunSummary:
    """着手できるアイテムを、上限まで順に処理する。

    1件終わるたびに Inbox を取り直して次を選ぶ。取り直すのは、
    クロコ自身が `croco_cli.py` でステータスを書き換えているため。

    止める条件は4つ：対象が尽きた／件数の上限／トークンの上限／
    異常終了・中断。異常終了で続けないのは、同じ原因で残りも失敗して
    試行回数だけを消費する可能性が高いため。
    """
    handled: set[str] = set()
    tokens = 0

    try:
        while len(handled) < config.max_items:
            outcome = run(config, exclude=frozenset(handled))
            if outcome.page_id is None:
                break

            handled.add(outcome.page_id)
            tokens += outcome.tokens

            if not outcome.ok:
                log.log("続きは次回の起動に回します。")
                break
            if config.dry_run:
                break
            if len(handled) >= config.max_items:
                log.log(f"1回の起動で処理する上限（{config.max_items}件）に達しました。")
                break
            if config.token_budget and tokens >= config.token_budget:
                log.log(
                    f"今回の起動で {usage.compact(tokens)} トークン使いました"
                    f"（上限 {usage.compact(config.token_budget)}）。ここで止めます。"
                )
                break
            if not _confirm_next(config, len(handled), tokens):
                break
    except KeyboardInterrupt:
        # まとめ（要確認の一覧）だけは出したいので、ここで受け止めて先へ進む。
        log.warn("中断されました。実装フェーズをここで終えます。")

    return RunSummary(len(handled), tokens)


def run(config: Config, *, exclude: frozenset[str] = frozenset()) -> Outcome:
    """着手できるアイテムを1件処理する。

    `exclude` は同じ起動の中で既に処理したアイテム。連続処理には必須。
    セッションを終えても「完了」にならなかったものは「処理中」のまま残り、
    並び順では最優先されるので、除外しないと同じアイテムを延々と起動し続け、
    試行回数だけが上限まで増えていく。
    """
    client = nt.Notion(config.notion_token, config.notion_version)

    data_source_id = config.inbox_data_source_id or client.resolve_data_source_id(
        config.inbox_database_id
    )

    pages = client.query_data_source(data_source_id, filter_=inbox.pending_filter())
    items = [inbox.InboxItem(page) for page in pages]

    # 「予定」（カレンダー表示用）と「資料」（参照用）は実装対象ではない。
    targets = [
        item
        for item in items
        if item.kind not in inbox.NON_IMPLEMENTABLE_KINDS and item.id not in exclude
    ]
    if not targets:
        log.log(
            "他に着手できるアイテムはありません。"
            if exclude
            else "着手できるアイテムはありませんでした。"
        )
        return Outcome(None, True, 0)

    targets.sort(key=inbox.sort_key)
    item = _choose(targets, config, _estimate(items))
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
                inbox.P_HOLD_REASON: {"select": {"name": inbox.HOLD_RETRIES}},
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
        return Outcome(item.id, True, 0)

    body = client.get_page_text(item.id)

    if config.dry_run:
        log.log("（dry-run）以下のプロンプトでクロコを起動します:")
        log.log(_build_prompt(config, item, body))
        return Outcome(item.id, True, 0)

    _mark_started(client, item)
    # 相談で決着して戻ってきた文書タスクなら、書く場所も一緒に開く。
    open_editor(config, item)

    prompt = _build_prompt(config, item, body)
    started_at = time.time()
    try:
        exit_code = launch_claude(config, prompt)
    except KeyboardInterrupt:
        # Ctrl+C はコンソール全体に届くので、クロコと一緒にこちらも止まる。
        # 使ったぶんだけ記録して、続けて次のアイテムには進まない。
        log.warn(
            "中断されました。ステータスは「処理中」のまま残すので、次回起動時に再開されます。"
        )
        return Outcome(item.id, False, _record_usage(client, config, item, since=started_at))

    tokens = _record_usage(client, config, item, since=started_at)

    if exit_code != 0:
        log.error(
            f"クロコが異常終了しました (exit={exit_code})。"
            " ステータスは「処理中」のまま残すので、次回起動時に再開されます。"
        )
        return Outcome(item.id, False, tokens)
    return Outcome(item.id, True, tokens)


def _record_usage(
    client: nt.Notion, config: Config, item: inbox.InboxItem, *, since: float
) -> int:
    """今回のセッションで使ったトークンを画面に出し、Notionに積み上げる。使った量を返す。

    対話モードでは出力を横取りできないため、Claude Codeが残すセッション記録から読む。
    失敗しても実装自体は終わっているので、警告だけ出して先へ進む。
    """
    try:
        used = usage.measure(config.projects_dir, since=since - 60)
    except Exception as exc:
        log.warn(f"使用トークンの取得に失敗しました: {exc}")
        return 0
    if used is None or used.messages == 0:
        log.warn("使用トークンの記録が見つかりませんでした。")
        return 0

    log.log(f"今回の使用: {used.format()}")
    accumulated = item.tokens + used.fresh
    if item.tokens:
        log.log(f"このアイテムの累計: {usage.compact(accumulated)} トークン")

    try:
        client.update_page(item.id, {inbox.P_TOKENS: {"number": accumulated}})
    except Exception as exc:
        log.warn(f"使用トークンのNotionへの記録に失敗しました: {exc}")
    return used.fresh


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
        resumed_note=_resumed_note(item),
        projects_dir=config.projects_dir,
        cli_path=CROCO_HOME / "croco_cli.py",
    )


def _resumed_note(item: inbox.InboxItem) -> str:
    """相談で決着して戻ってきたアイテムに付ける但し書き。

    保留理由が残ったまま着手対象になっているものは、一度止まって本人の了解を得たもの。
    これを言っておかないと、上の「着手してはいけないもの」に形の上で当てはまるせいで
    クロコがまた review に回し、resume と review を往復し続けることになる。
    """
    reason = item.hold_reason
    if reason in ("", inbox.HOLD_NONE):
        return ""
    return f"""
## このアイテムは相談済みです
一度「{reason}」として止まりましたが、**本人と相談したうえで着手可能に戻されています**。
上の「着手してはいけないもの」に形の上で当てはまっても、改めて `review` に
回す必要はありません。何が決まったかは「これまでの進捗」に書いてあります。
**ただし線引きそのものは引き続き守ること**（本人名義の文書なら本文は書かない、など）。
"""


def _estimate(items: list[inbox.InboxItem]) -> str:
    """過去の実績から「1件あたりどれくらい使いそうか」を出す。

    予測というより実績の平均。件数が少ないうちは当てにならないので、
    何件を根拠にしているかも一緒に出して判断材料にしてもらう。
    """
    spent = [item.tokens for item in items if item.tokens > 0]
    if not spent:
        return ""
    average = sum(spent) // len(spent)
    return (
        f"過去{len(spent)}件の平均は1件あたり {usage.compact(average)} トークン"
        f"（最小 {usage.compact(min(spent))} / 最大 {usage.compact(max(spent))}）"
    )


def _choose(
    targets: list[inbox.InboxItem], config: Config, estimate: str = ""
) -> inbox.InboxItem:
    """着手するアイテムを決める。

    候補が複数あるとき、本人がその場にいれば選べるようにする。
    ただしPC起動時に自動で走ることが前提なので、**待ち続けてはいけない**。
    制限時間を過ぎたら先頭（＝処理中を優先した並び順の先頭）で自動的に進める。
    画面が無い状況（リダイレクト等）では、そもそも尋ねない。
    """
    if len(targets) == 1 or config.pick_timeout <= 0:
        if estimate:
            log.log(f"見込み: {estimate}")
        return targets[0]
    if not (sys.stdin and sys.stdin.isatty()):
        return targets[0]

    log.log(f"着手できるアイテムが {len(targets)} 件あります:")
    for index, item in enumerate(targets, 1):
        mark = "※再開" if item.status == inbox.STATUS_DOING else "　　　"
        spent = f"（これまで {usage.compact(item.tokens)}）" if item.tokens else ""
        # 相談で戻ってきたものは保留理由が残っている。着手時の線引きが変わるので出す。
        reason = f"［{item.hold_reason}］" if item.hold_reason not in ("", inbox.HOLD_NONE) else ""
        log.log(f"  {index:2}) {mark} {reason}{item.title}{spent}")
    if estimate:
        log.log(f"  見込み: {estimate}")
    log.log(
        f"番号を入れてEnter（{config.pick_timeout:.0f}秒で 1) を自動選択）: ",
    )

    text = read_line(config.pick_timeout)
    if text is None:
        log.log("→ 時間切れ。1) で進みます。")
        return targets[0]
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


def _confirm_next(config: Config, done: int, tokens: int) -> bool:
    """次のアイテムに進むか確認する。

    無人で走ることが前提なので既定は「進む」。ここでも待ち続けてはいけない。
    実績を先に出しておくのは、使用枠を気にするときの判断材料になるため。
    """
    log.log("")
    log.log(f"ここまで {done}件 / {usage.compact(tokens)} トークン使いました。")

    if not (sys.stdin and sys.stdin.isatty()) or config.pick_timeout <= 0:
        log.log("続けて次のアイテムに着手します。")
        return True

    log.log(
        f"続けて次に着手します。やめるなら n を入れてEnter"
        f"（{config.pick_timeout:.0f}秒で自動的に進みます）: "
    )
    answer = read_line(config.pick_timeout)
    if answer and answer.lower() in ("n", "no", "q", "quit", "やめる", "いいえ"):
        log.log("→ ここで終了します。")
        return False
    return True


def read_line(timeout: float) -> str | None:
    """制限時間つきで標準入力から1行読む。時間内に入力が無ければ None。（consult.py も使う）

    ブロックする readline を別スレッドで待つ形にはしない。時間切れになっても
    そのスレッドは stdin を掴んだまま残り、直後に起動するクロコと入力を奪い合うため。
    対話モードでは同じコンソールを共有するので、本人の最初のキー入力が
    こちらに吸われる、という気づきにくい壊れ方をする。
    """
    if msvcrt is not None:
        return _read_line_windows(timeout)
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    return sys.stdin.readline().strip() if ready else None


def _read_line_windows(timeout: float) -> str | None:
    """キー入力の有無を確認しながら自分で1行を組み立てる。

    `msvcrt.getwch` は画面に出さないので、打った文字は自分で書き戻す。
    """
    deadline = time.monotonic() + timeout
    typed = ""
    while time.monotonic() < deadline:
        if not msvcrt.kbhit():
            time.sleep(0.05)
            continue
        char = msvcrt.getwch()
        if char in ("\r", "\n"):
            _echo("\n")
            return typed.strip()
        if char == "\x03":
            raise KeyboardInterrupt
        if char in ("\b", "\x7f"):
            typed = typed[:-1]
            _echo("\b \b")
            continue
        if char in ("\x00", "\xe0"):
            msvcrt.getwch()  # 矢印などは2文字で届くので読み捨てる
            continue
        typed += char
        _echo(char)
    return None


def _echo(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()


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


def open_editor(config: Config, item: inbox.InboxItem) -> None:
    """本人が書く文書のときだけ、下書きエディタを先に開いておく。

    クロコはCLIなので文章を書く場所にならない。書くのは本人・相談相手がクロコ、
    という分担なので、書く場所を人手で探させないために先に出しておく。
    見つからなければ黙って何もしない（無いと困るものではない）。

    開くのは `.md` の関連付けと同じエディタ（`CROCO_EDITOR_PATH`）。
    **入口を1つに揃えておく**。別々にすると下書きの置き場が二重になる。
    """
    if item.hold_reason != inbox.HOLD_DOCUMENT:
        return
    editor = config.editor_path
    if not editor or not editor.is_file():
        return
    try:
        launch_editor(editor)
    except Exception as exc:
        log.warn(f"下書きエディタを開けませんでした: {exc}")
        return
    log.log("下書きエディタを開きました")


def launch_editor(editor: Path, target: Path | None = None) -> None:
    """下書きエディタを別プロセスとして切り離して開く。

    エディタはクロコの一部ではない（単体で使える別の道具で、別リポジトリにある）。
    ここではパスで呼ぶだけにして、同じ実装を2箇所に置かない。

    run_croco.py はこの後クロコを起動して先へ進むので、親が終わっても
    エディタは残っていてほしい。コンソールを出さないため pythonw を使う。
    エディタ側に exe の起動口（croco-editor.exe）もあるので、
    そちらを指されていたら Python を挟まずそのまま起動する。
    """
    if editor.suffix.lower() == ".exe":
        command = [str(editor)]
    else:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not pythonw.exists():
            pythonw = Path(sys.executable)
        command = [str(pythonw), str(editor)]
    if target is not None:
        command.append(str(target))
    creation = 0
    if os.name == "nt":
        creation = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        command,
        cwd=str(editor.parent),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation,
    )


def launch_claude(config: Config, prompt: str) -> int:
    """クロコを起動する。

    共通のオプション:
    - `--permission-mode auto` : 日常操作は自動承認し、危険操作は内蔵判定でブロック
    - `--settings`             : 上に重ねる deny リスト（仕様書2.5章-9）
    - `--add-dir`              : 作業対象ディレクトリを明示

    既定は**対話モード**。`-p`（非対話）だと、変な方向に進み始めても
    終わるまで口を挟めないのが最大の弱点になるため。
    対話で起動しておけば、横で見ていて必要なときだけ軌道修正できる。
    離れていてもパーミッションは自動承認なので勝手に進む。
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
        prompt,
        "--permission-mode",
        "auto",
        "--settings",
        str(SETTINGS_PATH),
        "--add-dir",
        str(projects_dir),
    ]

    # 通知の入切をクロコ側にも伝える。croco_settings.json の Notification フックが
    # これを見る。伝えないと `CROCO_NOTIFY=0` にしてもクロコだけ鳴り続ける
    # ＝消し方が2箇所に分かれてしまう。
    child_env = os.environ | {"CROCO_NOTIFY": "1" if config.notify else "0"}

    if config.interactive:
        return _run_interactive(command, projects_dir, child_env)
    return _run_headless(command, projects_dir, child_env)


def _run_interactive(command: list[str], projects_dir: Path, env: dict[str, str]) -> int:
    """画面をクロコに明け渡して対話で動かす。

    出力を横取りするとTUIが壊れるので、標準入出力はそのまま引き継ぐ。
    そのぶんログファイルにはクロコの発言が残らないが、
    セッション自体はClaude Code側に残るので `claude --resume` で追える。
    """
    log.log(f"クロコを対話モードで起動します（作業ディレクトリ: {projects_dir}）")
    log.log("画面をクロコに渡します。必要なら途中で口を挟んでください。")
    log.log("")

    process = subprocess.run(command, cwd=str(projects_dir), env=env)

    log.log("")
    log.log(f"クロコのセッションが終了しました (exit={process.returncode})")
    return process.returncode


def _run_headless(command: list[str], projects_dir: Path, env: dict[str, str]) -> int:
    """非対話で動かす（`CROCO_INTERACTIVE=0` のとき）。

    既定の text 形式は完了までバッファされ、実行中に画面へ何も出ないため、
    逐次イベントが流れる stream-json を使って進行を可視化する。
    """
    command = command[:1] + ["-p"] + command[1:]
    command += ["--output-format", "stream-json", "--verbose"]

    log.log(f"クロコを非対話モードで起動します（作業ディレクトリ: {projects_dir}）")
    log.log("--- ここからクロコの出力 ---")

    process = subprocess.Popen(
        command,
        cwd=str(projects_dir),
        env=env,
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
