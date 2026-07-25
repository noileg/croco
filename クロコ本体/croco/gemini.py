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
                        "enum": ["アイデア", "予定"],
                        "description": (
                            "日時が決まった予定の話なら「予定」、それ以外は「アイデア」。"
                        ),
                    },
                    "scheduled_date": {
                        "type": "string",
                        "description": (
                            "kindが「予定」の場合のみ、その予定の日時をISO 8601形式"
                            "（YYYY-MM-DD または YYYY-MM-DDTHH:MM:SS）で。"
                            "日時が特定できない場合や「アイデア」の場合は空文字列。"
                        ),
                    },
                },
                "required": ["title", "body", "kind", "scheduled_date"],
            },
        }
    },
    "required": ["items"],
}

SYSTEM_INSTRUCTION = """\
あなたは、音声やチャットで書き留められた個人のブレスト生ログを、
後工程（コーディングAI）が扱えるように**機械的に仕分けるだけ**の処理系です。

厳守すること:
- 話題の切れ目で分割する。それ以外の加工を一切しない。
- body は生ログの原文を**逐語でそのまま**転記する。要約・言い換え・整形・省略・補完はしない。
  言い淀み、口語、繰り返しもそのまま残す。原文に無い語を足さない。
- 分割の結果、全アイテムの body を連結すると元の生ログとほぼ一致する状態を保つ。
  どの話題にも属さない部分があっても捨てず、最も近い話題に含める。
- title は本文の場所を指すラベルにすぎない。内容を解釈・評価・仕様化しない。
- 何を作るべきか、どう実装すべきかを考えない。それは後工程の仕事。
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
                "kind": item.get("kind") if item.get("kind") in ("アイデア", "予定") else "アイデア",
                "scheduled_date": (item.get("scheduled_date") or "").strip(),
            }
        )
    return valid
