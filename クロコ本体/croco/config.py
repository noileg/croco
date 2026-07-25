"""設定の読み込み。

APIキー等は仕様書2章-7の方針どおり、プロジェクトフォルダの外に置いた
`.env` から読む（既定: %USERPROFILE%\\.croco\\.env）。
依存パッケージを増やさないため、.env のパースは自前で行う。
"""

from __future__ import annotations

import os
from pathlib import Path

# .env の既定位置。CROCO_ENV_FILE で上書きできる。
DEFAULT_ENV_PATH = Path.home() / ".croco" / ".env"

# クロコ本体（このファイルの2つ上）のディレクトリ
CROCO_HOME = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """設定が足りない・壊れている場合。"""


def _parse_env_file(path: Path) -> dict[str, str]:
    """`KEY=VALUE` 形式の最小限のパーサ。

    - `#` 始まりの行と空行は無視
    - 値を囲む引用符（' か "）は剥がす
    - `export KEY=VALUE` 形式も許容
    """
    values: dict[str, str] = {}
    if not path.is_file():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_env() -> dict[str, str]:
    """.env を読み、実プロセスの環境変数を優先して重ねた辞書を返す。

    環境変数を優先するのは、一時的な上書き（テスト実行など）を
    .env を書き換えずに行えるようにするため。
    """
    env_path = Path(os.environ.get("CROCO_ENV_FILE", DEFAULT_ENV_PATH))
    values = _parse_env_file(env_path)
    for key, value in os.environ.items():
        if value:
            values[key] = value
    values["_ENV_PATH"] = str(env_path)
    return values


class Config:
    """クロコ全体で使う設定値。"""

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self._env = env if env is not None else load_env()

    # --- 内部ヘルパ ---------------------------------------------------

    def _get(self, key: str, default: str | None = None) -> str | None:
        value = self._env.get(key, default)
        return value or default

    def require(self, key: str) -> str:
        """必須の設定値。無ければ、どこに何を書けばよいかを示して失敗する。"""
        value = self._env.get(key)
        if not value:
            raise ConfigError(
                f"必須の設定 {key} がありません。\n"
                f"  {self._env.get('_ENV_PATH')} に `{key}=...` を追記してください。\n"
                f"  （雛形: クロコ本体\\.env.example）"
            )
        return value

    # --- 認証情報 -----------------------------------------------------

    @property
    def notion_token(self) -> str:
        return self.require("NOTION_TOKEN")

    @property
    def gemini_api_key(self) -> str:
        return self.require("GEMINI_API_KEY")

    # --- Notion 側の実体 ----------------------------------------------

    @property
    def unprocessed_page_id(self) -> str:
        """「未処理置き場」親ページのID。"""
        return self.require("NOTION_UNPROCESSED_PAGE_ID")

    @property
    def processed_page_id(self) -> str:
        """「処理済み置き場」親ページのID。"""
        return self.require("NOTION_PROCESSED_PAGE_ID")

    @property
    def inbox_database_id(self) -> str:
        """Inbox DB（データベース）のID。"""
        return self.require("NOTION_INBOX_DATABASE_ID")

    @property
    def inbox_data_source_id(self) -> str:
        """Inbox DB のデータソースID。

        Notion API 2025-09-03 以降、DB配下のページ作成・クエリは
        database_id ではなくこの data_source_id を親に指定する。
        未設定でも database_id から自動解決できるので必須にはしない。
        """
        return self._env.get("NOTION_INBOX_DATA_SOURCE_ID") or ""

    # --- 動作パラメータ -------------------------------------------------

    @property
    def gemini_model(self) -> str:
        # 仕様書2.5章-3では3.5 Flashで確定していたが、現行GAの Flash が
        # 3.6 になったため本人の判断で更新（2026-07-25）。切り替えは .env で行う。
        return self._get("GEMINI_MODEL", "gemini-3.6-flash") or "gemini-3.6-flash"

    @property
    def gemini_thinking_level(self) -> str:
        # 分割・転記に深い推論は不要（仕様書2.5章-11）。
        return self._get("GEMINI_THINKING_LEVEL", "minimal") or "minimal"

    @property
    def gemini_temperature(self) -> float:
        # 抽出タスクなので創造性は不要（仕様書2.5章-11）。
        return float(self._get("GEMINI_TEMPERATURE", "0.1") or "0.1")

    @property
    def notion_version(self) -> str:
        # Move Page API が使えるバージョン以上であること。
        return self._get("NOTION_VERSION", "2026-03-11") or "2026-03-11"

    @property
    def max_retries(self) -> int:
        """同一アイテムの再試行上限（仕様書2.5章-12、たたき台3回）。

        これを超えても進捗がなければ「要確認」に落として人間待ちにする。
        """
        return int(self._get("CROCO_MAX_RETRIES", "3") or "3")

    @property
    def max_items(self) -> int:
        """1回の起動で連続して処理するアイテム数の上限（たたき台3件）。

        1件終わるたびに次を取りに行くが、無制限にすると離席中に
        プランの使用枠を使い切りうる。PC起動のたびに走るものなので上限は必ず要る。
        0 にすると実装フェーズを何もしない（捕捉だけしたいときの逃げ道）。
        """
        return int(self._get("CROCO_MAX_ITEMS", "3") or "3")

    @property
    def token_budget(self) -> int:
        """1回の起動で使ってよいトークンの目安。0 で無制限（既定）。

        アイテムの途中で打ち切ることはできないので、
        「次を始めるかどうか」の判断にだけ使う。超過したら次に進まない。
        """
        return int(self._get("CROCO_TOKEN_BUDGET", "0") or "0")

    @property
    def projects_dir(self) -> Path:
        """クロコが無人実装するプロジェクトの置き場（仕様書2.5章-13）。

        パーミッション上の「プロジェクト外への書き込み禁止」の境界でもある。
        """
        value = self._env.get("CROCO_PROJECTS_DIR")
        if value:
            return Path(value)
        return CROCO_HOME.parent / "クロコ管轄プロジェクト"

    @property
    def editor_path(self) -> Path | None:
        """下書きエディタ（ブラウザで開くHTML）の場所。

        本人が書く文書のときだけ開く。クロコはCLIなので文章を書く場所にならず、
        書くのは本人・相談相手がクロコ、という分担にしているため。
        無ければ開かないだけなので必須ではない。
        """
        value = self._env.get("CROCO_EDITOR_PATH")
        if value:
            return Path(value)
        return CROCO_HOME.parent.parent / "プログラミング関係" / "Twitter-like-char-counter.html"

    @property
    def log_dir(self) -> Path:
        value = self._env.get("CROCO_LOG_DIR")
        if value:
            return Path(value)
        return CROCO_HOME / "logs"

    @property
    def pick_timeout(self) -> float:
        """着手アイテムを選ぶ猶予（秒）。0以下で「常に自動選択」。

        PC起動時に無人で走ることが前提なので、待ち続けない。
        """
        return float(self._get("CROCO_PICK_TIMEOUT", "60") or "60")

    @property
    def consult_timeout(self) -> float:
        """相談するか聞く猶予（秒）。0以下で「聞かない」。

        着手アイテムを選ぶとき（pick_timeout）より短くしてある。
        あちらは複数候補があるときだけ出るが、こちらは「要確認」が1件でもあれば出る。
        つまり**ほぼ毎回の起動で出る**ので、長いとその分だけ毎回待たされる。
        しかも全処理が終わったあとなので、待っている間は何も進んでいない。
        """
        return float(self._get("CROCO_CONSULT_TIMEOUT", "20") or "20")

    @property
    def interactive(self) -> bool:
        """対話モードでクロコを起動するか（既定：する）。

        非対話（`claude -p`）だと、途中で軌道修正できないのが最大の弱点になる。
        変な方向に進み始めても、終わるまで口を挟めない。
        対話で起動しておけば、横で見ていて必要なときだけ介入できる。
        本人が離れていても、パーミッションは自動承認なので勝手に進む。
        """
        return (self._get("CROCO_INTERACTIVE", "1") or "1").lower() not in (
            "0",
            "false",
            "no",
        )

    @property
    def notify(self) -> bool:
        """節目で音を鳴らすか（既定：鳴らす）。

        鳴らすのは「入力待ちになった」「全部終わった」の2つだけ（仕様書4章）。
        """
        return (self._get("CROCO_NOTIFY", "1") or "1").lower() not in (
            "0",
            "false",
            "no",
        )

    @property
    def claude_command(self) -> str:
        return self._get("CROCO_CLAUDE_COMMAND", "claude") or "claude"

    @property
    def dry_run(self) -> bool:
        return (self._get("CROCO_DRY_RUN", "") or "").lower() in ("1", "true", "yes")
