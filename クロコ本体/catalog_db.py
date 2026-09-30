"""資料管理DB（items / relations）の共通アクセス層。

設計の経緯・決めたこと・未決定点は `記録/検討中_資料管理DB化.md` を見ること。

- `items`：ファイル台帳。pathをキーに、project（クロコ管轄プロジェクト直下の
  トップレベルフォルダ名。自動推定）と、stage（進み具合：素材/作業中/確定等）・
  status・kind（中身の種類：一次ソース/二次ソース/資料/プログラム/文書/
  スケジュール等）・lore（AIや本人が後から判断して埋める。フックからは
  自動で埋めない）を持つ。stageとkindは別の軸（例：「作業中のプログラム」と
  「確定した資料」を区別できる。2026-08-19、本人指摘で分離）。
- `relations`：時刻つきの4つ組（s, p, o, t）＋weight。from_id/to_id はitemsの
  pathを想定しているが、将来ファイル以外（決定事項・概念等）も指せるよう、
  外部キー制約はかけていない。relation_typeは自由記述（閉じた語彙にしない）。
  weightは関係の重要度（確信度でも頻度でもない）。既定1、本人・AIが強いと
  判断したときだけ増やす手動運用（2026-08-20、本人指摘：「基本は1、強いと
  思ったら増やす」。索引に載っているだけの関係より、本体そのものを指す関係を
  上に出したい、という用途から）。段階の目安は決めていない（自由な数値）。
  小数も可（例：1件を複数箇所へ等分に引用する場合の1.5等、2026-08-20）。
  列の宣言型はINTEGERのままだが、SQLiteの型親和性により小数もREALとして
  そのまま保持される（実測で確認済み。列自体のマイグレーションは不要）。

stage/status/project の値は固定の列挙にしない。「既存にあれば使い回す」運用を
支えるため、`distinct_values()` で既存値の一覧を引けるようにしてある。

DBファイル自体は`クロコ本体/catalog.db`に置き、`.gitignore`で除外している
（頻繁に書き換わる実行時データであり、ソースコードではないため。
`クロコ本体/logs/`と同じ扱い）。

置き場所を`クロコ管轄プロジェクト/`配下ではなく`クロコ本体/`にしているのは、
`クロコ管轄プロジェクト/.claude/hooks/auto_commit.py`がトップレベルフォルダ単位で
機械的にgitリポジトリ化するため（2026-08-19、本人指摘）。もしこのDB関連ファイルを
`クロコ管轄プロジェクト/.claude/`配下に置いてしまうと、将来誰かがそこを編集した
拍子に`.claude`自体が「プロジェクトのトップレベルフォルダ」と誤認識されて
git repo化されるおそれがある。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "catalog.db"
PROJECTS_ROOT = Path(__file__).resolve().parent.parent / "クロコ管轄プロジェクト"

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    path TEXT PRIMARY KEY,
    project TEXT,
    stage TEXT,
    status TEXT,
    kind TEXT,
    lore TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS relations (
    from_id TEXT NOT NULL,
    relation_type TEXT NOT NULL,
    to_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    weight INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_relations_from ON relations(from_id);
CREATE INDEX IF NOT EXISTS idx_relations_to ON relations(to_id);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    # WALモード(2026-08-21)。書き込み中でも読み取りをブロックしにくくする保険。
    # 一度設定すればDBファイル側に記憶されるが、connectのたびに確認しても
    # 既にWALなら軽いPRAGMA参照で終わるだけなので毎回呼んでよい。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    _migrate_add_weight_column(conn)
    return conn


def _migrate_add_weight_column(conn: sqlite3.Connection) -> None:
    """2026-08-20以前に作られたcatalog.dbにweight列が無ければ足す。

    `CREATE TABLE IF NOT EXISTS`は既存テーブルの列追加までは面倒を見ないため、
    既存データが入ったDBファイル向けに別途マイグレーションする。既存行は
    SCHEMA側の`DEFAULT 1`がそのまま効いて1で埋まる（「基本は1」の運用と一致）。
    """
    columns = {row[1] for row in conn.execute("PRAGMA table_info(relations)")}
    if "weight" not in columns:
        conn.execute("ALTER TABLE relations ADD COLUMN weight INTEGER NOT NULL DEFAULT 1")


def upsert_item(path: str, project: str | None, updated_at: str) -> None:
    """台帳への機械的な登録・更新。stage/status/loreは触らない（判断が要るため）。"""
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO items (path, project, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                project = excluded.project,
                updated_at = excluded.updated_at
            """,
            (path, project, updated_at),
        )


def project_for_path(path: str) -> str | None:
    """クロコ管轄プロジェクト直下のトップレベルフォルダ名を推定する。対象外ならNone。"""
    try:
        rel = Path(path).resolve().relative_to(PROJECTS_ROOT)
    except (OSError, ValueError):
        return None
    return rel.parts[0] if rel.parts else None


def move_item(old_path: str, new_path: str) -> bool:
    """pathの付け替え。itemsの行とrelationsの参照(from_id/to_id)を両方直す。

    projectも新しいpathから引き直す(移動でトップレベルフォルダが変わりうるため)。
    old_pathの行が無ければ何もせずFalseを返す。new_path側に既に別の行があれば
    （移動先を上書きした場合）そちらは消して付け替えを優先する。

    「どこからどこへ移動したか」は判断が要らない機械的な事実なので、
    relation_type="moved_from" として自動でrelationsに1件残す
    （2026-08-19、本人指摘：「リレーションも自動にしたい」）。既存のrelationsの
    from_id/to_idはold_pathからnew_pathへ書き換えた後に追加するので、この
    moved_fromの行だけはold_pathを指したまま残る＝移動前の識別子として履歴に残る。
    """
    with connect() as conn:
        if conn.execute("SELECT 1 FROM items WHERE path = ?", (old_path,)).fetchone() is None:
            return False
        conn.execute("DELETE FROM items WHERE path = ? AND path != ?", (new_path, old_path))
        conn.execute(
            "UPDATE items SET path = ?, project = ? WHERE path = ?",
            (new_path, project_for_path(new_path), old_path),
        )
        conn.execute("UPDATE relations SET from_id = ? WHERE from_id = ?", (new_path, old_path))
        conn.execute("UPDATE relations SET to_id = ? WHERE to_id = ?", (new_path, old_path))
        conn.execute(
            "INSERT INTO relations (from_id, relation_type, to_id, created_at) VALUES (?, 'moved_from', ?, ?)",
            (new_path, old_path, datetime.now().isoformat(timespec="seconds")),
        )
        return True


def all_paths() -> list[str]:
    with connect() as conn:
        return [r[0] for r in conn.execute("SELECT path FROM items")]


_SETTABLE_COLUMNS = {"stage", "status", "kind", "lore", "project"}


def set_field(path: str, column: str, value: str) -> bool:
    """判断が要る列（stage/status/lore/project）を後から埋める・上書きする。

    対象の行が無ければ何もせずFalseを返す（先にupsert_itemで台帳に載っている前提）。
    """
    if column not in _SETTABLE_COLUMNS:
        raise ValueError(f"設定できない列: {column}")
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE items SET {column} = ? WHERE path = ?", (value, path)
        )
        return cur.rowcount > 0


def distinct_values(column: str) -> list[str]:
    """既存値の一覧。新規に値を作る前にここを見て、あれば使い回す運用を支える。"""
    if column not in _SETTABLE_COLUMNS:
        raise ValueError(f"対象外の列: {column}")
    with connect() as conn:
        rows = conn.execute(
            f"SELECT DISTINCT {column} FROM items WHERE {column} IS NOT NULL ORDER BY {column}"
        ).fetchall()
    return [r[0] for r in rows]


def distinct_relation_types() -> list[str]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT relation_type FROM relations ORDER BY relation_type"
        ).fetchall()
    return [r[0] for r in rows]


def add_relation(
    from_id: str, relation_type: str, to_id: str, created_at: str, weight: float = 1
) -> None:
    """点時刻つきの4つ組＋weightを1件追加する。対等な関係でも逆向きは自動で足さない

    （検索側で from/to 両方向を見る運用にしているため。逆向きも登録すると
    同じ関係が2行に分裂してメンテが増える）。weightは既定1、強いと判断した
    ときだけ呼び出し側で増やす。
    """
    with connect() as conn:
        conn.execute(
            "INSERT INTO relations (from_id, relation_type, to_id, created_at, weight) VALUES (?, ?, ?, ?, ?)",
            (from_id, relation_type, to_id, created_at, weight),
        )


def get_item(path: str) -> sqlite3.Row | None:
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM items WHERE path = ?", (path,)).fetchone()


def find_by_basename(path: str) -> list[str]:
    """同じファイル名（拡張子込み、パス除く）を持つ他のitemsを探す。

    新規ファイル作成時、複製・専用化（[[feedback_copy_dont_regenerate_long_files]]の
    ような運用）で生まれた既存の同名ファイルに気づかせるための検索。パス自体は除く。
    """
    name = Path(path).name
    with connect() as conn:
        rows = conn.execute(
            "SELECT path FROM items WHERE path != ? AND path LIKE '%' || ? ESCAPE '\\'",
            (path, name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")),
        ).fetchall()
    # LIKEの部分一致は末尾一致まで保証しないため、basenameが完全一致するものだけに絞る
    return [r[0] for r in rows if Path(r[0]).name == name]


def is_project_readme(path: str) -> bool:
    """クロコ管轄プロジェクト直下フォルダ自身のREADME.mdかどうか。

    `folder_lore_index()`が対象とする行と同じ条件（新規フォルダ検出フックとの
    重複判定を避けるため関数化、2026-09-19）。
    """
    try:
        rel = Path(path).resolve().relative_to(PROJECTS_ROOT)
    except (OSError, ValueError):
        return False
    return len(rel.parts) == 2 and rel.parts[1] == "README.md"


def folder_lore_index() -> list[tuple[str, str | None]]:
    """クロコ管轄プロジェクト直下のトップレベルフォルダ名と、そのREADME.mdのloreを列挙する。

    新規フォルダを作る前に「既存プロジェクトと関連しないか」をコンパクトに確認するための索引
    （2026-09-17、本人の設計）。フォルダ単位の意味は、そのフォルダのREADME.mdのloreに
    要約を1〜3文で置く運用で表す（列自体はitemsのlore列を流用、スキーマ変更は無い）。
    loreが未設定のフォルダも一覧に含める（埋め忘れが見える方が良いため）。
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT path, lore FROM items WHERE path LIKE ? ORDER BY path",
            (str(PROJECTS_ROOT) + "\\%\\README.md",),
        ).fetchall()
    result = []
    for path, lore in rows:
        if not is_project_readme(path):
            continue
        rel = Path(path).resolve().relative_to(PROJECTS_ROOT)
        result.append((rel.parts[0], lore))
    return result


def readme_folder_index() -> list[tuple[str, str | None]]:
    """README.mdを持つフォルダの絶対パスと、そのREADME.mdのloreを場所を問わず列挙する。

    フォルダ横断で探す場所を絞るための索引。フォルダの意味はREADME.mdのloreで表す運用の
    ため、README.mdを持たないフォルダ（環境を入れておくだけのフォルダ等）は対象にしない。
    README.mdの有無は台帳への登録で判定し、ファイルシステムは走査しない（走査すると
    node_modules等の他人のREADMEまで拾い、根をどこに置くかも決め打ちになるため）。
    その代わり、Claude Codeで一度も触れていないREADME.mdは載らない。
    登録済みでも実在しないものは除く（移動・削除済みの場所を探す先として示さないため）。
    loreが未設定のフォルダも含める（埋め忘れが見える方が良く、パスだけでも手がかりになるため）。
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT path, lore FROM items WHERE path LIKE '%README.md' ORDER BY path"
        ).fetchall()
    return [
        (str(Path(path).parent), lore)
        for path, lore in rows
        if Path(path).name == "README.md" and Path(path).exists()
    ]


def get_relations(node_id: str) -> list[sqlite3.Row]:
    """node_idがfrom/toどちらに立っていても、両方向まとめて返す。

    weight降順（強い関係を先に）→created_at昇順の順で並べる。索引経由のような
    弱い関係より本体そのものを指す強い関係を上に出したい、というweight導入の
    目的（2026-08-20）に合わせた並び。
    """
    with connect() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM relations WHERE from_id = ? OR to_id = ? ORDER BY weight DESC, created_at",
            (node_id, node_id),
        ).fetchall()
