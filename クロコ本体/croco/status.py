"""管轄プロジェクトの現状を、Notionの1ページに書き出す。

スマホから「クロコが今どこまで作ったか」を見るための窓。これまで
Notion→PC の向き（捕捉・実装）しか無く、逆向きが1本も無かった。

**ミラーではない。** 全ファイルの中身を写すと、Notion側に第二の原本ができて
どちらが最新か分からなくなる。ここに出すのは

  - 全ファイルのツリー（名前・大きさ・更新日時）
  - 各プロジェクトの README.md の中身（「これは何で、今どうなっているか」）

まで。それ以外の本文はPCで読む前提にしている。管轄プロジェクトの .md は
合計6万字あり、毎起動でまるごと貼り直す先としてNotionは向いていない。

**`下書き/` 配下の本文は写さない。** 本人が自分の名義で書いている原稿で、
ローカルにしか置かない線引きをしているもの（gitからも外してある）。
存在と更新日時が見えると進み具合が分かるのでツリーには出すが、中身は出さない。

**中身が変わっていなければ書き直さない。** 毎起動で貼り直すと Notion 側の
「最終更新」が毎回今になり、「いつ何か動いたか」がそこから読めなくなる。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path

from . import log
from . import notion as nt
from .config import Config

# 中身まで写すファイル名。増やすならここ。
CONTENT_FILES = ("README.md",)

# 中身を写さないフォルダ。パスのどの階層に現れても対象。
PRIVATE_DIRS = ("下書き",)

# ツリーにも出さないもの。
SKIP_NAMES = ("__pycache__", "node_modules")

# 差分判定のために本文へ埋め込む印。
MARKER = "内容ID"


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f}KB"
    return f"{size / 1024 / 1024:.1f}MB"


def _stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")


def _hidden(relative: Path) -> bool:
    return any(
        part in SKIP_NAMES or part.startswith(".") for part in relative.parts
    )


def is_private(relative: Path) -> bool:
    """中身を写してはいけない場所か。"""
    return any(part in PRIVATE_DIRS for part in relative.parts)


def collect(root: Path) -> list[Path]:
    """root配下をパス順で列挙する（フォルダも含む）。"""
    return [
        path
        for path in sorted(root.rglob("*"), key=lambda p: p.as_posix())
        if not _hidden(path.relative_to(root))
    ]


def tree_text(root: Path, paths: list[Path]) -> str:
    """ツリーを組む。

    **桁を揃えない。** ファイル名に日本語が混ざるので、等幅で揃えたつもりの
    列は見る環境によってずれる（エディタの表で実際にやらかした）。
    大きさと更新日時は名前の後ろに括弧で添えるだけにして、揃え自体を捨てる。
    """
    lines = [f"{root.name}/"]
    for path in paths:
        parts = path.relative_to(root).parts
        indent = "  " * len(parts)
        if path.is_dir():
            lines.append(f"{indent}{parts[-1]}/")
        else:
            stat = path.stat()
            lines.append(
                f"{indent}{parts[-1]}"
                f"  ({human_size(stat.st_size)}, {_stamp(stat.st_mtime)})"
            )
    return "\n".join(lines)


def content_sections(root: Path, paths: list[Path]) -> list[tuple[str, str]]:
    """(見出し, 本文) の並び。中身まで写すファイルだけを拾う。"""
    sections: list[tuple[str, str]] = []
    for path in paths:
        if path.is_dir() or path.name not in CONTENT_FILES:
            continue
        relative = path.relative_to(root)
        if is_private(relative):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            log.warn(f"{relative} を読めませんでした: {exc}")
            continue
        sections.append((relative.as_posix(), text.strip()))
    return sections


def build(root: Path) -> tuple[str, list[tuple[str, str]], int]:
    """(ツリー, 中身の並び, ファイル数) を返す。"""
    paths = collect(root)
    files = [path for path in paths if path.is_file()]
    return tree_text(root, paths), content_sections(root, paths), len(files)


def signature(tree: str, sections: list[tuple[str, str]]) -> str:
    """中身が変わったかを判定する短い印。

    更新日時込みのツリーを含むので、ファイルを触っただけでも変わる。
    「中身は同じだが触った」を無視したいわけではない（触ったこと自体が
    進捗の合図になる）ので、これでよい。
    """
    joined = tree + "\n".join(f"{name}\n{text}" for name, text in sections)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:12]


def to_blocks(
    tree: str, sections: list[tuple[str, str]], *, file_count: int, digest: str
) -> list[dict]:
    """Notionのブロック列に変換する。

    段落を1行ずつ並べるとブロック数が数百になり、貼り直しのたびに
    その数だけAPIを叩くことになる。コードブロックは1つで最大20万字
    （rich_text 100要素 × 2000字）入るので、まとめて放り込む。
    ツリーは等幅で見たいので、そもそもコードブロックが正しい。
    """
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    head = (
        f"{stamp} 時点 / ファイル {file_count}件"
        f"（このページはクロコが自動で書き直します。{MARKER}: {digest}）"
    )
    blocks: list[dict] = [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": nt.rich_text(head)},
        }
    ]
    blocks.extend(nt.code_blocks(tree, language="plain text"))
    for name, text in sections:
        blocks.append(
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": nt.rich_text(name)},
            }
        )
        blocks.extend(nt.code_blocks(text, language="markdown"))
    return blocks


def current_digest(client: nt.Notion, page_id: str) -> str:
    """ページに書いてある印を読む。読めなければ空文字列。"""
    try:
        for block in client.get_block_children(page_id):
            if block.get("type") != "paragraph":
                continue
            text = nt.plain_text_of(block.get("paragraph"))
            marker = f"{MARKER}: "
            if marker in text:
                return text.split(marker, 1)[1].rstrip("）) ").strip()
    except Exception as exc:
        log.warn(f"現状ページの読み取りに失敗しました: {exc}")
    return ""


def push(config: Config) -> bool:
    """書き出す。書き直したら True、変化なし・条件未達なら False。"""
    root = config.projects_dir
    if not root.is_dir():
        log.warn(f"管轄プロジェクトが見つかりません: {root}")
        return False

    page_id = config.status_page_id
    if not page_id:
        log.log(
            "現状ページが未設定なので書き出しません。"
            "`python setup_notion.py --status-page` で作れます。"
        )
        return False

    tree, sections, file_count = build(root)
    digest = signature(tree, sections)

    client = nt.Notion(config.notion_token, config.notion_version)
    if current_digest(client, page_id) == digest:
        log.log("管轄プロジェクトに変化なし。現状ページはそのままにします。")
        return False

    blocks = to_blocks(tree, sections, file_count=file_count, digest=digest)
    client.replace_children(page_id, blocks)
    log.log(f"現状ページを更新しました（ファイル {file_count}件）。")
    return True
