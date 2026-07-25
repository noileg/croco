"""相談フェーズ：「要確認」で止まっているものを、その場で話して片付ける。

「要確認」は本人の判断待ちだが、**戻す経路が無いと溜まる一方**になる。
実装フェーズが終わってまとめを見た直後が、何を話すべきか一番分かっている瞬間なので、
そこで「今ここで話すか」を聞く。

実装フェーズとの違いが2つある。

1. **既定は「話さない」**。実装フェーズは無人で進むことが前提なので時間切れなら続行するが、
   相談は本人が居ないと成立しないうえトークンも食う。時間切れなら黙って終わる。
2. **プロンプトが別**。目的は実装ではなく「本人の判断が要る点を特定して、聞いて、決着させる」。
   決着したら `croco_cli.py resume` でキューに戻し、そのまま実装へ移ってよい。
"""

from __future__ import annotations

import sys

from .config import CROCO_HOME, Config
from . import inbox, log
from . import notion as nt
from .dispatch import launch_claude, open_editor, read_line

# 保留理由ごとに、クロコが踏んではいけない線が違う。
# 表示用の一行（inbox.HOLD_ACTIONS）は本人向けなので、こちらはクロコ向けに書き分ける。
REASON_NOTES: dict[str, str] = {
    inbox.HOLD_DOCUMENT: (
        "本人名義で提出する文書です。**本文はあなたが書きません。**\n"
        "  してよいこと：要件の整理（字数制限・締切・必須項目）、下調べ、構成や骨子の相談、\n"
        "  `下書き/` にある原稿を読んでの指摘（どこが・なぜ）。\n"
        "  してはいけないこと：本文そのもの。書き出し例・言い換え案・清書・\n"
        "  「こう直すといい」という文章の提示を含みます。文は本人に書かせてください。\n"
        "  `下書き/` 配下は Edit も Write も拒否されます。要件を書き残す先はその外にしてください。\n"
        "  本人が書くための下書きエディタは、この起動と一緒に開いてあります。"
    ),
    inbox.HOLD_REAL_WORLD: (
        "現実世界の行動（取り寄せ・郵送・窓口手続き・予約など）が必要です。\n"
        "  あなたには実行できません。何が・いつまでに・どういう手順で要るのかを整理し、\n"
        "  本人がすぐ動ける形にするところまでをやってください。"
    ),
    inbox.HOLD_CROCO: (
        "クロコ自身（このワークフロー基盤）の改修依頼です。\n"
        "  `クロコ本体` 配下は書き換えが禁止されています。あなたを縛っている設定が\n"
        "  そこにあるためで、これは外しません。読んで改修案を出すところまでにしてください。\n"
        "  適用は本人が別途 Claude Code を開いて行います。"
    ),
    inbox.HOLD_JUDGEMENT: (
        "本人の価値判断そのものが中身です。**あなたが決めないでください。**\n"
        "  選択肢と、それぞれの判断材料（利点・代償・後戻りのしやすさ）を出して、\n"
        "  本人に選ばせてください。推奨を言うのは構いませんが、決定は本人のものです。"
    ),
    inbox.HOLD_RETRIES: (
        "同じ作業を繰り返し失敗して止まりました。\n"
        "  まず下の経緯を読んで**何が原因で失敗しているのか**を特定してから相談してください。\n"
        "  原因が分からないまま同じやり方を再開しても、また同じところで止まります。"
    ),
    inbox.HOLD_ASKED: (
        "前回のあなたが判断に詰まって本人に聞いた状態です。\n"
        "  その質問は下の経緯に残っています。まずそれを本人に聞いてください。"
    ),
}

PROMPT_TEMPLATE = """\
あなたは「クロコ」です。本人の判断が必要で止まっているアイテムについて、
**本人と話して決着させる**ために起動されました。実装のためではありません。

本人は画面の前にいます。**質問して、答えを待ってください。**

## 対象アイテム
- Notionページ ID: {page_id}
- タイトル: {title}
- 保留理由: {hold_reason}

## 内容（本人のブレスト生ログの逐語転記。要約されていないので冗長です）
{body}

## これまでの経緯
{progress}

## この保留理由についての注意
{reason_note}

## 進め方
1. 上の内容から、**何が決まっていないから止まっているのか**を具体的に特定する。
2. 本人に質問する。一度に全部並べず、答えやすい単位で聞く。
   **推測で埋めないこと。** 分からないことは聞く。
3. 決まったことは、その都度こう記録する:

```
python "{cli_path}" log {page_id} "決まったこと"
```

4. 着手できる状態まで決着したら、キューに戻す:

```
python "{cli_path}" resume {page_id} "決着した内容"
```

5. まだ本人が決められない・別途調べないと決まらない場合は、そのまま置いて構いません:

```
python "{cli_path}" review {page_id} "何が決まれば進めるか"
```

## 決着したあと
`resume` したら、そのまま実装に移って構いません。
本人が「やって」と言えば `{projects_dir}` の下で進めてください。
言われなければ、決着したことを伝えて終わってください。
"""


def offer(config: Config, items: list[inbox.InboxItem] | None) -> bool:
    """「要確認」があれば、その場で話すか聞く。話したら True。

    **既定は話さない。** 画面が無い・時間切れ・何も入力しない、のいずれでも
    黙って終わる。PC起動のたびに相談を強いられるのは本意ではないため。
    """
    if config.dry_run or not config.interactive or config.consult_timeout <= 0:
        return False
    if not (sys.stdin and sys.stdin.isatty()):
        return False

    review = [item for item in (items or []) if item.status == inbox.STATUS_REVIEW]
    if not review:
        return False

    log.log("")
    log.log("この中で今ここで話すものはありますか？")
    for index, item in enumerate(review, 1):
        reason = item.hold_reason or "理由の記録なし"
        log.log(f"  {index:2}) [{reason}] {item.title}")
    log.log(
        f"番号を入れてEnter（{config.consult_timeout:.0f}秒で、何もせず終了）: "
    )

    answer = read_line(config.consult_timeout)
    if not answer:
        return False
    try:
        index = int(answer)
    except ValueError:
        log.log("番号として読めないので、何もせず終了します。")
        return False
    if not 1 <= index <= len(review):
        log.log("範囲外なので、何もせず終了します。")
        return False

    return run(config, review[index - 1])


def run(config: Config, item: inbox.InboxItem) -> bool:
    """1件について相談する。"""
    client = nt.Notion(config.notion_token, config.notion_version)
    try:
        body = client.get_page_text(item.id)
    except Exception as exc:
        log.error(f"内容を読めませんでした: {exc}")
        return False

    log.log(f"相談します: [{item.hold_reason or '理由の記録なし'}] {item.title}")

    # 本人が書く文書なら、書く場所を先に開いておく。
    open_editor(config, item)

    reason_note = REASON_NOTES.get(
        item.hold_reason,
        "保留理由が記録されていません。まず**なぜ本人の判断が要るのか**から確認してください。",
    )
    prompt = PROMPT_TEMPLATE.format(
        page_id=item.id,
        title=item.title,
        hold_reason=item.hold_reason or "（記録なし）",
        body=body,
        progress=item.result_log.strip() or "（ありません）",
        reason_note=reason_note,
        cli_path=CROCO_HOME / "croco_cli.py",
        projects_dir=config.projects_dir,
    )

    exit_code = launch_claude(config, prompt)
    if exit_code != 0:
        log.warn(f"相談のセッションが異常終了しました (exit={exit_code})。")
    return True
