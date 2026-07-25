# クロコ本体

Notion + Gemini + Claude Code による個人用ワークフロー自動化パイプライン。
設計の意図・経緯は `クロコ_仕様書_統合版.md` を参照（そちらが正、本書は使い方）。

## 動作の流れ

```
スマホ（Notion AIチャット）
    │  「未処理置き場のサブページにメモを作って」と指示
    ▼
Notion「未処理置き場」の子ページ          ← 存在すること自体が「未処理」の印
    │
    │  ▼ PC起動時に run_croco.py が走る
    │
    ├─ 捕捉フェーズ（croco/capture.py）
    │    Gemini が話題の切れ目で分割（要約はしない・逐語転記）
    │    → Inbox DB に登録
    │    → 成功したものだけ「処理済み置き場」へ移動
    │
    └─ 実装フェーズ（croco/dispatch.py）
         Inbox DB から1件選ぶ（「処理中」を「未処理」より優先）
         → claude -p で無人起動
         → クロコが croco_cli.py 経由で進捗を書き戻す
```

失敗したものは**何もせず放置する**のが基本方針。放置すれば次回のPC起動時に
自然に再試行されるため、専用のクラッシュ復旧処理を持たない。

## 必要なもの

- Python 3.10 以上（外部パッケージ不要。標準ライブラリのみで動く）
- Claude Code CLI（`claude` が PATH にあること）
- Notion Integration のトークン
- Gemini API キー

外部パッケージに依存させていないのは、PC起動時に走らせる都合上、
pip や仮想環境の状態で起動が失敗する余地を無くすため。

## セットアップ

### 1. 設定ファイルを置く

```
mkdir %USERPROFILE%\.croco
copy .env.example %USERPROFILE%\.croco\.env
```

`.env` を開き、`NOTION_TOKEN` と `GEMINI_API_KEY` を記入する。
プロジェクトフォルダの外に置くのは、平文の秘密情報をプロジェクト内に
持ち込まないため（仕様書2章-7）。

### 2. Notion側を用意する

1. https://www.notion.so/my-integrations で Integration を作成し、トークンを取得
2. Notion上に親ページを1つ作る（例：「クロコ」）
3. **その親ページだけ**を Integration に接続する
   （ワークスペース全体に接続しないこと。鍵が漏れた場合の被害範囲を絞るため）
4. 置き場ページとInbox DBを作る：

```
python setup_notion.py <親ページのURL>
```

表示された4行を `.env` に貼る。その後、案内に従って
Inbox DB にカレンダービュー（フィルタ：種別＝予定）を手動で追加する。

### 3. 疎通確認

```
python setup_notion.py --check
```

### 4. 自動起動を有効にする

```
powershell -ExecutionPolicy Bypass -File launcher\install_startup.ps1
```

止めるときは `-Remove` を付けて実行する。

**先に `.env` の `CROCO_CLAUDE_COMMAND` に `claude` のフルパスを設定しておくこと。**
`claude` は永続PATH（User/Machine）に入っていないことがあり、その場合
スタートアップから起動したプロセスからは見つからない。
捕捉フェーズだけ動いて実装フェーズが毎回失敗する、という気づきにくい壊れ方をする。

`launcher\croco.bat` を編集するときは **ASCIIのみで書くこと**。
cmd.exeはバッチを実行中のコードページで読み直すため、日本語を混ぜると
`chcp 65001` との組み合わせでパースが壊れ、成功時にも `pause` が走って
コンソールが居座る。日本語の出力はPython側に任せる。

## 使い方

```
python run_croco.py            # 通常（捕捉 → 実装）
python run_croco.py --capture  # 捕捉のみ
python run_croco.py --dispatch # 実装のみ
python run_croco.py --dry-run  # 書き込まず、何をするかだけ表示
```

ログは `logs/croco_YYYY-MM-DD.log` に日付ごとに残る。

### 最初の1回

いきなり無人で走らせず、まず `--dry-run` で分割結果を確認し、
次に `--capture` だけを実行してInbox DBの中身を目視することを勧める。
実装フェーズの初回も、横で見ていられるときに手動で `--dispatch` するのが安全。

## 安全対策

| 層 | 手段 |
|---|---|
| 分類ベース | 本人自身が対応すべきものは、捕捉時点で「要確認」にして自動キューに入れない |
| 行為ベース | `croco_settings.json` の deny リスト（履歴破壊・広範囲削除・外部公開・鍵の読み取り等） |
| 判定ベース | `--permission-mode auto` の内蔵判定 |
| 範囲ベース | 作業ディレクトリを `クロコ管轄プロジェクト` 配下に限定（`--add-dir`） |
| 権限ベース | Notionトークンの接続先を必要なページ・DBだけに絞る |
| 回数ベース | 同一アイテムの試行回数が上限を超えたら「要確認」に落とす |

一番上の「分類ベース」が実務上いちばん効く。自己推薦書・志望理由書のような
**AIに書かせてはいけない文書**や、書類の取り寄せのような**現実世界の行動**は、
Geminiが捕捉の段階で判定して「要確認」に隔離するので、そもそも着手対象に選ばれない。
実装フェーズのプロンプトにも同じ線引きを書いてあるが、そちらは指示であって
保証ではないので、分類の段階で弾くことを主の防御としている。

deny リストはどのパーミッションモードでも効く最後の砦なので、
`croco_settings.json` には「絶対にやってほしくないこと」だけを書く。

**設定ファイルを編集したら必ず検証すること。** `-p`（非対話）モードでは
検証に失敗した設定ファイルは*無言で無視される*ため、書き間違えると
deny リストごと効かなくなる。検証方法：

```
mkdir tmpcheck\.claude
copy croco_settings.json tmpcheck\.claude\settings.json
cd tmpcheck && claude doctor
```

`Invalid settings` が出なければ通っている。

### 止め方

実行中に異常に気づいたら、コンソールを閉じるか
タスクマネージャーで `claude` / `python` を終了させる。
途中で止めてもステータスは「処理中」のまま残るので、次回起動時に再開される。

## ファイル構成

```
クロコ本体/
  run_croco.py          エントリポイント（起動時に走る本体）
  croco_cli.py          クロコ自身が進捗を書き戻すためのCLI
  setup_notion.py       Notion側の実体を作る初期セットアップ
  croco_settings.json   無人実行時のパーミッション設定
  .env.example          設定ファイルの雛形
  croco/
    config.py           .env の読み込みと設定値
    httpjson.py         JSON over HTTP（リトライ・起動時のネットワーク待ち付き）
    lock.py             二重起動の防止
    notion.py           Notion API
    gemini.py           Gemini API（分割・逐語転記の指示を含む）
    inbox.py            Inbox DB のスキーマと読み書き
    capture.py          捕捉フェーズ
    dispatch.py         実装フェーズ
    log.py              実行ログ
  launcher/
    croco.bat           起動用ランチャ
    install_startup.ps1 スタートアップへの登録・解除
  logs/                 実行ログ（自動生成）
```
