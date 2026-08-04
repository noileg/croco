"""Gemini API クライアント（生ログの分割・ラベリング専用）。

役割は仕様書2.5章-3で確定した範囲に限定する：
**話題の切れ目で分割するだけ。本文は要約・言い換えせず逐語転記。**
タイトルは本文を指す短いラベルのみで、意味理解・仕様化はさせない。

Geminiが要約した時点で失われた情報はクロコ側で復元できないため、
何を作るか決まっていない段階で非可逆圧縮を挟まないことが設計の要。

エンドポイントは `generateContent`（v1beta）を使う。新しい Interactions API が
推奨されつつあるが、リクエスト形が確実に検証できた方を採った。
API差し替えが必要になってもこのモジュール内で閉じる。
"""

from __future__ import annotations

import json

from . import httpjson

API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# 出力スキーマ。プロンプト本文にスキーマを重複記載しないこと（品質が落ちる）。
# 説明は各フィールドの description に持たせる（仕様書2.5章-11）。
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "description": "生ログを話題の切れ目で分割した結果。話題が1つだけなら要素も1つ。",
            "items": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": (
                            "本文がどの話題かを指し示すだけの短いラベル（30文字程度まで）。"
                            "内容を解釈・要約・仕様化した表現にはしないこと。"
                        ),
                    },
                    "body": {
                        "type": "string",
                        "description": (
                            "この話題に該当する生ログの原文をそのまま抜き出したもの。"
                            "要約・言い換え・省略・整形は一切しない逐語転記。"
                            "言い淀みや口語表現もそのまま残す。"
                        ),
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["アイデア", "予定", "資料"],
                        "description": (
                            "日時が決まった予定の話なら「予定」。"
                            "何かを作る・進めるという行動の話なら「アイデア」。"
                            "行動ではなく、前提・注意事項・参照先・現状整理など"
                            "後で読み返すための情報なら「資料」。"
                            "迷ったら「アイデア」ではなく「資料」に寄せること"
                            "（「アイデア」は自動で実装に着手される対象になるため）。"
                        ),
                    },
                    "scheduled_date": {
                        "type": "string",
                        "description": (
                            "日時が特定できる場合のみ、ISO 8601形式で。特定できなければ空文字列。"
                            "**時刻が原文に明示されていなければ日付のみ（YYYY-MM-DD）とし、"
                            "勝手に時刻を補わないこと。** 時刻が明示されている場合は"
                            "日本時間として YYYY-MM-DDTHH:MM:SS+09:00 の形式で書く。"
                            "期間の場合はここに開始日を入れる。"
                        ),
                    },
                    "scheduled_end": {
                        "type": "string",
                        "description": (
                            "「〜から〜まで」のように期間として示されている場合のみ、"
                            "その終了日時をscheduled_dateと同じ形式で。"
                            "単一の日時の場合や期間でない場合は空文字列。"
                        ),
                    },
                    "human_reason": {
                        "type": "string",
                        "enum": [
                            "なし",
                            "本人名義の文書",
                            "現実世界の行動",
                            "クロコ自身の改修",
                            "本人の判断",
                        ],
                        "description": (
                            "本人自身が対応する必要があり、AIに任せてはいけない内容なら"
                            "その理由を選ぶ。任せてよいなら「なし」。"
                            "「本人名義の文書」＝本人名義で提出する文書の作成"
                            "（自己推薦書、志望理由書、活動経過報告書、出願書類、"
                            "大学のレポート、エントリーシート等。"
                            "AIが書いたことが問題になり得るもの）。"
                            "「現実世界の行動」＝書類の取り寄せ、郵送、窓口手続き、"
                            "予約、支払いなど、AIが実行できない行動が必要なもの。"
                            "「クロコ自身の改修」＝このワークフロー基盤"
                            "（クロコ／Notion連携／捕捉・実装パイプライン自体）の"
                            "仕様変更・修正・機能追加の依頼。"
                            "「本人の判断」＝本人の価値判断・意思決定そのものが中身になるもの。"
                            "判断に迷う場合は「なし」以外を選ぶこと"
                            "（「なし」にすると自動で着手されるため、誤りの代償が大きい）。"
                        ),
                    },
                },
                "required": [
                    "title",
                    "body",
                    "kind",
                    "scheduled_date",
                    "scheduled_end",
                    "human_reason",
                ],
            },
        }
    },
    "required": ["items"],
}

# 種別の妥当性判定はスキーマのenumを唯一の情報源にする
# （2箇所に書くと、片方だけ増やしたときに黙って値が握り潰される）。
VALID_KINDS = frozenset(
    RESPONSE_SCHEMA["properties"]["items"]["items"]["properties"]["kind"]["enum"]
)
DEFAULT_KIND = "アイデア"

VALID_HUMAN_REASONS = frozenset(
    RESPONSE_SCHEMA["properties"]["items"]["items"]["properties"]["human_reason"]["enum"]
)
# 値が欠けていた・知らない値だった場合の倒し方。
# 「なし」に倒すと自動で着手されてしまうので、必ず要確認になる側へ倒す。
DEFAULT_HUMAN_REASON = "本人の判断"

SYSTEM_INSTRUCTION = """\
あなたは、音声やチャットで書き留められた個人のブレスト生ログを、
後工程（コーディングAI）が扱えるように**機械的に仕分けるだけ**の処理系です。

厳守すること:
- 話題の切れ目で分割する。それ以外の加工を一切しない。
- **分割の粒度は「後で1つずつ着手・消化できる単位」に揃える。**
  やること・タスク・手順が箇条書きで複数並んでいる場合は、
  **1項目＝1アイテムに分ける**。「やること一覧」のようにまとめてはいけない。
  逆に、1つの話題を意味もなく細切れにもしない。
- body は生ログの原文を**逐語でそのまま**転記する。要約・言い換え・整形・省略・補完はしない。
  言い淀み、口語、繰り返しもそのまま残す。原文に無い語を足さない。
- 分割の結果、全アイテムの body を連結すると元の生ログとほぼ一致する状態を保つ。
  どの話題にも属さない部分があっても捨てず、最も近い話題に含める。
- title は本文の場所を指すラベルにすぎない。内容を解釈・評価・仕様化しない。
- 何を作るべきか、どう実装すべきかを考えない。それは後工程の仕事。
"""

# 関連度判定用のスキーマ。分割によってDB上は別行になっていても、本人の頭の中では
# 地続きの話であることが普通にある（2026-07-30、本人の指摘）。クロコは面倒がって
# 自発的に横断を探しには行かないため、判定済みの候補を最初からプロンプトに書く。
RELATED_SCHEMA = {
    "type": "object",
    "properties": {
        "related_ids": {
            "type": "array",
            "description": (
                "新しいアイテムと実質的に関連する既存アイテムのIDだけを返す。"
                "表現が似ている・テーマが漠然と近いだけのものは含めない。"
                "同じ取り組み・同じプロジェクトの続き、明確な依存・参照関係がある場合だけ選ぶ。"
                "迷ったら含めない。関連が無ければ空配列。"
            ),
            "items": {"type": "string"},
        }
    },
    "required": ["related_ids"],
}

RELATED_SYSTEM_INSTRUCTION = """\
あなたは、新しいメモが既存アイテム群のどれと実質的に関連するかを判定するだけの処理系です。

厳守すること:
- 関連の有無だけを判定する。実装方針やアイデアの中身の評価はしない。
- 表面的なキーワードの一致ではなく、同じ取り組み・同じプロジェクトの続きかどうかで判断する。
- 迷ったら含めない。過剰に拾うと、無関係な文脈が後工程に混ざる。
"""

# 重複予定判定用のスキーマ。「まとめノート」が既に登録済みの予定を
# 別の言い回しで再掲すると、捕捉フェーズが律儀に二重登録してしまう
# （実例：2026-07-31、筑波大学AC入試の日程重複）。これを防ぐための狭い判定。
DUPLICATE_SCHEMA = {
    "type": "object",
    "properties": {
        "duplicate_id": {
            "type": "string",
            "description": (
                "新しい予定と実質的に同じ予定だと高い確信で判定できた"
                "既存アイテムのIDを1つだけ返す。予定日が違う、対象が違う、"
                "似ているが別件の可能性が少しでもあれば空文字列にすること。"
                "迷ったら空文字列（重複扱いにしない）。該当は最大1件。"
            ),
        }
    },
    "required": ["duplicate_id"],
}

DUPLICATE_SYSTEM_INSTRUCTION = """\
あなたは、新しい「予定」が既存の「予定」群のどれかと同一の予定を指しているかどうかを
判定するだけの処理系です。

厳守すること:
- 同一の予定を指しているかどうかだけを判定する。表現の違い・情報量の差は無視してよい。
- 予定日が異なる、または別件の可能性が少しでもあれば重複と判定しない。
- 迷ったら重複と判定しない（誤って統合すると後から分離できなくなるため）。
- 該当が無ければ duplicate_id は空文字列。
"""

# ジャンル/プロジェクト分類用のスキーマ。優先度と違い価値判断ではなく機械的な
# 仕分けなので、種別と同じ扱いでGeminiに任せる（2026-08-01、本人の指摘）。
# 既存ジャンル一覧を毎回渡し、表記ゆれで似た束が増殖しないようにする。
GENRE_SCHEMA = {
    "type": "object",
    "properties": {
        "genre": {
            "type": "string",
            "description": (
                "このアイテムが属するジャンル/プロジェクト名。"
                "既存ジャンル一覧の中に実質同じものがあれば、必ずその表記をそのまま使う"
                "（似た意味の別表記で新しい束を作らないこと）。"
                "無ければ短い新しいジャンル名を作る（数語程度、例：「受験」"
                "「クロコ本体」）。内容の要約ではなく、束ねるための短いラベル。"
            ),
        }
    },
    "required": ["genre"],
}

GENRE_SYSTEM_INSTRUCTION = """\
あなたは、新しいメモが既存のジャンル/プロジェクトのどれに属するかを判定する、
または新しいジャンル名を短く付けるだけの処理系です。

厳守すること:
- 既存ジャンル一覧に実質同じ括りがあれば、その表記をそのまま返す。
  「受験」と「受験関連」のような表記ゆれで別の束を作らないこと。
- 既存のどれにも実質同じ括りが無い場合のみ、新しい短いジャンル名を作る。
- ジャンル名は内容の要約ではなく、複数アイテムを束ねるための短いラベル
  （プロジェクト名・分野名程度）にする。
"""

# 棚卸し（backfill）用の一括判定スキーマ。1件ずつAPIを呼ぶと件数分だけ
# リクエストを消費し、Gemini無料枠の日次上限（実測20件/日、2026-08-01）に
# 即座に当たる。複数件をまとめて1回のプロンプトで渡し、まとめて分類させる
# （このモジュール冒頭の「ブレスト1セッション＝API1回」と同じ発想）。
GENRE_BATCH_SCHEMA = {
    "type": "object",
    "properties": {
        "assignments": {
            "type": "array",
            "description": "渡された全アイテムそれぞれに割り振ったジャンル。",
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "対象アイテムのid（渡された値をそのまま返す）。",
                    },
                    "genre": {
                        "type": "string",
                        "description": (
                            "このアイテムが属するジャンル/プロジェクト名。"
                            "既存ジャンル一覧、または他の対象アイテムと実質同じ括りなら"
                            "同じ表記を使う（表記ゆれで似た束を作らない）。"
                            "どれにも属さなければ短い新しいジャンル名を作る。"
                        ),
                    },
                },
                "required": ["id", "genre"],
            },
        }
    },
    "required": ["assignments"],
}

GENRE_BATCH_SYSTEM_INSTRUCTION = """\
あなたは、複数のメモをジャンル/プロジェクト単位に束ねるだけの処理系です。

厳守すること:
- 全対象アイテムに、渡されたidそのままで1件ずつジャンルを割り振る。抜かさない。
- 既存ジャンル一覧に実質同じ括りがあれば、その表記をそのまま使う。
- 既存に無くても、**対象アイテム同士で実質同じ括りなら同じ新しいジャンル名**を使う
  （表記ゆれで別々の束を作らない）。
- ジャンル名は内容の要約ではなく、束ねるための短いラベル（プロジェクト名・分野名程度）。
"""


class Gemini:
    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        thinking_level: str = "minimal",
        temperature: float = 0.1,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._thinking_level = thinking_level
        self._temperature = temperature

    def split_raw_log(self, raw_log: str, *, captured_at: str) -> list[dict]:
        """生ログ1件を話題ごとに分割して返す。

        ブレスト1セッション＝API1回（仕様書2.5章-11）。
        LLMは現在時刻を知らないため、相対表現（「来週の火曜」等）を
        解決できるよう捕捉日時をスクリプト側から注入する。
        """
        prompt = (
            f"このメモが書かれた日時: {captured_at}\n"
            "（「明日」「来週」などの相対表現はこの日時を基準に解釈すること）\n"
            "\n"
            "--- 生ログここから ---\n"
            f"{raw_log}\n"
            "--- 生ログここまで ---"
        )

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "systemInstruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
            "generationConfig": {
                "temperature": self._temperature,
                "thinkingConfig": {"thinkingLevel": self._thinking_level},
                # mimeType は自由文字列ではなく enum。"application/json" は400になる。
                # 有効値は実APIで確認済み（APPLICATION_JSON のみ通る）。
                "responseFormat": {
                    "text": {
                        "mimeType": "APPLICATION_JSON",
                        "schema": RESPONSE_SCHEMA,
                    }
                },
            },
        }

        response = httpjson.request_json(
            f"{API_BASE}/models/{self._model}:generateContent",
            method="POST",
            headers={"x-goog-api-key": self._api_key},
            payload=payload,
        )
        return _parse_items(response)

    def find_related(
        self, current_title: str, current_body: str, candidates: list[dict]
    ) -> list[str]:
        """既存アイテム群のうち、現在のアイテムと関連するものだけをidで返す。

        候補には本文（body）まで含めて渡す。タイトルは意味解釈しない短いラベルに
        留める設計（本モジュール冒頭のdocstring参照）なので、関連判定の材料としては薄い。
        呼び出し失敗（ネットワーク等）はここでは吸収しない。呼び出し元が
        「関連候補なしで通常どおり進める」形で吸収する（分割・登録本体を巻き込まないため）。
        """
        if not candidates:
            return []

        blocks = []
        for c in candidates:
            blocks.append(
                f"- id: {c['id']}\n"
                f"  タイトル: {c['title']}\n"
                f"  種別: {c['kind']} / ステータス: {c['status']}\n"
                f"  本文: {c['body']}"
            )
        prompt = (
            f"## 新しいアイテム\nタイトル: {current_title}\n本文: {current_body}\n\n"
            "## 既存アイテム一覧\n" + "\n".join(blocks)
        )

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "systemInstruction": {"parts": [{"text": RELATED_SYSTEM_INSTRUCTION}]},
            "generationConfig": {
                "temperature": self._temperature,
                "thinkingConfig": {"thinkingLevel": self._thinking_level},
                "responseFormat": {
                    "text": {
                        "mimeType": "APPLICATION_JSON",
                        "schema": RELATED_SCHEMA,
                    }
                },
            },
        }

        response = httpjson.request_json(
            f"{API_BASE}/models/{self._model}:generateContent",
            method="POST",
            headers={"x-goog-api-key": self._api_key},
            payload=payload,
        )
        valid_ids = {c["id"] for c in candidates}
        return _parse_related(response, valid_ids=valid_ids)

    def find_duplicate_schedule(
        self, title: str, body: str, scheduled_date: str, candidates: list[dict]
    ) -> str:
        """新しい「予定」が既存の「予定」群と重複していないか判定する。

        重複と高確信で判定できた場合のみIDを返す。呼び出し失敗はここでは吸収しない
        （呼び出し元の dedupe.py が「判定できなければ重複なし」として吸収する）。
        """
        if not candidates:
            return ""

        blocks = []
        for c in candidates:
            blocks.append(
                f"- id: {c['id']}\n"
                f"  タイトル: {c['title']}\n"
                f"  予定日: {c['scheduled']}\n"
                f"  本文: {c['body']}"
            )
        prompt = (
            f"## 新しい予定\nタイトル: {title}\n予定日: {scheduled_date}\n本文: {body}\n\n"
            "## 既存の「予定」一覧\n" + "\n".join(blocks)
        )

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "systemInstruction": {"parts": [{"text": DUPLICATE_SYSTEM_INSTRUCTION}]},
            "generationConfig": {
                "temperature": self._temperature,
                "thinkingConfig": {"thinkingLevel": self._thinking_level},
                "responseFormat": {
                    "text": {
                        "mimeType": "APPLICATION_JSON",
                        "schema": DUPLICATE_SCHEMA,
                    }
                },
            },
        }

        response = httpjson.request_json(
            f"{API_BASE}/models/{self._model}:generateContent",
            method="POST",
            headers={"x-goog-api-key": self._api_key},
            payload=payload,
        )
        valid_ids = {c["id"] for c in candidates}
        return _parse_duplicate(response, valid_ids=valid_ids)

    def assign_genre(self, title: str, body: str, existing_genres: list[str]) -> str:
        """アイテム1件のジャンル/プロジェクト名を判定する。

        既存ジャンル一覧に実質同じ括りがあればその表記を、無ければ新しい
        短いラベルを返す。呼び出し失敗はここでは吸収しない（呼び出し元の
        genre.py が「未分類のまま」として吸収する）。
        """
        genres_block = "\n".join(f"- {g}" for g in existing_genres) or "（まだ無い）"
        prompt = (
            f"## 既存ジャンル一覧\n{genres_block}\n\n"
            f"## 新しいアイテム\nタイトル: {title}\n本文: {body}"
        )

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "systemInstruction": {"parts": [{"text": GENRE_SYSTEM_INSTRUCTION}]},
            "generationConfig": {
                "temperature": self._temperature,
                "thinkingConfig": {"thinkingLevel": self._thinking_level},
                "responseFormat": {
                    "text": {
                        "mimeType": "APPLICATION_JSON",
                        "schema": GENRE_SCHEMA,
                    }
                },
            },
        }

        response = httpjson.request_json(
            f"{API_BASE}/models/{self._model}:generateContent",
            method="POST",
            headers={"x-goog-api-key": self._api_key},
            payload=payload,
        )
        return _parse_genre(response)

    def assign_genres_batch(
        self, items: list[dict], existing_genres: list[str]
    ) -> dict[str, str]:
        """複数アイテムのジャンルを1回のAPI呼び出しでまとめて判定する（棚卸し用）。

        `items` は [{"id":..,"title":..,"body":..}, ...]。件数が多いとプロンプトが
        長くなる分だけ時間がかかるが、API呼び出し回数は1回で済む（無料枠の
        日次上限に当たらないようにするため。2026-08-01、429連発への対応）。
        呼び出し失敗はここでは吸収しない（呼び出し元の genre.py が
        「未分類のまま」として吸収する）。
        """
        if not items:
            return {}

        genres_block = "\n".join(f"- {g}" for g in existing_genres) or "（まだ無い）"
        blocks = [
            f"- id: {i['id']}\n  タイトル: {i['title']}\n  本文: {i['body']}" for i in items
        ]
        prompt = (
            f"## 既存ジャンル一覧\n{genres_block}\n\n"
            "## 対象アイテム一覧\n" + "\n".join(blocks)
        )

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "systemInstruction": {"parts": [{"text": GENRE_BATCH_SYSTEM_INSTRUCTION}]},
            "generationConfig": {
                "temperature": self._temperature,
                "thinkingConfig": {"thinkingLevel": self._thinking_level},
                "responseFormat": {
                    "text": {
                        "mimeType": "APPLICATION_JSON",
                        "schema": GENRE_BATCH_SCHEMA,
                    }
                },
            },
        }

        response = httpjson.request_json(
            f"{API_BASE}/models/{self._model}:generateContent",
            method="POST",
            headers={"x-goog-api-key": self._api_key},
            payload=payload,
        )
        valid_ids = {i["id"] for i in items}
        return _parse_genre_batch(response, valid_ids=valid_ids)


def _parse_genre_batch(response: dict, *, valid_ids: set[str]) -> dict[str, str]:
    """レスポンスから id→genre の対応を取り出す。壊れていれば空辞書。"""
    candidates = response.get("candidates") or []
    if not candidates:
        return {}
    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts).strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    assignments = parsed.get("assignments")
    if not isinstance(assignments, list):
        return {}
    result: dict[str, str] = {}
    for entry in assignments:
        if not isinstance(entry, dict):
            continue
        item_id = entry.get("id")
        genre = entry.get("genre")
        if (
            isinstance(item_id, str)
            and item_id in valid_ids
            and isinstance(genre, str)
            and genre.strip()
        ):
            result[item_id] = genre.strip()
    return result


def _parse_genre(response: dict) -> str:
    """レスポンスから genre を取り出す。壊れていれば空文字列（未分類）。

    _parse_related と同じ理由（補助機能なので例外を投げない）。
    """
    candidates = response.get("candidates") or []
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts).strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return ""
    genre = parsed.get("genre")
    return genre.strip() if isinstance(genre, str) else ""


def _parse_duplicate(response: dict, *, valid_ids: set[str]) -> str:
    """レスポンスから duplicate_id を取り出す。壊れていれば重複なし扱いにする。

    _parse_related と同じ理由（補助機能なので例外を投げない）。
    """
    candidates = response.get("candidates") or []
    if not candidates:
        return ""
    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts).strip()
    if not text:
        return ""
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return ""
    dup_id = parsed.get("duplicate_id")
    if not isinstance(dup_id, str) or dup_id not in valid_ids:
        return ""
    return dup_id


def _parse_related(response: dict, *, valid_ids: set[str]) -> list[str]:
    """レスポンスから related_ids を取り出す。壊れていれば空扱いにする。

    _parse_items と違い、ここは補助機能（無くても本体のdispatch/consultは動く）
    なので、壊れたレスポンスで例外を投げず空リストに倒す。
    """
    candidates = response.get("candidates") or []
    if not candidates:
        return []
    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts).strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return []
    ids = parsed.get("related_ids")
    if not isinstance(ids, list):
        return []
    # 存在しないIDを返してくることに備え、候補集合と突き合わせて弾く。
    return [i for i in ids if isinstance(i, str) and i in valid_ids]


def _parse_items(response: dict) -> list[dict]:
    """レスポンスから items 配列を取り出す。"""
    candidates = response.get("candidates") or []
    if not candidates:
        feedback = response.get("promptFeedback", {})
        raise RuntimeError(f"Geminiが候補を返しませんでした: {feedback}")

    parts = candidates[0].get("content", {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts).strip()
    if not text:
        reason = candidates[0].get("finishReason", "不明")
        raise RuntimeError(f"Geminiの応答が空でした（finishReason={reason}）")

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"GeminiがJSONを返しませんでした: {text[:500]}") from exc

    items = parsed.get("items")
    if not isinstance(items, list):
        raise RuntimeError(f"items配列がありません: {text[:500]}")

    # スキーマで縛ってはいるが、後段のNotion登録で落ちないよう最低限を確認する。
    valid: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        body = (item.get("body") or "").strip()
        if not body:
            continue  # 本文が無いものは登録する意味がない
        valid.append(
            {
                "title": (item.get("title") or "").strip() or body[:30],
                "body": item.get("body", ""),
                "kind": item.get("kind") if item.get("kind") in VALID_KINDS else DEFAULT_KIND,
                "scheduled_date": (item.get("scheduled_date") or "").strip(),
                "scheduled_end": (item.get("scheduled_end") or "").strip(),
                # 値が欠けていた場合は安全側（本人対応が必要）に倒す。
                "human_reason": (
                    item.get("human_reason")
                    if item.get("human_reason") in VALID_HUMAN_REASONS
                    else DEFAULT_HUMAN_REASON
                ),
            }
        )
    return valid
