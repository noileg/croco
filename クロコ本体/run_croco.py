"""クロコのエントリポイント。PC起動時にこれ1本が走る。

仕様書2章-6の「1本のコマンドにまとめる」方針に対応する。
処理は2フェーズ:

  1. 捕捉  : 未処理置き場 → Gemini で分割 → Inbox DB → 処理済み置き場
  2. 実装  : Inbox DB から1件選び、クロコ（Claude Code）を無人で起動

使い方:
    python run_croco.py              # 通常（捕捉 → 実装）
    python run_croco.py --capture    # 捕捉のみ
    python run_croco.py --dispatch   # 実装のみ
    python run_croco.py --dry-run    # 書き込みを行わず、何をするかだけ表示
"""

from __future__ import annotations

import sys

from croco import capture, dispatch, httpjson, log
from croco.config import Config, ConfigError, load_env
from croco.lock import AlreadyRunning, SingleInstance


def main(argv: list[str]) -> int:
    do_capture = "--dispatch" not in argv
    do_dispatch = "--capture" not in argv

    env = load_env()
    if "--dry-run" in argv:
        env["CROCO_DRY_RUN"] = "1"
    config = Config(env)

    log_path = log.setup(config.log_dir)
    log.log("=" * 60)
    log.log(f"クロコを開始します（ログ: {log_path}）")

    if config.dry_run:
        log.log("dry-run モードです。Notionへの書き込みとクロコの起動は行いません。")

    # PC起動直後は回線がまだ上がっていないことがある。
    # ここで待たないと、起動のたびに何もせず終わる無言の失敗になりうる。
    if not httpjson.wait_for_network():
        log.error("ネットワークに接続できません。今回は何もせず終了します。")
        return 1

    try:
        with SingleInstance(config.log_dir / "croco.lock"):
            if do_capture:
                try:
                    count = capture.run(config)
                    log.log(f"捕捉フェーズ完了: {count}件をInbox DBに登録しました。")
                except ConfigError:
                    raise
                except Exception as exc:
                    # 捕捉に失敗しても、既にInboxにあるアイテムの実装は進められる。
                    # ここで止めない方が「起動したら何かしら前に進む」状態を保てる。
                    log.error(f"捕捉フェーズで想定外のエラー: {exc}")

            if do_dispatch:
                try:
                    dispatch.run(config)
                except ConfigError:
                    raise
                except Exception as exc:
                    log.error(f"実装フェーズで想定外のエラー: {exc}")
    except AlreadyRunning as exc:
        log.warn(f"{exc} 今回は何もせず終了します。")
        return 0

    log.log("クロコを終了します。")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\n中断しました。", file=sys.stderr)
        raise SystemExit(130)
