#!/usr/bin/env python3
"""
Build firmware/esp32/assets/font_cn16.bin from a TTF/OTF font.

Output format:
  16-byte header:
    magic "CNFONT\\0\\0"
    u16 cn_count
    u16 ascii_start
    u16 ascii_end
    u16 reserved
  cn_index:  cn_count * u16 codepoints (sorted)
  cn_bitmap: cn_count * 32 bytes (16 rows * 2 bytes)
  ascii:     (ascii_end - ascii_start + 1) * 16 bytes (16 rows * 1 byte)

By default this script reuses the codepoint index embedded in the existing
font_cn16.bin, so replacing the font does not accidentally drop characters.
"""

from __future__ import annotations

import argparse
import struct
import unicodedata
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


MAGIC = b"CNFONT\x00\x00"
ASCII_START = 32
ASCII_END = 126
CELL_W = 16
CELL_H = 16


def parse_args() -> argparse.Namespace:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Build font_cn16.bin from a TTF/OTF font.")
    parser.add_argument("--font", required=True, help="Path to source TTF/OTF font for CJK glyphs.")
    parser.add_argument("--font-index", type=int, default=0, help="Face index for TTC/OTC CJK fonts.")
    parser.add_argument(
        "--ascii-font",
        help="Optional TTF/OTF font for ASCII glyphs. Defaults to --font.",
    )
    parser.add_argument(
        "--ascii-font-index",
        type=int,
        help="Face index for the ASCII font. Defaults to --font-index.",
    )
    parser.add_argument(
        "--existing",
        default=str(root / "assets" / "font_cn16.bin"),
        help="Existing font_cn16.bin used as the source of codepoint coverage.",
    )
    parser.add_argument(
        "--output",
        default=str(root / "assets" / "font_cn16.bin"),
        help="Output font_cn16.bin path.",
    )
    parser.add_argument(
        "--include-text",
        action="append",
        default=[],
        help="Additional UTF-8 text whose non-ASCII codepoints should be included.",
    )
    parser.add_argument(
        "--include-file",
        action="append",
        default=[],
        help="UTF-8 file to scan for additional non-ASCII codepoints.",
    )
    parser.add_argument("--include-gb2312", action="store_true", help="Include all GB2312 Han characters.")
    parser.add_argument(
        "--include-jis-level1",
        action="store_true",
        help="Include JIS X 0208 Level 1 kanji (rows 16-47).",
    )
    parser.add_argument(
        "--include-japanese-kana",
        action="store_true",
        help="Include assigned hiragana and katakana characters.",
    )
    parser.add_argument(
        "--include-cjk-punctuation",
        action="store_true",
        help="Include assigned CJK punctuation from U+3000 through U+303F.",
    )
    parser.add_argument(
        "--include-general-punctuation",
        action="store_true",
        help="Include Latin-1 symbols and Unicode General Punctuation (smart quotes, dashes, ellipsis).",
    )
    parser.add_argument("--cn-size", type=int, default=12, help="Chinese glyph render size.")
    parser.add_argument("--ascii-size", type=int, default=12, help="ASCII glyph render size.")
    parser.add_argument(
        "--preserve-existing-cjk",
        action="store_true",
        help="Reuse existing CJK bitmaps and only rebuild the ASCII bitmap block.",
    )
    return parser.parse_args()


def load_existing_codepoints(path: Path) -> list[int]:
    data = path.read_bytes()
    if data[:8] != MAGIC:
        raise ValueError(f"Invalid existing font magic: {path}")
    cn_count = struct.unpack_from("<H", data, 8)[0]
    start = 16
    return list(struct.unpack_from(f"<{cn_count}H", data, start))


def load_existing_cn_bitmaps(path: Path) -> bytes:
    data = path.read_bytes()
    if data[:8] != MAGIC:
        raise ValueError(f"Invalid existing font magic: {path}")
    cn_count = struct.unpack_from("<H", data, 8)[0]
    start = 16 + cn_count * 2
    end = start + cn_count * 32
    return data[start:end]


def is_cjk_han(cp: int) -> bool:
    return 0x3400 <= cp <= 0x4DBF or 0x4E00 <= cp <= 0x9FFF


def gb2312_han_codepoints() -> set[int]:
    codepoints: set[int] = set()
    for lead in range(0xA1, 0xF8):
        for trail in range(0xA1, 0xFF):
            try:
                text = bytes((lead, trail)).decode("gb2312")
            except UnicodeDecodeError:
                continue
            codepoints.update(ord(ch) for ch in text if is_cjk_han(ord(ch)))
    return codepoints


def jis_level1_codepoints() -> set[int]:
    codepoints: set[int] = set()
    for row in range(16, 48):
        for cell in range(1, 95):
            try:
                text = bytes((row + 0xA0, cell + 0xA0)).decode("euc_jp")
            except UnicodeDecodeError:
                continue
            codepoints.update(ord(ch) for ch in text if is_cjk_han(ord(ch)))
    return codepoints


def assigned_codepoints(start: int, end: int) -> set[int]:
    return {cp for cp in range(start, end + 1) if unicodedata.category(chr(cp)) != "Cn"}


def printable_assigned_codepoints(start: int, end: int) -> set[int]:
    return {
        cp
        for cp in range(start, end + 1)
        if unicodedata.category(chr(cp))[0] != "C" or chr(cp).isspace()
    }


def render_glyph(ch: str, font: ImageFont.FreeTypeFont, width: int, height: int) -> Image.Image:
    img = Image.new("1", (width, height), 0)
    draw = ImageDraw.Draw(img)
    bbox = font.getbbox(ch)
    if bbox is None:
        return img

    left, top, right, bottom = bbox
    glyph_w = right - left
    glyph_h = bottom - top
    x = (width - glyph_w) // 2 - left
    y = (height - glyph_h) // 2 - top
    draw.text((x, y), ch, fill=1, font=font)
    return img


def validate_glyph_fits(ch: str, font: ImageFont.FreeTypeFont, width: int, height: int) -> None:
    bbox = font.getbbox(ch)
    if bbox is None:
        return
    glyph_w = bbox[2] - bbox[0]
    glyph_h = bbox[3] - bbox[1]
    if glyph_w > width or glyph_h > height:
        raise ValueError(
            f"Glyph {ch!r} is {glyph_w}x{glyph_h} and would be clipped by the {width}x{height} cell"
        )


def pack_cn_bitmap(img: Image.Image) -> bytes:
    out = bytearray()
    for y in range(CELL_H):
        row = 0
        for x in range(CELL_W):
            if img.getpixel((x, y)):
                row |= 1 << (15 - x)
        out.extend(struct.pack(">H", row))
    return bytes(out)


def pack_ascii_bitmap(img: Image.Image) -> bytes:
    out = bytearray()
    for y in range(CELL_H):
        row = 0
        for x in range(8):
            if img.getpixel((x, y)):
                row |= 1 << (7 - x)
        out.append(row)
    return bytes(out)


def main() -> None:
    args = parse_args()
    font_path = Path(args.font).expanduser().resolve()
    ascii_font_path = Path(args.ascii_font or args.font).expanduser().resolve()
    existing_path = Path(args.existing).expanduser().resolve()
    output_path = Path(args.output).expanduser().resolve()

    if not font_path.exists():
        raise FileNotFoundError(font_path)
    if not ascii_font_path.exists():
        raise FileNotFoundError(ascii_font_path)
    if not existing_path.exists():
        raise FileNotFoundError(existing_path)

    codepoints = set(load_existing_codepoints(existing_path))
    coverage = {"existing": len(codepoints)}
    if args.include_gb2312:
        included = gb2312_han_codepoints()
        codepoints.update(included)
        coverage["GB2312 Han"] = len(included)
    if args.include_jis_level1:
        included = jis_level1_codepoints()
        codepoints.update(included)
        coverage["JIS Level 1 kanji"] = len(included)
    if args.include_japanese_kana:
        included = assigned_codepoints(0x3040, 0x30FF)
        codepoints.update(included)
        coverage["hiragana/katakana"] = len(included)
    if args.include_cjk_punctuation:
        included = assigned_codepoints(0x3000, 0x303F)
        codepoints.update(included)
        coverage["CJK punctuation"] = len(included)
    if args.include_general_punctuation:
        included = printable_assigned_codepoints(0x00A0, 0x00FF)
        included.update(printable_assigned_codepoints(0x2000, 0x206F))
        codepoints.update(included)
        coverage["Latin-1/general punctuation"] = len(included)
    for text in args.include_text:
        codepoints.update(ord(ch) for ch in text if ord(ch) >= 128)
    for file_name in args.include_file:
        file_text = Path(file_name).expanduser().resolve().read_text(encoding="utf-8", errors="ignore")
        codepoints.update(ord(ch) for ch in file_text if ord(ch) >= 128)
    unsupported = sorted(cp for cp in codepoints if cp > 0xFFFF)
    if unsupported:
        preview = ", ".join(f"U+{cp:04X}" for cp in unsupported[:8])
        raise ValueError(
            f"Font format stores 16-bit codepoints; remove {len(unsupported)} characters above U+FFFF "
            f"(first: {preview})"
        )
    codepoints = sorted(codepoints)
    if len(codepoints) > 0xFFFF:
        raise ValueError(f"Font format supports at most 65535 non-ASCII glyphs, got {len(codepoints)}")

    ascii_font_index = args.font_index if args.ascii_font_index is None else args.ascii_font_index
    ascii_font = ImageFont.truetype(str(ascii_font_path), args.ascii_size, index=ascii_font_index)

    cn_bitmaps = bytearray()
    if args.preserve_existing_cjk:
        existing_codepoints = load_existing_codepoints(existing_path)
        if codepoints != existing_codepoints:
            raise ValueError("--preserve-existing-cjk cannot be used when the requested coverage changes")
        cn_bitmaps.extend(load_existing_cn_bitmaps(existing_path))
    else:
        cn_font = ImageFont.truetype(str(font_path), args.cn_size, index=args.font_index)
        blank_codepoints = []
        for cp in codepoints:
            ch = chr(cp)
            img = render_glyph(ch, cn_font, CELL_W, CELL_H)
            packed = pack_cn_bitmap(img)
            if not any(packed) and not ch.isspace():
                blank_codepoints.append(cp)
            cn_bitmaps.extend(packed)
        if blank_codepoints:
            preview = ", ".join(f"U+{cp:04X}" for cp in blank_codepoints[:12])
            raise ValueError(
                f"Source font rendered {len(blank_codepoints)} requested glyphs blank (first: {preview})"
            )

    ascii_bitmaps = bytearray()
    for cp in range(ASCII_START, ASCII_END + 1):
        ch = chr(cp)
        validate_glyph_fits(ch, ascii_font, 8, CELL_H)
        img = render_glyph(ch, ascii_font, 8, CELL_H)
        ascii_bitmaps.extend(pack_ascii_bitmap(img))

    header = bytearray(16)
    header[:8] = MAGIC
    struct.pack_into("<H", header, 8, len(codepoints))
    struct.pack_into("<H", header, 10, ASCII_START)
    struct.pack_into("<H", header, 12, ASCII_END)
    struct.pack_into("<H", header, 14, 0)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as fh:
        fh.write(header)
        fh.write(struct.pack(f"<{len(codepoints)}H", *codepoints))
        fh.write(cn_bitmaps)
        fh.write(ascii_bitmaps)

    print(f"Built {output_path}")
    print(f"  font: {font_path}")
    print(f"  font_index: {args.font_index}")
    print(f"  ascii_font: {ascii_font_path}")
    print(f"  ascii_font_index: {ascii_font_index}")
    print(f"  preserve_existing_cjk: {args.preserve_existing_cjk}")
    for name, count in coverage.items():
        print(f"  requested {name}: {count}")
    print(f"  codepoints: {len(codepoints)}")
    print(f"  size: {output_path.stat().st_size} bytes")


if __name__ == "__main__":
    main()
