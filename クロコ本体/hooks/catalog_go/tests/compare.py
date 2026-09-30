"""Python版(hooks/catalog.py)とGo版(catalog_hook.exe)に同じstdinを与え、
stdout・DBの中身・file_watchのstate・Read通知の記録が一致するか確認する。

本番の catalog.db / ~/.claude には一切触れない。スクラッチ配下に「クロコ本体」相当の
フォルダを作り、Python版はそこへ複製したcatalog.py/catalog_db.pyを、Go版は
CATALOG_BASE_DIRでそのフォルダを指して動かす（パスが両実装で同じになるので出力を直接比較できる）。
USERPROFILEも差し替えるので quota_cache.json・Read通知の記録もスクラッチ側になる。

使い方: PYTHONUTF8=1 python compare.py <スクラッチ作業フォルダ>
"""
from __future__ import annotations

import gc
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOKS = HERE.parent.parent  # クロコ本体/hooks
BODY = HOOKS.parent  # クロコ本体
EXE = HOOKS / "catalog_hook.exe"

work = Path(sys.argv[1]).resolve()
base = work / "クロコ本体"
projects = work / "クロコ管轄プロジェクト"
home = work / "home"


def reset() -> None:
    if work.exists():
        # catalog_dbは`with connect()`で接続を閉じない（commitするだけ）ので、
        # 前回シナリオのseedで開いた接続が残っている。GCで閉じてから消す。
        gc.collect()
        shutil.rmtree(work)
    (base / "hooks").mkdir(parents=True)
    shutil.copy(BODY / "catalog_db.py", base / "catalog_db.py")
    shutil.copy(HOOKS / "catalog.py", base / "hooks" / "catalog.py")
    (home / ".claude" / "hooks").mkdir(parents=True)
    # 各シナリオの終端で作り直すので、ここでは共通のファイル群だけ用意する
    p = projects / "案A"
    p.mkdir(parents=True)
    (p / "README.md").write_text("案Aの説明\n", encoding="utf-8")
    (p / "old.py").write_text("print('old')\n", encoding="utf-8")
    (p / "dep.md").write_text("依存先の中身 <tag> & 日本語\n2行目\n", encoding="utf-8", newline="")
    # CRLFの扱いだけは意図的に実装間で違う（末尾の専用確認で見る）
    (p / "crlf.md").write_text("CRLF\r\n行\r\n", encoding="utf-8", newline="")
    (p / "src.py").write_text("# src\n", encoding="utf-8")
    (p / "同名.py").write_text("x\n", encoding="utf-8")
    (projects / "案B").mkdir()
    (projects / "案B" / "同名.py").write_text("y\n", encoding="utf-8")
    (work / "outside.txt").write_text("外\n", encoding="utf-8")


def seed_db() -> None:
    """両実装が同じDBから始まるよう、Python版のcatalog_dbで下地を作る。"""
    sys.path.insert(0, str(base))
    import importlib

    import catalog_db  # noqa: E402

    importlib.reload(catalog_db)
    ts = "2026-01-01T00:00:00"
    catalog_db.upsert_item(str(projects / "案B" / "同名.py"), "案B", ts)
    catalog_db.upsert_item(str(projects / "案A" / "old.py"), "案A", ts)
    catalog_db.upsert_item(str(projects / "案A" / "src.py"), "案A", ts)
    catalog_db.set_field(str(projects / "案A" / "src.py"), "lore", "src本体")
    catalog_db.add_relation(str(projects / "案A" / "src.py"), "cites", str(projects / "案A" / "dep.md"), ts)
    catalog_db.upsert_item(str(projects / "案A" / "README.md"), "案A", ts)
    sys.path.pop(0)
    sys.modules.pop("catalog_db", None)


def run(impl: str, tool: str, path: Path | str, session: str = "s1", raw: str | None = None) -> str:
    payload = raw if raw is not None else json.dumps(
        {"session_id": session, "tool_name": tool, "tool_input": {"file_path": str(path)}}, ensure_ascii=False)
    env = dict(os.environ, USERPROFILE=str(home), HOME=str(home), PYTHONUTF8="1",
               FILE_WATCH_STATE_DIR="", CATALOG_BASE_DIR=str(base))
    env.pop("FILE_WATCH_STATE_DIR")
    if impl == "py":
        cmd = [sys.executable, "-X", "utf8", str(base / "hooks" / "catalog.py")]
    else:
        cmd = [str(EXE)]
    r = subprocess.run(cmd, input=payload.encode("utf-8"), capture_output=True, env=env, timeout=30)
    return r.stdout.decode("utf-8", "replace")


def write_quota(seven_day: float | None) -> None:
    q = home / ".claude" / "hooks" / "quota_cache.json"
    if seven_day is None:
        q.unlink(missing_ok=True)
        return
    q.write_text(json.dumps({"five_hour_used": 1, "seven_day_used": seven_day,
                             "seven_day_resets_at": time.time() + 86400, "fetched_at": time.time()}))


def scenarios(impl: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    def step(name, *a, **k):
        out.append((name, run(impl, *a, **k)))

    A, B = projects / "案A", projects / "案B"
    write_quota(10)
    step("新規Write(案A)", "Write", A / "new.py")
    step("新規Write(README)", "Write", projects / "案C" / "README.md")
    step("新規Write(同名あり)", "Write", A / "同名.py")
    step("再Write(既存)", "Write", A / "new.py")
    step("Edit(既存)", "Edit", A / "old.py")
    step("Read(lore無)", "Read", A / "old.py")
    step("Read(同セッション2回目)", "Read", A / "old.py")
    step("Read(別セッション)", "Read", A / "old.py", session="s2")
    step("Read(lore有・relation有)", "Read", A / "src.py")
    step("Read(管轄外・新規登録)", "Read", work / "outside.txt")
    step("Read(README lore無)", "Read", A / "README.md", session="s3")
    step("Read(shadow: 1回目)", "Read", A / "src.py", session="s4")
    step("NotebookEdit", "NotebookEdit", A / "nb.ipynb")
    step("対象外ツール", "Bash", A / "old.py")
    step("壊れたJSON", "Read", "", raw="{not json")
    step("file_path空", "Read", "")
    write_quota(91)
    step("週次91%でRead", "Read", A / "src2_new.py", session="s5")
    step("週次91%でWrite新規", "Write", A / "quota_write.py", session="s5")
    write_quota(87.9)
    step("週次87.9%でRead", "Read", A / "src2_new.py", session="s6")
    write_quota(None)
    step("キャッシュ無しでRead", "Read", A / "src3_new.py", session="s7")
    return out


def snapshot_state() -> dict:
    db = sqlite3.connect(base / "catalog.db")
    items = sorted((r[0], r[1], r[2], r[3], r[4], r[5]) for r in db.execute(
        "select path, project, stage, status, kind, lore from items"))
    times = {r[0]: r[1] for r in db.execute("select path, updated_at from items")}
    rels = sorted(db.execute("select from_id, relation_type, to_id, weight from relations").fetchall())
    db.close()
    state = {}
    sd = home / ".claude" / "tmp" / "file_watch"
    if sd.exists():
        for f in sorted(sd.glob("*.json")):
            state[f.name] = json.loads(f.read_text(encoding="utf-8"))
    notice = {}
    nd = home / ".claude" / "tmp" / "catalog_read_notice"
    if nd.exists():
        for f in sorted(nd.glob("*.json")):
            notice[f.name] = json.loads(f.read_text(encoding="utf-8"))
    log = base / "logs" / "catalog_hook_errors.log"
    return {"items": items, "rels": rels, "file_watch": state, "read_notice": notice,
            "log_exists": log.exists(), "updated_paths_touched": sorted(
                p for p, t in times.items() if not t.startswith("2026-01-01"))}


results = {}
for impl in ("py", "go"):
    reset()
    seed_db()
    outs = scenarios(impl)
    # ハッシュ・内容が同じか見るためstateは丸ごと比較する
    results[impl] = (outs, snapshot_state())

ok = True
for (name, py_out), (_, go_out) in zip(results["py"][0], results["go"][0]):
    # JSON化した上で比較（エスケープ表記の違いを吸収）
    def norm(s: str):
        return json.loads(s) if s.strip() else None
    same = norm(py_out) == norm(go_out)
    ok &= same
    print(("OK  " if same else "NG  ") + name + ("" if same else f"\n  py: {py_out!r}\n  go: {go_out!r}"))

for key in results["py"][1]:
    same = results["py"][1][key] == results["go"][1][key]
    ok &= same
    print(("OK  " if same else "NG  ") + "state:" + key)
    if not same:
        print("  py:", json.dumps(results["py"][1][key], ensure_ascii=False)[:800])
        print("  go:", json.dumps(results["go"][1][key], ensure_ascii=False)[:800])

# CRLFの依存先：Python版のread_textは改行を\nに変換して保存するが、状態ファイルを読む
# file_watch_check(Go)は生のバイト列(ReadTextUTF8Replace)を比較する。よってcheckと一致するのはGo版の
# 保存方法（改行を変換しない）。Python版だと変更していないファイルが「変わった」と誤検知される。
import hashlib  # noqa: E402

reset()
seed_db()
crlf = projects / "案A" / "crlf.md"
sys.path.insert(0, str(base))
import catalog_db  # noqa: E402

catalog_db.add_relation(str(projects / "案A" / "src.py"), "cites", str(crlf), "2026-01-01T00:00:00")
gc.collect()
sys.path.pop(0)
sys.modules.pop("catalog_db", None)
write_quota(10)
run("go", "Read", projects / "案A" / "src.py", session="crlf")
state = json.loads((home / ".claude" / "tmp" / "file_watch" / "crlf.json").read_text(encoding="utf-8"))
snap = state[str(crlf)]
want = hashlib.sha256(crlf.read_bytes()).hexdigest()
same = snap["hash"] == want and snap["content"] == "CRLF\r\n行\r\n"
ok &= same
print(("OK  " if same else "NG  ") + "CRLF依存先: Go版の保存はcheck側(生バイト列のハッシュ)と一致")

print("ALL OK" if ok else "FAILED")
sys.exit(0 if ok else 1)
