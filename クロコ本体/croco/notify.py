"""節目で音を鳴らす。

離席していても気づけるようにするのが目的。相談の誘いは待ち時間が短いので、
別ウィンドウを見ていると気づかないまま流れる。**通知が無いと相談フェーズは使われない。**

鳴らすのは2つの瞬間だけに絞る（仕様書4章）。全部の節目で鳴らすと狼少年になる。
  - 本人の入力を待ち始めたとき
  - 一連の処理が全部終わったとき（画面を見に来ていい合図）

呼び出し元は `run_croco.py` だけではない。対話モードのクロコは作業を終えても
ウィンドウが開いたままで、本人が `/exit` するまで run_croco.py は先へ進めない。
「1件片付いた」瞬間を知らせられるのは `croco_cli.py` の done / review だけ。

`MessageBeep` は使わない。このPCでは SystemQuestion に音が割り当てられておらず
**一番重要な合図が無音になる**うえ、他の種類は全部同じwavなので聞き分けられない。
`Beep` はサウンドスキームに依存せずトーンを合成するので、必ず鳴り、音程で区別できる
（Vista以降は既定の再生デバイスから出るので、イヤホンでも聞こえる）。

標準ライブラリのみ。鳴らないこと自体に実害はないので、失敗しても黙って進む。
"""

from __future__ import annotations

from .config import Config
from . import log

try:
    import winsound  # Windows標準
except ImportError:  # Windows以外
    winsound = None  # type: ignore[assignment]

# (周波数Hz, 長さms) の並び。イヤホンで聞くので高すぎず短く。
# 上がる音＝「まだ何か要る」、下がって落ち着く音＝「終わった」。
# 意味と音の向きを揃えておくと、画面を見なくても区別がつく。
WAITING = ((880, 110), (1175, 130))
FINISHED = ((1047, 100), (784, 90), (587, 190))


def waiting(config: Config) -> None:
    """本人の入力を待ち始めたとき。"""
    _play(config, WAITING)


def finished(config: Config) -> None:
    """一連の処理が全部終わったとき。"""
    _play(config, FINISHED)


def _play(config: Config, pattern: tuple[tuple[int, int], ...]) -> None:
    if winsound is None or not config.notify:
        return
    try:
        for frequency, duration in pattern:
            winsound.Beep(frequency, duration)
    except Exception as exc:
        # 音が出ないだけで処理には影響しない。一度だけ知らせて以後は黙る。
        log.warn(f"通知音を鳴らせませんでした: {exc}")
