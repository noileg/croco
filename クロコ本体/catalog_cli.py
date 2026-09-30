"""資料管理DB(catalog_db.py)を人・AIが後から使うためのCLI。

フック(hooks/catalog.py)が機械的に登録するのはpath/project/updated_atだけ。
stage/status/lore や relations は判断が要るため、こちらから明示的に埋める。

使用例:
  python catalog_cli.py values stage                    # 既存のstage値一覧（使い回すため先に見る）
  python catalog_cli.py set "C:\\path\\to\\file.md" stage 作業中
  python catalog_cli.py relate "A.md" derived_from "B.md"        # weight省略時は既定1
  python catalog_cli.py relate "A.md" derived_from "B.md" --weight 3  # 本体等、強い関係
  python catalog_cli.py show "C:\\path\\to\\file.md"
  python catalog_cli.py index                            # README.mdのあるフォルダ+loreの索引(新規フォルダ前の関連確認・横断検索の入口)
  python catalog_cli.py move "旧パス" "新パス"          # 実移動＋台帳付け替え＋該当すればgit commit
  python catalog_cli.py sweep                            # 台帳上で消えているpathを報告
  python catalog_cli.py scan "<フォルダ>"                # そのフォルダ配下で台帳に未登録のファイルを報告
  python catalog_cli.py backup "<出力先.db>"             # catalog.db自体のバックアップ
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import catalog_db


def cmd_values(args: argparse.Namespace) -> None:
    if args.column == "relation_type":
        values = catalog_db.distinct_relation_types()
    else:
        values = catalog_db.distinct_values(args.column)
    if not values:
        print("(まだ値がありません)")
        return
    for v in values:
        print(v)


def cmd_set(args: argparse.Namespace) -> None:
    ok = catalog_db.set_field(args.path, args.column, args.value)
    if not ok:
        print(
            f"警告: {args.path} はまだ台帳に無い（フックで登録される前）ため設定できませんでした。",
            file=sys.stderr,
        )
        sys.exit(1)
    print(f"OK: {args.path} の {args.column} を「{args.value}」に設定しました。")


def cmd_relate(args: argparse.Namespace) -> None:
    catalog_db.add_relation(
        args.from_id,
        args.relation_type,
        args.to_id,
        datetime.now().isoformat(timespec="seconds"),
        weight=args.weight,
    )
    print(f"OK: {args.from_id} --[{args.relation_type} w={args.weight}]--> {args.to_id}")


def cmd_backup(args: argparse.Namespace) -> None:
    """catalog.db自体のバックアップ。SQLiteの公式バックアップAPIを使う

    (単純にファイルをコピーすると、他プロセスが書き込み中に触った場合
    壊れた状態のコピーになりうるため。croco無人実行中に取っても安全)。
    """
    dest = Path(args.dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with catalog_db.connect() as src_conn:
        dest_conn = sqlite3.connect(dest)
        with dest_conn:
            src_conn.backup(dest_conn)
        dest_conn.close()
    print(f"OK: {dest} にバックアップしました。")


def cmd_show(args: argparse.Namespace) -> None:
    item = catalog_db.get_item(args.path)
    if item is None:
        print("(台帳に登録されていません)")
    else:
        for key in item.keys():
            print(f"{key}: {item[key]}")
    relations = catalog_db.get_relations(args.path)
    if relations:
        print("\n関係:")
        for r in relations:
            arrow = "→" if r["from_id"] == args.path else "←"
            other = r["to_id"] if r["from_id"] == args.path else r["from_id"]
            print(f"  {arrow} [{r['relation_type']} w={r['weight']}] {other}  ({r['created_at']})")


def cmd_index(args: argparse.Namespace) -> None:
    """README.mdを持つフォルダの名前とそのloreをコンパクトに一覧表示する。

    新規フォルダを作る前に既存プロジェクトとの関連を確認するための索引
    （2026-09-17、本人の設計）。loreは`set <README.mdの絶対パス> lore "<要約>"`で埋める。
    フォルダ横断で探す場所を絞る用途にも使うため、クロコ管轄プロジェクトの外や
    サブフォルダも出す。新規フォルダ作成前の確認で主に見るクロコ管轄プロジェクト直下は
    名前だけで先にまとめ、それ以外は絶対パスで後ろに続ける。
    """
    top: list[tuple[str, str | None]] = []
    others: list[tuple[str, str | None]] = []
    for folder, lore in catalog_db.readme_folder_index():
        if catalog_db.is_project_readme(str(Path(folder) / "README.md")):
            top.append((Path(folder).name, lore))
        else:
            others.append((folder, lore))
    if not top and not others:
        print("(README.mdのあるフォルダが台帳にありません)")
        return
    print("クロコ管轄プロジェクト直下:")
    for name, lore in top:
        print(f"- {name}: {lore if lore else '(lore未設定)'}")
    if others:
        print("\nその他（絶対パス）:")
        for folder, lore in others:
            print(f"- {folder}: {lore if lore else '(lore未設定)'}")


def cmd_scan(args: argparse.Namespace) -> None:
    """指定フォルダ配下で、台帳にまだ無いファイルを報告する(読み取り専用)。

    sweepの逆方向(台帳にあるのにディスクに無い→sweep、ディスクにあるのに
    台帳に無い→こちら)。croco/AIが作業フォルダに入ったときに、自分が今回
    触っていないファイルも含めて未登録が無いか確認する用途を想定
    (2026-08-19、本人指摘：「フォルダ内に未登録があればそれも」)。
    `.git`配下(auto_commit.pyが作るフォルダ単位リポジトリの中身)は
    登録対象ではないので除外する。どれを登録すべきか(一時ファイルは除く等)の
    判断はしない、一覧を出すだけ。
    """
    folder = Path(args.folder).resolve()
    if not folder.is_dir():
        print(f"エラー: フォルダではありません: {folder}", file=sys.stderr)
        sys.exit(1)
    registered = set(catalog_db.all_paths())
    unregistered = sorted(
        str(p) for p in folder.rglob("*")
        if p.is_file() and ".git" not in p.parts and str(p) not in registered
    )
    if not unregistered:
        print("(未登録のファイルはありません)")
        return
    print(f"{len(unregistered)}件、台帳に未登録です:")
    for p in unregistered:
        print(f"  {p}")


def cmd_sweep(args: argparse.Namespace) -> None:
    """台帳上のpathが実在するか全件チェックする(読み取り専用、自動修正はしない)。

    セッションの外(Explorer等)で移動・削除されたファイルは、そもそもフックが
    発火しないので自動検知できない。ここで機械的に「消えている」ことだけ報告し、
    直す(moveする/削除扱いにする)かどうかの判断は人・AIに委ねる。
    """
    missing = [p for p in catalog_db.all_paths() if not Path(p).exists()]
    if not missing:
        print("(台帳上のパスは全て実在します)")
        return
    print(f"{len(missing)}件、台帳上のパスが実在しません(移動・削除された可能性):")
    for p in missing:
        print(f"  {p}")


def _top_level_project_folder(path: Path) -> Path | None:
    try:
        rel = path.relative_to(catalog_db.PROJECTS_ROOT)
    except ValueError:
        return None
    if not rel.parts:
        return None
    return catalog_db.PROJECTS_ROOT / rel.parts[0]


def _git_commit_folder(folder: Path, message: str) -> None:
    """クロコ管轄プロジェクト配下のトップレベルフォルダにcommitする。

    `クロコ管轄プロジェクト/.claude/hooks/auto_commit.py`と同じ「1トップレベル
    フォルダ=1リポジトリ」方針に合わせる(あちらの関数は private 前提で
    import せず、同じロジックをここに複製している)。
    **メインのクロコ関係/クロコ本体リポジトリには絶対に使わないこと**
    (gitのコミットは明示的に頼まれた時だけ、という方針があるため)。
    """
    if not (folder / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=folder, check=False)
    subprocess.run(["git", "add", "-A"], cwd=folder, check=False)
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=folder,
        capture_output=True, text=True, encoding="utf-8", check=False,
    )
    if status.stdout.strip():
        subprocess.run(["git", "commit", "-q", "-m", message], cwd=folder, check=False)


def cmd_move(args: argparse.Namespace) -> None:
    """実ファイルの移動＋台帳の付け替え＋(クロコ管轄プロジェクト配下なら)git commit。

    クロコ関係全体に対応するが、gitの自動commitはクロコ管轄プロジェクト配下の
    トップレベルフォルダに対してのみ行う。クロコ本体側のメインリポジトリは
    このコマンドからは一切触らない。
    """
    old = Path(args.old).resolve()
    new = Path(args.new).resolve()

    if not old.exists():
        print(f"エラー: 移動元が存在しません: {old}", file=sys.stderr)
        sys.exit(1)
    if new.exists():
        print(f"エラー: 移動先が既に存在します(上書きはしない): {new}", file=sys.stderr)
        sys.exit(1)

    new.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old), str(new))

    in_catalog = catalog_db.move_item(str(old), str(new))

    touched_folders = {
        f for f in (_top_level_project_folder(old), _top_level_project_folder(new))
        if f is not None and f.is_dir()
    }
    for folder in touched_folders:
        _git_commit_folder(folder, f"[move] {old.name} -> {new.relative_to(folder.parent)}")

    detail = []
    if in_catalog:
        detail.append("台帳のpathを更新, relation(moved_from)を記録")
    if touched_folders:
        detail.append(f"git commit: {', '.join(f.name for f in touched_folders)}")
    suffix = f"（{', '.join(detail)}）" if detail else ""
    print(f"OK: {old} -> {new}{suffix}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_values = sub.add_parser("values", help="列の既存値一覧（stage/status/kind/project/relation_type）")
    p_values.add_argument("column", choices=["stage", "status", "kind", "project", "relation_type"])
    p_values.set_defaults(func=cmd_values)

    p_set = sub.add_parser("set", help="stage/status/kind/lore/projectを設定")
    p_set.add_argument("path")
    p_set.add_argument("column", choices=["stage", "status", "kind", "lore", "project"])
    p_set.add_argument("value")
    p_set.set_defaults(func=cmd_set)

    p_relate = sub.add_parser("relate", help="relationsに1件追加")
    p_relate.add_argument("from_id")
    p_relate.add_argument("relation_type")
    p_relate.add_argument("to_id")
    p_relate.add_argument(
        "--weight", type=float, default=1,
        help="関係の重要度。既定1、本体そのものを指す等の強い関係だけ増やす。"
             "複数箇所を等分に引用する等の場合は小数も可（例: 1.5）",
    )
    p_relate.set_defaults(func=cmd_relate)

    p_show = sub.add_parser("show", help="1件のitemとその関係を表示")
    p_show.add_argument("path")
    p_show.set_defaults(func=cmd_show)

    p_move = sub.add_parser("move", help="ファイル移動＋台帳の付け替え＋(該当すれば)git commit")
    p_move.add_argument("old")
    p_move.add_argument("new")
    p_move.set_defaults(func=cmd_move)

    p_sweep = sub.add_parser("sweep", help="台帳上のpathで実在しないものを報告(読み取り専用)")
    p_sweep.set_defaults(func=cmd_sweep)

    p_index = sub.add_parser("index", help="README.mdのあるフォルダとloreの一覧(新規フォルダ作成前の関連確認・横断検索の入口)")
    p_index.set_defaults(func=cmd_index)

    p_scan = sub.add_parser("scan", help="指定フォルダ配下で台帳に未登録のファイルを報告(読み取り専用)")
    p_scan.add_argument("folder")
    p_scan.set_defaults(func=cmd_scan)

    p_backup = sub.add_parser("backup", help="catalog.db自体をバックアップ")
    p_backup.add_argument("dest")
    p_backup.set_defaults(func=cmd_backup)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
