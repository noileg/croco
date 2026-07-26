"""下書きエディタのアイコン（.ico）を作る。

    python make_icon.py

外部パッケージは使わない。ICOは「BITMAPINFOHEADER + BGRAの画素 + マスク」を
並べただけの素直な形式なので、手で組める。PNG埋め込み形式のICOもあるが、
Tkの `iconbitmap` が読めないことがあるので**DIB形式で書く**。

絵は図形だけで組み立てている（文字を描くには字形を持つ仕組みが要るため）。
用意した画像に差し替えたくなったら、この生成をやめて .ico を置き換えればよい。
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "assets" / "editor.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)
SUPER = 4  # この倍率で描いてから縮める（輪郭を滑らかにするため）

# 色。濃紺の角丸に白い紙、下に文字数の目安を示す帯。
BACK = (58, 74, 110, 255)
PAPER = (250, 250, 248, 255)
LINE = (176, 182, 196, 255)
ACCENT = (226, 124, 74, 255)


class Canvas:
    def __init__(self, size: int) -> None:
        self.size = size
        self.px = bytearray(size * size * 4)   # RGBA

    def blend(self, x: int, y: int, color: tuple[int, int, int, int]) -> None:
        if not (0 <= x < self.size and 0 <= y < self.size):
            return
        i = (y * self.size + x) * 4
        self.px[i:i + 4] = bytes(color)

    def rect(self, x0: float, y0: float, x1: float, y1: float,
             color: tuple[int, int, int, int], radius: float = 0.0) -> None:
        for y in range(int(y0), int(y1) + 1):
            for x in range(int(x0), int(x1) + 1):
                if radius:
                    cx = min(max(x, x0 + radius), x1 - radius)
                    cy = min(max(y, y0 + radius), y1 - radius)
                    if (x - cx) ** 2 + (y - cy) ** 2 > radius ** 2:
                        continue
                self.blend(x, y, color)

    def downsample(self, factor: int) -> "Canvas":
        """平均を取って縮める。これで輪郭のギザギザが消える。"""
        small = Canvas(self.size // factor)
        area = factor * factor
        for y in range(small.size):
            for x in range(small.size):
                sums = [0, 0, 0, 0]
                for dy in range(factor):
                    row = ((y * factor + dy) * self.size + x * factor) * 4
                    for dx in range(factor):
                        i = row + dx * 4
                        for c in range(4):
                            sums[c] += self.px[i + c]
                small.blend(x, y, tuple(s // area for s in sums))
        return small


def draw(size: int) -> Canvas:
    """1枚描く。座標は 0〜size で、比率で置いている。"""
    canvas = Canvas(size)
    unit = size / 256

    canvas.rect(6 * unit, 6 * unit, 250 * unit, 250 * unit, BACK, 52 * unit)
    canvas.rect(58 * unit, 40 * unit, 198 * unit, 216 * unit, PAPER, 10 * unit)
    for index in range(5):                       # 本文の行
        top = (72 + index * 26) * unit
        right = (176 if index != 4 else 140) * unit
        canvas.rect(78 * unit, top, right, top + 9 * unit, LINE, 4 * unit)
    canvas.rect(78 * unit, 196 * unit, 176 * unit, 205 * unit, ACCENT, 4 * unit)
    return canvas


def dib(canvas: Canvas) -> bytes:
    """1枚ぶんのDIB（ヘッダ＋BGRA＋マスク）。行は下から上へ並べる。"""
    size = canvas.size
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    rows = []
    for y in range(size - 1, -1, -1):
        row = bytearray()
        for x in range(size):
            i = (y * size + x) * 4
            r, g, b, a = canvas.px[i:i + 4]
            row += bytes((b, g, r, a))
        rows.append(bytes(row))
    mask_row = b"\x00" * (((size + 31) // 32) * 4)   # 32ビット境界に揃える
    return header + b"".join(rows) + mask_row * size


def main() -> int:
    images = []
    for size in SIZES:
        big = draw(size * SUPER)
        images.append(dib(big.downsample(SUPER)))

    count = len(images)
    offset = 6 + 16 * count
    directory = b""
    for size, data in zip(SIZES, images):
        directory += struct.pack(
            "<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(struct.pack("<HHH", 0, 1, count) + directory + b"".join(images))
    print(f"{OUT} を作りました（{OUT.stat().st_size:,} バイト / {count}サイズ）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
