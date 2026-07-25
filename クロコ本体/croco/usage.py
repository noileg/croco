"""クロコが1回のセッションで使ったトークン量を、実行後に拾う。

Claude Code はセッションの記録を `~/.claude/projects/<エンコードしたパス>/<id>.jsonl`
に残しており、その各行に実際の使用量が入っている。対話モードでは出力を横取りできない
（横取りするとTUIが壊れる）ので、終わったあとにこの記録から読む。

**プランの残量そのものは取れない。** ここで分かるのは「実際にいくつ使ったか」だけ。
残りいくつ使えるかはセッション内の `/usage` で見ること。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

PROJECTS_ROOT = Path.home() / ".claude" / "projects"


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    messages: int = 0

    @property
    def fresh(self) -> int:
        """新しく処理されたトークン。比較の基準にはこれを使う。

        キャッシュ読み込みを含めない。Claude Codeは同じ文脈を毎ターン読み直すため、
        キャッシュ読み込みは会話が長引くだけで際限なく膨らみ、しかも大幅に割安に扱われる。
        これを混ぜると「何をどれだけやったか」の指標として役に立たなくなる。
        """
        return self.input_tokens + self.output_tokens + self.cache_write_tokens

    @property
    def total(self) -> int:
        """キャッシュ読み込みも含めた、実際に処理された総量。"""
        return self.fresh + self.cache_read_tokens

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.messages + other.messages,
        )

    def format(self) -> str:
        return (
            f"{_compact(self.fresh)} トークン"
            f"（出力 {_compact(self.output_tokens)}"
            f" / 入力 {_compact(self.input_tokens + self.cache_write_tokens)}"
            f" / {self.messages}往復）"
            f" ＋ キャッシュ読み {_compact(self.cache_read_tokens)}"
        )


def compact(value: int) -> str:
    """桁数を読みやすく丸める。"""
    return _compact(value)


def _compact(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def find_transcript(work_dir: Path, *, since: float) -> Path | None:
    """指定ディレクトリで動いたセッションの記録のうち、`since` 以降に更新された最新のもの。

    ディレクトリ名のエンコード規則に依存すると仕様変更で壊れるため、
    記録の中の `cwd` を照合して特定する。
    """
    if not PROJECTS_ROOT.is_dir():
        return None

    target = str(work_dir).rstrip("\\/").lower()
    best: tuple[float, Path] | None = None

    for path in PROJECTS_ROOT.glob("*/*.jsonl"):
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        if modified < since:
            continue
        if best and modified <= best[0]:
            continue
        if _transcript_cwd(path).rstrip("\\/").lower() != target:
            continue
        best = (modified, path)

    return best[1] if best else None


def _transcript_cwd(path: Path) -> str:
    """記録の先頭付近から作業ディレクトリを読む。全文は読まない。"""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for _ in range(20):
                line = handle.readline()
                if not line:
                    break
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(entry, dict) and entry.get("cwd"):
                    return str(entry["cwd"])
    except OSError:
        pass
    return ""


def read(path: Path) -> Usage:
    """セッション記録1つ分の使用量を合計する。"""
    total = Usage()
    seen: set[str] = set()
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict):
                    continue
                message = entry.get("message")
                if not isinstance(message, dict):
                    continue
                stats = message.get("usage")
                if not isinstance(stats, dict):
                    continue
                # 同じメッセージが複数回書かれることがあるので重複を除く。
                key = str(message.get("id") or entry.get("uuid") or "")
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                total = total + Usage(
                    int(stats.get("input_tokens") or 0),
                    int(stats.get("output_tokens") or 0),
                    int(stats.get("cache_creation_input_tokens") or 0),
                    int(stats.get("cache_read_input_tokens") or 0),
                    1,
                )
    except OSError:
        pass
    return total


def measure(work_dir: Path, *, since: float) -> Usage | None:
    """`since` 以降にそのディレクトリで動いたセッションの使用量。見つからなければ None。"""
    path = find_transcript(work_dir, since=since)
    return read(path) if path else None
