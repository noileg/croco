# catalog_go

`hooks/catalog.py`（台帳登録フック）のGo移植。Read/Edit/Write のたびに走る高頻度フックなので、
`ノート監視フック書き直し`（file_watch系）と同じ理由で移した。設計の経緯・登録する項目・
通知の意味は `hooks/catalog.py` のdocstringと `記録/検討中_資料管理DB化.md` を参照
（挙動はPython版と同一。ここには移植に固有のことだけ書く）。

## 構成

- `main.go` : 入口。登録・shadow-tracking（file_watch連携）・通知の出し分け
- `db.go` : catalog.db（items / relations）へのアクセス。SQLiteは純Goの`modernc.org/sqlite`
  （cgo不要。標準ライブラリだけでは触れないため、外部依存はこれだけ）
- `notices.go` : 通知文（同名ファイル警告・新規フォルダのlore促し・新規ファイル/Read時のlore・relation促し）と週次クォータ判定
- `paths.go` : 「クロコ本体」「クロコ管轄プロジェクト」の位置。exeの2つ上を「クロコ本体」とみなす
  （`hooks/`直下に置く前提）。`CATALOG_BASE_DIR`で差し替え可能（テスト用）
- `tests/compare.py` : Python版とGo版に同じ入力を流し、stdout・DB・file_watchのstate・Read通知の記録を比較

## ビルドと配置

```
cd hooks\catalog_go
$env:CGO_ENABLED = "0"
go build -ldflags="-s -w" -o ..\catalog_hook.exe .
```

出力先は `hooks/catalog_hook.exe`（パス決定の前提）。`~/.claude/settings.json` のPostToolUse
`Edit|Write|NotebookEdit|Read` のcommandがこのexeを指している。

## テスト

```
PYTHONUTF8=1 python tests\compare.py <スクラッチ作業フォルダ>
```

本番の`catalog.db`・`~/.claude`には触れない（USERPROFILEとCATALOG_BASE_DIRを差し替える）。
`ALL OK`が出れば一致。

## Python版との意図的な違い

- CRLFの依存先ファイルのshadow-trackingで、Python版は`read_text`の改行変換（`\r\n`→`\n`）でハッシュを
  取っていた。状態ファイルを読む`file_watch_check.exe`は生のバイト列で比較するため、Python版だと
  変更していないCRLFファイルが「変わった」と誤検知されていた。Go版は変換しない（checkと一致）。
- 壊れたUTF-8の置換は、Pythonが1バイトずつU+FFFDにするのに対し、Goは連続する不正バイトをまとめて
  1個にする。file_watch側のGo実装（`ReadTextUTF8Replace`）と同じ挙動。
