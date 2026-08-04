#!/usr/bin/env python3
"""face.png (345x345, 3x3 grid of 115x115 outfit portraits) -> face_115.bin

素材格子顺序（美术给定，左上→右下）:
  睡衣 睡裙 休闲装1 / 休闲装2 洋装 冬装 / 汉服 旗袍 马面裙
固件 outfit 索引（renderer.c s_outfit_files）:
  0睡衣 1洋装 2睡裙 3休闲装1 4休闲装2 5冬装 6旗袍 7马面裙 8汉服

输出与 emoji_48.bin 相同的裸灰度格式: 9 张 115x115 依 outfit 索引排列。
用法: python3 convert_face_sheet.py <face.png> <out.bin>
"""
import sys

from PIL import Image

CELL = 115
# outfit_idx -> sheet cell index (row-major)
SHEET_FOR_OUTFIT = [0, 4, 1, 2, 3, 5, 7, 8, 6]


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    src, dst = sys.argv[1], sys.argv[2]
    img = Image.open(src).convert("L")
    if img.size != (CELL * 3, CELL * 3):
        raise SystemExit(f"expected {CELL*3}x{CELL*3}, got {img.size}")
    out = bytearray()
    for sheet_idx in SHEET_FOR_OUTFIT:
        r, c = divmod(sheet_idx, 3)
        tile = img.crop((c * CELL, r * CELL, (c + 1) * CELL, (r + 1) * CELL))
        out += tile.tobytes()
    with open(dst, "wb") as f:
        f.write(out)
    print(f"wrote {dst}: {len(out)} bytes (9 x {CELL}x{CELL})")


if __name__ == "__main__":
    main()
